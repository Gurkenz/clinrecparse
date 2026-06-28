from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import structlog
import typer

from clinrec.api.catalog_sync import SyncError
from clinrec.api.catalog_sync import sync_catalog as run_catalog_sync
from clinrec.api.catalog_sync import sync_references as run_references_sync
from clinrec.api.client import ClinrecApiClient
from clinrec.api.document_download import DownloadError, DownloadOptions
from clinrec.api.document_download import download_documents as run_download_documents
from clinrec.api.document_download import download_pdfs as run_download_pdfs
from clinrec.api.version_discovery import DiscoveryError, DiscoveryOptions
from clinrec.api.version_discovery import discover_versions as run_discover_versions
from clinrec.bank.candidate import fetch_candidate_catalog as run_bank_fetch_candidate
from clinrec.bank.common import BankError, BankRecordFilter
from clinrec.bank.current import download_current_documents as run_bank_download_current
from clinrec.bank.decisions import build_decision_template, decisions_path_for_plan
from clinrec.bank.identities import analyze_identities as run_bank_analyze_identities
from clinrec.bank.previous import check_previous_documents as run_bank_check_previous
from clinrec.bank.qa import run_bank_qa
from clinrec.bank.reconcile import (
    apply_update_plan as run_bank_apply_update,
)
from clinrec.bank.reconcile import (
    bank_bootstrap as run_bank_bootstrap,
)
from clinrec.bank.reconcile import (
    bank_update as run_bank_update,
)
from clinrec.bank.reconcile import (
    build_update_plan as run_bank_build_update_plan,
)
from clinrec.bank.reconcile import (
    stage_update as run_bank_stage_update,
)
from clinrec.bank.references import (
    enrich_developers as run_bank_enrich_developers,
)
from clinrec.bank.references import (
    update_references as run_bank_update_references,
)
from clinrec.bank.run import run_bank_pipeline
from clinrec.bank.statuses import analyze_statuses as run_bank_analyze_statuses
from clinrec.bank.transaction import (
    list_transactions,
    read_journal,
    reconcile_started_operations,
    rollback_transaction,
)
from clinrec.config import DEFAULT_CONFIG_PATH, Settings, ensure_data_directories, load_settings
from clinrec.logging import configure_logging
from clinrec.parsing.document import ParseError, ParseOptions
from clinrec.parsing.document import parse_documents as run_parse_documents
from clinrec.qa.checks import QaOptions
from clinrec.qa.checks import run_qa as run_qa_checks
from clinrec.research.corpus import ResearchCorpusOptions
from clinrec.research.corpus import build_research_corpus as run_research_build_corpus
from clinrec.research.migration import migrate_layout as run_research_migrate_layout
from clinrec.research.schema import profile_corpus_offline as run_research_profile_corpus
from clinrec.research.validation import validate_corpus as run_research_validate_corpus

app = typer.Typer(help="Clinical recommendations pipeline CLI.")


ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        help="Path to YAML configuration file.",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
]


def bootstrap(config_path: Path) -> Settings:
    settings = load_settings(config_path)
    ensure_data_directories(settings)
    configure_logging(settings.logging)
    structlog.get_logger().info("command_bootstrap_complete", config=str(config_path))
    return settings


def placeholder(command_name: str, config_path: Path, *, http_planned: bool = False) -> None:
    bootstrap(config_path)
    typer.echo(f"{command_name}: command skeleton is ready.")
    if http_planned:
        typer.echo("HTTP download is not implemented for this command yet.")
    structlog.get_logger().info("placeholder_command_completed", command=command_name)


def bank_exit_code(exc: Exception) -> int:
    if not isinstance(exc, BankError):
        return 1
    message = str(exc).lower()
    if "writer lock" in message or "another writer" in message:
        return 6
    if "rollback_failed" in message or "rollback failure" in message:
        return 5
    if "resume" in message or "incomplete" in message:
        return 4
    if "manual review" in message or "review decisions" in message:
        return 3
    if any(token in message for token in ("qa", "hash", "validation", "staging", "manifest")):
        return 2
    return 1


@app.command("sync-catalog")
def sync_catalog(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize active and all-status catalog snapshots."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_catalog_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"sync-catalog failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("sync-catalog completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"snapshot: {summary.snapshot_root}")
    typer.echo(
        f"active: pages={summary.active.pages}, records={summary.active.records}, "
        f"total={summary.active.total_records}"
    )
    typer.echo(
        f"all-statuses: pages={summary.all_statuses.pages}, "
        f"records={summary.all_statuses.records}, total={summary.all_statuses.total_records}"
    )
    typer.echo(f"active_index: {summary.active_index_path}")
    typer.echo(f"all_statuses_index: {summary.all_statuses_index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")
    typer.echo(f"qa_issues: {len(summary.issues)}")


@app.command("bank-sync-catalog")
def bank_sync_catalog(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize catalog indexes required by the raw JSON bank."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_catalog_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"bank-sync-catalog failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-sync-catalog completed")
    typer.echo(f"active_records: {summary.active.records}")
    typer.echo(f"active_unique_code_versions: {summary.active.unique_code_versions}")
    typer.echo(f"all_statuses_records: {summary.all_statuses.records}")
    typer.echo(f"active_index: {summary.active_index_path}")
    typer.echo(f"all_statuses_index: {summary.all_statuses_index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")


@app.command("bank-bootstrap")
def bank_bootstrap(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Required: apply the bootstrap transaction."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload documents even when local manifests are valid."),
    ] = False,
    bootstrap_over_existing: Annotated[
        bool,
        typer.Option("--bootstrap-over-existing", help="Allow bootstrap over existing bank."),
    ] = False,
) -> None:
    """Create the initial active raw-bank from the current active catalog."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_bank_bootstrap(
                settings,
                client,
                force=force,
                apply=apply,
                bootstrap_over_existing=bootstrap_over_existing,
            )
    except (BankError, SyncError) as exc:
        typer.echo(f"bank-bootstrap failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-bootstrap completed")
    typer.echo(f"transaction_id: {summary['transaction_id']}")
    typer.echo(f"catalog_active_records: {summary['catalog_active_records']}")
    typer.echo(f"downloaded: {summary['downloaded']}")
    typer.echo(f"plan: {summary['plan']}")
    typer.echo(f"accepted_total: {summary['accepted']['total_records']}")
    typer.echo(f"references: {summary['references']}")


@app.command("bank-fetch-candidate")
def bank_fetch_candidate(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    transaction_id: Annotated[
        str | None,
        typer.Option("--transaction-id", help="Explicit transaction id."),
    ] = None,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Pilot/filter CodeVersion; can be repeated."),
    ] = None,
    pilot: Annotated[
        bool,
        typer.Option("--pilot", help="Mark this candidate snapshot as an isolated pilot."),
    ] = False,
) -> None:
    """Fetch an immutable candidate catalog snapshot."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_bank_fetch_candidate(
                settings,
                client,
                transaction_id=transaction_id,
                include_code_versions=set(code_version) if code_version else None,
                pilot=pilot,
            )
    except (BankError, SyncError) as exc:
        typer.echo(f"bank-fetch-candidate failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-fetch-candidate completed")
    typer.echo(f"transaction_id: {summary.transaction_id}")
    typer.echo(f"candidate: {summary.root}")
    typer.echo(f"active_records: {summary.active_total_records}")
    typer.echo(f"active_sha256: {summary.active_index_sha256}")


@app.command("bank-plan-update")
def bank_plan_update(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    candidate: Annotated[
        Path | None,
        typer.Option(
            "--candidate",
            help="Path to data/bank/candidates/{TRANSACTION_ID}.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ] = None,
    allow_large_delta: Annotated[
        bool,
        typer.Option(
            "--allow-large-delta",
            help="Allow catalog deltas above configured thresholds.",
        ),
    ] = False,
) -> None:
    """Synchronize a fresh active catalog and write an update plan without applying it."""
    settings = bootstrap(config)
    try:
        if candidate is not None:
            plan_summary = run_bank_build_update_plan(
                settings,
                transaction_id=candidate.name,
                candidate_records_path=candidate / "catalog-active.jsonl",
                candidate_snapshot_path=candidate,
                allow_large_delta=allow_large_delta,
            )
            summary = {
                "plan": str(plan_summary.plan_path),
                "requires_manual_review": plan_summary.requires_manual_review,
            }
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_update(
                    settings,
                    client,
                    apply=False,
                    allow_large_delta=allow_large_delta,
                )
    except (BankError, SyncError) as exc:
        typer.echo(f"bank-plan-update failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-plan-update completed")
    typer.echo(f"plan: {summary['plan']}")
    typer.echo(f"requires_manual_review: {summary['requires_manual_review']}")


@app.command("bank-apply-update")
def bank_apply_update(
    plan: Annotated[
        Path,
        typer.Option(
            "--plan",
            help="Path to data/bank/plans/{TIMESTAMP}/plan.json.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    allow_manual_review: Annotated[
        bool,
        typer.Option("--allow-manual-review", help="Apply a plan marked as manual review."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume an interrupted transaction."),
    ] = False,
    rollback_on_error: Annotated[
        bool,
        typer.Option("--rollback-on-error/--no-rollback-on-error"),
    ] = True,
    recover_stale_lock: Annotated[
        bool,
        typer.Option("--recover-stale-lock", help="Remove a stale writer lock first."),
    ] = False,
) -> None:
    """Apply a reviewed bank update plan."""
    settings = bootstrap(config)
    try:
        summary = run_bank_apply_update(
            settings,
            plan,
            allow_manual_review=allow_manual_review,
            resume=resume,
            rollback_on_error=rollback_on_error,
            recover_stale_lock=recover_stale_lock,
        )
    except BankError as exc:
        typer.echo(f"bank-apply-update failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-apply-update completed")
    typer.echo(f"transaction_id: {summary.transaction_id}")
    typer.echo(f"applied: {summary.applied}")
    typer.echo(f"moved_to_legacy: {summary.moved_to_legacy}")
    typer.echo(f"reactivated: {summary.reactivated}")


@app.command("bank-stage-update")
def bank_stage_update(
    plan: Annotated[
        Path,
        typer.Option(
            "--plan",
            help="Path to data/bank/plans/{TRANSACTION_ID}/plan.json.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    force: Annotated[bool, typer.Option("--force", help="Redownload staged documents.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry documents with previous failed attempts."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Write a dry-run staging summary only."),
    ] = False,
    verify_existing: Annotated[
        bool,
        typer.Option("--verify-existing", help="Also stage unchanged records for verification."),
    ] = False,
) -> None:
    """Download and strictly validate documents required by an immutable plan."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_bank_stage_update(
                settings,
                client,
                plan,
                force=force,
                retry_failed=retry_failed,
                dry_run=dry_run,
                verify_existing=verify_existing,
            )
    except BankError as exc:
        typer.echo(f"bank-stage-update failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-stage-update completed")
    typer.echo(f"transaction_id: {summary.transaction_id}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"already_valid: {summary.already_valid}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"not_attempted: {summary.not_attempted}")
    typer.echo(f"summary: {summary.summary_path}")


@app.command("bank-rollback")
def bank_rollback(
    transaction_id: Annotated[str, typer.Option("--transaction-id")],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Rollback an interrupted or failed raw-bank transaction."""
    settings = bootstrap(config)
    try:
        journal = rollback_transaction(settings, transaction_id)
    except BankError as exc:
        typer.echo(f"bank-rollback failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    typer.echo("bank-rollback completed")
    typer.echo(f"transaction_id: {journal['transaction_id']}")
    typer.echo(f"state: {journal['state']}")


@app.command("bank-transaction-status")
def bank_transaction_status(
    transaction_id: Annotated[str, typer.Option("--transaction-id")],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the complete journal as JSON."),
    ] = False,
) -> None:
    """Show a raw-bank transaction journal."""
    settings = bootstrap(config)
    try:
        journal = read_journal(settings, transaction_id)
    except BankError as exc:
        typer.echo(f"bank-transaction-status failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    if json_output:
        typer.echo(json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True))
        return
    typer.echo("bank-transaction-status completed")
    typer.echo(f"transaction_id: {journal['transaction_id']}")
    typer.echo(f"state: {journal['state']}")
    typer.echo(f"operations: {len(journal.get('operations') or [])}")
    typer.echo(f"errors: {len(journal.get('errors') or [])}")


@app.command("bank-transaction-list")
def bank_transaction_list(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """List raw-bank transactions."""
    settings = bootstrap(config)
    rows = list_transactions(settings)
    typer.echo("bank-transaction-list completed")
    for row in rows:
        typer.echo(
            f"{row['transaction_id']}: state={row['state']}, "
            f"plan={row['plan_id']}, updated_at={row['updated_at']}"
        )


@app.command("bank-transaction-recover")
def bank_transaction_recover(
    transaction_id: Annotated[str, typer.Option("--transaction-id")],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Reconcile started journal operations after a crash."""
    settings = bootstrap(config)
    try:
        reconcile_started_operations(settings, transaction_id)
        journal = read_journal(settings, transaction_id)
    except BankError as exc:
        typer.echo(f"bank-transaction-recover failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    typer.echo("bank-transaction-recover completed")
    typer.echo(f"transaction_id: {transaction_id}")
    typer.echo(f"state: {journal['state']}")


@app.command("bank-review-update")
def bank_review_update(
    plan: Annotated[
        Path,
        typer.Option(
            "--plan",
            help="Path to data/bank/plans/{TRANSACTION_ID}/plan.json.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    decision: Annotated[
        str,
        typer.Option("--decision", help="Decision to apply to every required review item."),
    ] = "use_staged_candidate",
    reason: Annotated[
        str,
        typer.Option("--reason", help="Human review reason."),
    ] = "manual review approved",
) -> None:
    """Create review decisions for plan conflicts."""
    _ = bootstrap(config)
    from clinrec.bank.reconcile import load_verified_plan

    plan_payload = load_verified_plan(plan)
    payload = build_decision_template(plan_payload, decision=decision, reason=reason)
    path = decisions_path_for_plan(plan)
    from clinrec.bank.common import atomic_write_json

    atomic_write_json(path, payload)
    typer.echo("bank-review-update completed")
    typer.echo(f"decisions: {path}")


@app.command("research-build-corpus")
def research_build_corpus(
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="Research corpus output directory, e.g. data/research/corpora/live-json-50.",
        ),
    ],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    current_count: Annotated[int, typer.Option("--current-count", min=1)] = 50,
    previous_target: Annotated[
        int | None,
        typer.Option("--previous-target", min=0, help="Preferred previous-version target."),
    ] = None,
    previous_minimum: Annotated[
        int | None,
        typer.Option("--previous-minimum", min=0, help="Preferred previous-version minimum."),
    ] = None,
    previous_attempt_limit: Annotated[
        int | None,
        typer.Option("--previous-attempt-limit", min=0, help="Preferred previous attempt limit."),
    ] = None,
    legacy_target: Annotated[
        int | None,
        typer.Option("--legacy-target", min=0, help="Deprecated alias for --previous-target."),
    ] = None,
    legacy_minimum: Annotated[
        int | None,
        typer.Option("--legacy-minimum", min=0, help="Deprecated alias for --previous-minimum."),
    ] = None,
    legacy_attempt_limit: Annotated[
        int | None,
        typer.Option(
            "--legacy-attempt-limit",
            min=0,
            help="Deprecated alias for --previous-attempt-limit.",
        ),
    ] = None,
    seed: Annotated[int, typer.Option("--seed")] = 20260627,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Force one active CodeVersion into selection."),
    ] = None,
    resume: Annotated[bool, typer.Option("--resume")] = False,
    retry_failed: Annotated[bool, typer.Option("--retry-failed")] = False,
    profile_only: Annotated[bool, typer.Option("--profile-only")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Build an isolated raw JSON research corpus without touching the production bank."""
    settings = bootstrap(config)
    effective_previous_target = previous_target if previous_target is not None else legacy_target
    effective_previous_minimum = (
        previous_minimum if previous_minimum is not None else legacy_minimum
    )
    effective_previous_attempt_limit = (
        previous_attempt_limit
        if previous_attempt_limit is not None
        else legacy_attempt_limit
    )
    options = ResearchCorpusOptions(
        output=output,
        current_count=current_count,
        legacy_target=effective_previous_target if effective_previous_target is not None else 10,
        legacy_minimum=effective_previous_minimum if effective_previous_minimum is not None else 5,
        legacy_attempt_limit=effective_previous_attempt_limit
        if effective_previous_attempt_limit is not None
        else 20,
        seed=seed,
        include=tuple(include or ()),
        resume=resume,
        retry_failed=retry_failed,
        profile_only=profile_only,
        dry_run=dry_run,
    )
    try:
        if dry_run or profile_only:
            summary = run_research_build_corpus(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_research_build_corpus(settings, client, options)
    except BankError as exc:
        typer.echo(f"research-build-corpus failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("research-build-corpus completed")
    typer.echo(f"output: {summary.output}")
    typer.echo(f"status: {summary.status}")
    typer.echo(f"valid_current_count: {summary.valid_current_count}")
    typer.echo(f"valid_legacy_count: {summary.valid_legacy_count}")
    typer.echo(f"legacy_attempts: {summary.legacy_attempts}")
    typer.echo(f"corpus: {summary.corpus_path}")
    typer.echo(f"summary: {summary.summary_path}")


@app.command("research-validate-corpus")
def research_validate_corpus(
    input: Annotated[
        Path,
        typer.Option(
            "--input",
            help="Research corpus directory, e.g. data/research/corpora/live-json-50.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
) -> None:
    """Validate a local research corpus without opening an HTTP client."""
    try:
        summary = run_research_validate_corpus(input)
    except BankError as exc:
        typer.echo(f"research-validate-corpus failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    typer.echo("research-validate-corpus completed")
    typer.echo(f"input: {summary.input}")
    typer.echo(f"valid: {summary.valid}")
    typer.echo(f"errors: {summary.errors}")
    typer.echo(f"warnings: {summary.warnings}")
    typer.echo(f"report: {summary.report_json}")
    typer.echo(f"markdown: {summary.report_markdown}")
    if not summary.valid:
        raise typer.Exit(2)


@app.command("research-migrate-layout")
def research_migrate_layout(
    input: Annotated[
        Path,
        typer.Option(
            "--input",
            help="Research corpus directory, e.g. data/research/corpora/live-json-50.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            writable=True,
        ),
    ],
) -> None:
    """Migrate a research corpus from legacy/ to previous/."""
    try:
        summary = run_research_migrate_layout(input)
    except BankError as exc:
        typer.echo(f"research-migrate-layout failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    typer.echo("research-migrate-layout completed")
    typer.echo(f"input: {summary.input}")
    typer.echo(f"migrated: {summary.migrated}")
    typer.echo(f"previous: {summary.previous_root}")
    typer.echo(f"attempts: {summary.attempts_path}")
    typer.echo(f"corpus: {summary.corpus_path}")


@app.command("research-profile-corpus")
def research_profile_corpus(
    input: Annotated[
        Path,
        typer.Option(
            "--input",
            help="Research corpus directory, e.g. data/research/corpora/live-json-50.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            writable=True,
        ),
    ],
    rebuild_reports: Annotated[
        bool,
        typer.Option("--rebuild-reports/--no-rebuild-reports"),
    ] = True,
) -> None:
    """Rebuild local research reports without opening an HTTP client."""
    try:
        summary = run_research_profile_corpus(input, rebuild_reports=rebuild_reports)
    except BankError as exc:
        typer.echo(f"research-profile-corpus failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc
    typer.echo("research-profile-corpus completed")
    typer.echo(f"input: {summary.input}")
    typer.echo(f"raw_files: {summary.raw_files}")
    typer.echo(f"raw_hashes_unchanged: {summary.raw_hashes_unchanged}")
    typer.echo(f"active_catalog_records: {summary.catalog.active_records}")
    typer.echo(f"all_statuses_records: {summary.catalog.all_statuses_records}")
    typer.echo(f"documents: {summary.documents}")
    typer.echo(f"sections: {summary.sections}")
    typer.echo(f"pairs: {summary.pairs}")
    typer.echo(f"findings: {summary.findings_path}")


@app.command("bank-update")
def bank_update(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    verify_existing: Annotated[
        bool,
        typer.Option(
            "--verify-existing",
            help="Re-fetch unchanged documents and detect raw changes.",
        ),
    ] = False,
    allow_large_delta: Annotated[
        bool,
        typer.Option(
            "--allow-large-delta",
            help="Allow catalog deltas above configured thresholds.",
        ),
    ] = False,
) -> None:
    """Plan or apply an incremental active raw-bank update."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_bank_update(
                settings,
                client,
                apply=False,
                verify_existing=verify_existing,
                allow_large_delta=allow_large_delta,
            )
    except (BankError, SyncError) as exc:
        typer.echo(f"bank-update failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-update completed")
    typer.echo(f"plan: {summary['plan']}")
    typer.echo(f"requires_manual_review: {summary['requires_manual_review']}")


@app.command("bank-update-references")
def bank_update_references(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Update the NKO organization reference snapshot outside the raw download path."""
    settings = bootstrap(config)
    with ClinrecApiClient(settings.http, settings.rate_limit) as client:
        summary = run_bank_update_references(settings, client)

    typer.echo("bank-update-references completed")
    typer.echo(f"report: {summary.report_path}")
    typer.echo(f"new: {summary.new}")
    typer.echo(f"updated: {summary.updated}")
    typer.echo(f"missing_from_latest_reference: {summary.missing_from_latest_reference}")
    if summary.warnings:
        typer.echo(f"warnings: {', '.join(summary.warnings)}")


@app.command("bank-enrich-developers")
def bank_enrich_developers(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Enrich one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Enrich one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Enrich Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Enrich Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Enrich every active and legacy document."),
    ] = False,
) -> None:
    """Create or refresh developers.json from catalog, raw associations, and NKO index."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records or not any((code_version, code, from_code, to_code)),
    )
    summary = run_bank_enrich_developers(settings, options)

    typer.echo("bank-enrich-developers completed")
    typer.echo(f"documents: {summary.documents}")
    typer.echo(f"updated: {summary.updated}")
    typer.echo(f"unresolved: {summary.unresolved}")
    typer.echo(f"reference_status: {summary.reference_status}")


@app.command("sync-references")
def sync_references(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize external reference dictionaries."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_references_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"sync-references failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("sync-references completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"snapshot: {summary.snapshot_dir}")
    typer.echo(f"raw: {summary.raw_path}")
    typer.echo(f"index: {summary.index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")
    typer.echo(f"organizations: {summary.organizations}")
    typer.echo(f"qa_issues: {len(summary.issues)}")


@app.command("discover-versions")
def discover_versions(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Check every catalog candidate version."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Recheck every selected version.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Recheck temporary failures."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build candidates without HTTP requests or index writes."),
    ] = False,
) -> None:
    """Discover available document versions independently."""
    settings = bootstrap(config)
    options = DiscoveryOptions(
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_discover_versions(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_discover_versions(settings, client, options)
    except DiscoveryError as exc:
        typer.echo(f"discover-versions failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("discover-versions completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"checked: {summary.checked}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"codes: {summary.codes}")
    typer.echo(f"dry_run: {summary.dry_run}")
    typer.echo(f"index: {summary.index_path}")
    typer.echo(f"report: {summary.report_path}")
    typer.echo(f"availability: {summary.availability_counts}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")


@app.command("download")
def download(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Download one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Download every available JSON version."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected documents without HTTP requests or writes."),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Include temporary discovery failures."),
    ] = False,
) -> None:
    """Download raw GetClinrec2 JSON files only."""
    settings = bootstrap(config)
    options = DownloadOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        dry_run=dry_run,
        retry_failed=retry_failed,
    )
    try:
        if dry_run:
            summary = run_download_documents(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_download_documents(settings, client, options)
    except DownloadError as exc:
        typer.echo(f"download failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("download completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"partial: {summary.partial}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    else:
        for document in summary.documents:
            typer.echo(
                f"{document.code_version}: status={document.status}, "
                f"manifest={document.manifest_path}"
            )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-download-current")
def bank_download_current(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option(
            "--code-version",
            help="Download one active bank CodeVersion; can be repeated.",
        ),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Download every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry temporary failed records."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected records without HTTP requests or writes."),
    ] = False,
    unsafe_direct_active_write: Annotated[
        bool,
        typer.Option(
            "--unsafe-direct-active-write",
            help="Write directly to active instead of maintenance staging.",
        ),
    ] = False,
) -> None:
    """Download active-bank raw GetClinrec2 JSON files only."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
        unsafe_direct_active_write=unsafe_direct_active_write,
    )
    try:
        if dry_run:
            summary = run_bank_download_current(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_download_current(settings, client, options)
    except BankError as exc:
        typer.echo(f"bank-download-current failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-download-current completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if summary.references_index_path is not None:
        typer.echo(f"references_index: {summary.references_index_path}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: status={document.status}, "
            f"manifest={document.manifest_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-check-previous")
def bank_check_previous(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one active bank CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Check every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Recheck selected relations.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry previous_temporary_failure relations."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected previous candidates without writes."),
    ] = False,
) -> None:
    """Check only Version - 1 for active bank records."""
    if all_records:
        typer.echo(
            "bank-check-previous --all is disabled for the transactional raw bank.",
            err=True,
        )
        raise typer.Exit(1)
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_bank_check_previous(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_check_previous(settings, client, options)
    except BankError as exc:
        typer.echo(f"bank-check-previous failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-check-previous completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"checked: {summary.checked}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: previous={document.previous_code_version}, "
            f"relation_status={document.relation_status}, relation={document.relation_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-qa")
def bank_qa(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    against: Annotated[
        str,
        typer.Option("--against", help="QA target: accepted or candidate."),
    ] = "accepted",
    plan: Annotated[
        Path | None,
        typer.Option(
            "--plan",
            help="Required for --against candidate.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    phase: Annotated[
        str | None,
        typer.Option("--phase", help="Candidate QA phase: staged or applied."),
    ] = None,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one active bank CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Check every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Accepted for filter parity.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Accepted for filter parity."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Accepted for filter parity."),
    ] = False,
) -> None:
    """Run global active-bank completeness checks."""
    _ = (force, retry_failed, dry_run)
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records or not any((code_version, code, from_code, to_code)),
    )
    try:
        summary = run_bank_qa(
            settings,
            options,
            against=against,
            phase=phase,
            plan_path=plan,
        )
    except BankError as exc:
        typer.echo(f"bank-qa failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-qa completed")
    typer.echo(f"expected: {summary.expected}")
    typer.echo(f"folders: {summary.folders}")
    typer.echo(f"valid_current_json: {summary.valid_current_json}")
    typer.echo(f"valid_manifests: {summary.valid_manifests}")
    typer.echo(f"fatal: {summary.fatal}")
    typer.echo(f"errors: {summary.errors}")
    typer.echo(f"completeness: {summary.completeness_path}")
    typer.echo(f"previous_relations: {summary.previous_relations_path}")
    typer.echo(f"anomalies: {summary.anomalies_path}")
    if summary.fatal or summary.errors:
        raise typer.Exit(1)


@app.command("bank-analyze-identities")
def bank_analyze_identities(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option(
            "--code-version",
            help="Analyze one active bank CodeVersion; can be repeated.",
        ),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Analyze one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Analyze Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Analyze Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Analyze every active catalog record."),
    ] = False,
) -> None:
    """Analyze catalog/source db_id identity consistency without changing bank keys."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records or not any((code_version, code, from_code, to_code)),
    )
    try:
        summary = run_bank_analyze_identities(settings, options)
    except BankError as exc:
        typer.echo(f"bank-analyze-identities failed: {exc}", err=True)
        raise typer.Exit(bank_exit_code(exc)) from exc

    typer.echo("bank-analyze-identities completed")
    typer.echo(f"unique_db_ids: {summary.unique_db_ids}")
    typer.echo(f"duplicate_db_ids: {summary.duplicate_db_ids}")
    typer.echo(f"duplicate_code_versions: {summary.duplicate_code_versions}")
    typer.echo(f"db_id_to_many_code_versions: {summary.db_id_to_many_code_versions}")
    typer.echo(f"code_version_to_many_db_ids: {summary.code_version_to_many_db_ids}")
    typer.echo(f"mismatches: {summary.mismatches}")
    typer.echo(f"report: {summary.report_path}")
    typer.echo(f"pairs: {summary.pairs_path}")


@app.command("bank-analyze-statuses")
def bank_analyze_statuses(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    candidate: Annotated[
        Path | None,
        typer.Option(
            "--candidate",
            help="Analyze statuses from a candidate plan.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
) -> None:
    """Count raw catalog/document status values without interpreting them."""
    settings = bootstrap(config)
    summary = run_bank_analyze_statuses(settings, candidate_plan=candidate)

    typer.echo("bank-analyze-statuses completed")
    typer.echo(f"active_catalog_records: {summary.active_catalog_records}")
    typer.echo(f"all_statuses_catalog_records: {summary.all_statuses_catalog_records}")
    typer.echo(f"documents: {summary.documents}")
    typer.echo(f"report: {summary.report_path}")
    typer.echo(f"csv: {summary.csv_path}")
    typer.echo(f"transitions: {summary.transitions_path}")


@app.command("bank-run")
def bank_run(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option(
            "--code-version",
            help="Run bank pipeline for one CodeVersion; can be repeated.",
        ),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Run one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Run Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Run Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Run every active catalog record."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload/recheck selected files."),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry previous temporary failures."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected records without HTTP requests or writes."),
    ] = False,
) -> None:
    """Run the new raw JSON bank pipeline only."""
    typer.echo("bank-run is disabled; use bank-fetch-candidate, bank-plan-update, "
               "bank-stage-update, and bank-apply-update.", err=True)
    raise typer.Exit(1)
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_bank_pipeline(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_pipeline(settings, client, options)
    except (BankError, RuntimeError, SyncError) as exc:
        typer.echo(f"bank-run failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-run completed")
    typer.echo(f"catalog_active_records: {summary.catalog_active_records}")
    typer.echo(f"download_planned: {summary.download.planned}")
    typer.echo(f"download_failed: {summary.download.failed}")
    typer.echo(f"previous_planned: {summary.previous.planned}")
    typer.echo(f"previous_failed: {summary.previous.failed}")
    if summary.qa is not None:
        typer.echo(f"qa_fatal: {summary.qa.fatal}")
        typer.echo(f"qa_errors: {summary.qa.errors}")
    if summary.download.failed or summary.previous.failed or (
        summary.qa is not None and (summary.qa.fatal or summary.qa.errors)
    ):
        raise typer.Exit(1)


@app.command("download-pdf")
def download_pdf(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Download one CodeVersion PDF; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Download every available PDF."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected PDFs without HTTP requests or writes."),
    ] = False,
) -> None:
    """Download official PDF files only."""
    settings = bootstrap(config)
    options = DownloadOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_download_pdfs(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_download_pdfs(settings, client, options)
    except DownloadError as exc:
        typer.echo(f"download-pdf failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("download-pdf completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    else:
        for document in summary.documents:
            typer.echo(
                f"{document.code_version}: pdf_status={document.pdf_status}, "
                f"manifest={document.manifest_path}"
            )
    if summary.failed:
        raise typer.Exit(1)


@app.command("parse")
def parse(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Parse one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Parse one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Parse Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Parse Codes less than or equal to this value."),
    ] = None,
) -> None:
    """Parse downloaded raw GetClinrec2 JSON into normalized document artifacts."""
    settings = bootstrap(config)
    options = ParseOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
    )
    try:
        summary = run_parse_documents(settings, options)
    except ParseError as exc:
        typer.echo(f"parse failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("parse completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"parsed: {summary.parsed}")
    typer.echo(f"failed: {summary.failed}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: status={document.status}, "
            f"sections={document.sections}, tables={document.tables}, "
            f"images={document.images}, recommendations={document.recommendations}, "
            f"qa_issues={document.issues}, document={document.document_json_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("build-families")
def build_families(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Prepare revision family analysis command."""
    placeholder("build-families", config)


@app.command("qa")
def qa(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    strict_pdf: Annotated[
        bool,
        typer.Option("--strict-pdf", help="Treat missing PDF control sources as errors."),
    ] = False,
) -> None:
    """Run local source, parsed artifact, manifest, and optional PDF QA checks."""
    settings = bootstrap(config)
    summary = run_qa_checks(
        settings,
        QaOptions(
            code_versions=code_version,
            code=code,
            from_code=from_code,
            to_code=to_code,
            strict_pdf=strict_pdf,
        ),
    )
    typer.echo("qa completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"fatal: {summary.fatal}")
    typer.echo(f"errors: {summary.errors}")
    typer.echo(f"warnings: {summary.warnings}")
    typer.echo(f"info: {summary.info}")
    typer.echo(f"report: {summary.report_path}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: fatal={document.fatal}, errors={document.errors}, "
            f"warnings={document.warnings}, info={document.info}"
        )
    if summary.fatal or summary.errors:
        raise typer.Exit(1)


@app.command("run-all")
def run_all(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Prepare full pipeline orchestration command."""
    bootstrap(config)
    typer.echo("run-all is disabled for the transactional raw-bank stage.", err=True)
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
