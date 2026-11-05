"""Remove downloaded PDFs and raster images; retain tables and OCR text.

Dry-run by default. Use after extraction/review is finished or deliberately
paused: deleting a source also removes the ability to recheck it offline.
Symlinks are never followed. Numerical OCR templates are retained.
"""

import argparse
from collections import Counter
import fcntl
import json
from pathlib import Path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
root = args.directory.resolve()
media_extensions = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.gif', '.bmp'}

# A running queue must not lose the file it is currently reading. Lock files
# remain after a finished process, so their existence alone is not a signal.
locks = []
for path in root.rglob('*.lock'):
    if path.name not in {'run.lock', 'coordinator.lock'} or path.is_symlink():
        continue
    lock = path.open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    locks.append(lock)

counts = Counter()
size = 0
for path in root.rglob('*'):
    if path.is_symlink() or not path.is_file() or '.git' in path.relative_to(root).parts:
        continue
    extensions = set(suffix.lower() for suffix in path.suffixes)
    matched = extensions & media_extensions
    if not matched and path.name != 'download.partial':
        continue
    size += path.stat().st_size
    counts[next(iter(sorted(matched)), '.partial')] += 1
    if args.apply:
        path.unlink()
print(json.dumps({
    'directory': str(root), 'applied': args.apply,
    'files': sum(counts.values()), 'bytes': size, 'types': dict(counts),
}), flush=True)
