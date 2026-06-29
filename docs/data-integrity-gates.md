# Parsed Data Integrity Gates

The canonical parsed pipeline fails closed when completeness cannot be proven.

## Required Reports

- `reports/showcase-validation.json`
- `reports/showcase-validation.md`
- `reports/raw-integrity.json`
- `reports/text-preservation.json`
- `reports/text-index-coverage.json`
- `reports/table-index-coverage.json`
- `reports/image-occurrence-coverage.json`
- `reports/referential-integrity.json`
- `reports/html-safety.json`
- `reports/table-validation.json`
- `reports/image-validation.json`
- `reports/chunk-validation.json`
- `reports/determinism-comparison.json`
- `reports/anomalies.jsonl`

## Hard Gates

- Raw JSON SHA must match the manifest and remain unchanged during build.
- Parsed section/table/image occurrence counts must match the raw source.
- Section-level visible text must match after deterministic sanitization.
- Unsafe tags, event handlers, JavaScript URLs, and frontend base64 are rejected.
- Every indexable text block must appear as primary content in a text chunk.
- Every non-empty table cell must appear in at least one table chunk.
- Every image occurrence must have a record and an image-context chunk.
- Every decoded local image must appear in frontend HTML and preview.
- Every chunk must have source SHA and citation metadata with document title.
- Every chunk must be at or below the maximum token budget.
- Duplicate IDs and unresolved section/table/image/chunk references are errors.

Warnings are retained for expected non-fatal conditions such as empty sections, unknown
tags that were unwrapped, external images not fetched, and deterministic oversized text
splits.
