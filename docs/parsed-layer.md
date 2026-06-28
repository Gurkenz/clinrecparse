# Parsed layer

Parsed data is a deterministic application layer above immutable research raw
JSON. It does not edit medical text, does not fix spelling, and does not use an
LLM during parsing.

Commands:

- `clinrec parsed-build --input data/research/corpora/... --output data/parsed/...`
- `clinrec parsed-validate --input data/parsed/...`
- `clinrec parsed-export --input data/parsed/... --output data/exports/...`
- `clinrec parsed-build-diff --input data/parsed/...`

Layer rules:

- Every document, section, table, image, and chunk stores source raw path and
  source raw SHA-256.
- Section order is preserved through `source_order`.
- Normalized HTML removes scripts, event handlers, and `javascript:` URLs.
- Base64 images are decoded into `assets/by-sha256/*` and normalized HTML uses
  asset references rather than base64 blobs.
- Plain text is extracted for search and RAG chunks.
- Markdown is not canonical; HTML and JSON records are canonical parsed output.

