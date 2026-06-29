# Parsed data contract

General parsed dataset schema version: `0.3-pilot`.

Canonical parser version: `parsed-canonical-0.4`.

Core files:

- `dataset.json`
- `documents.jsonl`
- `sections.jsonl`
- `blocks.jsonl`
- `tables.jsonl`
- `table-cells.jsonl`
- `table-placements.jsonl`
- `images.jsonl`
- `assets.jsonl`
- `recommendations.jsonl`
- `references.jsonl`
- `relations.jsonl`
- `search/chunks.jsonl`
- `rag/chunks.jsonl`
- `rag/citation-index.jsonl`
- `rag/embedding-input.jsonl`

Showcase-only files:

- `canonical/blocks.jsonl`
- `canonical/table-cells.jsonl`
- `canonical/table-placements.jsonl`
- `canonical/assets.jsonl`
- `canonical/recommendations.jsonl`
- `canonical/references.jsonl`
- `canonical/chunks.jsonl`
- `canonical/coverage-map.json`
- `canonical/tables/{table_safe_id}/table.html`
- `canonical/tables/{table_safe_id}/table.json`
- `canonical/tables/{table_safe_id}/table.csv`

Stable IDs:

- Document: `current:{CodeVersion}` or
  `previous:{CurrentCodeVersion}:{PreviousCodeVersion}`
- Section: `{document_id}:{safe_source_section_id}#{occurrence_index}`
- Table: `{section_id}:table#{index}`
- Image occurrence: `{section_id}:image#{index}`
- Image asset: `sha256:{asset_sha256}` when decoded
- Chunk: `{CodeVersion}:{section_key}:chunk#{index}`

Each record is standalone JSON. Nullable fields are represented as `null`.
Unknown source fields are not discarded from raw JSON; parsed records only expose
the fields needed by application layers and keep raw references for audit.

`source_order` preserves raw order but is not part of logical section identity.
Repeated source section IDs are represented with `occurrence_index` starting at
zero. Image occurrence IDs and asset IDs are separate because the same decoded
asset can appear more than once in a document.

Tables are represented as table records, physical cell records, and expanded
logical placement records.
Showcase ML exports keep normal text chunks, table chunks, and image-context
chunks separate so full tables are not duplicated into ordinary text chunks.

`parsed-build` and `parsed-build-showcase` both consume the same
`ParsedDocumentBundle`. Export layers must not re-parse raw JSON. Coverage files
are evidence outputs; validators recompute coverage from raw source and
canonical records.
