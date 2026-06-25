# clinrec

`clinrec` is a reproducible pipeline for clinical recommendations from the Russian
Ministry of Health. The project is being built in stages. This first stage contains
only the project skeleton, configuration, structured logging, CLI placeholders, and
smoke tests.

The current stage performs catalog and reference synchronization. Document JSON/PDF
downloads are still planned for later stages.

## Install

```powershell
python -m pip install -e ".[dev]"
```

## CLI

```powershell
clinrec sync-catalog
clinrec sync-references
clinrec discover-versions
clinrec download
clinrec parse
clinrec build-families
clinrec qa
clinrec run-all
```

Command intent:

- `sync-catalog`: saves active and all-status catalog snapshots, builds
  `data/indexes/catalog.jsonl`, and writes a catalog QA report.
- `sync-references`: saves the raw `GetNkoList` response, builds
  `data/references/nko-organizations.jsonl`, and writes a references QA report.
- `discover-versions`: independently checks candidate document versions and writes
  `data/indexes/version-availability.jsonl`.
- `download`: downloads raw `GetClinrec2` JSON and official PDF files for
  selected `available_json` versions.
- `parse`: planned conversion from raw source data to normalized structures.
- `build-families`: planned construction of probable revision chains.
- `qa`: local smoke checks for configuration, paths, and logging.
- `run-all`: planned orchestration command for the full pipeline.

Targeted discovery examples:

```powershell
clinrec discover-versions --code 270 --force
clinrec discover-versions --from-code 1 --to-code 20 --force
clinrec discover-versions --code 270 --dry-run
clinrec download --code-version 843_1 --force
```

## Configuration

Defaults live in `config/default.yaml`. They define local data paths, HTTP timeout and
retry defaults, rate limiting, concurrency, and JSONL logging.

## Data Policy

The `data/` directory is local working storage and is excluded from Git. It may contain
raw API responses, PDFs, normalized documents, indexes, logs, and reports. Do not commit
downloaded medical texts or generated large artifacts unless a later publishing step
explicitly requires it.

## Development Checks

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```
