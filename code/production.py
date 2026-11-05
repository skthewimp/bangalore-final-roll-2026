"""Durable booth queue. Commands: init, run, status, pause, resume.

Use the central datascience Python. Initialization freezes the extraction code,
source list and Arrow schema; launchd runs that snapshot, not mutable pilot code.
"""

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
import requests

from durable import atomic_json, cached_json, sync_directory


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
FILES = [
    "production.py",
    "worker.py",
    "extract.py",
    "context.py",
    "totals.py",
    "durable.py",
    "hash-mark.npy",
    "schema.json",
]
STOP = threading.Event()


def database(root):
    connection = sqlite3.connect(root / "progress.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS booths (
            ac INTEGER NOT NULL,
            part INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,
            updated_at REAL,
            rows INTEGER,
            deleted INTEGER,
            flagged INTEGER,
            error TEXT,
            PRIMARY KEY (ac, part)
        )
    """
    )
    connection.commit()
    return connection


def update(connection, ac, part, **fields):
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE booths SET {assignments} WHERE ac = ? AND part = ?",
        [*fields.values(), ac, part],
    )
    connection.commit()


def initialise(root):
    if (root / "manifest.json").exists():
        print("Existing run preserved:", root)
        return
    project = Path(__file__).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    code = root / "code"
    code.mkdir(exist_ok=True)
    digest = hashlib.sha256()
    for name in FILES:
        content = (project / name).read_bytes()
        (code / name).write_bytes(content)
        digest.update(name.encode() + content)
    source_bytes = (project / "source.json").read_bytes()
    (root / "source.json").write_bytes(source_bytes)
    digest.update(source_bytes)
    # A fresh checkout must initialise without private pilot voter records.
    # The checked-in definition preserves the production column types.
    definition = json.loads((project / "schema.json").read_text())
    schema = pa.schema(
        [
            pa.field(
                field["name"],
                pa.type_for_alias(field["type"]),
                nullable=field["nullable"],
            )
            for field in definition
        ]
    )
    (root / "schema.arrow").write_bytes(schema.serialize().to_pybytes())
    digest.update(schema.serialize().to_pybytes())
    source = json.loads(source_bytes)
    connection = database(root)
    for constituency in source["constituencies"]:
        connection.executemany(
            "INSERT OR IGNORE INTO booths (ac, part) VALUES (?, ?)",
            [
                (constituency["ac"], part)
                for part in range(1, constituency["expected_parts"] + 1)
            ],
        )
    connection.commit()
    connection.close()
    atomic_json(
        root / "manifest.json",
        {
            "pipeline_id": digest.hexdigest(),
            "created_at": time.time(),
            "python": sys.executable,
            "pilot_directory": str(project.parent / "raw" / "ENG"),
            "language": "ENG",
            "workers": 4,
            "booths": sum(row["expected_parts"] for row in source["constituencies"]),
        },
    )
    print("Initialized frozen run:", root)


def status(root):
    connection = database(root)
    for row in connection.execute(
        "SELECT state, COUNT(*) AS booths, SUM(rows) AS rows FROM booths GROUP BY state ORDER BY state"
    ):
        print(dict(row))
    for row in connection.execute(
        "SELECT ac, part, state, updated_at, error FROM booths WHERE updated_at IS NOT NULL ORDER BY updated_at DESC LIMIT 5"
    ):
        print(dict(row))
    print("Paused:", (root / "PAUSED").exists())
    print("Free GB:", round(shutil.disk_usage(root).free / 1e9, 1))
    print("Data:", root / "data")
    print("Log:", root / "run.log")


def valid_pdf(path):
    try:
        with pymupdf.open(path) as document:
            return document.page_count > 0 and not document.needs_pass
    except Exception:
        return False


def download(root, ac, part, source, session):
    work = root / "work" / f"{ac}-{part}"
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / "source.pdf"
    if pdf.exists() and valid_pdf(pdf):
        return pdf
    manifest = json.loads((root / "manifest.json").read_text())
    pilot = Path(manifest["pilot_directory"]) / str(ac)
    candidates = list(pilot.glob(f"*-ENG-{part}-WI.pdf"))
    if candidates and valid_pdf(candidates[0]):
        with candidates[0].open("rb") as original, pdf.with_suffix(".copying").open(
            "wb"
        ) as copy:
            shutil.copyfileobj(original, copy)
            copy.flush()
            os.fsync(copy.fileno())
        pdf.with_suffix(".copying").replace(pdf)
        sync_directory(work)
        return pdf

    partial = work / "download.partial"
    checkpoint = work / "download.json"
    saved = cached_json(checkpoint) or {}
    offset = partial.stat().st_size if partial.exists() else 0
    if offset and offset == saved.get("expected_total") and valid_pdf(partial):
        # Power may have gone off after the last byte, before the rename.
        partial.replace(pdf)
        sync_directory(work)
        return pdf
    headers = {}
    # Byte resumption is conditional on a source validator; without one the
    # partial stays on disk until a complete fresh response can replace it.
    validator = saved.get("etag") or saved.get("last_modified")
    if offset and validator:
        headers = {"Range": f"bytes={offset}-", "If-Range": validator}
    url = source["url_template"].format(ac=ac, part=part, language="ENG")
    with session.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
        response.raise_for_status()
        append = response.status_code == 206 and offset and headers
        if append and not response.headers.get("Content-Range", "").startswith(
            f"bytes {offset}-"
        ):
            raise ValueError("Source returned an inconsistent byte range")
        atomic_json(
            checkpoint,
            {
                "url": url,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "expected_total": (
                    (offset if append else 0) + int(response.headers["Content-Length"])
                    if response.headers.get("Content-Length")
                    else None
                ),
            },
        )
        received = 0
        with partial.open("ab" if append else "wb") as output:
            try:
                for block in response.iter_content(1024 * 1024):
                    if STOP.is_set() or (root / "PAUSED").exists():
                        raise InterruptedError(
                            "Run interrupted; partial download retained"
                        )
                    output.write(block)
                    received += len(block)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                output.flush()
                os.fsync(output.fileno())
        expected = response.headers.get("Content-Length")
        if expected and received != int(expected):
            raise ValueError("Download ended before Content-Length")
    with partial.open("rb") as stream:
        signature = stream.read(5)
        stream.seek(max(0, partial.stat().st_size - 2048))
        ending = stream.read()
    if signature != b"%PDF-" or b"%%EOF" not in ending or not valid_pdf(partial):
        raise ValueError("Downloaded response is not a complete readable PDF")
    partial.replace(pdf)
    sync_directory(work)
    return pdf


def recover_output(root, connection, ac, part):
    path = root / "data" / f"ac_{ac}" / f"part_{part:04}.parquet"
    if not path.exists():
        return False
    metadata = pq.read_metadata(path)
    result = json.loads(metadata.metadata[b"roll_validation"])
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        result["pipeline_id"]
        not in {manifest["pipeline_id"], *manifest.get("compatible_pipeline_ids", [])}
        or result["state"] != "complete"
        or metadata.num_rows != result["rows"]
    ):
        raise RuntimeError("Existing Parquet does not match this validated run")
    update(
        connection,
        ac,
        part,
        state="complete",
        rows=result["rows"],
        deleted=result["deleted"],
        flagged=result["flagged"],
        error=None,
    )
    shutil.rmtree(root / "work" / f"{ac}-{part}", ignore_errors=True)
    return True


def run(root, args):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another runner is active")
            return
        execute_queue(root, args)


def execute_queue(root, args):
    logger = logging.getLogger("roll")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(root / "run.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda signum, frame: STOP.set())
    connection = database(root)
    for completed in connection.execute(
        "SELECT ac, part, rows FROM booths WHERE state = 'complete'"
    ).fetchall():
        path = (
            root
            / "data"
            / f"ac_{completed['ac']}"
            / f"part_{completed['part']:04}.parquet"
        )
        try:
            intact = pq.read_metadata(path).num_rows == completed["rows"]
        except (OSError, ValueError):
            intact = False
        if not intact:
            logger.error(
                "AC %s part %s: completed output missing/unreadable; queued for recovery",
                completed["ac"],
                completed["part"],
            )
            update(
                connection,
                completed["ac"],
                completed["part"],
                state="retry",
                retry_at=0,
                error="Completed output missing/unreadable",
            )
    connection.execute(
        "UPDATE booths SET state = 'retry', retry_at = 0, error = 'Interrupted; saved files retained' WHERE state IN ('downloading', 'downloaded', 'extracting')"
    )
    connection.commit()
    source = json.loads((root / "source.json").read_text())
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    finished = 0
    logger.info("Runner started, PID %s, workers %s", os.getpid(), args.workers)
    while not STOP.is_set() and not (root / "PAUSED").exists():
        if shutil.disk_usage(root).free < args.min_free_gb * 1e9:
            logger.error(
                "Waiting: less than %s GB free; no downloaded files discarded",
                args.min_free_gb,
            )
            STOP.wait(60)
            continue
        predicates = ["state IN ('pending', 'retry')", "retry_at <= ?"]
        values = [time.time()]
        for field in ("ac", "part"):
            if getattr(args, field) is not None:
                predicates.append(f"{field} = ?")
                values.append(getattr(args, field))
        job = connection.execute(
            "SELECT * FROM booths WHERE "
            + " AND ".join(predicates)
            + " ORDER BY ac, part LIMIT 1",
            values,
        ).fetchone()
        if job is None:
            outstanding = connection.execute(
                "SELECT COUNT(*) FROM booths WHERE state NOT IN ('complete', 'review')"
            ).fetchone()[0]
            if not outstanding or args.max_booths:
                break
            STOP.wait(30)
            continue
        ac, part = job["ac"], job["part"]
        # A helper can reserve a booth after this runner selected it. Claim it
        # conditionally before touching files so only one machine can proceed.
        claimed = connection.execute(
            """
            UPDATE booths SET state = 'downloading', updated_at = ?
            WHERE ac = ? AND part = ? AND state IN ('pending', 'retry')
            """,
            (time.time(), ac, part),
        )
        connection.commit()
        if claimed.rowcount != 1:
            continue
        work = root / "work" / f"{ac}-{part}"
        try:
            if recover_output(root, connection, ac, part):
                logger.info("AC %s part %s: recovered durable output", ac, part)
                continue
            update(
                connection,
                ac,
                part,
                state="downloading",
                attempts=job["attempts"] + 1,
                error=None,
            )
            logger.info("AC %s part %s: download/resume", ac, part)
            download(root, ac, part, source, session)
            update(connection, ac, part, state="extracting")
            logger.info(
                "AC %s part %s: extracting; page checkpoints retained", ac, part
            )
            command = [
                sys.executable,
                "-u",
                str(root / "code" / "worker.py"),
                "--directory",
                str(root),
                "--ac",
                str(ac),
                "--part",
                str(part),
                "--workers",
                str(args.workers),
            ]
            with (work / "worker.log").open("a") as output:
                process = subprocess.Popen(
                    command,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                while process.poll() is None:
                    if STOP.is_set() or (root / "PAUSED").exists():
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                        raise InterruptedError(
                            "Stopped; PDF and page checkpoints retained"
                        )
                    STOP.wait(1)
            if process.returncode == 0 and recover_output(root, connection, ac, part):
                logger.info(
                    "AC %s part %s: COMPLETE; Parquet committed, temporary PDF removed",
                    ac,
                    part,
                )
            else:
                result = cached_json(work / "result.json") or {}
                if result.get("state") == "review":
                    reason = json.dumps(result.get("problems") or result.get("error"))
                    update(
                        connection,
                        ac,
                        part,
                        state="review",
                        error=reason,
                        rows=result.get("rows"),
                        deleted=result.get("deleted"),
                        flagged=result.get("flagged"),
                    )
                    logger.warning(
                        "AC %s part %s: REVIEW, source retained: %s", ac, part, reason
                    )
                else:
                    raise RuntimeError(
                        f"Worker exit {process.returncode}; see {work / 'worker.log'}"
                    )
            finished += 1
            if args.max_booths and finished >= args.max_booths:
                break
        except InterruptedError as error:
            update(connection, ac, part, state="retry", retry_at=0, error=str(error))
            logger.info("AC %s part %s: interrupted safely", ac, part)
            break
        except Exception as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            delay = min(3600, 60 * 2 ** min(job["attempts"], 6))
            if status_code == 404:
                delay = 21600
            update(
                connection,
                ac,
                part,
                state="retry",
                retry_at=time.time() + delay,
                error=str(error),
            )
            logger.exception("AC %s part %s: retry in %s s", ac, part, delay)
            if args.max_booths:
                break
            if isinstance(error, requests.RequestException):
                STOP.wait(1800 if status_code in (401, 403, 406) else 60)
    logger.info("Runner stopped; all committed progress retained")
    connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "run", "status", "pause", "resume"])
    parser.add_argument(
        "--directory", type=Path, default=Path(__file__).resolve().parents[1] / "production"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-free-gb", type=float, default=5)
    parser.add_argument("--max-booths", type=int)
    parser.add_argument("--ac", type=int)
    parser.add_argument("--part", type=int)
    args = parser.parse_args()
    root = args.directory.resolve()
    if args.command == "init":
        initialise(root)
    elif args.command == "run":
        run(root, args)
    elif args.command == "status":
        status(root)
    elif args.command == "pause":
        root.mkdir(parents=True, exist_ok=True)
        (root / "PAUSED").touch()
        print("Pause requested; current PDF and checkpoints will be retained")
    elif args.command == "resume":
        (root / "PAUSED").unlink(missing_ok=True)
        print("Resume enabled; the scheduled runner will restart within two minutes")
