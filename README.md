# Bangalore electoral roll 2026: PDF to Parquet

An OCR pipeline for the English edition of Bangalore's SIR Final Roll 2026,
published on 27 October 2026. It covers 28 assembly constituencies and 9,008
polling parts across Bangalore Urban and the three BBMP electoral districts.
Bangalore Rural is outside this extraction.

The completed 5 November snapshot contains **5,974,040 physical-card records**.
Deleted cards remain in the tables with deletion flags. A card is a source
record, not necessarily a distinct person or an active elector.

This repository contains the downloader, card and context OCR, durable queue,
automated review, historical OCR repair tools, explicit exception decisions,
media cleanup and public aggregate exporter. It contains no person-level data,
PDFs, card images, private audit text or credentials.

## Public data

[Download the booth-count release](https://github.com/skthewimp/bangalore-final-roll-2026/releases/tag/data-2026-11-05)
as CSV or Parquet. Its key is `(ac, part)`, one row per polling part. It includes
card, deletion and retained gender counts, plus an accepted-exception indicator.
Names, EPIC numbers, individual records, addresses and raw OCR are excluded.

The complete internal Parquet dataset occupies 729,424,992 bytes (about 729 MB).
Size would fit a GitHub Release: the [per-asset limit is 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
The released dataset here is the aggregate version. See [the data guide](docs/data.md).

## Local aggregate analysis

The [R notebooks](notebooks/) read only `data_combined/`:

- [Arrow: constituency counts and deletion shares](notebooks/arrow_constituencies.Rmd)
- [DuckDB: polling-part size comparisons](notebooks/duckdb_booths.Rmd)
- [Arrow → DuckDB: age profiles by gender](notebooks/arrow_duckdb_ages.Rmd)

Open them in RStudio or Positron and run individual chunks. Edit `root` in the
setup chunk for your checkout. They use `arrow`, `duckdb`, `DBI`, `dbplyr`,
`dplyr`, `ggplot2` and `scales`; filtering and aggregation happen before
`collect()`. No notebook needs the original booth files.

The combined folder contains 28 constituency files with all 5,974,040 card
records, occupying 545,177,622 bytes. Deleted records remain in it. Use
`is_deleted == FALSE` for retained-card analysis. A card is not necessarily a
distinct person. The combined files are a frozen snapshot; later booth repairs
require rebuilding them.

`code/combine_data.py` builds this folder from `data/`, checks uniqueness of
`(ac, part, page, card)`, preserves booth metadata and verifies every output
against its source records. It needs Python `duckdb` and `pyarrow` in your
existing environment. Run `python code/combine_data.py` from the repository root,
or choose a new destination with `--output`. It refuses an existing destination
and leaves the source files untouched. Detailed Parquet files remain local
and are excluded from Git.

API references: [Arrow datasets](https://arrow.apache.org/docs/r/reference/open_dataset.html)
and [Arrow's DuckDB bridge](https://arrow.apache.org/docs/r/reference/to_duckdb.html).

## Run a small extraction

Use Linux or macOS, Python 3.12 or 3.13, and Tesseract 5 on `PATH`. Windows users
can use a Linux environment. Work inside an existing Python environment; the
project does not create one. The Python versions are the environments used for
this run, rather than a promise that every OCR version produces identical text.

Install Tesseract with `brew install tesseract` on macOS, or
`sudo apt-get install tesseract-ocr` on Debian/Ubuntu. Then set `PYTHON` to your
existing interpreter and run:

```bash
export PYTHON=/absolute/path/to/your/python
uv pip install --python "$PYTHON" -r code/requirements.txt
"$PYTHON" code/fetch_ocr_template.py
"$PYTHON" code/production.py init --directory production
"$PYTHON" code/production.py run --directory production --ac 162 --part 1 --workers 2 --max-booths 1
"$PYTHON" code/production.py status --directory production
```

Initialisation uses the checked-in `code/schema.json`. No pilot voter table or
machine-specific files are required. The small numerical `#` classifier
template is fetched from a pinned upstream revision and checksum-verified;
see [attribution](THIRD_PARTY_NOTICES.md).

The output is `production/data/ac_162/part_0001.parquet` if validation passes.
A booth that fails validation goes to `production/review-data`, with the PDF
and page checkpoints retained until review or explicit cleanup. Read the status
and worker log to distinguish those outcomes.

## Run the configured coverage

```bash
"$PYTHON" code/production.py run --directory production --workers 4
```

This is a long-running OCR job. It processes one booth at a time, with parallel
page OCR, so it does not load the whole roll into memory. Reserve several GB
for review PDFs and scratch space. The runner pauses below 5 GB free disk.
The 9,008 configured part counts come from an upstream transcription, not a
fresh discovery of every official source URL.

```bash
"$PYTHON" code/production.py pause --directory production
"$PYTHON" code/production.py resume --directory production
"$PYTHON" code/production.py run --directory production --workers 4
```

`resume` clears the persistent pause flag. Start `run` again for a foreground
job; a configured LaunchAgent starts it on its next schedule. A shell job ends
when its process is stopped, so use [Docker or a LaunchAgent](docs/deployment.md)
for unattended work.

## Review and finish

Stop other writers before changing review outputs.

```bash
"$PYTHON" code/automated_review.py --directory production --workers 2
"$PYTHON" code/automated_review.py --directory production --workers 2 --apply
"$PYTHON" code/production.py status --directory production
```

The first command records decisions without promotion. The second promotes
booths that pass all checks. Review caches avoid repeating unchanged checks.
In the original run, this cleared 354 of 370 held booths. The remaining 16
were explicitly accepted as count/OCR exceptions, with their discrepancies
kept in metadata and row flags:

```bash
"$PYTHON" code/accept_review_exceptions.py --directory production
"$PYTHON" code/accept_review_exceptions.py --directory production --apply
```

Those decisions are specific to the documented snapshot. The script checks
source PDF hashes, observed row/deletion counts and the allowed problems;
it refuses a changed extraction. It does not waive arbitrary new failures.
See [how extraction and review work](docs/method.md).

## Historical parser repair

An early TSV reader treated literal quotation marks as CSV quoting. That could
swallow text from later cards in the same OCR contact sheet. The current reader
uses `csv.QUOTE_NONE`. A fresh run should not need this repair sequence, but the
tools used to repair the historical tables are included:

```bash
"$PYTHON" code/prepare_ocr_repairs.py --directory production
"$PYTHON" code/repair_ocr_text.py --directory production --workers 3
"$PYTHON" code/refine_ocr_headers.py --directory production
"$PYTHON" code/apply_ocr_repairs.py --directory production
```

The repair re-reads the affected page-column groups, verifies original PDF
hashes, and checks that physical-card keys and non-text fields do not change.
Original canonical files are archived before an atomic replacement. In this
run, 300 malformed text records led to 2,576 cards being re-read in 245 booths.
The repair recovered 789 missing names, 702 ages and 867 gender readings.

## Export counts and delete source media

```bash
"$PYTHON" code/aggregate_public_data.py --directory production --output release-data
"$PYTHON" code/cleanup_media.py --directory production
"$PYTHON" code/cleanup_media.py --directory production --apply
```

The exporter checks the public `(ac, part)` key and coverage, and constructs
a new table from a fixed count-column allowlist. It never carries source
Parquet metadata or person fields into the public output.

Cleanup is a dry run unless `--apply` is supplied. It removes PDFs, raster
images and partial PDF downloads, preserving Parquet, SQLite, OCR text caches,
decisions and numerical model files. It refuses active queue locks. After
cleanup, a PDF-based recheck requires downloading the original source again.

## Files and further documentation

Pipeline code, runtime configuration and deployment files live in `code/`.
Run the commands above from the repository root. Data stays in its existing
folders; notebooks, tests and benchmarks each have their own directory.

| File | Purpose |
|---|---|
| `code/source.json`, `code/schema.json` | Configured coverage, URLs and 45-column table schema |
| `code/production.py`, `code/worker.py` | Frozen runtime, queue, checkpoints, validation and atomic writes |
| `code/extract.py`, `code/context.py`, `code/totals.py` | Card fields, section/cover context, markers and printed totals |
| `code/automated_review.py` | Targeted cover, map-page and deletion-marker review |
| `code/accepted-count-exceptions.json` | Non-personal snapshot decisions and source fingerprints |
| `code/shared_queue.py`, `code/local_helper.py` | Coordinated contribution from a second machine |
| `code/prepare_ocr_repairs.py`, `code/repair_ocr_text.py`, `code/refine_ocr_headers.py`, `code/apply_ocr_repairs.py` | Historical parser repair and verified import |
| `code/download.py`, `code/extract.py` | Optional standalone downloader and five-PDF exploration pilot |
| `code/cleanup_media.py`, `code/aggregate_public_data.py` | Source-media deletion and public booth counts |

- [Method, validation and limitations](docs/method.md)
- [Schema and public data](docs/data.md)
- [Docker, macOS and two-machine deployment](docs/deployment.md)
- [Development history](DEVLOG.md)
- [Draft project post](blog-post.md)
- [Upstream attribution](THIRD_PARTY_NOTICES.md)

Run the offline checks with `"$PYTHON" -m unittest discover -s tests -v` after
fetching the numerical template. CI does not download electoral PDFs or publish
runtime artifacts. The original code and documentation are MIT licensed;
external source material and the separately fetched template retain their own
terms.
