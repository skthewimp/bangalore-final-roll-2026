# Tables and public release

## Public files

The [5 November 2026 release](https://github.com/skthewimp/bangalore-final-roll-2026/releases/tag/data-2026-11-05)
contains `booth-counts.parquet`, `booth-counts.csv`, `metadata.json` and
`SHA256SUMS`. The public key is `(ac, part)`. There are 9,008 rows.

| Column | Meaning |
|---|---|
| `ac`, `constituency_name`, `part` | Configured constituency and polling-part identity |
| `cards` | All detected physical-card records kept in the final snapshot |
| `retained_cards`, `deleted_cards`, `unread_deletion_status` | Counts from the card-level deletion flags |
| `retained_male`, `retained_female` | Retained cards with those printed gender readings |
| `retained_other_or_unread_gender` | Retained cards outside the two preceding categories, including unread values |
| `accepted_count_exception` | Whether this booth's residual count/OCR checks were explicitly accepted |

These are OCR-derived counts. They include the documented exceptions and do not
establish that every retained card is a different person. Other/unread gender
is not a count of third-gender electors alone. Constituency rollups can be
computed from this table without retaining another summary dataset.

The public files contain no names, EPICs, addresses, house numbers, individual
ages or relationships, raw OCR, or person-level record locations. The exporter
constructs a new table and strips metadata; it does not merely hide columns in
a source file. All person-level runtime directories are excluded from Git and
from the CI artifact workflow.

## Detailed local schema

`schema.json` defines the 45 columns used by extraction. One row is one card,
keyed by `(ac, part, page, card)`. Detailed tables stay local/private.

| Group | Columns |
|---|---|
| Source and identity | `ac`, `constituency_name`, `part`, `language`, `source_url`, `pdf_sha256`, `page`, `card`, `bbox` |
| Parsed card | `serial`, `epic`, `name`, `relation_type`, `relation_name`, `house_number`, `age`, `gender` |
| Locality | `main_town_or_village`, `ward`, `post_office`, `police_station`, `patwari`, `tehsil`, `district`, `pincode` |
| Station and cover text | `polling_station_name`, `polling_station_address`, `polling_area_details`, `polling_station_details` |
| Section | `page_header`, `section_number`, `section_address`, `section_source`, `roll_component` |
| Status | `is_deleted`, `is_modified`, `deletion_reason_code`, `deletion_text_detected`, `status` |
| OCR and review | `ocr_text`, `serial_ocr`, `serial_without_marker`, `ocr_mean_confidence`, `marker_hash_correlation`, `review_flags` |

Blank source fields and unresolved readings remain missing. EPIC case is
normalised, but ambiguous characters are not substituted. Duplicate serial or
EPIC readings are flagged, not used as a reason to delete a physical card.
The double type of `serial_without_marker` preserves the historical schema;
use `serial` for the parsed serial observation.

## Provenance and acceptance

Parquet footers contain `roll_validation`: source PDF hash, extraction pipeline,
printed totals, observed counts and validation outcome. Review promotions add
their audit. Accepted exceptions retain their failed checks and approval;
`complete` in the queue means included in canonical output, not that an accepted
exception independently reconciled. Historical text repairs add parent hashes
and changed-card keys, with original files archived.

The detailed 5 November snapshot occupies 729,424,992 bytes in 9,008 files; the
largest booth file is about 165 KB. Its size is manageable, but the public data
distribution here is aggregate only. Use release assets for versioned data
downloads rather than adding repeated binary snapshots to Git history.
