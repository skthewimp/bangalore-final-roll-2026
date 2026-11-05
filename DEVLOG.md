# Development log

## 29–30 October 2026: pilot and durable extraction

- Read five English PDFs covering ACs 150, 162, 170, 174 and 176. The pilot had
  3,251 cards, with deleted cards retained and flagged.
- Added separate card, cover, section and serial-marker OCR; verified physical
  card keys and cover/count checks before canonical writes.
- Added atomic page/context checkpoints, resumable downloads, a SQLite queue,
  frozen runtime/schema, disk-space checks and bounded logs.
- Parallel context reading and worker-count comparisons preserved observed
  values in the checked comparisons. Widened null-only optional text schema
  fields instead of constraining later booths to the pilot's missing values.

## 1–4 November 2026: server and temporary Mac helper

- Moved the queue to a two-CPU Docker deployment, preserving 4,458 completed
  booths and the remaining review/pending state. Transfer files were checked
  by size and SHA-256.
- Added atomic server reservations and conditional claims so the Mac could
  contribute three OCR workers without processing the same booth twice.
- Mac batches were imported only after hash, schema, pipeline and card-key
  checks. Local results were removed after server acknowledgement.
- The initial pass completed at about 02:30 IST on 5 November.

## 5 November 2026: automated review and explicit exceptions

> “this is too many to manually review. do a pass of automated reiew”

- Re-read held cover cells and continuation pages; identified map/QR geometry
  mistaken for cards; recovered compact deletion letters from source glyphs.
- Cleared 354 of 370 held booths, excluded 127 false map detections and recovered
  62 deletion markers, with per-booth audit evidence.
- Fixed literal TSV quote parsing. Re-read affected page-column groups rather
  than only the record containing malformed text: 2,576 cards in 245 booths.
  Recovered 789 names, 702 ages and 867 gender readings that had been missing.
- Verified hashes, unchanged non-text fields, keys and read-back before import.
  Archived the previous tables. A full canonical scan found no remaining
  embedded TSV in card OCR text.

> “these are minor deltas. good to ignore.”

- Included the remaining 16 as explicit, flagged exceptions. Kept unresolved
  printed-count readings rather than inventing corrections.
- Final canonical coverage: 9,008 booths and 5,974,040 card records, including
  65,883 marked deleted. Production and helper services were stopped/paused.

## 5 November 2026: public repository

> “now let's make a PUBLIC github repo with this scraper”

> “delete any stored PDFs / images etc.”

- Isolated this project from the surrounding analytics repository.
- Removed stored project PDFs on both machines: 550 files, about 5.6 GB.
  Retained detailed tables, numerical OCR templates and text audit evidence
  outside Git.
- Replaced private-pilot schema inference with a checked-in schema definition.
  Replaced the unpublished Docker base with a self-contained build.
- Made helper paths configurable and generated LaunchAgent settings from the
  current interpreter/check-out. Added regression checks and CI.
- Published code, method/schema/deployment docs and explicit snapshot decisions.
  The public data export uses a count-column allowlist; person-level data stays
  private. The upstream numerical template is pinned and fetched separately,
  with attribution and checksum verification.

Remaining limitations: OCR is not ground truth; the 16 accepted booths retain
their exceptions; platform OCR differences prevent a byte-identical rerun
promise. The public count release does not expose the detailed person records.

<!-- codex-session:01a1aa1c-7110-7ae1-b8f5-bf523e823af1:start -->
## 2026-11-05 10:39 IST | Codex session `01a1aa1c-711`

### User prompts
> show me top 10 rows of data_combined
> create a CSV from data_combined .
> wait is that all the columns? i thought we had more

### Work done
- Created [data_combined.csv](/Users/Karthik/Documents/work/data_work/bangalore/voters/final_roll_2026/data_combined.csv): **5,974,040 rows, 32 columns, 2.91 GB**. Row count verified; physical-card keys are unique. All...
- `polling_area_details` and `polling_station_details` contain the **raw multiline header text** from the roll. Their structured versions remain in the CSV. For the first row: | Removed column | Example content | Kept a...

<!-- codex-session:01a1aa1c-7110-7ae1-b8f5-bf523e823af1:end -->

<!-- codex-session:01a1aa36-fb23-7371-a694-9b2b4d91f535:start -->
## 2026-11-05 11:08 IST | Codex session `01a1aa36-fb2`

### User prompts
> the parquet structure of @data is bad. way too many files. we shuld just combine into 1 parquet file per constituency orsomething

### Work done
- Done. `data/` now has **28 Parquet files**, one per constituency: `ac_150.parquet`, etc. - Reduced from **9,008 files** and **729 MB → 545 MB**. - Preserved all **5,974,040 records** and booth metadata; verified every...
- Touched files: `README.md`, `code/combine_data.py`, `notebooks/arrow_constituencies.Rmd`, `notebooks/arrow_duckdb_ages.Rmd`, `notebooks/duckdb_booths.Rmd`
- README.md | 15 +++++++++------
- code/combine_data.py | 4 ++--

<!-- codex-session:01a1aa36-fb23-7371-a694-9b2b4d91f535:end -->
