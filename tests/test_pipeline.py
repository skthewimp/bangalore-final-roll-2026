"""Offline regressions: literal TSV quotes and initialisation from a clean clone."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

PROJECT = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(PROJECT))

from extract import ocr


class PipelineTests(unittest.TestCase):
    def test_ocr_quote_does_not_swallow_later_words(self):
        tsv = 'level\tpage_num\tconf\ttext\n5\t1\t95\t"\n5\t1\t96\tExample\n'
        result = subprocess.CompletedProcess([], 0, stdout=tsv, stderr='')
        with patch('extract.subprocess.run', return_value=result):
            words = ocr(Image.new('RGB', (20, 20), 'white'))
        self.assertEqual([word['text'] for word in words], ['"', 'Example'])

    def test_init_needs_no_private_pilot_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            subprocess.run(
                [sys.executable, str(PROJECT / 'production.py'), 'init', '--directory', str(root)],
                check=True, capture_output=True, text=True,
            )
            source = json.loads((PROJECT / 'source.json').read_text())
            expected = sum(row['expected_parts'] for row in source['constituencies'])
            with sqlite3.connect(root / 'progress.sqlite') as connection:
                actual = connection.execute('SELECT COUNT(*) FROM booths').fetchone()[0]
            connection.close()
            self.assertEqual(actual, expected)
            schema = pa.ipc.read_schema(pa.BufferReader((root / 'schema.arrow').read_bytes()))
            definition = json.loads((PROJECT / 'schema.json').read_text())
            self.assertEqual(schema.names, [field['name'] for field in definition])
            self.assertTrue(all(not pa.types.is_null(field.type) for field in schema))
            self.assertTrue((root / 'code' / 'worker.py').exists())
            self.assertFalse((root / 'data').exists())

    def test_public_export_drops_person_fields_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'data' / 'ac_1').mkdir(parents=True)
            (root / 'source.json').write_text(json.dumps({
                'url_template': 'https://example.invalid/{ac}/{part}',
                'constituencies': [{'ac': 1, 'name': 'Example constituency', 'expected_parts': 1}],
            }))
            source = pa.table({
                'ac': [1, 1], 'part': [1, 1], 'is_deleted': [False, True],
                'gender': ['Female', 'Male'], 'review_flags': ['', ''],
                'epic': ['SYNTHETIC_PRIVATE', 'SYNTHETIC_PRIVATE'],
            }).replace_schema_metadata({
                b'roll_validation': json.dumps({'state': 'complete', 'rows': 2}).encode(),
                b'private_marker': b'DO_NOT_PUBLISH',
            })
            pq.write_table(source, root / 'data' / 'ac_1' / 'part_0001.parquet')
            command = [
                sys.executable, str(PROJECT / 'aggregate_public_data.py'),
                '--directory', str(root), '--output', str(root / 'public'),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            public = pq.read_table(root / 'public' / 'booth-counts.parquet')
            self.assertEqual(public.num_rows, 1)
            self.assertEqual(public['cards'].to_pylist(), [2])
            self.assertNotIn('epic', public.column_names)
            self.assertIsNone(public.schema.metadata)

            # A second file claiming the same public key must block the write,
            # rather than quietly double the booth's counts.
            pq.write_table(source, root / 'data' / 'ac_1' / 'duplicate.parquet')
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('duplicate output key', failed.stderr)


if __name__ == '__main__':
    unittest.main()
