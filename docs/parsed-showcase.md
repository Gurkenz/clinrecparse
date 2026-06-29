# Parsed Showcase

`clinrec parsed-build-showcase` builds a single-document showcase package from
an immutable `getclinrec.json` source. The pilot target is `843_1`.

Example:

```powershell
clinrec parsed-build-showcase --input-corpus data/research/corpora/live-json-250 --code-version 843_1 --output data/showcase/843_1 --overwrite
```

The command writes:

- `data/showcase/843_1`
- `data/showcase/clinrec-showcase-843_1.zip`

The build is deterministic within one command run. It creates build A and build
B, validates both, compares content, then moves build A into the final output.
The raw JSON is copied byte-for-byte into `source/getclinrec.json`; it is never
rewritten or normalized.

Included packages:

- `canonical/`: draft `0.4-pilot` records and extracted local assets.
- `backend/`: JSON/JSONL import package with stable foreign keys.
- `frontend/`: document payload with normalized HTML and local asset paths.
- `ml/`: text chunks, table chunks, image-context chunks, and
  `embedding-input.jsonl`.
- `preview/`: static local HTML preview.

The command does not download PDFs, fetch external images, compute embeddings,
create a vector index, update a production database, or generate summaries.

The command uses `clinrec.parsed.pipeline.parse_document()` and shares the same
canonical parser with `clinrec parsed-build`. Validation fails closed when raw
occurrence counts, visible text preservation, block coverage, logical table
placement coverage, image coverage, citation titles, or deterministic rebuild
checks fail.
