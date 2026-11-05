# Running unattended

## Docker on Linux

The Dockerfile builds from `python:3.12-slim`, installs Tesseract and the pinned
Python requirements, and checksum-verifies the external numerical template.
It does not depend on a pre-existing benchmark image or private pilot tables.

```bash
docker build -f server.Dockerfile -t bangalore-final-roll:local .
mkdir -p production
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/production:/data" bangalore-final-roll:local init --directory /data
bash server-run.sh
```

`server-run.sh` mounts only `production/`, limits the job to two CPUs and
1,280 MiB RAM, uses a bounded temporary filesystem and bounded Docker logs,
and sets `unless-stopped` restart behaviour. Run it from the server's checkout.
The frozen worker code and schema live in the mounted run directory.

```bash
docker exec bangalore-final-roll python /data/code/production.py status --directory /data
docker stats --no-stream bangalore-final-roll
docker stop bangalore-final-roll
docker start bangalore-final-roll
```

Use `docker stop` for a persistent pause. A runner that simply exits is restarted
by the policy. Stop the container when the queue is finished to avoid an empty
queue restart loop. SSH disconnections do not stop a detached container.

For review, stop the production container and run a one-off process using the
same image and volume. Its entry point is the queue CLI, so override it:

```bash
docker stop bangalore-final-roll
docker run --rm --cpus 2 --memory 1280m --network none \
  -v "$PWD/production:/data" --entrypoint python bangalore-final-roll:local \
  /app/automated_review.py --directory /data --workers 2 --apply
```

Review needs the retained PDFs. Historical text repair may need network access
to re-download completed-booth PDFs and verify their original hashes; omit
`--network none` for that operation. Do not mount private credentials into the
OCR image.

## macOS scheduling

After initialising a queue with your chosen existing Python interpreter:

```bash
"$PYTHON" install_agent.py --directory production --workers 3
```

The installer generates a LaunchAgent using the current checkout and
`sys.executable`; it does not contain another user's paths. It installs and
starts `org.bangalore.final-roll` for the logged-in user, checks every two
minutes, and runs under `caffeinate -i`. It cannot keep a closed-lid Mac or an
explicitly sleeping/shut-down machine processing.

To stop and unload it:

```bash
"$PYTHON" production.py pause --directory production
launchctl bootout "gui/$(id -u)/org.bangalore.final-roll"
```

## Contributing from a second machine

This is optional; a single queue is sufficient. The server ledger is always
authoritative. Do not initialise two independent queues and merge files by hand.

`shared_queue.py` reserves pending/retry booths atomically under an owner name.
The server claims from the front; the helper reserves from the end. Imports
verify source identity, pipeline, schema, transfer hashes and physical-card
keys. A failed upload retains the local output and reservation. Reservations
are released explicitly, never merely because a timeout elapsed.

On the server, copy `shared_queue.py` to its mounted production directory:

```bash
cp shared_queue.py production/shared_queue.py
```

On the helper, copy the server's frozen runtime and metadata before starting:

```bash
mkdir -p helper
rsync -a server:/srv/bangalore-final-roll/production/code helper/
rsync -a server:/srv/bangalore-final-roll/production/manifest.json \
  server:/srv/bangalore-final-roll/production/source.json \
  server:/srv/bangalore-final-roll/production/schema.arrow helper/
"$PYTHON" local_helper.py --server server \
  --server-directory /srv/bangalore-final-roll/production \
  --owner mac-helper --workers 3 --batch-size 100 --hours 36
```

`server` is an SSH alias you configure yourself. Substitute your deployment's
absolute production path. SSH, rsync and access to the Docker container are
required. Keep the server container running while the helper is contributing.
The helper creates its own empty local ledger from server reservations.

`touch helper/PAUSED` requests a graceful upload/release and exit. The deadline
does the same. After an interruption, restart using the same owner to recover
reservations; do not release that owner's booths while its OCR could be active.

## Cleaning up

After all booths are validated or explicitly accepted, stop writers and run
`cleanup_media.py --directory production --apply`. It retains tables and text
evidence. It does not erase the source URLs or SHA-256 values needed to verify
a later download. Media cleanup is an explicit final operation, not a reason
to throw away a failed source during extraction.
