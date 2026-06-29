# Parser consolidation inventory

Task id: `clinrec-parser-consolidation-and-correctness-v1`

## Baseline

- Branch: `codex/clinrec-raw-bank-transactional-lifecycle`
- Base commit: `a19174efc59d780d4fd416b0beff6e76140873e2`
- Initial git status: clean
- `python -m pytest`: system Python missing `pytest`
- `python -m ruff check .`: system Python missing `ruff`
- `python -m mypy src`: system Python missing `mypy`
- `.venv\Scripts\python.exe -m pytest`: 112 passed
- `.venv\Scripts\python.exe -m ruff check .`: passed
- `.venv\Scripts\python.exe -m mypy src`: passed
- 843_1 raw SHA-256: `05783d290cae2e7cf5120220291d41fc0bf44e6e0462e9b8c872d6d684291956`
- 843_1 manifest: valid SHA and size

## Implementation Matrix

| Operation | Legacy parser | Parsed layer | Showcase | Selected canonical implementation |
| --- | --- | --- | --- | --- |
| Raw source resolution | data/documents layout | corpus selection | strongest single-document manifest check | `parsed.pipeline.resolve_showcase_input` |
| Document metadata | extended metadata | minimal dataset metadata | catalog-aware metadata | `parsed.pipeline.refresh_document_record` |
| Section traversal | recursive old layout | flat corpus traversal | duplicate-ID stable keys | `parsed.pipeline.parse_showcase_document` |
| HTML sanitization | broad old sanitizer | prototype sanitizer | allowlist sanitizer | `parsed.pipeline.sanitize_html_tree` |
| Block extraction | strongest block model | none | top-level logical blocks | `parsed.pipeline.process_section_blocks` |
| Tables | strongest old grid ideas | prototype table rows | occurrence/cell sidecars | `parsed.pipeline.process_section_tables` |
| Images | strongest signature checks | prototype assets | occurrence/asset split | `parsed.pipeline.process_section_images` |
| Recommendations | strongest UUR/UDD patterns | none | recommendation rows | `parsed.pipeline.extract_recommendations` with full-form grades |
| Chunks | truncating old chunks | section/table chunks | truncating section chunks | `parsed.pipeline.build_chunks` lossless source-unit chunks |
| Validation | QA issue model | dataset validation | weak showcase validation | `parsed.pipeline.validate_showcase_directory` coverage gates |
| Export | legacy per-document files | backend/frontend/RAG exports | full showcase packages | `parsed.pipeline.write_showcase_packages`; layer adapts bundle rows |

## Migrated Or Replaced

- `src/clinrec/parsed/showcase.py` is now a thin compatibility import surface.
- The previous showcase implementation was promoted to `src/clinrec/parsed/pipeline.py`.
- `src/clinrec/parsed/layer.py` now calls `parse_document()` and no longer has its own HTML/table/image/chunk parser.
- Shared stable helpers moved to `src/clinrec/parsed/models.py`.
- The dataset-layer dead parser block was deleted.

## Known 843_1 Baseline Defects

Measured against the previous showcase behavior in a temporary build:

- Text chunks: 27 for 831 blocks.
- Indexable block substring coverage: 446/817 = 54.59%.
- Chunks sliced to exactly 4000 chars: 9.
- Citation titles missing: 71/71 chunks.
- Full-form UUR/UDD detected: 0/0 recommendations with grades.

## Corrected 843_1 Result

Measured after consolidation in a temporary build:

- Sections: 31.
- Blocks: 832.
- Tables: 14.
- Table cells: 348 physical cells, 314 non-empty indexed cells.
- Images: 30 occurrences, 30 assets.
- Recommendations: 112.
- Recommendations with UUR: 96.
- Recommendations with UDD: 96.
- Text chunks: 78.
- Table chunks: 16.
- Image chunks: 30.
- Visible text coverage: 100%.
- Text index coverage: 817/817 = 100%.
- Table index coverage: 314/314 = 100%.
- Image occurrence coverage: 30/30 = 100%.
- Silent truncations: 0.
- Chunks over maximum: 0.
- Citation titles missing: 0.
- Hard validation errors: 0.

## Compatibility Impact

- `clinrec parsed-build-showcase` uses the canonical parser and canonical exporters.
- `clinrec parsed-build` uses the same `parse_document()` bundle and adapts rows to the existing dataset export contract.
- `clinrec.parsing.document` remains for the legacy `parse` command and old per-document output contract. It is not used by `parsed-build` or showcase.

## Remaining Risk

- The old `parse` command still carries the historic per-document parser because existing QA and legacy tests assert that contract. It should be deprecated or wrapped in a later compatibility-only pass.
