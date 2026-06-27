# clinrec

`clinrec` is a local pipeline for Russian Ministry of Health clinical recommendations.
The current priority is a stable bank of raw source JSON for active recommendations.
HTML parsing, Markdown, chunks, recommendation extraction, and family analysis are
paused for this stage. PDF is kept as a separate official control layer and must not
be downloaded together with the JSON bank.

## Current Status

- `sync-catalog` and `bank-sync-catalog` save raw catalog snapshots plus separate
  `catalog-active.jsonl` and `catalog-all-statuses.jsonl` indexes.
- `bank-fetch-candidate` creates an immutable candidate catalog under
  `data/bank/candidates/{TRANSACTION_ID}/` without modifying production indexes.
- `bank-plan-update --candidate ...` creates a plan bound to the candidate catalog,
  candidate manifest, and previous accepted generation hashes under
  `data/bank/plans/{TRANSACTION_ID}/`.
- `bank-stage-update --plan ...` downloads required raw JSON into
  `data/bank/staging/{TRANSACTION_ID}/` and writes transaction metadata under
  `data/bank/transactions/{TRANSACTION_ID}/`.
- `bank-review-update --plan ...` records explicit review decisions for identity,
  raw-source, and orphan conflicts before apply.
- `bank-qa --against candidate --phase staged|applied --plan ...` verifies the
  plan-bound candidate before and after apply.
- `bank-apply-update --plan ...` acquires a writer lock, applies a staged plan with
  a write-ahead journal, switches the accepted pointer atomically, and rolls back on
  failed post-apply QA.
- `bank-rollback`, `bank-transaction-status --json`, `bank-transaction-list`, and
  `bank-transaction-recover` inspect or recover transactions.
- `bank-bootstrap --apply` creates the initial bank through the same candidate,
  plan, stage, apply, QA, accept workflow.
- `bank-download-current` downloads selected raw JSON into maintenance staging by
  default; direct active writes require `--unsafe-direct-active-write`.
- `bank-update-references` updates the NKO reference history after raw-bank sync.
- `bank-enrich-developers` creates `developers.json` after references are available.
- `bank-analyze-statuses` counts raw status values without interpreting them.
- `bank-check-previous` checks only the nearest `Version - 1` candidate.
- `bank-qa --against accepted` verifies the accepted catalog against active.
- `bank-qa --against candidate --phase staged|applied --plan ...` verifies a
  plan-bound candidate state.
- `bank-analyze-identities` reports `catalog.source_record_id` / `GetClinrec2.db_id`
  consistency, duplicate ids, and db id to `CodeVersion` pairs.
- `bank-run` is disabled for this transactional stage.
- `discover-versions` independently checks candidate `CodeVersion` values.
- `download` downloads only raw `GetClinrec2` JSON.
- `download-pdf` downloads official PDF files separately.
- `parse` converts downloaded JSON into normalized document artifacts.
- `qa` performs source, parsed artifact, manifest, chunk, table, markdown, and optional PDF checks.
- `build-families` is still a placeholder.
- `run-all` is disabled for this transactional stage.

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
clinrec bank-fetch-candidate
clinrec bank-plan-update --candidate data/bank/candidates/{TRANSACTION_ID}
clinrec bank-stage-update --plan data/bank/plans/{TRANSACTION_ID}/plan.json
clinrec bank-qa --against candidate --phase staged --plan data/bank/plans/{TRANSACTION_ID}/plan.json
clinrec bank-review-update --plan data/bank/plans/{TRANSACTION_ID}/plan.json --decision use_staged_candidate --reason "reviewed source identity"
clinrec bank-apply-update --plan data/bank/plans/{TRANSACTION_ID}/plan.json
clinrec bank-qa --against accepted
clinrec bank-update-references
clinrec bank-enrich-developers --all
clinrec bank-analyze-identities
clinrec bank-analyze-statuses
```

For an initial bank, use the same lifecycle through bootstrap:

```powershell
clinrec bank-bootstrap --apply
clinrec bank-qa --against accepted
```

If apply is interrupted or fails after mutating active/legacy, inspect and recover with:

```powershell
clinrec bank-transaction-status --transaction-id {TRANSACTION_ID}
clinrec bank-transaction-status --transaction-id {TRANSACTION_ID} --json
clinrec bank-transaction-list
clinrec bank-transaction-recover --transaction-id {TRANSACTION_ID}
clinrec bank-apply-update --plan data/bank/plans/{TRANSACTION_ID}/plan.json --resume
clinrec bank-rollback --transaction-id {TRANSACTION_ID}
```

For a controlled pilot before a full active-bank run:

```powershell
clinrec bank-fetch-candidate --pilot --code-version 773_2 --code-version 843_1 --code-version 270_2 --code-version 270_3
clinrec bank-plan-update --candidate data/bank/candidates/{TRANSACTION_ID}
clinrec bank-stage-update --plan data/bank/plans/{TRANSACTION_ID}/plan.json
clinrec bank-qa --against candidate --phase staged --plan data/bank/plans/{TRANSACTION_ID}/plan.json
```

Pilot candidates are isolated review artifacts. Production apply rejects `mode=pilot`
candidates.

Do not run `clinrec parse`, old `clinrec qa`, `clinrec build-families`, or old
`clinrec run-all` as part of this raw-bank stage. Do not run `bank-check-previous --all`,
mass PDF, or a full active-corpus download until the controlled fixture and isolated
pilot flows have been reviewed.

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

The accepted raw JSON bank lives under `data/bank/active/{CODE_VERSION}/` and does
not create `parsed/`, `assets/`, `content.md`, `document.json`, or
`search_chunks.jsonl`. Documents removed from the latest accepted active catalog are
moved to `data/bank/legacy/{CODE_VERSION}/` with `lifecycle.json`; they are not
deleted.

Candidate catalogs, plans, staging, and journals are transaction scoped:

- `data/bank/candidates/{TRANSACTION_ID}/`
- `data/bank/plans/{TRANSACTION_ID}/`
- `data/bank/staging/{TRANSACTION_ID}/`
- `data/bank/transactions/{TRANSACTION_ID}/`

## Accepted Generation Model

The accepted catalog in `data/bank/state/` is the membership source of truth. Accepted
state is stored as immutable generations:

- `data/bank/state/generations/{GENERATION_ID}/catalog-active.jsonl`
- `data/bank/state/generations/{GENERATION_ID}/manifest.json`
- `data/bank/state/generations/{GENERATION_ID}/source.json`
- `data/bank/state/current.json`

`current.json` is switched with an atomic file replacement and records the generation
id, catalog path, catalog SHA-256, total records, acceptance time, and transaction id.
Legacy `accepted-catalog.json` and `accepted-catalog-records.jsonl` are migrated on
first access and kept only for audit compatibility.

## Candidate Trust Chain

Candidate manifests record the transaction id, mode, selected records, requested/found
CodeVersions, validation status, and SHA-256 hashes for active and all-status indexes.
Plan, stage, QA, apply, and resume verify those hashes before trusting the candidate.
Candidate fetch writes candidate-local indexes and reports, not production `data/indexes/`.

## Transaction Safety

Apply uses one writer lock at `data/bank/state/writer.lock`, an atomic write-ahead
journal at `data/bank/transactions/{TRANSACTION_ID}/journal.json`, and backups for
active, legacy, quarantine, and the accepted pointer. Mutating operations are recorded
before filesystem changes and marked completed only after verification. Rollback runs
in reverse from backups and restores the previous accepted pointer.

`data/bank/staging/{TRANSACTION_ID}/` contains staged document directories only.
`staging-summary.json`, QA reports, and journal metadata live under the transaction
directory. A completed transaction removes its staging directory.

## Review Decisions

Plans with identity conflicts, raw db_id changes, silent source changes, or orphaned
local records require `data/bank/plans/{TRANSACTION_ID}/decisions.json`. The review
file records CodeVersion, conflict type, decision, reason, and decision time. Apply
rejects missing decisions and any decision that asks to abort.

Raw `current/manifest.json` files use schema version `2.0` and must contain a valid
SHA-256, byte size, validation state, opaque raw status fields, and `db_id_state`.
Failed HTTP, timeout, invalid JSON, or validation attempts are written under
`current/attempts/{TIMESTAMP}.json`; a failed forced download must not overwrite the
last valid raw JSON or manifest.
Mass PDF download is intentionally outside the bank; bank manifests record
`pdf_status: not_requested`.

The bank folder key remains `CodeVersion`. `db_id` from raw `GetClinrec2` and
`source_record_id` from the catalog are stored in manifests as strong identity checks
only; they are not used as the only lifecycle key until full-corpus identity statistics
are reviewed.

Raw status fields such as `status`, `ApplyStatus`, and `ApplyStatusCalculated` are
preserved as opaque source values. They do not decide active/legacy placement,
predecessor confirmation, or automatic replacement links in the raw-bank lifecycle.

## Exit Codes

- `0`: success
- `1`: command/runtime failure
- `2`: validation failure
- `3`: manual review required
- `4`: transaction incomplete
- `5`: rollback failure
- `6`: writer lock conflict

## Development Checks

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
```
