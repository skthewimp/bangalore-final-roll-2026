"""Combine the frozen booth files into one verified Parquet per constituency."""

import argparse
import base64
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=Path('data'))
parser.add_argument('--output', type=Path, default=Path('data_combined'))
args = parser.parse_args()
source = args.source.resolve()
output = args.output.resolve()
staging = output.with_name(output.name + '.partial')
key = ['ac', 'part', 'page', 'card']
files = sorted(source.glob('ac_*/*.parquet'))

if not files:
    raise RuntimeError(f'No booth Parquet files found in {source}')
if output.exists() or staging.exists():
    raise RuntimeError('Output or staging folder already exists; choose a new output folder')

# Check the intended physical-card key before writing any combined dataset.
# DuckDB reads only the key columns rather than loading the detailed roll.
connection = duckdb.connect()
connection.read_parquet([str(path) for path in files]).create_view('cards')
conflicts = connection.execute(
    """
    SELECT
        ac,
        part,
        page,
        card,
        COUNT(*) AS records
    FROM cards
    GROUP BY
        ac,
        part,
        page,
        card
    HAVING
        COUNT(*) > 1
        OR ac IS NULL
        OR part IS NULL
        OR page IS NULL
        OR card IS NULL
    """
).fetchall()
connection.close()
if conflicts:
    print('Conflicting or missing physical-card keys:', flush=True)
    for conflict in conflicts:
        print(dict(zip([*key, 'records'], conflict)), flush=True)
    raise RuntimeError('Combined write blocked: source card keys need review')
print('Source physical-card keys are unique and non-null', flush=True)

# Preserve common schema metadata and all booth-specific metadata. In particular,
# a combined file must not present one booth's roll_validation as its own audit.
staging.mkdir(parents=True)
total_rows = 0
for folder in sorted({path.parent for path in files}):
    booth_files = sorted(folder.glob('*.parquet'))
    tables = [pq.ParquetFile(path).read() for path in booth_files]
    source_metadata = [table.schema.metadata or {} for table in tables]
    common_metadata = {
        name: value
        for name, value in source_metadata[0].items()
        if name != b'roll_validation'
        and all(metadata.get(name) == value for metadata in source_metadata)
    }
    # Base64 preserves arbitrary Arrow metadata bytes without lossy decoding.
    booth_metadata = {
        str(path.relative_to(source)): {
            base64.b64encode(name).decode('ascii'): base64.b64encode(value).decode('ascii')
            for name, value in metadata.items()
            if name not in common_metadata
        }
        for path, metadata in zip(booth_files, source_metadata)
    }
    combined = pa.concat_tables([table.replace_schema_metadata(None) for table in tables])
    combined_metadata = {
        **common_metadata,
        b'source_booth_metadata_base64': json.dumps(booth_metadata).encode(),
        b'physical_card_key': json.dumps(key).encode(),
    }
    combined = combined.replace_schema_metadata(combined_metadata)
    destination = staging / f'{folder.name}.parquet'
    pq.write_table(combined, destination, compression='zstd', row_group_size=65536)

    # Verify every value, row order, schema, and retained metadata after writing.
    check = pq.ParquetFile(destination).read()
    if not combined.equals(check, check_metadata=True):
        raise RuntimeError(f'Combined file differs from its source records: {destination}')
    total_rows += combined.num_rows
    print(f'{destination.name}: {len(booth_files)} booths, {combined.num_rows:,} rows', flush=True)
    del tables, combined, check

# Publish the new folder only once every constituency passes read-back checks.
staging.rename(output)
print(f'Created {output}: {len(list(output.glob("*.parquet")))} files, {total_rows:,} rows', flush=True)
