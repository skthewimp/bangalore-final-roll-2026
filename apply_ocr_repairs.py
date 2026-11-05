"""Verify prepared OCR repairs, archive original booths, then commit atomically."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import pyarrow.compute as pc
import pyarrow.parquet as pq


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, default=Path('/data'))
args = parser.parse_args()
root = args.directory.resolve()
sys.path.insert(0, str(root / 'code'))
sys.path.insert(0, str(root / 'review-code'))
sys.path.insert(0, str(root))
from durable import sync_directory
from repair_ocr_text import TEXT_FIELDS


connection = sqlite3.connect(root / 'progress.sqlite', timeout=30)
connection.execute('PRAGMA synchronous=FULL')
imported = already_present = changed_cards = 0
for audit_path in sorted((root / 'ocr-repair-audits').glob('*.json')):
    audit = json.loads(audit_path.read_text())
    ac, part = audit['ac'], audit['part']
    relative = Path(f'ac_{ac}') / f'part_{part:04}.parquet'
    canonical = root / 'data' / relative
    incoming = root / 'ocr-repaired' / relative
    current_hash = hashlib.sha256(canonical.read_bytes()).hexdigest()
    if current_hash == audit['repaired_sha256']:
        already_present += 1
    else:
        if current_hash != audit['original_sha256']:
            raise RuntimeError(f'{ac}-{part}: canonical output changed after repair was prepared')
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != audit['repaired_sha256']:
            raise RuntimeError(f'{ac}-{part}: transferred repair hash differs')
        original = pq.read_table(canonical)
        repaired = pq.read_table(incoming)
        if not original.schema.equals(repaired.schema, check_metadata=False):
            raise RuntimeError(f'{ac}-{part}: repaired schema differs')
        if original.num_rows != repaired.num_rows or repaired.num_rows != audit['rows']:
            raise RuntimeError(f'{ac}-{part}: repaired row count differs')
        immutable = [field for field in original.column_names if field not in {*TEXT_FIELDS, 'review_flags'}]
        if not original.select(immutable).equals(repaired.select(immutable), check_metadata=False):
            raise RuntimeError(f'{ac}-{part}: repair changed a non-text field')
        keys = repaired.select(['ac', 'part', 'page', 'card']).to_pandas()
        if keys.isna().any().any() or keys.duplicated().any():
            raise RuntimeError(f'{ac}-{part}: physical-card key is missing or duplicated')
        if set(keys.ac) != {ac} or set(keys.part) != {part}:
            raise RuntimeError(f'{ac}-{part}: repaired booth identity differs')
        embedded_tsv = pc.match_substring_regex(repaired['ocr_text'], pattern=r'(?m)^[1-5]\t[0-9]+\t[0-9]+\t')
        if pc.any(embedded_tsv).as_py():
            raise RuntimeError(f'{ac}-{part}: repaired output still has embedded TSV')
        before = json.loads(original.schema.metadata[b'roll_validation'])
        after = json.loads(repaired.schema.metadata[b'roll_validation'])
        for field in ('state', 'ac', 'part', 'rows', 'deleted', 'pdf_sha256', 'pipeline_id', 'printed_totals', 'problems'):
            if before[field] != after[field]:
                raise RuntimeError(f'{ac}-{part}: repair changed validation field {field}')
        if after['state'] != 'complete' or after['pdf_sha256'] != audit['pdf_sha256']:
            raise RuntimeError(f'{ac}-{part}: repair source or validation state differs')
        if after['ocr_text_repair']['original_sha256'] != audit['original_sha256']:
            raise RuntimeError(f'{ac}-{part}: repair metadata has a different parent')

        backup = root / 'ocr-repair-history' / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            if hashlib.sha256(backup.read_bytes()).hexdigest() != audit['original_sha256']:
                raise RuntimeError(f'{ac}-{part}: existing original archive differs')
        else:
            temporary = backup.with_suffix('.parquet.partial')
            shutil.copyfile(canonical, temporary)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != audit['original_sha256']:
                raise RuntimeError(f'{ac}-{part}: archive read-back hash differs')
            temporary.replace(backup)
            sync_directory(backup.parent)
        incoming.replace(canonical)
        sync_directory(canonical.parent)
        imported += 1

    with connection:
        updated = connection.execute(
            """
            UPDATE booths
            SET flagged = ?, updated_at = ?
            WHERE ac = ? AND part = ? AND state = 'complete'
            """,
            (audit['flagged'], time.time(), ac, part),
        ).rowcount
        if updated != 1:
            raise RuntimeError(f'{ac}-{part}: canonical booth is not complete in the ledger')
    changed_cards += len(audit['changed_keys'])
    if (imported + already_present) % 50 == 0:
        print(f'Imported {imported}; already present {already_present}; cards {changed_cards}', flush=True)

print(json.dumps({
    'imported_booths': imported, 'already_present': already_present,
    'repaired_cards': changed_cards,
}), flush=True)
