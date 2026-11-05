#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f production/manifest.json ]]; then
  echo 'Initialise production/ first; see docs/deployment.md.' >&2
  exit 1
fi

if docker container inspect bangalore-final-roll >/dev/null 2>&1; then
  docker start bangalore-final-roll
  exit 0
fi

docker run -d \
  --name bangalore-final-roll \
  --restart unless-stopped \
  --init \
  --user "$(id -u):$(id -g)" \
  --cpus 2 \
  --cpu-shares 256 \
  --memory 1280m \
  --memory-swap 1536m \
  --pids-limit 128 \
  --stop-timeout 30 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --log-opt max-size=5m \
  --log-opt max-file=3 \
  --env TZ=Asia/Kolkata \
  -v "$PWD/production:/data" \
  bangalore-final-roll:local
