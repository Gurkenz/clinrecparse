# clinrec

`clinrec` is a local pipeline for Russian Ministry of Health clinical recommendations.
The current priority is a stable bank of raw source JSON for active recommendations.
HTML parsing, Markdown, chunks, recommendation extraction, and family analysis are
paused for this stage. PDF is kept as a separate official control layer and must not
be downloaded together with the JSON bank.

## Current Status

- `sync-catalog` and `bank-sync-catalog` save raw catalog snapshots plus separate
  `catalog-active.jsonl` and `catalog-all-statuses.jsonl` indexes.
- `bank-download-current` downloads byte-for-byte raw `GetClinrec2` JSON into
  `data/bank/active/{CODE_VERSION}/current/`.
- `bank-check-previous` checks only the nearest `Version - 1` candidate.
- `bank-qa` verifies active-bank completeness and writes bank reports.
- `bank-run` orchestrates the new raw JSON bank pipeline only.
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
clinrec bank-sync-catalog
clinrec bank-download-current --code-version 843_1 --force
clinrec bank-download-current --code-version 270_2 --force
clinrec bank-download-current --code-version 270_3 --force
clinrec bank-check-previous --code-version 843_1 --force
clinrec bank-check-previous --code-version 270_3 --force
clinrec bank-qa --code 843
clinrec bank-qa --code 270
```

For an intentional full active-bank JSON run:

```powershell
clinrec bank-sync-catalog
clinrec bank-download-current --all
clinrec bank-check-previous --all
clinrec bank-qa
```

Do not run `clinrec parse`, old `clinrec qa`, `clinrec build-families`, or old
`clinrec run-all` as part of this raw-bank stage.

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

The active raw JSON bank lives under `data/bank/active/{CODE_VERSION}/` and does not
create `parsed/`, `assets/`, `content.md`, `document.json`, or `search_chunks.jsonl`.
Mass PDF download is intentionally outside the bank; bank manifests record
`pdf_status: not_requested`.

## Development Checks

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```
