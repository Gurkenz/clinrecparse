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

`clinrec parsed-build-showcase` writes schema `0.2-pilot` backend records to
`data/showcase/{CodeVersion}/backend/`. The package mirrors canonical records:

- `documents.jsonl`
- `sections.jsonl`
- `blocks.jsonl`
- `tables.jsonl`
- `table-cells.jsonl`
- `images.jsonl`
- `assets.jsonl`
- `recommendations.jsonl`
- `references.jsonl`
- `chunks.jsonl`
- `citation-index.jsonl`
- `manifest.json`

Image occurrence IDs and asset IDs are separate. Backends should import both
relationships if they need to distinguish repeated placements of the same file.
The showcase contract is a pilot import contract and must not be treated as a
production-final schema.
