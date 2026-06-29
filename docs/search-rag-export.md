# Search and RAG export

Search files:

- `search/chunks.jsonl`
- `search/documents.jsonl` may be derived from `backend/documents.jsonl`

RAG files:

- `rag/chunks.jsonl`
- `rag/citation-index.jsonl`
- `rag/embedding-input.jsonl`

Rules:

- Chunks preserve source text; no summaries or paraphrases are generated.
- Each RAG chunk includes citation metadata: `code_version`, `section_key`,
  `section_title`, `source_order`, and `source_raw_sha256`.
- Table chunks keep table IDs. Large-table row-grouping can be added without
  changing the raw layer.
- `embedding-input.jsonl` is model-neutral and does not compute embeddings.

Showcase ML package:

- `ml/documents.jsonl`
- `ml/sections.jsonl`
- `ml/chunks.jsonl`
- `ml/table-chunks.jsonl`
- `ml/image-chunks.jsonl`
- `ml/tables.jsonl`
- `ml/images.jsonl`
- `ml/assets.jsonl`
- `ml/citation-index.jsonl`
- `ml/embedding-input.jsonl`
- `ml/manifest.json`
- `ml/assets/by-sha256/*`

Showcase text chunks exclude full table bodies. Tables are indexed through
`table-chunks.jsonl`, and local image occurrences are indexed through
`image-chunks.jsonl` using source alt text, section context, and asset metadata
only. No image descriptions, summaries, embeddings, model selection, or vector
database writes are produced by the showcase command.
