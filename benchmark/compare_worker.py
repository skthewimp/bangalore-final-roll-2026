"""Time complete, uncached old/new workers and compare their Parquet values."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pyarrow.parquet as pq


project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project / "code"))
from production import FILES

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", type=Path, default=project / "production" / "code")
parser.add_argument(
    "--counts",
    type=int,
    nargs="+",
    help="Compare worker counts using the same frozen code",
)
args = parser.parse_args()

measurements = []
tables = []
variants = (
    [
        (f"workers-{count}-trial-{index}", args.baseline, count)
        for index, count in enumerate(args.counts)
    ]
    if args.counts
    else [("baseline", args.baseline, 4), ("parallel", project / "code", 4)]
)
with tempfile.TemporaryDirectory(prefix="roll-worker-speed-") as temporary:
    for name, code_source, worker_count in variants:
        root = Path(temporary) / name
        code = root / "code"
        code.mkdir(parents=True)
        for filename in FILES:
            shutil.copy2(code_source / filename, code / filename)
        for filename in ("source.json", "schema.arrow"):
            shutil.copy2(project / "production" / filename, root / filename)
        (root / "manifest.json").write_text(json.dumps({"pipeline_id": "benchmark"}))
        work = root / "work" / "162-1"
        work.mkdir(parents=True)
        shutil.copy2(
            next((project / "raw" / "ENG" / "162").glob("*.pdf")), work / "source.pdf"
        )
        started = time.perf_counter()
        with (root / "worker.log").open("w") as log:
            result = subprocess.run(
                [
                    sys.executable,
                    str(code / "worker.py"),
                    "--directory",
                    str(root),
                    "--ac",
                    "162",
                    "--part",
                    "1",
                    "--workers",
                    str(worker_count),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "OMP_THREAD_LIMIT": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                },
            )
        seconds = time.perf_counter() - started
        if result.returncode:
            print((root / "worker.log").read_text())
            raise SystemExit("Benchmark worker failed; do not deploy")
        table = pq.ParquetFile(root / "data" / "ac_162" / "part_0001.parquet").read()
        tables.append(table)
        measurement = {
            "variant": name,
            "workers": worker_count,
            "seconds": seconds,
            "rows": table.num_rows,
        }
        measurements.append(measurement)
        print(json.dumps(measurement), flush=True)
equal = all(tables[0].equals(table, check_metadata=False) for table in tables[1:])
result = {"measurements": measurements, "identical_values": equal}
(
    project
    / "benchmark"
    / "results"
    / ("mac-worker-counts.json" if args.counts else "mac-parallel-worker.json")
).write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
if not equal:
    raise SystemExit("Worker output values differ; do not deploy")
