"""Export booth counts with an explicit output allowlist and no person records."""

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as csv
import pyarrow.parquet as pq


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True, help='Canonical production directory')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = args.directory.resolve()
source = json.loads((root / 'source.json').read_text())
names = {row['ac']: row['name'] for row in source['constituencies']}
expected = {
    (row['ac'], part)
    for row in source['constituencies']
    for part in range(1, row['expected_parts'] + 1)
}
rows = []
seen = set()
for path in sorted((root / 'data').glob('ac_*/*.parquet')):
    table = pq.read_table(path, columns=['ac', 'part', 'is_deleted', 'gender', 'review_flags'])
    acs, parts = pc.unique(table['ac']).to_pylist(), pc.unique(table['part']).to_pylist()
    if len(acs) != 1 or len(parts) != 1:
        raise RuntimeError(f'{path}: booth identity is missing or ambiguous')
    ac, part = acs[0], parts[0]
    key = (ac, part)
    if key not in expected or key in seen:
        raise RuntimeError(f'{path}: unexpected or duplicate output key {key}')
    seen.add(key)
    validation = json.loads(table.schema.metadata[b'roll_validation'])
    if validation['state'] != 'complete' or validation['rows'] != table.num_rows:
        raise RuntimeError(f'{path}: canonical validation metadata differs')
    retained_mask = pc.equal(table['is_deleted'], False)
    retained = pc.sum(retained_mask).as_py() or 0
    deleted = pc.sum(table['is_deleted']).as_py() or 0
    unread = pc.sum(pc.is_null(table['is_deleted'])).as_py() or 0
    if retained + deleted + unread != table.num_rows:
        raise RuntimeError(f'{path}: deletion categories do not partition the records')
    male = pc.sum(pc.and_(retained_mask, pc.equal(table['gender'], 'Male'))).as_py() or 0
    female = pc.sum(pc.and_(retained_mask, pc.equal(table['gender'], 'Female'))).as_py() or 0
    # Construct a new table from this allowlist. Source metadata, raw OCR,
    # names, IDs, addresses and individual age/relationship fields never pass.
    rows.append({
        'ac': ac, 'constituency_name': names[ac], 'part': part,
        'cards': table.num_rows, 'retained_cards': retained,
        'deleted_cards': deleted, 'unread_deletion_status': unread,
        'retained_male': male, 'retained_female': female,
        'retained_other_or_unread_gender': retained - male - female,
        'accepted_count_exception': 'accepted_exceptions' in validation,
    })
if seen != expected:
    raise RuntimeError(f'Canonical coverage differs; missing booths: {sorted(expected - seen)}')

# Intended public key: (ac, part), one row per configured booth. The preceding
# checks reject duplicates instead of silently aggregating conflicting files.
public = pa.Table.from_pylist(rows).replace_schema_metadata(None)
args.output.mkdir(parents=True, exist_ok=True)
parquet = args.output / 'booth-counts.parquet'
comma_separated = args.output / 'booth-counts.csv'
pq.write_table(public, parquet, compression='zstd')
csv.write_csv(public, comma_separated)
if not pq.read_table(parquet).equals(public):
    raise RuntimeError('Public Parquet read-back differs')
metadata = {
    'snapshot_date': '2026-11-05', 'grain': 'one polling part per assembly constituency',
    'key': ['ac', 'part'], 'booths': len(rows),
    'cards': sum(row['cards'] for row in rows),
    'retained_cards': sum(row['retained_cards'] for row in rows),
    'deleted_cards': sum(row['deleted_cards'] for row in rows),
    'accepted_count_exception_booths': sum(row['accepted_count_exception'] for row in rows),
    'columns': public.column_names,
    'source': source['url_template'],
    'note': 'OCR-derived card counts, including explicitly accepted booth exceptions; not a deduplicated register of people.',
}
manifest = args.output / 'metadata.json'
manifest.write_text(json.dumps(metadata, indent=2) + '\n')
checksums = ''.join(
    f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
    for path in [parquet, comma_separated, manifest]
)
(args.output / 'SHA256SUMS').write_text(checksums)
print(json.dumps(metadata), flush=True)
