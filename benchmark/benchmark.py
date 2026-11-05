"""Time an uncached full-booth extraction inside the server benchmark container."""

import argparse
import hashlib
import json
import resource
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd
import pymupdf

import context
from extract import lines_from_words, ocr, parse_card, read_page


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--workers", type=int, required=True)
args = parser.parse_args()
output = Path("/output")
output.mkdir(exist_ok=True)
url = "https://voters.eci.gov.in/eroll/2026/s10/sir-finalroll/162/2026-EROLLGEN-S10-162-SIR-FinalRoll-Revision1-ENG-1-WI.pdf"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
started = time.perf_counter()
with tempfile.TemporaryDirectory() as temporary:
    work = Path(temporary)
    context.ROOT = work
    pdf = work / "sample.pdf"
    with urlopen(
        Request(url, headers={"User-Agent": user_agent}), timeout=90
    ) as response:
        pdf.write_bytes(response.read())
    download_seconds = time.perf_counter() - started
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    with pymupdf.open(pdf) as document:
        page_count = len(document)
    print(f"Downloaded {pdf.stat().st_size} bytes; {page_count} pages", flush=True)

    phase = time.perf_counter()
    cover = context.read_cover_context(pdf, digest, ocr, lines_from_words)
    cover_seconds = time.perf_counter() - phase
    phase = time.perf_counter()
    records = []
    with ThreadPoolExecutor(args.workers) as pool:
        for rows, text, inspection in pool.map(
            read_page,
            [(str(pdf), page) for page in range(1, page_count + 1)],
        ):
            records.extend(
                parse_card(
                    {
                        "ac": 162,
                        "constituency_name": "Shivajinagar",
                        "part": 1,
                        "source_url": url,
                        "pdf_sha256": digest,
                        **cover,
                        **row,
                    }
                )
                for row in rows
            )
            print(f"Page {inspection['page']}: {len(rows)} cards", flush=True)
    card_seconds = time.perf_counter() - phase
    phase = time.perf_counter()
    records = context.enrich_pdf(pdf, records, digest, ocr, lines_from_words)
    context_seconds = time.perf_counter() - phase
    phase = time.perf_counter()
    data = pd.DataFrame(records).sort_values(["ac", "part", "page", "card"])
    data.to_parquet(output / f"{args.workers}-workers.parquet", index=False)
    write_seconds = time.perf_counter() - phase

result = {
    "workers": args.workers,
    "pdf_sha256": digest,
    "pages": page_count,
    "cards": len(data),
    "deleted": int(data.is_deleted.sum()),
    "unknown_deletion": int(data.is_deleted.isna().sum()),
    "missing_section_address": int(data.section_address.isna().sum()),
    "duplicate_card_keys": int(data.duplicated(["ac", "part", "page", "card"]).sum()),
    "download_seconds": download_seconds,
    "cover_seconds": cover_seconds,
    "card_seconds": card_seconds,
    "context_seconds": context_seconds,
    "write_seconds": write_seconds,
    "total_seconds": time.perf_counter() - started,
    "python_peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
    "tesseract_version": subprocess.check_output(
        ["tesseract", "--version"], text=True
    ).splitlines()[0],
}
memory_peak = Path("/sys/fs/cgroup/memory.peak")
if memory_peak.exists():
    result["container_peak_memory_mb"] = int(memory_peak.read_text()) / 1024**2
(output / f"{args.workers}-workers.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2), flush=True)
