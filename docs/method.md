# How the tables are made

## Source and unit of observation

`source.json` selects ACs 150 through 177, with an expected part count for each.
The official URL pattern supplies separate English and Kannada PDFs. Production
reads the English edition. The PDF pages are raster scans, so this pipeline
uses image OCR rather than pretending that PDF text extraction is sufficient.

The canonical key is **`(ac, part, page, card)`**, the position of a physical
card. OCR serials and EPICs are observations. They can repeat or be misread;
they are not used to discard, deduplicate or invent records. Deleted entries
stay in the output. Retained-card counts are not independently established
counts of distinct people.

## Extraction

1. Download to a partial file, preserve HTTP validators where supplied, and
   validate the complete PDF before renaming it. HTTP 404 is a failure, not an
   empty booth. A compatible partial download can resume.
2. Read the cover's polling-area and polling-station context in separate
   columns. Locality fields and station addresses remain distinct from each
   elector's section address and house-number text.
3. Detect card rectangles from printed horizontal rules. Read serial numbers
   separately and build three OCR contact sheets per page, one per card column.
   Mask the photograph placeholder while reading text fields.
4. Parse fields without guessing ambiguous EPIC characters or filling a serial
   sequence. Keep raw OCR, confidence, bounding boxes, source URL and PDF hash.
5. Read page headers for section names. Addition cards have individual section
   boxes; join a section number only to an unambiguous name actually observed
   in that PDF.
6. Inspect the serial-box symbol. `#` marks a modified record; a separated
   deletion-reason letter marks a deleted one. Unread markers stay unresolved.

The queue freezes code, source configuration and Arrow schema at initialisation.
Page OCR and context have atomic checkpoints. Rendering is bounded by the worker
count, and completed page OCR is reused after interruption.

## Validation and storage

Before ordinary promotion, the worker checks physical-card key uniqueness,
printed constituency/part identity, cover arithmetic, detected card count,
deletion count, retained count and missing deletion markers. It also requires
cover address context. The count reader treats the ending serial range and
printed net count as separate observations.

Parquet is written to a temporary sibling, synced, read back and atomically
renamed. SQLite is committed afterward. A crash between those steps can be
reconciled from the durable output. Canonical files are partitioned by booth:
`data/ac_<ac>/part_<part:04>.parquet`.

Successful booth PDFs can be deleted after the data and ledger commit. Held
booths retain PDFs and checkpoints until they are reviewed or explicitly
cleaned up. `review-data` is never silently mixed into canonical output.

## Automated review

The review pass re-reads individual cover cells at two render resolutions,
using segmentation and glyph-size variants when readings disagree. It handles
count rows on continuation pages and cover sections spanning page breaks.
Count arithmetic triggers further reading; it never supplies an unread digit.

Printed map/photograph headings identify ancillary pages whose ruled pictures
were mistaken for elector cards. Pages are excluded only when that evidence is
present and the supposed rows have no elector identity/demographic fields.

Compact deletion letters sometimes sit closer to the serial than the first
detector allowed. A correction requires two literal letter reads, a separately
read matching numeric suffix, and a glyph that does not match the modification
hash. No particular card is chosen merely to make a total reconcile.

For each booth, `work/<ac>-<part>/automated-review.json` stores observations,
changes, source fingerprints and remaining problems. Promoted Parquet carries
that evidence in `roll_validation` metadata.

The historical pass cleared 354 booths, excluded 127 false map/QR detections
and recovered 62 deletion markers. Sixteen booths retained cover-reading or
small card/deletion discrepancies. Their explicit acceptance is recorded in
`accepted-count-exceptions.json`. Accepted rows carry
`accepted_booth_validation_exception`; failed checks remain in their footer
metadata. Another known map detection was excluded when those booths were
included. The final snapshot has 5,974,040 cards across all 9,008 parts.

## Historical text repair

Tesseract emits literal TSV, not quoted CSV. A quote character in a word can
make a default CSV parser swallow later TSV rows. The fix is
`csv.DictReader(..., delimiter='\t', quoting=csv.QUOTE_NONE)`.

The damaged unit is the whole page-column OCR contact sheet. Re-reading only
the record containing malformed text misses neighbouring records whose words
were swallowed. The repair therefore re-reads each affected group, with source
PDF hashes checked against the original extraction. A header retry requires
two agreeing EPIC reads when a fresh full-card read loses an earlier value.

The import checks source and transfer hashes, schema, row count, unique card
keys, unchanged non-text columns, unchanged count validation and absence of
embedded TSV. Original tables are archived in `ocr-repair-history` before
replacement. Missing fields under the fresh read remain flagged rather than
being invented.

## Limits of reproduction

Pinned Python packages and the template support reproducibility, but OCR text
can differ between Tesseract builds and platforms. The Linux benchmark used
Tesseract 5.5.0; the Mac used 5.5.2, with observed text differences even when
section and deletion assignments agreed. A fresh run reproduces the workflow,
schema, validation and explicit decision procedure; byte-identical OCR output
is not promised.

The final table is an extraction of the selected PDFs, with accepted exceptions.
It is not an independent reconciliation against every official constituency
total, a guarantee of field-level OCR accuracy, or a deduplicated register of
people. Use the flags and original official source when a particular entry
matters.
