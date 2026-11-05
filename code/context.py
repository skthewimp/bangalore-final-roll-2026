"""Read roll sections and serial-box markers without discarding voter cards.

The '#' glyph template comes from gouthamganeshm/Karnataka_Final_Roll_2026,
scripts/ocr/hash-mark.npy. Its documented marker convention distinguishes
modified cards (#) from deletion reason letters beside the printed serial.
"""

import json
import re
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np
import pymupdf
from PIL import Image
from scipy import ndimage

from durable import atomic_json, cached_json


ROOT = Path(__file__).resolve().parents[1]
HASH_TEMPLATE = np.load(Path(__file__).resolve().parent / "hash-mark.npy")


def read_cover_context(pdf_path, digest, ocr, lines_from_words):
    """Keep polling-area and polling-station addresses as separate contexts."""
    cache = ROOT / "pilot" / "cover-cache" / f"{digest}.json"
    saved = cached_json(cache)
    if saved is not None:
        return parse_cover_context(saved)
    with pymupdf.open(pdf_path) as document:
        pixmap = document[0].get_pixmap()
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
    words = ocr(image)
    lines = {}
    for word in words:
        key = tuple(word[field] for field in ("block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(word)
    bands = {}
    headings = {
        "area": r"Details of part and polling area",
        "station": r"Polling station details",
        "counts": r"NUMBER OF ELECTORS",
    }
    for line in lines.values():
        text = " ".join(word["text"] for word in line)
        for name, pattern in headings.items():
            if re.search(pattern, text, re.I):
                bands[name] = (
                    min(int(word["top"]) for word in line),
                    max(int(word["top"]) + int(word["height"]) for word in line),
                )
    contexts = {"polling_area_details": None, "polling_station_details": None}
    # The printed vertical rule determines the column split. This avoids
    # mixing section names with locality fields, or station type with address.
    for field, start, end, side in (
        ("polling_area_details", "area", "station", "right"),
        ("polling_station_details", "station", "counts", "left"),
    ):
        if start not in bands or end not in bands:
            continue
        top, bottom = bands[start][1] + 8, bands[end][0] - 8
        ink = np.asarray(image.convert("L"))[top:bottom] < 130
        candidates = np.flatnonzero(ink.mean(axis=0) > 0.45)
        interior = candidates[
            (candidates > image.width * 0.2) & (candidates < image.width * 0.8)
        ]
        if not len(interior):
            continue
        split = int(interior[0])
        left, right = (
            (split + 6, image.width - 20) if side == "right" else (20, split - 6)
        )
        contexts[field] = lines_from_words(ocr(image.crop((left, top, right, bottom))))

    atomic_json(cache, contexts)
    return parse_cover_context(contexts)


def parse_cover_context(contexts):
    """Parse cached cover OCR afresh when field-reading rules change."""
    area = contexts["polling_area_details"] or ""
    for field, label in {
        "main_town_or_village": "Main Town or Village",
        "ward": "Ward",
        "post_office": "Post Office",
        "police_station": "Police Station",
        "patwari": "Patwari",
        "tehsil": "Tehsil",
        "district": "District",
        "pincode": r"Pin\s*code",
    }.items():
        match = re.search(
            rf"^[ \t]*{label}[ \t]*[:;>?]?[ \t]*([^\n]*)", area, re.I | re.M
        )
        contexts[field] = match.group(1).strip() or None if match else None
    station = contexts["polling_station_details"] or ""
    address = re.search(
        r"Address of Polling Station\s*[:;]?\s*(.*)", station, re.I | re.S
    )
    name = re.search(
        r"Name of Polling Station\s*[:;]?\s*(.*?)(?=Address of Polling Station|\Z)",
        station,
        re.I | re.S,
    )
    contexts["polling_station_name"] = " ".join(name.group(1).split()) if name else None
    contexts["polling_station_address"] = (
        " ".join(address.group(1).split()) if address else None
    )
    return contexts


def header_boxes(image, bounds):
    """Locate the serial box and any separate section-number box."""
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    header = image.crop((left + 3, top + 3, left + width * 0.64, top + height * 0.24))
    ink = (np.asarray(header.convert("L")) < 130).astype(np.uint8)
    length = int(width * 0.1)
    rules = ndimage.minimum_filter1d(ink, length, axis=1, mode="constant")
    rules = ndimage.maximum_filter1d(rules, length, axis=1, mode="constant")
    labels, _ = ndimage.label(rules)
    lines = ndimage.find_objects(labels)
    boxes = []
    for upper in lines:
        for lower in lines:
            if (
                lower[0].start > upper[0].stop + 8
                and abs(upper[1].start - lower[1].start) < 5
                and abs(upper[1].stop - lower[1].stop) < 5
                and 0.1 * width < upper[1].stop - upper[1].start < 0.6 * width
            ):
                boxes.append(
                    (
                        upper[1].start,
                        header.crop(
                            (
                                upper[1].start + 3,
                                upper[0].stop + 2,
                                upper[1].stop - 3,
                                lower[0].start - 2,
                            )
                        ),
                    )
                )
    return [crop for _, crop in sorted(boxes, key=lambda item: item[0])]


def marker_in_box(image):
    """Return the separated marker glyph, '#' correlation and printed number.

    Digits have narrow inter-character gaps. The leading mark is separated
    from the right-aligned serial by a substantially wider blank band.
    """
    ink = 255 - np.asarray(image.convert("L")).astype(np.float32)
    columns = np.flatnonzero((ink > 127).any(axis=0))
    if len(columns) < 2:
        return None, None, image
    gaps = np.diff(columns)
    wide_gaps = np.flatnonzero(gaps > max(7, 0.55 * image.height))
    if not len(wide_gaps):
        return None, None, image
    boundary = wide_gaps[0]
    marker_end = boundary
    symbol_gaps = np.flatnonzero(gaps[:boundary] > 1)
    if len(symbol_gaps):
        marker_end = symbol_gaps[0]
    blob = ink[:, columns[0] : columns[marker_end] + 1]
    ys, xs = np.where(blob > 64)
    blob = blob[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    marker = Image.fromarray(255 - blob.astype(np.uint8))
    normalised = (
        np.asarray(
            Image.fromarray(blob.astype(np.uint8)).resize(
                (16, 20), Image.Resampling.BILINEAR
            )
        )
        .astype(np.float32)
        .ravel()
    )
    normalised -= normalised.mean()
    normalised /= np.linalg.norm(normalised) + 1e-6
    score = float(normalised @ HASH_TEMPLATE)
    number = image.crop((columns[boundary + 1], 0, image.width, image.height))
    return marker, score, number


def read_numbers(images, ocr):
    """OCR aligned number crops together, retaining unresolved readings."""
    if not images:
        return []
    height = 90
    sheet = Image.new("RGB", (450, height * len(images)), "white")
    for index, image in enumerate(images):
        ink = np.argwhere(np.asarray(image.convert("L")) < 150)
        if not len(ink):
            continue
        y0, x0 = ink.min(axis=0)
        y1, x1 = ink.max(axis=0) + 1
        crop = image.crop((x0, y0, x1, y1))
        crop = crop.resize((max(1, round(crop.width * 40 / crop.height)), 40))
        sheet.paste(crop, (20, index * height + 20))
    words = ocr(sheet, digits_only=True)
    readings = []
    for index in range(len(images)):
        text = " ".join(
            word["text"]
            for word in words
            if index * height <= int(word["top"]) < (index + 1) * height
        )
        number = int(text) if re.fullmatch(r"\d+", text) else None
        if number is None:
            crop = images[index]
            enlarged = crop.resize((crop.width * 3, crop.height * 3))
            padded = Image.new(
                "RGB", (enlarged.width + 40, enlarged.height + 40), "white"
            )
            padded.paste(enlarged, (20, 20))
            retry_readings = []
            for mode in ("7", "10"):
                retry = " ".join(
                    word["text"] for word in ocr(padded, config=mode, digits_only=True)
                )
                if re.fullmatch(r"\d+", retry):
                    retry_readings.append(int(retry))
            number = (
                retry_readings[0]
                if len(retry_readings) == 2 and len(set(retry_readings)) == 1
                else None
            )
        readings.append(number)
    return readings


def read_context_image(image, page_number, page_records, ocr, lines_from_words):
    """Read one rendered page independently; PDF access stays in the parent."""
    contexts = {}
    page_header = lines_from_words(
        ocr(
            image.crop(
                (
                    0,
                    0,
                    image.width,
                    round(image.height * 0.033),
                )
            )
        )
    )
    section_match = re.search(
        r"Section\s*No\.?\s*and\s*Name\s*[:;]?\s*(\d+)\s*[-–:]\s*(.+)",
        page_header,
        re.I,
    )
    page_section = int(section_match.group(1)) if section_match else None
    section_address = section_match.group(2).strip() if section_match else None
    additions = bool(re.search(r"List\s+of\s+Additions", page_header, re.I))
    number_images, section_images, section_keys = [], [], []
    for record in page_records:
        key = f"{page_number}-{record['card']}"
        boxes = header_boxes(image, json.loads(record["bbox"]))
        context = {
            "page_header": page_header,
            "section_number": page_section,
            "section_address": section_address,
            "section_source": "page_header" if section_match else None,
            "roll_component": "addition" if additions else "main",
            "is_deleted": None,
            "is_modified": None,
            "deletion_reason_code": None,
            "marker_hash_correlation": None,
        }
        if boxes:
            marker, score, number = marker_in_box(boxes[0])
            context["marker_hash_correlation"] = score
            context["is_modified"] = bool(marker is not None and score >= 0.5)
            context["is_deleted"] = bool(marker is not None and score < 0.5)
            number_images.append(number)
            if context["is_deleted"]:
                enlarged = marker.resize((marker.width * 4, marker.height * 4))
                padded = Image.new(
                    "RGB",
                    (enlarged.width + 40, enlarged.height + 40),
                    "white",
                )
                padded.paste(enlarged, (20, 20))
                reason = lines_from_words(ocr(padded, config="10")).strip().upper()
                context["deletion_reason_code"] = (
                    reason if reason in {"E", "S", "R", "M", "Q"} else None
                )
        else:
            number_images.append(Image.new("RGB", (30, 30), "white"))
        if additions and len(boxes) >= 2:
            section_images.append(boxes[1])
            section_keys.append(key)
        contexts[key] = context
    for record, number in zip(page_records, read_numbers(number_images, ocr)):
        contexts[f"{page_number}-{record['card']}"]["serial_without_marker"] = number
    for key, number in zip(section_keys, read_numbers(section_images, ocr)):
        contexts[key]["section_number"] = number
        contexts[key]["section_source"] = "card_section_box"
    print(
        f"  context page {page_number}: section {page_section}, additions {additions}",
        flush=True,
    )
    return contexts


def enrich_pdf(pdf_path, records, digest, ocr, lines_from_words, workers=4):
    """Read page context once, then attach it to every physical card."""
    cache = ROOT / "pilot" / "context-cache" / f"{digest}.json"
    saved = cached_json(cache)
    if saved is not None:
        contexts = saved
        named_sections = {
            value["section_number"]
            for value in contexts.values()
            if value["section_address"] is not None
        }
        # Unresolved geometry is retried after reader improvements; successful
        # pages retain their costly OCR rather than being read again.
        unresolved_pages = {record["page"] for record in records} - {
            int(key.split("-")[0]) for key in contexts
        }
        unresolved_pages.update(
            {
                int(key.split("-")[0])
                for key, value in contexts.items()
                if value["is_deleted"] is None
                or value["section_number"] is None
                or (
                    value["section_source"] == "card_section_box"
                    and value["section_number"] not in named_sections
                )
            }
        )
    else:
        contexts = {}
        unresolved_pages = {record["page"] for record in records}
    if unresolved_pages:
        pages = iter(sorted(unresolved_pages))
        grouped_records = {
            page: [record for record in records if record["page"] == page]
            for page in unresolved_pages
        }
        with pymupdf.open(pdf_path) as document, ThreadPoolExecutor(workers) as pool:
            pending = set()

            def submit_next():
                page = next(pages, None)
                if page is None:
                    return False
                # Bound in-flight images to the worker count. PyMuPDF is called
                # only here, on the parent thread; OCR tasks use plain images.
                pixmap = document[page - 1].get_pixmap()
                image = Image.frombytes(
                    "RGB", [pixmap.width, pixmap.height], pixmap.samples
                )
                pending.add(
                    pool.submit(
                        read_context_image,
                        image,
                        page,
                        grouped_records[page],
                        ocr,
                        lines_from_words,
                    )
                )
                return True

            for slot in range(workers):
                if not submit_next():
                    break
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.remove(future)
                    contexts.update(future.result())
                    atomic_json(cache, contexts)
                    submit_next()

    # Main pages document section names; additions reference those same section
    # numbers individually. Only an unambiguous observed name is propagated.
    addresses = {}
    for context in contexts.values():
        if context["section_address"]:
            addresses.setdefault(context["section_number"], set()).add(
                context["section_address"]
            )
    for record in records:
        context = dict(contexts[f"{record['page']}-{record['card']}"])
        number = context["serial_without_marker"]
        flags = record["review_flags"].split(";") if record["review_flags"] else []
        if number is not None:
            record["serial"] = number
            flags = [flag for flag in flags if flag != "missing_serial"]
        candidates = addresses.get(context["section_number"], set())
        if context["section_address"] is None and len(candidates) == 1:
            context["section_address"] = next(iter(candidates))
        if context["section_number"] is None or context["section_address"] is None:
            flags.append("missing_section_context")
        if context["is_deleted"] is None:
            flags.append("unread_deletion_marker")
        if context["is_deleted"] is True:
            flags.append("deleted_card_text_review")
        record.update(context)
        record["status"] = (
            "deleted"
            if context["is_deleted"] is True
            else "retained" if context["is_deleted"] is False else "unverified"
        )
        record["review_flags"] = ";".join(flags)
    return records
