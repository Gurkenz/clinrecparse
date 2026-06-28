# Parsed data contract

Schema version: `1.0`.

Core files:

- `dataset.json`
- `documents.jsonl`
- `sections.jsonl`
- `tables.jsonl`
- `images.jsonl`
- `relations.jsonl`
- `search/chunks.jsonl`
- `rag/chunks.jsonl`
- `rag/citation-index.jsonl`
- `rag/embedding-input.jsonl`

Stable IDs:

- Document: `current:{CodeVersion}` or
  `previous:{CurrentCodeVersion}:{PreviousCodeVersion}`
- Section: `{document_id}:{source_section_id}#{source_order}`
- Table: `{section_id}:table#{index}`
- Image asset: `image:{asset_sha256}` when decoded
- Chunk: `{CodeVersion}:{section_key}:chunk#{index}`

Each record is standalone JSON. Nullable fields are represented as `null`.
Unknown source fields are not discarded from raw JSON; parsed records only expose
the fields needed by application layers and keep raw references for audit.

