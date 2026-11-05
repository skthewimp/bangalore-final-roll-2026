"""Install the verified parallel reader, preserving runtime lineage and progress."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from durable import atomic_json, cached_json, sync_directory
from production import FILES


project = Path(__file__).resolve().parent
root = project.parent / "production"
if not (root / "PAUSED").exists():
    raise SystemExit("Pause the queue before upgrading its runtime")
context_check = json.loads(
    (project.parent / "benchmark/results/mac-parallel-context.json").read_text()
)
worker_check = json.loads(
    (project.parent / "benchmark/results/mac-parallel-worker.json").read_text()
)
if (
    not all(row["identical_records"] for row in context_check)
    or not worker_check["identical_values"]
):
    raise SystemExit("Comparison found changed records; runtime left untouched")

with (root / "run.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    manifest = json.loads((root / "manifest.json").read_text())
    checkpoint = root / "runtime-upgrade.json"
    pending = cached_json(checkpoint)
    if pending and pending["state"] == "prepared":
        target_manifest = pending["target_manifest"]
    else:
        stage = root / "code-next"
        stage.mkdir(exist_ok=True)
        digest = hashlib.sha256()
        for name in FILES:
            content = (project / name).read_bytes()
            with (stage / name).open("wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            digest.update(name.encode() + content)
        digest.update((root / "source.json").read_bytes())
        digest.update((root / "schema.arrow").read_bytes())
        sync_directory(stage)
        target_manifest = dict(manifest)
        target_manifest["pipeline_id"] = digest.hexdigest()
        target_manifest["compatible_pipeline_ids"] = sorted(
            set(manifest.get("compatible_pipeline_ids", []) + [manifest["pipeline_id"]])
        )
        target_manifest["runtime_history"] = manifest.get("runtime_history", []) + [
            {
                "previous_pipeline_id": manifest["pipeline_id"],
                "pipeline_id": digest.hexdigest(),
                "upgraded_at": time.time(),
                "reason": "Parallel context OCR and parent-only PDF rendering; identical pilot values verified",
            }
        ]
        pending = {
            "state": "prepared",
            "target_manifest": target_manifest,
            "previous_id": manifest["pipeline_id"],
        }
        atomic_json(checkpoint, pending)

    archive = root / "code-history" / pending["previous_id"]
    archive.parent.mkdir(exist_ok=True)
    if (root / "code-next").exists():
        if not archive.exists():
            (root / "code").rename(archive)
            atomic_json(archive / "run-manifest.json", manifest)
        elif (root / "code").exists():
            raise SystemExit(
                "Both archived and live old code exist; inspect before replacing"
            )
        (root / "code-next").rename(root / "code")
        sync_directory(root)
    atomic_json(root / "manifest.json", target_manifest)
    pending["state"] = "complete"
    atomic_json(checkpoint, pending)
    print("Runtime upgraded:", target_manifest["pipeline_id"])
    print("Progress, output, downloads and checkpoints retained")
