"""Server-side reservations and validated imports for a second OCR machine.

Run inside the existing server container. The server's SQLite ledger remains
canonical; delegated booths are excluded by its unchanged production runner.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

# On the server this coordinator lives beside the frozen code directory.
sys.path.insert(0, str(Path(__file__).resolve().parent / "code"))
from durable import sync_directory


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('command', choices=['reserve', 'commit', 'release'])
parser.add_argument('--directory', type=Path, default=Path('/data'))
parser.add_argument('--owner', required=True)
parser.add_argument('--batch-size', type=int, default=100)
args = parser.parse_args()
root = args.directory
# Owner also names the upload directory: restrict it to a single safe component.
if not args.owner or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in args.owner):
    raise ValueError('Owner must contain lowercase letters, digits and hyphens only')
owner = 'delegated:' + args.owner
connection = sqlite3.connect(root / 'progress.sqlite', timeout=60)
connection.row_factory = sqlite3.Row
connection.execute('PRAGMA synchronous=FULL')

if args.command == 'reserve':
    connection.execute('BEGIN IMMEDIATE')
    jobs = connection.execute(
        """
        SELECT ac, part FROM booths
        WHERE state = 'delegated' AND error = ?
        ORDER BY ac DESC, part DESC
        """, (owner,)
    ).fetchall()
    available = connection.execute(
        """
        SELECT ac, part FROM booths
        WHERE state IN ('pending', 'retry') AND retry_at <= ?
        ORDER BY ac DESC, part DESC
        LIMIT ?
        """, (time.time(), max(0, args.batch_size - len(jobs)))
    ).fetchall()
    for job in available:
        connection.execute(
            """
            UPDATE booths SET state = 'delegated', error = ?, updated_at = ?
            WHERE ac = ? AND part = ?
            """, (owner, time.time(), job['ac'], job['part'])
        )
    connection.commit()
    print(json.dumps([dict(job) for job in [*jobs, *available]]))

elif args.command == 'release':
    cursor = connection.execute(
        """
        UPDATE booths SET state = 'pending', error = NULL, retry_at = 0,
            updated_at = ?
        WHERE state = 'delegated' AND error = ?
        """, (time.time(), owner)
    )
    connection.commit()
    print(json.dumps({'released': cursor.rowcount}))

elif args.command == 'commit':
    incoming = root / 'incoming' / args.owner
    results = json.loads((incoming / 'results.json').read_text())
    manifest = json.loads((root / 'manifest.json').read_text())
    pipeline_ids = {manifest['pipeline_id'], *manifest.get('compatible_pipeline_ids', [])}
    schema = pa.ipc.read_schema(pa.BufferReader((root / 'schema.arrow').read_bytes()))
    committed = []
    for result in results:
        ac, part, state = result['ac'], result['part'], result['state']
        if state not in ('complete', 'review'):
            raise ValueError('Only finished booths may be imported')
        job = connection.execute(
            'SELECT state, error FROM booths WHERE ac = ? AND part = ?', (ac, part)
        ).fetchone()
        if job is None or not (
            (job['state'] == 'delegated' and job['error'] == owner)
            or job['state'] == state
        ):
            raise ValueError(f'Booth {ac}-{part} is not reserved by this machine')
        category = 'data' if state == 'complete' else 'review-data'
        relative = Path(category) / f'ac_{ac}' / f'part_{part:04}.parquet'
        staged = incoming / relative
        validation = None
        if result.get('sha256'):
            if hashlib.sha256(staged.read_bytes()).hexdigest() != result['sha256']:
                raise ValueError(f'Transfer checksum failed for {relative}')
            table = pq.read_table(staged)
            validation = json.loads(table.schema.metadata[b'roll_validation'])
            if (
                validation['pipeline_id'] not in pipeline_ids
                or validation['state'] != state
                or validation['ac'] != ac or validation['part'] != part
                or table.num_rows != result['rows']
                or not table.schema.equals(schema, check_metadata=False)
            ):
                raise ValueError(f'Incompatible output for {relative}')
            # Intended canonical key is the physical card, never the EPIC.
            keys = table.select(['ac', 'part', 'page', 'card']).to_pandas()
            if (keys.isna().any().any() or keys.duplicated().any()
                    or not keys.ac.eq(ac).all() or not keys.part.eq(part).all()):
                raise ValueError(f'Invalid physical-card keys for {relative}')
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != result['sha256']:
                    raise ValueError(f'Conflicting existing output for {relative}')
            else:
                temporary = destination.with_suffix('.parquet.importing')
                shutil.copyfile(staged, temporary)
                with temporary.open('rb') as stream:
                    os.fsync(stream.fileno())
                temporary.replace(destination)
                sync_directory(destination.parent)
        elif state == 'complete':
            raise ValueError('Complete output must have Parquet')
        if state == 'review':
            source_work = incoming / 'work' / f'{ac}-{part}'
            if not (source_work / 'source.pdf').exists():
                raise ValueError('Review source PDF must be retained')
            if validation and (
                hashlib.sha256((source_work / 'source.pdf').read_bytes()).hexdigest()
                != validation['pdf_sha256']
            ):
                raise ValueError(f'Review PDF differs from extracted source for {ac}-{part}')
            destination_work = root / 'work' / f'{ac}-{part}'
            existing_pdf = destination_work / 'source.pdf'
            if existing_pdf.exists() and (
                hashlib.sha256(existing_pdf.read_bytes()).digest()
                != hashlib.sha256((source_work / 'source.pdf').read_bytes()).digest()
            ):
                raise ValueError(f'Conflicting retained source for {ac}-{part}')
            shutil.copytree(source_work, destination_work, dirs_exist_ok=True)
            # Flush files and directory entries before the review ledger commit.
            for directory, subdirectories, files in os.walk(destination_work, topdown=False):
                for name in files:
                    with (Path(directory) / name).open('rb') as stream:
                        os.fsync(stream.fileno())
                sync_directory(directory)
            sync_directory(destination_work.parent)
        connection.execute(
            """
            UPDATE booths SET state = ?, rows = ?, deleted = ?, flagged = ?,
                error = ?, updated_at = ?
            WHERE ac = ? AND part = ?
            """,
            (state, result['rows'], result['deleted'], result['flagged'],
             result['error'], time.time(), ac, part)
        )
        connection.commit()
        if state == 'complete':
            shutil.rmtree(root / 'work' / f'{ac}-{part}', ignore_errors=True)
        committed.append({'ac': ac, 'part': part})
    # Clear staged data only after every booth is durably committed. A repeated
    # commit after a lost SSH response is safe and checks existing file hashes.
    print(json.dumps(committed))
connection.close()
