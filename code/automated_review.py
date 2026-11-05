"""Re-read review-booth covers and reconcile every structural check.

Run in the existing OCR image with /data mounted. Each retained PDF is the
authority; count arithmetic never supplies a missing OCR digit. Original
review Parquet and checkpoints remain intact after a successful promotion.
The per-booth audit is the evidence needed to reproduce that decision.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
from PIL import Image, ImageOps
from scipy import ndimage


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, default=Path('/data'))
parser.add_argument('--workers', type=int, default=2)
parser.add_argument('--limit', type=int)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args() if __name__ == '__main__' else parser.parse_args([])
root = args.directory.resolve()
sys.path.insert(0, str(root / 'code'))
sys.path.insert(0, str(root / 'review-code'))
from durable import atomic_json, sync_directory
from extract import ocr, lines_from_words
import context
from context import header_boxes, HASH_TEMPLATE, read_numbers, read_cover_context, parse_cover_context

# Keep fresh cover OCR separate from the original extraction checkpoints.
context.ROOT = root / 'automated-review-cache'

COUNT_NAMES = ['start', 'end', 'male', 'female', 'third_gender', 'net']
REVIEW_VERSION = hashlib.sha256(
    Path(__file__).read_bytes() + Path(sys.modules['extract'].__file__).read_bytes()
).hexdigest()


def ocr_images(images, mode='13', digits_only=True):
    """One Tesseract invocation for independent images; retain page boundaries."""
    if not images:
        return []
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for index, image in enumerate(images):
            path = Path(directory) / f'{index:04}.png'
            image.save(path)
            paths.append(str(path))
        listing = Path(directory) / 'images.txt'
        listing.write_text('\n'.join(paths) + '\n')
        command = ['tesseract', str(listing), 'stdout', '--psm', mode]
        if digits_only:
            command += ['-c', 'tessedit_char_whitelist=0123456789']
        result = subprocess.run(
            command + ['tsv'], check=True, capture_output=True, text=True,
            timeout=300, env={**os.environ, 'OMP_THREAD_LIMIT': '1'},
        )
    readings = [[] for _ in images]
    for word in csv.DictReader(io.StringIO(result.stdout), delimiter='\t', quoting=csv.QUOTE_NONE):
        if word.get('text', '').strip() and float(word['conf']) >= 0:
            readings[int(word['page_num']) - 1].append(word['text'])
    return [' '.join(words) for words in readings]


def agreed_count(attempts):
    votes = Counter(attempt['value'] for attempt in attempts if attempt['value'] is not None)
    if not votes:
        return None
    # Several resize variants of one OCR mode are correlated observations.
    # Agreement across segmentation modes takes precedence over that raw tally.
    scores = {
        value: (len({attempt['mode'] for attempt in attempts if attempt['value'] == value}), count)
        for value, count in votes.items()
    }
    value = max(scores, key=scores.get)
    count = votes[value]
    scales = {attempt['scale'] for attempt in attempts if attempt['value'] == value}
    unique_winner = sum(score == scores[value] for score in scores.values()) == 1
    return value if unique_winner and len(scales) == 2 and (len(votes) == 1 or count >= 3) else None


def count_cells(image, scale):
    """Use the actual final table rules, with no fixed page coordinates."""
    ink = np.asarray(image.convert('L')) < 130
    labels, count = ndimage.label(ink.mean(axis=1) > 0.6)
    rules = [
        round(float(np.flatnonzero(labels == label).mean()))
        for label in range(1, count + 1)
    ]
    if len(rules) < 2:
        return []
    top, bottom = rules[-2:]
    band = ink[top + 3 * scale:bottom - 2 * scale]
    # Slightly broken vertical rules should not make a whole cover unreadable.
    for threshold in (0.8, 0.6, 0.45):
        labels, count = ndimage.label(band.mean(axis=0) > threshold)
        columns = [
            round(float(np.flatnonzero(labels == label).mean()))
            for label in range(1, count + 1)
        ]
        cells = [
            image.crop((left + 4 * scale, top + 4 * scale,
                        right - 3 * scale, bottom - 3 * scale))
            for left, right in zip(columns, columns[1:])
            if right - left > 15 * scale
        ]
        if len(cells) == len(COUNT_NAMES):
            return cells
    return []


def reread_cover(pdf, original_totals, observed_rows, page_number=0):
    readings = {name: [] for name in COUNT_NAMES}
    crops = {}
    source_cells = {}
    inputs, keys = [], []
    with pymupdf.open(pdf) as document:
        for scale in (1, 2):
            pixmap = document[page_number].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
            image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
            cells = count_cells(image, scale)
            source_cells[scale] = cells
            for name, cell in zip(COUNT_NAMES, cells):
                bounds = ImageOps.invert(cell.convert('L')).point(
                    lambda value: 255 if value > 100 else 0
                ).getbbox()
                if bounds is None:
                    continue
                crop = cell.crop(bounds)
                crops[(name, scale)] = crop
                crop = crop.resize((max(1, round(crop.width * 64 / crop.height)), 64))
                crop = ImageOps.expand(crop, border=20, fill='white')
                inputs.append(crop)
                keys.append((name, scale))
    for (name, scale), text in zip(keys, ocr_images(inputs)):
        value = int(text) if re.fullmatch(r'\d+', text) else None
        readings[name].append({'scale': scale, 'mode': '13', 'value': value})

    initial = {name: agreed_count(attempts) for name, attempts in readings.items()}
    incomplete = any(value is None for value in initial.values())
    inconsistent = not incomplete and (
        initial['male'] + initial['female'] + initial['third_gender'] != initial['net']
        or initial['end'] - initial['start'] + 1 != observed_rows
    )
    retry_names = {name for name, value in initial.items() if value is None}
    if inconsistent:
        retry_names.update(name for name in COUNT_NAMES if initial[name] != original_totals.get(name))
        if not retry_names:
            retry_names.update(COUNT_NAMES)

    # Only ambiguous cells need more OCR. Different glyph sizes and binarising
    # the actual crop address the font-dependent 5/9 and 7/1 confusions.
    for mode in ('7', '13'):
        inputs, keys = [], []
        for name in sorted(retry_names):
            for scale in (1, 2):
                original = crops.get((name, scale))
                if original is None:
                    continue
                variants = [(96, False)] if mode == '7' else [(96, True), (128, False)]
                for height, binary in variants:
                    crop = original.resize((max(1, round(original.width * height / original.height)), height))
                    if binary:
                        crop = crop.convert('L').point(lambda value: 255 if value >= 150 else 0)
                    inputs.append(ImageOps.expand(crop, border=24, fill='white'))
                    keys.append((name, scale, height, binary))
        for (name, scale, height, binary), text in zip(keys, ocr_images(inputs, mode=mode)):
            value = int(text) if re.fullmatch(r'\d+', text) else None
            readings[name].append({'scale': scale, 'mode': mode, 'height': height, 'binary': binary, 'value': value})

    if incomplete or inconsistent:
        for scale, cells in source_cells.items():
            for name, value in zip(COUNT_NAMES, read_numbers(cells, ocr)):
                readings[name].append({'scale': scale, 'mode': '6', 'height': 40, 'value': value})

    totals = {name: agreed_count(attempts) for name, attempts in readings.items()}
    for attempts in readings.values():
        for attempt in attempts:
            attempt['page'] = page_number + 1
    # Long lists of section names push the count row onto a continuation page.
    # A blank/header-only row is not evidence that the printed counts are absent.
    if page_number == 0 and all(value is None for value in totals.values()):
        with pymupdf.open(pdf) as document:
            cover_pages = min(3, len(document))
        alternatives = []
        for continuation in range(1, cover_pages):
            candidate, evidence = reread_cover(pdf, original_totals, observed_rows, continuation)
            if all(value is not None for value in candidate.values()):
                alternatives.append((candidate, evidence))
        if len(alternatives) == 1:
            return alternatives[0]
    return totals, readings


def exclude_illustration_pages(pdf, data):
    """A map/photograph page is not an elector card, regardless of its rules."""
    exclusions = []
    captions = [
        r'Nazri\s+Naksha', r'Google\s+Map\s+View',
        r'Polling\s+Station\s+Building\s+Front\s+View',
        r'Polling\s+Station\s+Front\s+View', r'Cad\s+View', r'Key\s+Map\s+View',
    ]
    # Examine suspicious pages, rather than assuming that a particular page
    # number is always ancillary. Genuine cards with poor OCR are retained.
    with pymupdf.open(pdf) as document:
        for page, rows in data.groupby('page'):
            if rows[['epic', 'name', 'age', 'gender']].notna().any().any():
                continue
            pixmap = document[int(page) - 1].get_pixmap()
            image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
            # Cyan caption bands otherwise disappear during OCR binarisation.
            image = image.convert('L').point(lambda value: 255 if value >= 150 else 0)
            text = lines_from_words(ocr(image, config='11'))
            matches = [caption for caption in captions if re.search(caption, text, re.I)]
            if len(matches) >= 2:
                exclusions.append({
                    'page': int(page), 'cards': [int(card) for card in rows.card],
                    'matched_captions': matches, 'page_ocr': text,
                })
    excluded_pages = {exclusion['page'] for exclusion in exclusions}
    return data[~data.page.isin(excluded_pages)].copy(), exclusions


def retry_compact_markers(pdf, data):
    """Read a separated letter even when its gap is below the old threshold.

    Digits can also have wide spacing. A candidate therefore needs two literal
    letter reads, a matching independently read numeric suffix, and a glyph
    that does not match the modification hash. Totals never choose the card.
    """
    candidates = []
    with pymupdf.open(pdf) as document:
        for page, rows in data.groupby('page'):
            rows = rows[rows.is_deleted.eq(False) & rows.is_modified.eq(False) & rows.serial.notna()]
            if rows.empty:
                continue
            pixmap = document[int(page) - 1].get_pixmap()
            image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
            for index, row in rows.iterrows():
                boxes = header_boxes(image, json.loads(row.bbox))
                if not boxes:
                    continue
                box = boxes[0]
                ink = np.asarray(box.convert('L')) < 130
                ys, _ = np.where(ink)
                columns = np.flatnonzero(ink.any(axis=0))
                gaps = np.diff(columns)
                if not len(gaps):
                    continue
                boundary = int(np.argmax(gaps))
                others = gaps[(gaps > 1) & (np.arange(len(gaps)) != boundary)]
                if gaps[boundary] < max(4, 0.2 * (ys.max() - ys.min() + 1)):
                    continue
                if len(others) and gaps[boundary] < 1.7 * np.median(others):
                    continue
                glyph = box.crop((int(columns[0]), 0, int(columns[boundary]) + 1, box.height))
                bounds = ImageOps.invert(glyph.convert('L')).point(lambda value: 255 if value > 100 else 0).getbbox()
                if bounds is None:
                    continue
                glyph = glyph.crop(bounds)
                if not 0.15 < glyph.width / glyph.height < 1.1:
                    continue
                normalised = (255 - np.asarray(glyph.convert('L').resize((16, 20)))).astype(float).ravel()
                normalised -= normalised.mean()
                normalised /= np.linalg.norm(normalised) + 1e-6
                score = float(normalised @ HASH_TEMPLATE)
                if score >= 0.5:
                    continue
                suffix = box.crop((int(columns[boundary + 1]), 0, box.width, box.height))
                candidates.append({'index': index, 'glyph': glyph, 'suffix': suffix, 'score': score})

    votes = []
    for height, mode in ((80, '10'), (112, '13')):
        images = []
        for candidate in candidates:
            glyph = candidate['glyph']
            glyph = glyph.resize((max(1, round(glyph.width * height / glyph.height)), height))
            images.append(ImageOps.expand(glyph, border=24, fill='white'))
        votes.append(ocr_images(images, mode=mode, digits_only=False))
    suffix_images = []
    for candidate in candidates:
        suffix = candidate['suffix']
        suffix = suffix.resize((suffix.width * 3, suffix.height * 3))
        suffix_images.append(ImageOps.expand(suffix, border=24, fill='white'))
    suffixes = ocr_images(suffix_images, mode='7')
    changes = []
    for candidate, first, second, suffix in zip(candidates, *votes, suffixes):
        first, second = first.strip().upper(), second.strip().upper()
        index = candidate['index']
        if first != second or first not in {'E', 'S', 'R', 'M', 'Q'}:
            continue
        if not suffix.isdigit() or int(suffix) != int(data.loc[index, 'serial']):
            continue
        changes.append({
            'page': int(data.loc[index, 'page']), 'card': int(data.loc[index, 'card']),
            'reason': first, 'serial': int(suffix), 'hash_score': candidate['score'],
            'letter_readings': [first, second],
        })
        data.loc[index, 'is_deleted'] = True
        data.loc[index, 'deletion_reason_code'] = first
        data.loc[index, 'marker_hash_correlation'] = candidate['score']
        data.loc[index, 'status'] = 'deleted'
        flags = set(filter(None, data.loc[index, 'review_flags'].split(';')))
        flags.add('deleted_card_text_review')
        data.loc[index, 'review_flags'] = ';'.join(sorted(flags))
    return data, changes


def read_continued_cover_context(pdf):
    """Locate cover sections across page breaks using their printed headings."""
    images, offsets, bands = [], [], {}
    headings = {
        'area': r'Details\s+of\s+part\s+and\s+polling\s+area',
        'station': r'Polling\s+station\s+details',
        'counts': r'NUMBER\s+OF\s+ELECTORS',
    }
    offset = 0
    with pymupdf.open(pdf) as document:
        for page in range(min(3, len(document))):
            pixmap = document[page].get_pixmap()
            image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
            image = image.convert('L').point(lambda value: 255 if value >= 150 else 0)
            images.append(image)
            offsets.append(offset)
            lines = {}
            for word in ocr(image):
                key = tuple(word[field] for field in ('block_num', 'par_num', 'line_num'))
                lines.setdefault(key, []).append(word)
            for words in lines.values():
                text = ' '.join(word['text'] for word in words)
                for name, pattern in headings.items():
                    if name not in bands and re.search(pattern, text, re.I):
                        bands[name] = (
                            offset + min(int(word['top']) for word in words),
                            offset + max(int(word['top']) + int(word['height']) for word in words),
                        )
            offset += image.height
            if len(bands) == len(headings):
                break
    contexts = {'polling_area_details': None, 'polling_station_details': None}
    for field, start, end, side in (
        ('polling_area_details', 'area', 'station', 'right'),
        ('polling_station_details', 'station', 'counts', 'left'),
    ):
        if start not in bands or end not in bands:
            continue
        top, bottom = bands[start][1] + 8, bands[end][0] - 8
        snippets = []
        for image, offset in zip(images, offsets):
            y0, y1 = max(0, top - offset), min(image.height, bottom - offset)
            if y1 <= y0:
                continue
            band = np.asarray(image)[y0:y1] < 130
            strengths = band.mean(axis=0)
            interior = np.arange(round(image.width * 0.2), round(image.width * 0.8))
            strongest = strengths[interior].max()
            if strongest < 0.2:
                continue
            split = int(interior[np.flatnonzero(strengths[interior] >= 0.7 * strongest)[0]])
            left, right = (split + 6, image.width - 20) if side == 'right' else (20, split - 6)
            snippets.append(lines_from_words(ocr(image.crop((left, y0, right, y1)))))
        contexts[field] = '\n'.join(filter(None, snippets)) or None
    return parse_cover_context(contexts)


def retry_missing_cover_context(pdf, data, digest):
    missing_area = data.polling_area_details.fillna('').eq('').any()
    missing_station = data.polling_station_details.fillna('').eq('').any()
    if not missing_area and not missing_station:
        return data, {}
    fresh = read_cover_context(pdf, digest, ocr, lines_from_words)
    if (missing_area and not fresh.get('polling_area_details')) or (missing_station and not fresh.get('polling_station_details')):
        fresh = read_continued_cover_context(pdf)
    changes = {}
    fields = []
    if missing_area and fresh.get('polling_area_details'):
        fields += ['polling_area_details', 'main_town_or_village', 'ward', 'post_office',
                   'police_station', 'patwari', 'tehsil', 'district', 'pincode']
    if missing_station and fresh.get('polling_station_details'):
        fields += ['polling_station_details', 'polling_station_name', 'polling_station_address']
    for field in fields:
        changes[field] = fresh.get(field)
        data[field] = fresh.get(field)
    return data, changes


def review_booth(job):
    ac, part = job
    work = root / 'work' / f'{ac}-{part}'
    path = root / 'review-data' / f'ac_{ac}' / f'part_{part:04}.parquet'
    pdf = work / 'source.pdf'
    audit_path = work / 'automated-review.json'
    if audit_path.exists():
        saved = json.loads(audit_path.read_text())
        if saved.get('review_version') == REVIEW_VERSION:
            return saved

    table = pq.read_table(path)
    original = json.loads(table.schema.metadata[b'roll_validation'])
    data = table.to_pandas()
    original_rows = len(data)
    data, exclusions = exclude_illustration_pages(pdf, data)
    data, cover_changes = retry_missing_cover_context(pdf, data, original['pdf_sha256'])
    totals, readings = reread_cover(pdf, original['printed_totals'], len(data))
    marker_changes = []
    if all(value is not None for value in totals.values()):
        expected_cards = totals['end'] - totals['start'] + 1
        expected_deleted = expected_cards - totals['net']
        if len(data) == expected_cards and int(data.is_deleted.sum()) < expected_deleted:
            data, marker_changes = retry_compact_markers(pdf, data)
    problems = []
    if any(value is None for value in totals.values()):
        problems.append('cover counts lack independent OCR agreement')
    else:
        totals['cards'] = totals['end'] - totals['start'] + 1
        totals['deleted'] = totals['cards'] - totals['net']
        if totals['male'] + totals['female'] + totals['third_gender'] != totals['net']:
            problems.append('printed gender counts do not sum to net')
        if totals['end'] < totals['start'] or totals['deleted'] < 0:
            problems.append('printed count arithmetic inconsistent')
        if len(data) != totals['cards']:
            problems.append('detected cards disagree with printed serial range')
        if int(data.is_deleted.sum()) != totals['deleted']:
            problems.append('deletion flags disagree with printed deletion count')
        if int((~data.is_deleted).sum()) != totals['net']:
            problems.append('retained cards disagree with printed net electors')

    # The canonical grain is a physical card, not an OCR serial or EPIC.
    key = ['ac', 'part', 'page', 'card']
    if data[key].isna().any().any() or data.duplicated(key).any():
        problems.append('physical-card key missing or duplicated')
    if set(data.ac) != {ac} or set(data.part) != {part}:
        problems.append('record booth identity inconsistent')
    observed_acs, observed_parts = set(), set()
    for header in data.page_header.dropna().unique():
        identity = re.search(r'Constituency.*?[:;]\s*(\d+)\s*[-–]', header, re.I)
        booth = re.search(r'Part\s*No\.?\s*[:;]?\s*(\d+)', header, re.I)
        if identity:
            observed_acs.add(int(identity.group(1)))
        if booth:
            observed_parts.add(int(booth.group(1)))
    if observed_acs != {ac} or observed_parts != {part}:
        problems.append('printed constituency/part identity unread or inconsistent')
    if data.is_deleted.isna().any():
        problems.append('some deletion markers unread')
    for field in ('polling_area_details', 'polling_station_details'):
        if data[field].fillna('').eq('').any():
            problems.append(f'{field} unread')
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != original['pdf_sha256']:
        problems.append('source PDF hash differs')
    schema = pa.ipc.read_schema(pa.BufferReader((root / 'schema.arrow').read_bytes()))
    if not table.schema.equals(schema, check_metadata=False):
        problems.append('output schema differs')
    manifest = json.loads((root / 'manifest.json').read_text())
    if original['pipeline_id'] not in {manifest['pipeline_id'], *manifest.get('compatible_pipeline_ids', [])}:
        problems.append('extraction pipeline unrecognised')

    audit = {
        'ac': ac, 'part': part, 'review_version': REVIEW_VERSION,
        'checked_at': time.time(), 'rows': len(data), 'original_rows': original_rows,
        'excluded_illustration_pages': exclusions,
        'marker_changes': marker_changes,
        'cover_changes': cover_changes,
        'original_problems': original['problems'],
        'original_totals': original['printed_totals'],
        'printed_totals': totals, 'count_readings': readings,
        'observed_deleted': int(data.is_deleted.sum()),
        'unknown_deletions': int(data.is_deleted.isna().sum()),
        'problems': problems, 'eligible': not problems,
        'source_parquet_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    atomic_json(audit_path, audit)
    return audit


def promote(audit, accepted_exceptions=None, directory=root):
    """Write and verify canonical output before committing its ledger entry."""
    root = Path(directory).resolve()
    if not audit['eligible'] and accepted_exceptions is None:
        raise RuntimeError('Unresolved validation requires explicit acceptance')
    if accepted_exceptions is not None and accepted_exceptions['problems'] != audit['problems']:
        raise RuntimeError('Accepted exceptions do not match the audited problems')
    ac, part = audit['ac'], audit['part']
    source = root / 'review-data' / f'ac_{ac}' / f'part_{part:04}.parquet'
    if hashlib.sha256(source.read_bytes()).hexdigest() != audit['source_parquet_sha256']:
        raise RuntimeError(f'{ac}-{part}: review output changed after audit')
    table = pq.read_table(source)
    excluded_pages = {exclusion['page'] for exclusion in audit['excluded_illustration_pages']}
    if excluded_pages or audit['marker_changes'] or audit['cover_changes'] or accepted_exceptions is not None:
        data = table.to_pandas()
        data = data[~data.page.isin(excluded_pages)].reset_index(drop=True)
        for field, value in audit['cover_changes'].items():
            data[field] = value
        for change in audit['marker_changes']:
            selected = data.page.eq(change['page']) & data.card.eq(change['card'])
            if int(selected.sum()) != 1:
                raise RuntimeError(f'{ac}-{part}: changed marker key is ambiguous')
            data.loc[selected, 'is_deleted'] = True
            data.loc[selected, 'deletion_reason_code'] = change['reason']
            data.loc[selected, 'marker_hash_correlation'] = change['hash_score']
            data.loc[selected, 'status'] = 'deleted'
            data.loc[selected, 'review_flags'] = data.loc[selected, 'review_flags'].map(
                lambda value: ';'.join(sorted(set(filter(None, value.split(';'))) | {'deleted_card_text_review'}))
            )
        if accepted_exceptions is not None:
            data.review_flags = data.review_flags.map(
                lambda value: ';'.join(sorted(set(filter(None, value.split(';'))) | {'accepted_booth_validation_exception'}))
            )
        table = pa.Table.from_pandas(data, schema=table.schema, preserve_index=False)
    keys = table.select(['ac', 'part', 'page', 'card']).to_pandas()
    if keys.isna().any().any() or keys.duplicated().any():
        raise RuntimeError(f'{ac}-{part}: physical-card key is missing or duplicated')
    if set(keys.ac) != {ac} or set(keys.part) != {part}:
        raise RuntimeError(f'{ac}-{part}: canonical booth identity differs')
    metadata = dict(table.schema.metadata)
    validation = json.loads(metadata[b'roll_validation'])
    validation.update(
        state='complete', problems=audit['problems'] if accepted_exceptions is not None else [],
        printed_totals=audit['printed_totals'],
        rows=table.num_rows, deleted=audit['observed_deleted'],
        flagged=sum(bool(value) for value in table.column('review_flags').to_pylist()),
    )
    validation['automated_review'] = audit
    if accepted_exceptions is not None:
        validation['accepted_exceptions'] = accepted_exceptions
    metadata[b'roll_validation'] = json.dumps(validation).encode()
    table = table.replace_schema_metadata(metadata)
    destination = root / 'data' / f'ac_{ac}' / f'part_{part:04}.parquet'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = pq.read_table(destination)
        if not existing.equals(table, check_metadata=True):
            raise RuntimeError(f'{ac}-{part}: conflicting canonical output')
    else:
        temporary = destination.with_suffix('.parquet.partial')
        pq.write_table(table, temporary, compression='zstd')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        checked = pq.read_table(temporary)
        if not checked.equals(table, check_metadata=True):
            raise RuntimeError(f'{ac}-{part}: canonical read-back differs')
        temporary.replace(destination)
        sync_directory(destination.parent)
    with sqlite3.connect(root / 'progress.sqlite', timeout=30) as connection:
        connection.execute('PRAGMA synchronous=FULL')
        changed = connection.execute(
            """
            UPDATE booths
            SET state = 'complete', error = NULL, updated_at = ?,
                rows = ?, deleted = ?, flagged = ?
            WHERE ac = ? AND part = ? AND state = 'review'
            """,
            (time.time(), validation['rows'], validation['deleted'], validation['flagged'], ac, part),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f'{ac}-{part}: ledger was not in review state')


if __name__ == '__main__':
    connection = sqlite3.connect(f'file:{root}/progress.sqlite?mode=ro', uri=True)
    jobs = connection.execute(
        "SELECT ac, part FROM booths WHERE state = 'review' ORDER BY ac, part"
    ).fetchall()
    connection.close()
    if args.limit:
        jobs = jobs[:args.limit]
    outcomes = Counter()
    reasons = Counter()
    print(f'Reviewing {len(jobs)} booths; apply={args.apply}', flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        for audit in pool.map(review_booth, jobs):
            outcome = 'eligible' if audit['eligible'] else 'unresolved'
            if audit['eligible'] and args.apply:
                promote(audit)
                outcome = 'promoted'
            outcomes[outcome] += 1
            reasons.update(audit['problems'])
            print(json.dumps({
                'ac': audit['ac'], 'part': audit['part'], 'outcome': outcome,
                'totals': audit['printed_totals'], 'problems': audit['problems'],
            }), flush=True)
    print(json.dumps({'outcomes': dict(outcomes), 'remaining_reasons': dict(reasons)}), flush=True)
