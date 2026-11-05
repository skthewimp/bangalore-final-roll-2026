"""Generate and install a macOS LaunchAgent for an initialised production queue."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent / 'production')
parser.add_argument('--workers', type=int, default=3)
args = parser.parse_args()
if sys.platform != 'darwin':
    raise SystemExit('LaunchAgents are macOS-only; use the Docker runner on Linux.')
root = args.directory.resolve()
if not (root / 'manifest.json').exists():
    raise SystemExit('Run production.py init first.')
label = 'org.bangalore.final-roll'
settings = {
    'Label': label,
    'ProgramArguments': [
        '/usr/bin/caffeinate', '-i', sys.executable, '-u',
        str(root / 'code' / 'production.py'), 'run',
        '--directory', str(root), '--workers', str(args.workers),
    ],
    'WorkingDirectory': str(Path(__file__).resolve().parent),
    'EnvironmentVariables': {
        'PATH': f'{Path(sys.executable).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
        'OMP_THREAD_LIMIT': '1', 'OPENBLAS_NUM_THREADS': '1', 'PYTHONUNBUFFERED': '1',
    },
    'RunAtLoad': True,
    'StartInterval': 120,
    'ProcessType': 'Standard',
    'ExitTimeOut': 30,
    'StandardErrorPath': str(root / 'launch-agent-error.log'),
    'StandardOutPath': str(root / 'launch-agent.log'),
}
target = Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(plistlib.dumps(settings))
domain = f"gui/{os.getuid()}"
subprocess.run(
    ["/bin/launchctl", "bootout", domain + "/" + settings["Label"]],
    capture_output=True,
)
subprocess.run(["/bin/launchctl", "bootstrap", domain, str(target)], check=True)
print("Installed and started:", target)
