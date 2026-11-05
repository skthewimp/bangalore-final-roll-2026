# DigitalOcean benchmark, 2026-10-29

Host: `ssh server`, 2 DO-Regular vCPUs, approximately 4 GB RAM. The benchmark
runs in Docker with a 1 GB memory limit. The one-worker trial is capped at one
CPU; the two-worker trial can use both CPUs. No bulk extraction was started.

Both trials independently download and process Shivajinagar AC 162, part 1:
35 pages, 841 cards. Source SHA-256 matches the Mac pilot. All OCR and cover/
section context caches are fresh per trial. Temporary PDFs and caches are
removed after each trial. Only Parquet data and JSON timings are retained.

| Workers | Total elapsed | Card OCR | Cover/context | Peak container memory | Projection for 60 lakh cards |
|---|---:|---:|---:|---:|---:|
| 1 | 248.94 s | 203.45 s | 43.07 s | 542 MiB | 20.6 days |
| 2 | 149.55 s | 106.01 s | 41.35 s | 658 MiB | 12.3 days |

Totals include download and Parquet writing. Projection scales elapsed time
by card count; one booth is not a representative sample of all booth layouts,
retries or future shared-CPU performance. Allow additional time for source
failures, validation and contention with existing services. Manual correction
is not included.

Both server runs produced identical cells, 841 cards, three deletions, no
missing section addresses, and no duplicate physical-card keys. Against the
Mac pilot, section assignments and deletion/modification flags agree. Text
OCR differs: six EPIC fields, 18 names, 19 relative names and five house-number
fields. Differences are not an accuracy score against ground truth. Linux
uses Tesseract 5.5.0; the Mac uses 5.5.2. Pin and check the OCR environment
before a production run.

The Docker image occupies approximately 1.06 GB. Approximately 14 GB remains
free after setup, sufficient for booth-by-booth Parquet output and a bounded
temporary/review queue. The all-in-memory pilot CLI is not the production
streaming runner.

Server files: `~/bangalore-final-roll-benchmark/`; image:
`bangalore-roll-benchmark:local`. Result metrics are copied to local
`results/1-workers.json` and `results/2-workers.json`. Comparison Parquet files
are ignored by Git. The Dockerfile and benchmark script reproduce the test;
mount the extraction modules and glyph template at `/app`, writable results
at `/output`, and pass `--workers 1` or `--workers 2`.
