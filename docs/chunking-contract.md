# Chunking Contract

Chunking is source-unit based and must not shorten medical text.

## Limits

- Target: 700 estimated tokens.
- Maximum: 1100 estimated tokens.
- Estimator: deterministic, model-neutral, `ceil(characters / 4)`.
- Chunk records expose `estimated_token_count` with `estimator_name=chars_div_4`;
  `token_estimate` remains only as a compatibility alias.

## Text Chunks

Text chunks are built from ordered indexable blocks within one section.

- Mixed wrappers are flattened before chunking, so ordinary text before or after
  tables/images remains indexable.
- Recommendation, comment, grade, and reference neighbor blocks are grouped when they fit.
- Oversized units are split by deterministic sentence boundaries.
- Oversized sentences are split at whitespace and emit `oversized_sentence_split`.
- Coverage is proven with `primary_block_ids` and `source_fragments`.
- Overlap is disabled in this iteration. `overlap_block_ids` remains an empty
  compatibility field.

## Table Chunks

Tables are chunked by row groups.

- Header rows are repeated in row-group chunks.
- Rows are never cut in the normal case.
- Oversized rows are split by cells, then by text fragments if required.
- Chunks carry `row_start`, `row_end`, `header_row_indices`, `cell_ids`, and
  `placement_ids`.
- Every non-empty logical placement must appear in at least one table chunk.

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
