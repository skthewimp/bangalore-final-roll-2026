"""Apply the documented snapshot decisions after checking source and counts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pyarrow.parquet as pq

from automated_review import promote
from durable import atomic_json


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
parser.add_argument('--decisions', type=Path, default=Path(__file__).parent / 'accepted-count-exceptions.json')
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
root = args.directory.resolve()
decisions = json.loads(args.decisions.read_text())
seen = set()
with sqlite3.connect(root / 'progress.sqlite') as connection:
    for item in decisions['booths']:
        ac, part = item['ac'], item['part']
        if (ac, part) in seen:
            raise RuntimeError(f'Duplicate decision key {ac}-{part}')
        seen.add((ac, part))
        job = connection.execute('SELECT state FROM booths WHERE ac = ? AND part = ?', (ac, part)).fetchone()
        if job and job[0] == 'complete':
            continue
        if job is None or job[0] != 'review':
            raise RuntimeError(f'{ac}-{part}: run extraction and automated review first')
        work = root / 'work' / f'{ac}-{part}'
        audit = json.loads((work / 'automated-review.json').read_text())
        source = root / 'review-data' / f'ac_{ac}' / f'part_{part:04}.parquet'
        validation = json.loads(pq.read_metadata(source).metadata[b'roll_validation'])
        if (validation['pdf_sha256'] != item['pdf_sha256']
                or audit['rows'] != item['rows']
                or audit['observed_deleted'] != item['deleted']
                or set(audit['problems']) - set(item['problems'])):
            raise RuntimeError(f'{ac}-{part}: fresh output differs from the approved snapshot')
        print(ac, part, audit['problems'], 'apply' if args.apply else 'dry run')
        if args.apply:
            approval = {
                'approved_by': 'snapshot owner', 'snapshot_date': decisions['snapshot_date'],
                'reason': decisions['reason'], 'problems': audit['problems'],
                'accepted_at': datetime.now(timezone.utc).isoformat(),
            }
            atomic_json(work / 'accepted-exceptions.json', approval)
            promote(audit, accepted_exceptions=approval, directory=root)
