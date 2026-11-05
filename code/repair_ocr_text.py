"""Re-read cards whose old OCR text contains swallowed Tesseract TSV rows.

Prepare repaired booth files locally. The server imports them only if its
original Parquet hash still matches. Original server files are archived by
the import step. Source PDF hashes and physical-card keys must match exactly.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
from PIL import Image, ImageDraw
import requests

from durable import atomic_json, sync_directory
from extract import ocr, lines_from_words, parse_card


TSV_ROWS = re.compile(r'(?m)^[1-5]\t\d+\t\d+\t')
TEXT_FIELDS = [
    'ocr_text', 'ocr_mean_confidence', 'epic', 'name', 'relation_type',
    'relation_name', 'house_number', 'age', 'gender', 'deletion_text_detected',
]
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)


def repair_booth(job):
    root = Path(job['directory'])
    ac, part = job['ac'], job['part']
    source = root / 'ocr-originals' / f'ac_{ac}' / f'part_{part:04}.parquet'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_hash != job['sha256']:
        raise RuntimeError(f'{ac}-{part}: input Parquet changed')
    audit_path = root / 'ocr-repair-audits' / f'{ac}-{part}.json'
    destination = root / 'ocr-repaired' / f'ac_{ac}' / f'part_{part:04}.parquet'
    repair_version = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if audit_path.exists() and destination.exists():
        audit = json.loads(audit_path.read_text())
        if audit.get('repair_version') == repair_version and audit['original_sha256'] == source_hash and hashlib.sha256(destination.read_bytes()).hexdigest() == audit['repaired_sha256']:
            return audit

    table = pq.read_table(source)
    data = table.to_pandas()
    key = ['ac', 'part', 'page', 'card']
    if data[key].isna().any().any() or data.duplicated(key).any():
        raise RuntimeError(f'{ac}-{part}: physical-card key is not unique')
    selected = data.ocr_text.map(lambda text: bool(TSV_ROWS.search(text or '')))
    if not selected.any():
        raise RuntimeError(f'{ac}-{part}: no corrupted OCR text found')
    validation = json.loads(table.schema.metadata[b'roll_validation'])

    # Completed booth PDFs were intentionally removed. Re-download only the
    # affected booths and require the exact original bytes before reading them.
    with tempfile.TemporaryDirectory() as directory:
        pdf = root / 'work' / f'{ac}-{part}' / 'source.pdf'
        if not pdf.exists():
            pdf = Path(directory) / 'source.pdf'
            url = data.source_url.iloc[0]
            for attempt in range(3):
                try:
                    with requests.get(url, headers={'User-Agent': USER_AGENT}, stream=True, timeout=(20, 120)) as response:
                        response.raise_for_status()
                        with pdf.open('wb') as output:
                            for block in response.iter_content(1024 * 1024):
                                output.write(block)
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if digest != validation['pdf_sha256']:
            raise RuntimeError(f'{ac}-{part}: source PDF bytes differ from original')
        cached_pdf = root / 'work' / f'{ac}-{part}' / 'source.pdf'
        if pdf != cached_pdf:
            cached_pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.replace(cached_pdf)
            pdf = cached_pdf

        changed_keys = []
        with pymupdf.open(pdf) as document:
            for page, corrupt_rows in data[selected].groupby('page'):
                pixmap = document[int(page) - 1].get_pixmap()
                image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
                # The failed OCR invocation was a contact sheet for one page
                # column. A swallowed TSV field can hide later cards on that
                # same sheet, so re-read its entire source group.
                page_rows = data[data.page.eq(page)]
                columns = page_rows.bbox.map(
                    lambda bounds: min(2, int(
                        ((json.loads(bounds)[0] + json.loads(bounds)[2]) / 2)
                        / (image.width / 3)
                    ))
                )
                affected_columns = set(columns.loc[corrupt_rows.index])
                rows = page_rows[columns.isin(affected_columns)]
                for index, row in rows.iterrows():
                    left, top, right, bottom = json.loads(row.bbox)
                    crop = image.crop((left + 4, top + 4, right - 4, bottom - 4))
                    draw = ImageDraw.Draw(crop)
                    draw.rectangle((crop.width * 0.75, crop.height * 0.2, crop.width, crop.height), fill='white')
                    draw.rectangle((0, 0, crop.width * 0.33, 5), fill='white')
                    draw.rectangle((0, crop.height * 0.145, crop.width * 0.33, crop.height * 0.19), fill='white')
                    words = ocr(crop)
                    text = lines_from_words(words)
                    if TSV_ROWS.search(text):
                        raise RuntimeError(f'{ac}-{part}: fresh OCR still contains TSV rows')
                    record = row.to_dict()
                    record['ocr_text'] = text
                    record['ocr_mean_confidence'] = (
                        round(sum(float(word['conf']) for word in words) / len(words), 1)
                        if words else None
                    )
                    parsed = parse_card(record)
                    for field in TEXT_FIELDS:
                        data.loc[index, field] = parsed[field]
                    # Retain the separately read serial and all source context.
                    contextual_flags = {
                        flag for flag in row.review_flags.split(';')
                        if flag and not flag.startswith('missing_')
                        and flag != 'age_outside_18_120_review'
                        and flag != 'duplicate_epic_reading'
                    }
                    contextual_flags.update(
                        flag for flag in row.review_flags.split(';')
                        if flag in {'missing_section_context', 'missing_serial'}
                    )
                    text_flags = set(filter(None, parsed['review_flags'].split(';')))
                    text_flags.discard('missing_serial')
                    data.loc[index, 'review_flags'] = ';'.join(sorted(contextual_flags | text_flags))
                    changed_keys.append({'page': int(page), 'card': int(row.card)})

    # An EPIC correction can change the flag on another row in the same booth.
    data.review_flags = data.review_flags.map(
        lambda value: ';'.join(flag for flag in value.split(';') if flag != 'duplicate_epic_reading')
    )
    duplicated = data.epic.notna() & data.duplicated(['epic'], keep=False)
    data.loc[duplicated, 'review_flags'] = data.loc[duplicated, 'review_flags'].map(
        lambda value: ';'.join(filter(None, [value, 'duplicate_epic_reading']))
    )
    if data.duplicated(key).any() or len(data) != table.num_rows:
        raise RuntimeError(f'{ac}-{part}: repair changed the physical-card grain')
    audit = {
        'ac': ac, 'part': part, 'rows': len(data), 'changed_keys': changed_keys,
        'original_sha256': source_hash, 'pdf_sha256': digest,
        'repair_version': repair_version,
        'original_corrupted_cards': int(selected.sum()),
        'reader_version': hashlib.sha256(Path(sys.modules['extract'].__file__).read_bytes()).hexdigest(),
        'flagged': int(data.review_flags.ne('').sum()),
    }
    validation['flagged'] = audit['flagged']
    validation['ocr_text_repair'] = dict(audit)
    metadata = dict(table.schema.metadata)
    metadata[b'roll_validation'] = json.dumps(validation).encode()
    repaired = pa.Table.from_pandas(data, schema=table.schema, preserve_index=False).replace_schema_metadata(metadata)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.parquet.partial')
    pq.write_table(repaired, temporary, compression='zstd')
    with temporary.open('rb') as stream:
        os.fsync(stream.fileno())
    if not pq.read_table(temporary).equals(repaired, check_metadata=True):
        raise RuntimeError(f'{ac}-{part}: repaired Parquet read-back differs')
    temporary.replace(destination)
    sync_directory(destination.parent)
    audit['repaired_sha256'] = hashlib.sha256(destination.read_bytes()).hexdigest()
    atomic_json(audit_path, audit)
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    jobs = json.loads((args.directory / 'ocr-repair-jobs.json').read_text())
    for job in jobs:
        job['directory'] = str(args.directory)
    print(f'Repairing {len(jobs)} booths with embedded TSV in card OCR', flush=True)
    failures = []
    with ProcessPoolExecutor(args.workers) as pool:
        pending = {pool.submit(repair_booth, job): job for job in jobs}
        for future in as_completed(pending):
            job = pending[future]
            try:
                print(json.dumps(future.result()), flush=True)
            except Exception as error:
                failure = {'ac': job['ac'], 'part': job['part'], 'error': str(error)}
                failures.append(failure)
                print(json.dumps({'failed': failure}), flush=True)
    print(json.dumps({'finished': len(jobs) - len(failures), 'failures': failures}), flush=True)
    raise SystemExit(1 if failures else 0)
