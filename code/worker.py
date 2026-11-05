"""Extract one booth with durable page checkpoints and atomic Parquet output."""

import argparse
import hashlib
import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
from PIL import Image

import context
from durable import atomic_json, cached_json, sync_directory
from extract import lines_from_words, ocr, parse_card, read_page_image
from totals import read_cover_totals


def cached_page(job):
    image, page, directory, saved = job
    checkpoint = directory / "pages" / f"{page:04}.json"
    if saved is not None:
        print(f"Page {page}: restored checkpoint", flush=True)
        return saved
    result = read_page_image(image, page)
    atomic_json(checkpoint, result)
    print(f"Page {page}: saved {len(result[0])} cards", flush=True)
    return result


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--ac", type=int, required=True)
parser.add_argument("--part", type=int, required=True)
parser.add_argument("--workers", type=int, default=4)
args = parser.parse_args()
root = args.directory.resolve()
work = root / "work" / f"{args.ac}-{args.part}"
pdf = work / "source.pdf"
source = json.loads((root / "source.json").read_text())
manifest = json.loads((root / "manifest.json").read_text())
context.ROOT = work
digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
url = source["url_template"].format(ac=args.ac, part=args.part, language="ENG")
constituency = next(row for row in source["constituencies"] if row["ac"] == args.ac)
cover = context.read_cover_context(pdf, digest, ocr, lines_from_words)
with pymupdf.open(pdf) as document:
    page_count = len(document)

records = []
page_results = {}
with pymupdf.open(pdf) as document, ThreadPoolExecutor(args.workers) as pool:
    pages = iter(range(1, page_count + 1))
    pending = set()

    def submit_next():
        page = next(pages, None)
        if page is None:
            return False
        saved = cached_json(work / "pages" / f"{page:04}.json")
        image = None
        if saved is None:
            pixmap = document[page - 1].get_pixmap()
            image = Image.frombytes(
                "RGB", [pixmap.width, pixmap.height], pixmap.samples
            )
        pending.add(pool.submit(cached_page, (image, page, work, saved)))
        return True

    for slot in range(args.workers):
        if not submit_next():
            break
    while pending:
        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in completed:
            pending.remove(future)
            rows, page_text, inspection = future.result()
            page_results[inspection["page"]] = (rows, page_text, inspection)
            submit_next()
for page in sorted(page_results):
    rows, page_text, inspection = page_results[page]
    for row in rows:
        records.append(
            parse_card(
                {
                    "ac": args.ac,
                    "constituency_name": constituency["name"],
                    "part": args.part,
                    "language": "ENG",
                    "source_url": url,
                    "pdf_sha256": digest,
                    **cover,
                    **row,
                }
            )
        )
records = context.enrich_pdf(
    pdf, records, digest, ocr, lines_from_words, workers=args.workers
)
data = pd.DataFrame(records)
if data.empty:
    atomic_json(
        work / "result.json", {"state": "review", "error": "No elector cards detected"}
    )
    raise SystemExit(2)

key = ["ac", "part", "page", "card"]
duplicates = data[data.duplicated(key, keep=False)]
if len(duplicates):
    print(duplicates[key].to_string(index=False), flush=True)
    atomic_json(
        work / "result.json",
        {"state": "review", "error": "Duplicate physical-card keys"},
    )
    raise SystemExit(2)

for column in ("serial", "age", "section_number"):
    data[column] = data[column].astype("Int64")
for column in ("is_deleted", "is_modified"):
    data[column] = data[column].astype("boolean")
for column, identity in (("serial", ["ac", "part", "serial"]), ("epic", ["epic"])):
    repeated = data[column].notna() & data.duplicated(identity, keep=False)
    data.loc[repeated, "review_flags"] = data.loc[repeated, "review_flags"].map(
        lambda value: ";".join(filter(None, [value, f"duplicate_{column}_reading"]))
    )
data = data.sort_values(key).reset_index(drop=True)
totals = read_cover_totals(pdf, ocr)
problems = []
observed_acs = set()
observed_parts = set()
for header in data.page_header.dropna().unique():
    identity = re.search(r"Constituency.*?[:;]\s*(\d+)\s*[-–]", header, re.I)
    booth = re.search(r"Part\s*No\.?\s*[:;]?\s*(\d+)", header, re.I)
    if identity:
        observed_acs.add(int(identity.group(1)))
    if booth:
        observed_parts.add(int(booth.group(1)))
if observed_acs != {args.ac} or observed_parts != {args.part}:
    problems.append("printed constituency/part identity unread or inconsistent")
if "error" in totals:
    problems.append(totals["error"])
else:
    if len(data) != totals["cards"]:
        problems.append("detected cards disagree with printed serial range")
    if int(data.is_deleted.sum()) != totals["deleted"]:
        problems.append("deletion flags disagree with printed deletion count")
    if int((~data.is_deleted).sum()) != totals["net"]:
        problems.append("retained cards disagree with printed net electors")
if data.is_deleted.isna().any():
    problems.append("some deletion markers unread")
if not cover.get("polling_area_details") or not cover.get("polling_station_details"):
    problems.append("cover address context unread")

# Preserve review flags and raw OCR in Parquet; do not retain image crops.
# Structurally failed booths keep their PDF and checkpoints for later review.
flagged = data[data.review_flags.ne("")]

result = {
    "state": "review" if problems else "complete",
    "ac": args.ac,
    "part": args.part,
    "rows": len(data),
    "deleted": int(data.is_deleted.sum()),
    "flagged": len(flagged),
    "pages": page_count,
    "pdf_sha256": digest,
    "pipeline_id": manifest["pipeline_id"],
    "printed_totals": totals,
    "problems": problems,
}
destination = (
    root
    / ("review-data" if problems else "data")
    / f"ac_{args.ac}"
    / f"part_{args.part:04}.parquet"
)
destination.parent.mkdir(parents=True, exist_ok=True)
schema = pa.ipc.read_schema(pa.BufferReader((root / "schema.arrow").read_bytes()))
table = pa.Table.from_pandas(data, schema=schema, preserve_index=False, safe=True)
metadata = dict(table.schema.metadata or {})
metadata[b"roll_validation"] = json.dumps(result).encode()
table = table.replace_schema_metadata(metadata)
temporary = destination.with_suffix(".parquet.partial")
pq.write_table(table, temporary, compression="zstd")
with temporary.open("rb") as stream:
    os.fsync(stream.fileno())
check = pq.read_metadata(temporary)
if check.num_rows != len(data):
    raise RuntimeError("Parquet read-back row count differs")
temporary.replace(destination)
sync_directory(destination.parent)
result["output"] = str(destination)
atomic_json(work / "result.json", result)
print(json.dumps(result), flush=True)
raise SystemExit(2 if problems else 0)
