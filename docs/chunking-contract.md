# Chunking Contract

Chunking is source-unit based and must not shorten medical text.

## Limits

- Target: 700 estimated tokens.
- Maximum: 1100 estimated tokens.
- Estimator: deterministic, model-neutral, `ceil(characters / 4)`.

## Text Chunks

Text chunks are built from ordered indexable blocks within one section.

- Blocks with table IDs or image IDs are excluded from ordinary text chunks.
- Recommendation, comment, grade, and reference neighbor blocks are grouped when they fit.
- Oversized units are split by deterministic sentence boundaries.
- Oversized sentences are split at whitespace and emit `oversized_sentence_split`.
- Coverage is proven with `primary_block_ids` and `source_fragments`.
- Overlap metadata exists as `overlap_block_ids`; overlap never counts as primary coverage.

## Table Chunks

Tables are chunked by row groups.

- Header rows are repeated in row-group chunks.
- Rows are never cut in the normal case.
- Oversized rows are split by cells, then by text fragments if required.
- Chunks carry `row_start`, `row_end`, `header_row_indices`, and `cell_ids`.
- Every non-empty physical cell must appear in at least one table chunk.

## Image Chunks

Every image occurrence gets one source-context chunk.

Allowed context fields:

- Section title.
- Image alt.
- Image title.
- Caption.
- Preceding source text.
- Following source text.

Generated visual descriptions, medical interpretation, embeddings, and summaries are not
part of this contract.
