# Raw downloader

`clinrec research-build-corpus` builds an isolated research raw corpus from
`GetJsonClinrecsFilterV2` catalog rows and `GetClinrec2` document payloads.

`--all-current` means every unique valid active `CodeVersion` from
`catalog-active.jsonl`. A valid value has positive integer code and positive
integer version, for example `843_1`. Malformed catalog rows are reported but
excluded from the download universe.

Important behavior:

- Raw `getclinrec.json` is written byte-for-byte and is never reserialized.
- Existing raw plus `manifest.json` is validated before any HTTP request.
- `--resume` keeps the same catalog SHA and output path; a changed catalog
  requires a new output path.
- `--retry-failed` retries only transient/content failures. `403` and `404` are
  permanent unless a future explicit override is added.
- `selection.json` stores `selection_mode`, `requested_current_count`,
  `initially_selected`, `final_selected`, replacements, and failed candidates.

Audit files:

- `reports/current-universe.json`
- `reports/current-universe.jsonl`
- `reports/catalog-malformed-current.csv`
- `reports/catalog-current-collisions.csv`
- `reports/current-coverage.json`
- `reports/selection-provenance.jsonl`
- `attempts/current-attempts.jsonl`

