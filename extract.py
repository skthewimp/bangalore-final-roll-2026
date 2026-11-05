"""Pilot full-field OCR of English final-roll PDFs; no bulk download or deletion.

Output grain: one physical elector card, keyed by AC, part, PDF page and card
position. OCR serials and EPICs are observations, never inferred from sequence.
The output is a review sample, not a canonical voter register.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pymupdf
from PIL import Image, ImageDraw
from scipy import ndimage

from context import enrich_pdf, read_cover_context


ROOT = Path(__file__).resolve().parent


def ocr(image, config="6", digits_only=False):
    """Read a temporary image; return words with locations and confidence."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory).resolve() / "ocr.png"
        image.save(path)
        command = ["tesseract", str(path), "stdout", "--psm", config]
        if digits_only:
            command += ["-c", "tessedit_char_whitelist=0123456789"]
        result = subprocess.run(
            command + ["tsv"],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    return [
        word
        # Tesseract TSV text is literal: a quotation mark is an OCR character,
        # not the start of a quoted, potentially multiline CSV field.
        for word in csv.DictReader(
            io.StringIO(result.stdout), delimiter="\t", quoting=csv.QUOTE_NONE
        )
        if word.get("text", "").strip() and float(word["conf"]) >= 0
    ]


def lines_from_words(words):
    lines = {}
    for word in words:
        key = tuple(word[field] for field in ("block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(word["text"])
    return "\n".join(" ".join(values) for values in lines.values())


def find_cards(image):
    """Pair long horizontal rules into card rectangles, including lone cards.

    Use the page's own rules rather than assuming ten complete rows. A card is
    about one-third of the page width and 5–12% of its height in this template.
    Text checks downstream distinguish elector cards from other ruled tables.
    """
    ink = (np.asarray(image.convert("L")) < 130).astype(np.uint8)
    length = int(image.width * 0.25)
    horizontal = ndimage.minimum_filter1d(ink, length, axis=1, mode="constant")
    horizontal = ndimage.maximum_filter1d(horizontal, length, axis=1, mode="constant")
    labelled, _ = ndimage.label(horizontal)
    rules = []
    for region in ndimage.find_objects(labelled):
        y, x = region
        if 0.25 < (x.stop - x.start) / image.width < 0.4:
            rules.append((x.start, x.stop, (y.start + y.stop) // 2))
    rectangles = []
    for left, right, top in rules:
        below = [
            rule
            for rule in rules
            if abs(rule[0] - left) < image.width * 0.01
            and abs(rule[1] - right) < image.width * 0.01
            and rule[2] > top + 4
        ]
        if not below:
            continue
        bottom = min(rule[2] for rule in below)
        if 0.05 < (bottom - top) / image.height < 0.12:
            rectangles.append((left, top, right, bottom))
    return sorted(rectangles, key=lambda box: (round(box[1] / 10), box[0]))


def read_page(job):
    """Read three contact sheets per page, keeping words attached to cards."""
    pdf_path, page_number = job
    with pymupdf.open(pdf_path) as document:
        page = document[page_number - 1]
        pixmap = page.get_pixmap()
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
    return read_page_image(image, page_number)


def read_page_image(image, page_number):
    """OCR a rendered page; callers can keep native PDF access in one thread."""
    rectangles = find_cards(image) if page_number >= 3 else []
    if not rectangles:
        return [], lines_from_words(ocr(image)), {"page": page_number, "rectangles": 0}

    # Give serials a separate, border-free contact sheet. Reading the printed
    # number directly avoids substituting a guessed sequential serial.
    serial_height = 90
    serial_sheet = Image.new("RGB", (450, serial_height * len(rectangles)), "white")
    for slot, box in enumerate(rectangles):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        header_image = image.crop(
            (left + 3, top + 3, left + width * 0.45, top + height * 0.24)
        )
        header_ink = (np.asarray(header_image.convert("L")) < 130).astype(np.uint8)
        rule_length = int(width * 0.15)
        header_rules = ndimage.minimum_filter1d(
            header_ink, rule_length, axis=1, mode="constant"
        )
        header_rules = ndimage.maximum_filter1d(
            header_rules, rule_length, axis=1, mode="constant"
        )
        header_labels, _ = ndimage.label(header_rules)
        frames = sorted(
            ndimage.find_objects(header_labels), key=lambda region: region[0].start
        )
        frame_pairs = [
            (upper, lower)
            for upper in frames
            for lower in frames
            if lower[0].start > upper[0].stop + 8
            and abs(upper[1].start - lower[1].start) < 5
            and abs(upper[1].stop - lower[1].stop) < 5
        ]
        if frame_pairs:
            upper, lower = frame_pairs[0]
            number = header_image.crop(
                (
                    upper[1].start + 3,
                    upper[0].stop + 2,
                    upper[1].stop - 3,
                    lower[0].start - 2,
                )
            )
        else:
            number = header_image
        # Trim whitespace so single digits and multi-digit serials have the same
        # baseline and readable size in the sheet.
        number_ink = np.asarray(number.convert("L")) < 150
        locations = np.argwhere(number_ink)
        if len(locations):
            y0, x0 = locations.min(axis=0)
            y1, x1 = locations.max(axis=0) + 1
            number = number.crop((x0, y0, x1, y1))
            number = number.resize(
                (max(1, round(number.width * 40 / number.height)), 40)
            )
        serial_sheet.paste(number, (20, slot * serial_height + 20))
    serial_words = ocr(serial_sheet, digits_only=True)
    serials = {}
    for slot in range(len(rectangles)):
        readings = [
            word["text"]
            for word in serial_words
            if slot * serial_height <= int(word["top"]) < (slot + 1) * serial_height
        ]
        serials[slot + 1] = " ".join(readings)

    records = []
    for column in range(3):
        cards = [
            (index + 1, box)
            for index, box in enumerate(rectangles)
            if min(2, int(((box[0] + box[2]) / 2) / (image.width / 3))) == column
        ]
        if not cards:
            continue
        width = max(box[2] - box[0] for _, box in cards)
        height = max(box[3] - box[1] for _, box in cards) + 40
        sheet = Image.new("RGB", (width + 40, height * len(cards)), "white")
        for slot, (_, box) in enumerate(cards):
            crop = image.crop((box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4))
            draw = ImageDraw.Draw(crop)
            # The photo placeholder is outside the text field; do not OCR it.
            draw.rectangle(
                (crop.width * 0.75, crop.height * 0.2, crop.width, crop.height),
                fill="white",
            )
            # Erase the serial box outline without erasing its number.
            draw.rectangle((0, 0, crop.width * 0.33, 5), fill="white")
            draw.rectangle(
                (0, crop.height * 0.145, crop.width * 0.33, crop.height * 0.19),
                fill="white",
            )
            sheet.paste(crop, (20, slot * height + 20))
        words = ocr(sheet)
        for slot, (position, box) in enumerate(cards):
            card_words = [
                word
                for word in words
                if slot * height <= int(word["top"]) < (slot + 1) * height
            ]
            text = lines_from_words(card_words)
            # Keep questionable cards, rather than silently dropping OCR failures.
            records.append(
                {
                    "page": page_number,
                    "card": position,
                    "bbox": json.dumps(box),
                    "ocr_text": text,
                    "serial_ocr": serials[position],
                    "ocr_mean_confidence": (
                        round(float(np.mean([float(w["conf"]) for w in card_words])), 1)
                        if card_words
                        else None
                    ),
                }
            )
    return records, "", {"page": page_number, "rectangles": len(rectangles)}


def read_cached_page(job):
    pdf_path, page_number, digest = job
    cache = ROOT / "pilot" / "ocr-cache" / digest / f"{page_number}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    result = read_page((pdf_path, page_number))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result))
    return result


def parse_card(record):
    text = record["ocr_text"]

    def field(pattern):
        match = re.search(pattern, text, re.I | re.M)
        return match.group(1).strip() if match else None

    header = text.split("\nName", 1)[0]
    serial = re.fullmatch(r"\d{1,5}", record["serial_ocr"])
    # Letter case is immaterial to EPIC; do not substitute O/0 or S/5.
    epic = re.search(r"\b[A-Z]{3}[0-9]{7}\b", header.upper())
    age = field(r"\bAge\s*[:;]?\s*(\d{1,3})\b")
    relation = re.search(
        r"\b(Father|Husband|Mother|Wife|Other|Guardian)[’'s]*\s*(?:Name)?\s*[:;=]\s*(.*)",
        text,
        re.I,
    )
    house = re.search(
        r"House\s*(?:Number|No\.?)[\s:;=?]*(.*?)(?=\n\s*Age\b|\Z)",
        text,
        re.I | re.S,
    )
    result = {
        **record,
        "serial": int(serial.group(0)) if serial else None,
        "epic": epic.group(0) if epic else None,
        "name": field(r"^Name\s*[:;=]?\s*(.*)"),
        "relation_type": relation.group(1).lower() if relation else None,
        "relation_name": relation.group(2).strip() if relation else None,
        "house_number": " ".join(house.group(1).split()) if house else None,
        "age": int(age) if age else None,
        "gender": field(r"\bGender\s*[:;]?\s*(Female|Male|Third Gender|Other)\b"),
        "deletion_text_detected": bool(re.search(r"\bDELETED\b", text, re.I)),
        "status": "unverified",
    }
    problems = [
        f"missing_{key}"
        for key in (
            "serial",
            "epic",
            "name",
            "relation_name",
            "house_number",
            "age",
            "gender",
        )
        if result[key] is None
    ]
    if result["age"] is not None and not 18 <= result["age"] <= 120:
        problems.append("age_outside_18_120_review")
    result["review_flags"] = ";".join(problems)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ac", type=int, nargs="+", default=[150, 162, 170, 174, 176])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pages", type=int, help="Read only the first N pages per PDF")
    args = parser.parse_args()
    output_dir = ROOT / "pilot"
    output_dir.mkdir(exist_ok=True)
    all_records = []
    source = json.loads((ROOT / "source.json").read_text())
    constituency_names = {row["ac"]: row["name"] for row in source["constituencies"]}
    for pdf_path in sorted((ROOT / "raw" / "ENG").glob("*/*.pdf")):
        ac = int(pdf_path.parent.name)
        if ac not in args.ac:
            continue
        part = int(re.search(r"-ENG-(\d+)-WI", pdf_path.name).group(1))
        with pymupdf.open(pdf_path) as document:
            pages = min(len(document), args.pages) if args.pages else len(document)
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        cover_context = read_cover_context(pdf_path, digest, ocr, lines_from_words)
        print(f"AC {ac}, part {part}: OCR {pages} pages", flush=True)
        booth_records = []
        page_notes = []
        with ThreadPoolExecutor(args.workers) as pool:
            for records, page_text, inspection in pool.map(
                read_cached_page,
                [(str(pdf_path), page, digest) for page in range(1, pages + 1)],
            ):
                print(
                    f"  page {inspection['page']}: {len(records)} candidate cards",
                    flush=True,
                )
                page_notes.append(
                    f"## Page {inspection['page']}\n\n{page_text or chr(10).join(r['ocr_text'] for r in records)}"
                )
                for record in records:
                    booth_records.append(
                        parse_card(
                            {
                                "ac": ac,
                                "constituency_name": constituency_names[ac],
                                "part": part,
                                "language": "ENG",
                                "source_url": json.loads(
                                    (ROOT / "source.json").read_text()
                                )["url_template"].format(
                                    ac=ac, part=part, language="ENG"
                                ),
                                "pdf_sha256": digest,
                                **cover_context,
                                **record,
                            }
                        )
                    )
        # Reusable OCR derivative supports review without repeatedly OCRing PDFs.
        (output_dir / f"{ac}-{part}.md").write_text("\n\n".join(page_notes))
        booth_records = enrich_pdf(
            pdf_path, booth_records, digest, ocr, lines_from_words
        )
        all_records.extend(booth_records)
        print(f"AC {ac}: {len(booth_records)} candidate records", flush=True)

    result = pd.DataFrame(all_records)
    physical_key = ["ac", "part", "page", "card"]
    duplicates = result[result.duplicated(physical_key, keep=False)]
    print("Duplicate physical-card keys:", len(duplicates))
    if len(duplicates):
        print(duplicates[physical_key].to_string(index=False))
        raise SystemExit("Conflicting source-card keys: review before saving")
    for column in ("serial", "age"):
        result[column] = result[column].astype("Int64")
    result["section_number"] = result["section_number"].astype("Int64")
    result["is_deleted"] = result["is_deleted"].astype("boolean")
    result["is_modified"] = result["is_modified"].astype("boolean")
    # Keep both rows when OCR produces a duplicate identity reading. A physical
    # source-card key stays unique even when the serial or EPIC is ambiguous.
    for column, key in (("serial", ["ac", "part", "serial"]), ("epic", ["epic"])):
        repeated = result[column].notna() & result.duplicated(key, keep=False)
        result.loc[repeated, "review_flags"] = result.loc[repeated, "review_flags"].map(
            lambda value: ";".join(filter(None, [value, f"duplicate_{column}_reading"]))
        )
    result = result.sort_values(physical_key).reset_index(drop=True)
    result.to_parquet(output_dir / "cards.parquet", index=False)
    print(
        result.groupby("ac")
        .agg(
            cards=("card", "size"),
            missing_serial=("serial", lambda s: s.isna().sum()),
            missing_epic=("epic", lambda s: s.isna().sum()),
            flagged=("review_flags", lambda s: s.ne("").sum()),
        )
        .to_string()
    )
    print(
        result.groupby("ac")
        .agg(
            deleted=("is_deleted", "sum"),
            missing_sections=("section_address", lambda s: s.isna().sum()),
        )
        .to_string()
    )
    print("Sample saved for human review; PDFs retained.")
