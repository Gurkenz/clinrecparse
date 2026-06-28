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

