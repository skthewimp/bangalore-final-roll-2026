"""Compare sequential and parallel context reading on the five pilot PDFs."""

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time


project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
import context
from extract import lines_from_words, ocr, parse_card

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", type=Path, default=project / "production" / "code")
args = parser.parse_args()
spec = importlib.util.spec_from_file_location(
    "sequential_context", args.baseline / "context.py"
)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
results = []
for pdf in sorted((project / "raw" / "ENG").glob("*/*.pdf")):
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    checkpoints = project / "pilot" / "ocr-cache" / digest
    records = []
    for checkpoint in sorted(
        checkpoints.glob("*.json"), key=lambda path: int(path.stem)
    ):
        rows, text, inspection = json.loads(checkpoint.read_text())
        records.extend(parse_card(row) for row in rows)
    with tempfile.TemporaryDirectory() as directory:
        baseline.ROOT = Path(directory) / "sequential"
        context.ROOT = Path(directory) / "parallel"
        started = time.perf_counter()
        expected = baseline.enrich_pdf(
            pdf, copy.deepcopy(records), digest, ocr, lines_from_words
        )
        old_seconds = time.perf_counter() - started
        started = time.perf_counter()
        actual = context.enrich_pdf(
            pdf, copy.deepcopy(records), digest, ocr, lines_from_words, workers=4
        )
        new_seconds = time.perf_counter() - started
    equal = json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)
    result = {
        "ac": int(pdf.parent.name),
        "cards": len(records),
        "sequential_seconds": old_seconds,
        "parallel_seconds": new_seconds,
        "identical_records": equal,
    }
    results.append(result)
    print(json.dumps(result), flush=True)
    if not equal:
        raise SystemExit("Record differences found; do not deploy")
output = project / "benchmark" / "results" / "mac-parallel-context.json"
output.write_text(json.dumps(results, indent=2))
print(
    "TOTAL",
    sum(row["sequential_seconds"] for row in results),
    sum(row["parallel_seconds"] for row in results),
    flush=True,
)
