# Previous vs corrected showcase

| Metric | Previous showcase | Corrected showcase |
| --- | ---: | ---: |
| Text chunks | 27 | 78 |
| Indexable block coverage | 54.59% | 100.0% |
| Table coverage | not proven | 100.0% |
| Image occurrence coverage | not proven | 100.0% |
| Chunks silently sliced to 4000 chars | 9 | 0 |
| Citation titles missing | 71 | 0 |
| Recommendations with UUR | 0 | 96 |
| Recommendations with UDD | 0 | 96 |
| Frontend rendered decoded images | 29/30 baseline audit | 30/30 |
| Hard validation errors | not enforced | 0 |

Removed duplicated parser code:

- `src/clinrec/parsed/showcase.py` is now a thin wrapper.
- `src/clinrec/parsed/layer.py` calls canonical `parse_document()` and its old parser block was deleted.
- Canonical chunking, recommendation extraction, validation, and exports live in `src/clinrec/parsed/pipeline.py`.

Remaining warnings are the validation warnings listed in `reports/parser-consolidation-summary.json`.
