"""Fetch the pinned upstream numerical '#' template without bundling it in Git."""

import hashlib
from pathlib import Path
from urllib.request import urlopen


REVISION = 'a44c7e524d2fc3a9bd465ccf68f7a3deb2455456'
SHA256 = 'afae22f70f5035f4d05a198c63338368d8e8d9716e3d9d8a05f8fdecfab699ce'
url = (
    'https://raw.githubusercontent.com/gouthamganeshm/Karnataka_Final_Roll_2026/'
    f'{REVISION}/scripts/ocr/hash-mark.npy'
)
destination = Path(__file__).resolve().parent / 'hash-mark.npy'
if destination.exists():
    content = destination.read_bytes()
else:
    with urlopen(url, timeout=30) as response:
        content = response.read()
if hashlib.sha256(content).hexdigest() != SHA256:
    raise SystemExit('OCR template checksum differs; existing files were not replaced.')
destination.write_bytes(content)
print('Verified numerical OCR template:', destination.name)
