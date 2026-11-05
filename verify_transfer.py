"""Verify the paused run's transferred bytes and SQLite integrity before start."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
args = parser.parse_args()
root = args.directory.resolve()
manifest = json.loads((root / "transfer-manifest.json").read_text())
problems = []
for name, expected in manifest["files"].items():
    path = root / name
    if not path.is_file():
        problems.append((name, "missing"))
        continue
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if path.stat().st_size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        problems.append((name, "bytes/checksum differ"))
print("Verified files:", len(manifest["files"]) - len(problems), flush=True)
print("Verified GB:", round(manifest["total_bytes"] / 1e9, 3), flush=True)
print("Mismatches:", problems[:20], flush=True)
if problems:
    raise SystemExit("Transfer verification failed; keep the run paused")
connection = sqlite3.connect(f"file:{root / 'progress.sqlite'}?mode=ro", uri=True)
integrity = connection.execute("PRAGMA quick_check").fetchall()
print("SQLite integrity:", integrity)
if integrity != [("ok",)]:
    raise SystemExit("SQLite integrity check failed")
print("Queue:", connection.execute(
    "SELECT state, COUNT(*), SUM(rows) FROM booths GROUP BY state ORDER BY state"
).fetchall())
connection.close()
