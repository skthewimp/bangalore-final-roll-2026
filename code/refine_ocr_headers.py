"""Retry EPIC headers when a fresh full-card read loses a prior reading."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
from PIL import Image, ImageOps

from durable import atomic_json, sync_directory
from extract import ocr


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
root = args.directory
recovered = attempted = 0
for audit_path in sorted((root / 'ocr-repair-audits').glob('*.json')):
    audit = json.loads(audit_path.read_text())
    relative = Path(f"ac_{audit['ac']}") / f"part_{audit['part']:04}.parquet"
    original = pq.read_table(root / 'ocr-originals' / relative).to_pandas()
    destination = root / 'ocr-repaired' / relative
    table = pq.read_table(destination)
    data = table.to_pandas()
    missing = original.epic.notna() & data.epic.isna()
    if not missing.any():
        continue
    pdf = root / 'work' / f"{audit['ac']}-{audit['part']}" / 'source.pdf'
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != audit['pdf_sha256']:
        raise RuntimeError(f'{pdf}: source hash differs')
    retries = []
    with pymupdf.open(pdf) as document:
        for page, rows in data[missing].groupby('page'):
            pixmap = document[int(page) - 1].get_pixmap()
            image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
            for index, row in rows.iterrows():
                attempted += 1
                left, top, right, bottom = json.loads(row.bbox)
                header = image.crop((left + (right - left) * 0.35, top + 5,
                                     right - 5, top + (bottom - top) * 0.24))
                readings = []
                for scale, mode in ((3, '7'), (4, '6')):
                    enlarged = header.resize((header.width * scale, header.height * scale))
                    enlarged = ImageOps.expand(enlarged, border=24, fill='white')
                    text = ' '.join(word['text'] for word in ocr(enlarged, config=mode)).upper()
                    readings.append(sorted(set(re.findall(r'\b[A-Z]{3}[0-9]{7}\b', text))))
                if len(readings[0]) == 1 and readings[0] == readings[1]:
                    data.loc[index, 'epic'] = readings[0][0]
                    data.loc[index, 'review_flags'] = ';'.join(
                        flag for flag in row.review_flags.split(';') if flag != 'missing_epic'
                    )
                    recovered += 1
                retries.append({'page': int(page), 'card': int(row.card), 'readings': readings})

    data.review_flags = data.review_flags.map(
        lambda value: ';'.join(flag for flag in value.split(';') if flag != 'duplicate_epic_reading')
    )
    duplicated = data.epic.notna() & data.duplicated(['epic'], keep=False)
    data.loc[duplicated, 'review_flags'] = data.loc[duplicated, 'review_flags'].map(
        lambda value: ';'.join(filter(None, [value, 'duplicate_epic_reading']))
    )
    audit['header_retries'] = retries
    audit['header_retry_version'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    audit['flagged'] = int(data.review_flags.ne('').sum())
    metadata = dict(table.schema.metadata)
    validation = json.loads(metadata[b'roll_validation'])
    validation['flagged'] = audit['flagged']
    validation['ocr_text_repair']['header_retries'] = retries
    validation['ocr_text_repair']['header_retry_version'] = audit['header_retry_version']
    metadata[b'roll_validation'] = json.dumps(validation).encode()
    repaired = pa.Table.from_pandas(data, schema=table.schema, preserve_index=False).replace_schema_metadata(metadata)
    temporary = destination.with_suffix('.parquet.partial')
    pq.write_table(repaired, temporary, compression='zstd')
    with temporary.open('rb') as stream:
        os.fsync(stream.fileno())
    if not pq.read_table(temporary).equals(repaired, check_metadata=True):
        raise RuntimeError(f'{destination}: header retry read-back differs')
    temporary.replace(destination)
    sync_directory(destination.parent)
    audit['repaired_sha256'] = hashlib.sha256(destination.read_bytes()).hexdigest()
    atomic_json(audit_path, audit)
print(json.dumps({'headers_retried': attempted, 'epics_recovered': recovered}), flush=True)
