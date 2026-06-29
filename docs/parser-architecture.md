# Canonical Parsed Parser Architecture

The parsed pipeline has one canonical parser entrypoint:

`clinrec.parsed.pipeline.parse_document(source, config) -> ParsedDocumentBundle`

`parsed-build-showcase` resolves one raw source, calls this parser, writes canonical,
backend, frontend, ML, preview, validation, and ZIP outputs.

`parsed-build` selects corpus documents, calls the same parser, and adapts bundle rows
to the existing dataset-level JSONL contract. It does not re-parse HTML, tables,
images, recommendations, or chunks.

## Canonical Modules

- `parsed.models`: stable helper functions and canonical version constants.
- `parsed.pipeline`: raw validation, document/section/block/table/image/recommendation parsing,
  chunk construction, validation, and package writing.
- `parsed.showcase`: compatibility import surface for the showcase CLI.
- `parsed.layer`: corpus selection, dataset artifact writing, validation, export, and diff.

## Identity

- Document: `current:{CodeVersion}` or `previous:{CurrentCodeVersion}:{CodeVersion}`.
- Section: `{document_id}:{safe_source_section_id}#{occurrence_index}`.
- Block: `{section_id}:block#{block_index}`.
- Table: `{section_id}:table#{table_occurrence_index}`.
- Image: `{section_id}:image#{image_occurrence_index}`.
- Asset: `sha256:{decoded_asset_sha256}`.
- Text chunk: `{section_id}:chunk#{chunk_index}`.
- Table chunk: `{table_id}:rows#{row_start}-{row_end}` plus a suffix when needed.
- Image chunk: `{image_id}:context`.

## Failure Behavior

Validation is fail-closed for source count mismatches, visible text loss, missing primary
block coverage, missing table-cell coverage, missing image chunks/frontend/preview
coverage, unsafe HTML, unresolved foreign keys, chunks over the maximum token budget,
and missing citation document titles.
