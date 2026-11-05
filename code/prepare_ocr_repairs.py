"""Prepare only booths with malformed legacy OCR for repair_ocr_text.py."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import pyarrow.compute as pc
import pyarrow.parquet as pq


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
root = args.directory.resolve()
jobs = []
for path in sorted((root / 'data').glob('ac_*/*.parquet')):
    table = pq.read_table(path, columns=['ac', 'part', 'ocr_text'])
    affected = pc.sum(pc.match_substring_regex(
        table['ocr_text'], r'(?m)^[1-5]\t[0-9]+\t[0-9]+\t'
    )).as_py() or 0
    if not affected:
        continue
    acs, parts = pc.unique(table['ac']).to_pylist(), pc.unique(table['part']).to_pylist()
    if len(acs) != 1 or len(parts) != 1:
        raise RuntimeError(f'{path}: ambiguous booth identity')
    ac, part = acs[0], parts[0]
    original = root / 'ocr-originals' / f'ac_{ac}' / f'part_{part:04}.parquet'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    original.parent.mkdir(parents=True, exist_ok=True)
    if original.exists():
        if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'{original}: existing repair input differs')
    else:
        shutil.copyfile(path, original)
    jobs.append({'ac': ac, 'part': part, 'sha256': digest, 'affected_cards': affected})
(root / 'ocr-repair-jobs.json').write_text(json.dumps(jobs, indent=2) + '\n')
print(f'Prepared {len(jobs)} booths, {sum(job["affected_cards"] for job in jobs)} malformed OCR records')
