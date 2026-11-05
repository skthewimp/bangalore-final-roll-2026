"""Download Bengaluru's October 2026 SIR final roll; preserve source PDFs.

Run with the central datascience Python environment. Defaults to a single
sample; pass --all for every configured booth. Re-running resumes locally.
"""

import argparse
import json
import time
from pathlib import Path

import pymupdf
import requests


ROOT = Path(__file__).resolve().parent
SOURCE = json.loads((ROOT / "source.json").read_text())
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--all", action="store_true", help="Download all 28 ACs")
parser.add_argument("--ac", type=int, nargs="+", help="Download selected ACs")
parser.add_argument("--parts", type=int, nargs="+", help="Select booth numbers")
parser.add_argument("--language", choices=["KAN", "ENG"], default="KAN")
parser.add_argument("--limit", type=int, help="Limit the number of PDFs")
parser.add_argument("--plan", action="store_true", help="List scope without downloading")
args = parser.parse_args()

# English is optional: the Kannada edition is the source project's baseline.
selected_acs = args.ac or ([c["ac"] for c in SOURCE["constituencies"]] if args.all else [162])
known_acs = {c["ac"] for c in SOURCE["constituencies"]}
if set(selected_acs) - known_acs:
    parser.error("Choose an AC listed in source.json")
jobs = []
for constituency in SOURCE["constituencies"]:
    if constituency["ac"] not in selected_acs:
        continue
    parts = args.parts or (
        range(1, constituency["expected_parts"] + 1) if args.ac or args.all else [1]
    )
    if any(p < 1 or p > constituency["expected_parts"] for p in parts):
        parser.error(f"Part outside expected range for AC {constituency['ac']}")
    jobs.extend((constituency["ac"], part) for part in sorted(set(parts)))
jobs = jobs[:args.limit] if args.limit is not None else jobs
print(f"{SOURCE['roll']}: {len(jobs):,} PDFs, {args.language}", flush=True)
if args.plan:
    for constituency in SOURCE["constituencies"]:
        count = sum(ac == constituency["ac"] for ac, part in jobs)
        if count:
            print(f"{constituency['ac']} {constituency['name']}: {count} parts")
    raise SystemExit(0)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
downloaded = skipped = failed = 0

# Stream to a temporary sibling, validate, then rename. An interrupted request
# never leaves an incomplete PDF that would be mistaken for a completed booth.
for index, (ac, part) in enumerate(jobs, 1):
    url = SOURCE["url_template"].format(ac=ac, part=part, language=args.language)
    destination = ROOT.parent / "raw" / args.language / str(ac) / url.rsplit("/", 1)[1]
    if destination.exists():
        try:
            with pymupdf.open(destination) as document:
                valid = document.page_count > 0 and not document.needs_pass
        except Exception:
            valid = False
        if valid:
            skipped += 1
            print(f"[{index}/{len(jobs)}] {ac}/{part}: already saved", flush=True)
            continue
        print(f"{ac}/{part}: existing PDF is invalid; fetching again", flush=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".pdf.partial")
    for attempt in range(3):
        try:
            with session.get(url, stream=True, timeout=(15, 120)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for block in response.iter_content(1024 * 1024):
                        output.write(block)
            with temporary.open("rb") as saved:
                signature = saved.read(5)
            if signature != b"%PDF-":
                raise ValueError("Response is not a PDF")
            with pymupdf.open(temporary) as document:
                if not document.page_count or document.needs_pass:
                    raise ValueError("PDF is empty or encrypted")
                page_count = document.page_count
            temporary.replace(destination)
            downloaded += 1
            print(
                f"[{index}/{len(jobs)}] {ac}/{part}: saved "
                f"{destination.stat().st_size / 1e6:.1f} MB, {page_count} pages",
                flush=True,
            )
            break
        except (requests.RequestException, ValueError, pymupdf.FileDataError) as error:
            temporary.unlink(missing_ok=True)
            status = getattr(getattr(error, "response", None), "status_code", None)
            print(f"{ac}/{part}: attempt {attempt + 1}: {error}", flush=True)
            if status in (401, 403, 406):
                raise SystemExit("Source refused access; stopping without treating booths as absent.")
            if status == 404 or attempt == 2:
                failed += 1
                break
            time.sleep(2 ** (attempt + 1))
    time.sleep(0.5)

print(f"Downloaded: {downloaded}; already saved: {skipped}; failed: {failed}")
raise SystemExit(1 if failed else 0)
