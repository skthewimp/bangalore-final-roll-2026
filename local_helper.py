"""Contribute three Mac OCR workers to the server's queue for up to 36 hours.

Reserve the last 100 available booths, process with the unchanged frozen reader,
then transfer and commit results before requesting another batch. Interruptions
retain local checkpoints and reservations; restart this command to recover them.
Touch helper/PAUSED for a graceful stop, upload and release of unfinished work.
"""

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from durable import atomic_json
import production


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hours', type=float, default=36)
parser.add_argument('--workers', type=int, default=3)
parser.add_argument('--batch-size', type=int, default=100)
parser.add_argument('--server', default='server', help='SSH host or configured alias')
parser.add_argument('--server-directory', default='/srv/bangalore-final-roll/production')
parser.add_argument('--container', default='bangalore-final-roll')
parser.add_argument('--owner', default='mac-helper')
args = parser.parse_args()
project = Path(__file__).resolve().parent
root = project / 'helper'
root.mkdir(exist_ok=True)
lock = (root / 'coordinator.lock').open('w')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
logger = logging.getLogger('helper')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(root / 'helper.log', maxBytes=5_000_000, backupCount=2)
handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(handler)
owner = args.owner
server_root = args.server_directory
if not server_root.startswith('/') or any(character.isspace() for character in server_root):
    raise ValueError('Server directory must be an absolute path without whitespace')
if not owner or any(character not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for character in owner):
    raise ValueError('Owner must contain lowercase letters, digits and hyphens only')
ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
       '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', args.server]
remote_script = ['docker', 'exec', args.container, 'python',
                 '/data/shared_queue.py']
ending = time.time() + args.hours * 3600
stopping = False
child = None


def request_stop(signum, frame):
    global stopping
    stopping = True


for signum in (signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, request_stop)


def remote(command):
    response = subprocess.run(
        [*ssh, *remote_script, command, '--owner', owner,
         '--batch-size', str(args.batch_size)],
        check=False, capture_output=True, text=True, timeout=600,
    )
    if response.returncode:
        raise RuntimeError(f"Server {command} failed: {response.stderr.strip()}")
    return json.loads(response.stdout)


def upload_finished():
    connection = production.database(root)
    results = [dict(row) for row in connection.execute(
        "SELECT ac, part, state, rows, deleted, flagged, error FROM booths "
        "WHERE state IN ('complete', 'review')"
    )]
    connection.close()
    if not results:
        return
    paths = []
    for result in results:
        category = 'data' if result['state'] == 'complete' else 'review-data'
        relative = Path(category) / f"ac_{result['ac']}" / f"part_{result['part']:04}.parquet"
        path = root / relative
        result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if path.exists():
            paths.append(str(relative))
        if result['state'] == 'review':
            paths.append(f"work/{result['ac']}-{result['part']}/")
    atomic_json(root / 'results.json', results)
    paths.append('results.json')
    subprocess.run(
        [*ssh, 'mkdir', '-p', f'{server_root}/incoming/{owner}'], check=True, timeout=60
    )
    subprocess.run(
        ['rsync', '-aR', '--timeout=120', '-e',
         'ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3',
         *paths, f'{args.server}:{server_root}/incoming/{owner}/'],
        cwd=root, check=True, timeout=1800,
    )
    committed = remote('commit')
    connection = production.database(root)
    for job in committed:
        connection.execute('DELETE FROM booths WHERE ac = ? AND part = ?',
                           (job['ac'], job['part']))
    connection.commit()
    connection.close()
    # Server is now the durable owner; do not accumulate duplicate PDFs here.
    for job in committed:
        for category in ('data', 'review-data'):
            (root / category / f"ac_{job['ac']}" / f"part_{job['part']:04}.parquet").unlink(missing_ok=True)
        shutil.rmtree(root / 'work' / f"{job['ac']}-{job['part']}", ignore_errors=True)
    subprocess.run([*ssh, 'rm', '-rf', f'{server_root}/incoming/{owner}'], check=True, timeout=60)
    logger.info('Server committed %s booths', len(committed))


logger.info('Helper started: %s OCR workers, batches %s, deadline %s',
            args.workers, args.batch_size, time.ctime(ending))
while True:
    try:
        upload_finished()
        if stopping or time.time() >= ending or (root / 'PAUSED').exists():
            logger.info('Releasing unfinished reservations: %s', remote('release'))
            connection = production.database(root)
            connection.execute('DELETE FROM booths')
            connection.commit()
            connection.close()
            break
        jobs = remote('reserve')
        if not jobs:
            logger.info('No available booths remain')
            break
        connection = production.database(root)
        connection.executemany(
            'INSERT OR IGNORE INTO booths (ac, part) VALUES (?, ?)',
            [(job['ac'], job['part']) for job in jobs],
        )
        connection.commit()
        connection.close()
        logger.info('Reserved %s booths from AC %s part %s backwards',
                    len(jobs), jobs[0]['ac'], jobs[0]['part'])
        child = subprocess.Popen([
            sys.executable, '-u', str(root / 'code' / 'production.py'), 'run',
            '--directory', str(root), '--workers', str(args.workers),
            '--max-booths', str(args.batch_size),
        ])
        while child.poll() is None:
            if stopping or time.time() >= ending or (root / 'PAUSED').exists():
                child.terminate()
                child.wait(timeout=60)
                break
            time.sleep(2)
        if child.returncode:
            raise RuntimeError(f'Local production runner exited {child.returncode}')
        child = None
    except Exception:
        logger.exception('Coordination failed; reservations and checkpoints retained; retry in 60s')
        time.sleep(60)
logger.info('Helper stopped; server remains authoritative')
