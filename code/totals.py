"""Read the cover's six printed count cells for booth validation."""

import numpy as np
import pymupdf
from PIL import Image
from scipy import ndimage

from context import read_numbers


def read_cover_totals(pdf_path, ocr):
    with pymupdf.open(pdf_path) as document:
        pixmap = document[0].get_pixmap()
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
    ink = np.asarray(image.convert("L")) < 130
    labels, _ = ndimage.label(ink.mean(axis=1) > 0.6)
    rules = [
        round(float(np.flatnonzero(labels == label).mean()))
        for label in range(1, int(labels.max()) + 1)
    ]
    if len(rules) < 2:
        return {"error": "cover count row rules not found"}
    top, bottom = rules[-2:]
    band = ink[top + 3 : bottom - 2]
    labels, _ = ndimage.label(band.mean(axis=0) > 0.8)
    columns = [
        round(float(np.flatnonzero(labels == label).mean()))
        for label in range(1, int(labels.max()) + 1)
    ]
    cells = [
        image.crop((left + 4, top + 4, right - 3, bottom - 3))
        for left, right in zip(columns, columns[1:])
        if right - left > 15
    ]
    if len(cells) != 6:
        return {"error": "cover count row does not have six cells"}
    values = read_numbers(cells, ocr)
    names = ["start", "end", "male", "female", "third_gender", "net"]
    totals = dict(zip(names, values))
    if any(value is None for value in values):
        return {**totals, "error": "cover count cell unread"}
    if totals["male"] + totals["female"] + totals["third_gender"] != totals["net"]:
        return {**totals, "error": "printed gender counts do not sum to net"}
    if totals["end"] < totals["start"]:
        return {**totals, "error": "printed serial range is reversed"}
    totals["cards"] = totals["end"] - totals["start"] + 1
    totals["deleted"] = totals["cards"] - totals["net"]
    return totals
