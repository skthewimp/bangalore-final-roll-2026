# Getting Bangalore's electoral roll into a table

*Draft for Karthik, prepared with Codex from the extraction and review logs.*

At one point in this project, 370 polling booths were waiting for “manual
review”. The whole point of getting the electoral roll into a table was to be
able to work with it. Reading hundreds of PDFs was not an attractive next step.

The scope was the English edition of Bangalore's 2026 final roll: 28 assembly
constituencies, 9,008 polling parts and nearly 60 lakh cards. The source PDFs
look like documents, but the useful contents are pictures. So getting a table
means finding the cards on each page, reading them, figuring out their section,
and deciding what to do with deletion and modification marks.

That last bit matters. A deleted entry remains in the PDF. If you simply count
the cards, you get a different number from the printed net electorate. Dropping
the deleted cards would lose information I might later want. The table keeps
them, with flags. Its row is a physical card, not a claim that this is a unique,
active voter.

The extraction ran on my Mac, moved to a server, and then got a temporary
contribution from the Mac again. This required rather more queue management
than I had initially expected: checkpoints, resumable downloads, file hashes,
and making sure that two machines did not claim the same booth. The useful
result is one Parquet file per booth. The PDFs can be thrown away once the
result has been checked and committed.

The interesting part came after the bulk run. “Manual review” turned out to be
a mixture of quite different problems.

Some cover totals were ordinary OCR mistakes. A printed 571 could come back
as 971. In other PDFs, a long list of section names pushed the count row onto
the next page, which the first reader had not considered. Some rectangles were
on maps and QR-code pages, rather than voter cards. Some deletion letters sat
too close to the serial number for the first detector to separate them.

An automated pass cleared 354 of those 370 booths. It excluded 127 false map
detections and recovered 62 deletion markers. I accepted the remaining 16
small count/OCR discrepancies, keeping both the exceptions and the decision
visible in the data.

There was also a more annoying bug. Tesseract returns TSV with literal text.
Our reader was using the CSV parser's default handling of quotation marks.
A quote could swallow later rows of OCR output. The fix was small; the repair
was wider. An OCR batch contained several cards from the same column, so the
records after the visibly broken one could have lost their text too.

Re-reading the affected groups meant 2,576 cards, and recovered 789 previously
missing names. It was a useful reminder that the row showing an error is not
necessarily the full unit damaged by it.

The finished detailed table has 5,974,040 cards. It is still OCR, with missing
values and flags, rather than ground truth. The code and the checks are public.
The downloadable data is a booth-count table; the detailed names, voter IDs
and addresses remain outside the repository.

The [repo](https://github.com/skthewimp/bangalore-final-roll-2026) includes the
extraction, review and repair tools, plus the decisions needed to understand
the snapshot. If you run it on a different OCR build, expect to inspect the
differences rather than assume that matching row counts prove matching text.
