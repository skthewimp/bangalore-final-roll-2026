"""Lift pilot-inferred null-only fields to text without changing extracted values.

Run with the queue paused. Existing Parquet files and the output schema are
replaced atomically; the original extraction pipeline ID remains as lineage.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

import pyarrow as pa
import pyarrow.parquet as pq

from durable import atomic_json, cached_json, sync_directory


root = Path(__file__).resolve().parents[1] / "production"
if not (root / "PAUSED").exists():
    raise SystemExit("Pause the queue before a schema migration")
with (root / "run.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    schema_path = root / "schema.arrow"
    original = schema_path.read_bytes()
    schema = pa.ipc.read_schema(pa.BufferReader(original))
    changed = [field.name for field in schema if pa.types.is_null(field.type)]
    target = pa.schema(
        [
            pa.field(
                field.name,
                pa.string() if pa.types.is_null(field.type) else field.type,
                nullable=field.nullable,
                metadata=field.metadata,
            )
            for field in schema
        ],
        metadata=schema.metadata,
    )
    checkpoint = root / "schema-migration.json"
    migration = cached_json(checkpoint)
    if changed:
        migration = {
            "started_at": time.time(),
            "state": "migrating",
            "columns": changed,
            "original_schema_sha256": hashlib.sha256(original).hexdigest(),
            "target_schema_sha256": hashlib.sha256(
                target.serialize().to_pybytes()
            ).hexdigest(),
        }
        atomic_json(checkpoint, migration)
        backup = root / "schema.original.arrow"
        if not backup.exists():
            backup.write_bytes(original)
        temporary = schema_path.with_suffix(".partial")
        with temporary.open("wb") as output:
            output.write(target.serialize().to_pybytes())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(schema_path)
        sync_directory(root)
    if not migration:
        raise SystemExit("No null-only fields or interrupted migration")
    files = list((root / "data").rglob("*.parquet")) + list(
        (root / "review-data").rglob("*.parquet")
    )
    rewritten = 0
    for path in files:
        table = pq.ParquetFile(path).read()
        if all(
            table.schema.field(name).type == target.field(name).type
            for name in migration["columns"]
        ):
            continue
        metadata = dict(table.schema.metadata or {})
        metadata[b"schema_revision"] = b"2"
        table = table.cast(target.with_metadata(metadata), safe=True)
        temporary = path.with_suffix(".parquet.partial")
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if pq.read_metadata(temporary).num_rows != table.num_rows:
            raise RuntimeError("Migration row count changed")
        temporary.replace(path)
        sync_directory(path.parent)
        rewritten += 1
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    migration.update(
        state="complete", completed_at=time.time(), files_rewritten=rewritten
    )
    manifest["schema_revision"] = 2
    manifest["schema_migration"] = migration
    atomic_json(manifest_path, manifest)
    atomic_json(checkpoint, migration)
    print(json.dumps(migration, indent=2))
