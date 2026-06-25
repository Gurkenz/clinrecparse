# clinrec

`clinrec` is a local pipeline for Russian Ministry of Health clinical recommendations.
The current priority is a stable JSON corpus and parser. PDF is kept as a separate
official control layer and must not be downloaded together with JSON.

## Current Status

- `sync-catalog` and `sync-references` save raw snapshots and normalized indexes.
- `discover-versions` independently checks candidate `CodeVersion` values.
- `download` downloads only raw `GetClinrec2` JSON.
- `download-pdf` downloads official PDF files separately.
- `parse` converts downloaded JSON into normalized document artifacts.
- `qa` performs source, parsed artifact, manifest, chunk, table, markdown, and optional PDF checks.
- `build-families` is still a placeholder.
- `run-all` is not recommended until the individual stages are clean on a controlled sample.

Do not run a full corpus or mass PDF layer accidentally. Use `--all` only when that is
intentional.

## Install

```powershell
python -m pip install -e ".[dev]"
```

Pinned direct dependencies live in `pyproject.toml`. A full local lock is committed as
`requirements.lock`.

## Recommended Order

```powershell
clinrec sync-catalog
clinrec sync-references
clinrec discover-versions --code 270 --force
clinrec discover-versions --code 843 --force
clinrec download --code-version 843_1 --force
clinrec download --code-version 270_2 --force
clinrec download --code-version 270_3 --force
clinrec parse --code-version 843_1
clinrec parse --code-version 270_2
clinrec parse --code-version 270_3
clinrec qa --code 843
clinrec qa --code 270
```

For an intentional full JSON run:

```powershell
clinrec discover-versions --all
clinrec download --all
clinrec parse
clinrec qa
```

If `parse --all` is not available in a future intermediate state, parse by filters or
explicit `--code-version` values.

## PDF Layer

PDF is separate from JSON:

```powershell
clinrec download-pdf --code-version 843_1 --force
clinrec download-pdf --all
```

The mass PDF layer should be run later, after JSON download, parsing, and QA are stable.
Missing PDF is currently a warning/info condition unless QA is run with `--strict-pdf`.

## Data Policy

Raw data is preserved unchanged under `source/*.json` and `data/snapshots/**`.
Normalized indexes and `parsed/document.json` store publication dates as date-only
`YYYY-MM-DD`. Technical fields such as `fetched_at`, `checked_at`, manifests, and logs
may keep timestamps.

The `data/` directory is local working storage and excluded from Git. It may contain raw
API responses, PDFs, normalized documents, indexes, logs, and reports.

## Development Checks

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```
