# Backend export

`clinrec parsed-export` writes backend records to `backend/`:

- `documents.jsonl`
- `sections.jsonl`
- `tables.jsonl`
- `images.jsonl`
- `relations.jsonl`
- `dataset.json`

Properties:

- UTF-8 JSON/JSONL.
- Stable ordering.
- No binary or base64 blobs.
- Foreign keys are stable parsed IDs.
- Source raw path and raw SHA-256 remain available for audit.

