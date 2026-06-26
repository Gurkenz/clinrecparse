from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.candidate import (
    fetch_candidate_catalog,
    load_candidate_records,
)
from clinrec.bank.common import (
    PLAN_SCHEMA_VERSION,
    BankError,
    BankRecordFilter,
    atomic_write_json,
    bank_active_root,
    bank_history_root,
    bank_legacy_root,
    bank_plans_root,
    bank_staging_root,
    bank_state_root,
    catalog_record_for_bank,
    compact_timestamp,
    current_validation_issues,
    db_id_state,
    load_catalog_records,
    normalize_title,
    read_json_file,
    read_jsonl,
    relative_to_data_root,
    sha256_file,
    sha256_json,
    source_record_id_from_catalog,
    stable_json_dumps,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.bank.current import download_current_documents
from clinrec.bank.references import enrich_developers, update_references
from clinrec.bank.transaction import (
    create_journal,
    ensure_area_backup,
    journal_path,
    promote_staged_to_active,
    read_journal,
    remove_legacy_for_reactivation,
    rollback_transaction,
    set_journal_state,
)
from clinrec.bank.transaction import (
    move_active_to_legacy as tx_move_active_to_legacy,
)
from clinrec.config import Settings

METADATA_FIELDS = ("name", "mkbs", "developers", "age_category", "publish_date")


@dataclass(frozen=True)
class BankPlanSummary:
    plan_path: Path
    markdown_path: Path
    total_actions: int
    requires_manual_review: bool


@dataclass(frozen=True)
class BankStageSummary:
    transaction_id: str
    plan_id: str
    summary_path: Path
    planned: int
    attempted: int
    downloaded: int
    already_valid: int
    failed: int
    not_attempted: int
    circuit_open: bool


@dataclass(frozen=True)
class BankApplySummary:
    applied: int
    moved_to_legacy: int
    reactivated: int
    plan_path: Path
    transaction_id: str


def accepted_catalog_records_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "accepted-catalog-records.jsonl"


def accepted_catalog_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "accepted-catalog.json"


def read_accepted_catalog_records(settings: Settings) -> list[dict[str, Any]]:
    return read_jsonl(accepted_catalog_records_path(settings))


def accepted_catalog_sha256(settings: Settings) -> str | None:
    accepted = read_json_file(accepted_catalog_path(settings))
    value = accepted.get("sha256")
    return string_value(value) if value else None


def accept_current_catalog(
    settings: Settings,
    *,
    timestamp: str | None = None,
    snapshot_path: Path | None = None,
    records: list[dict[str, Any]] | None = None,
    records_path: Path | None = None,
) -> dict[str, Any]:
    if records is None:
        if records_path is not None:
            records = load_candidate_records(records_path)
        else:
            records = [
                catalog_record_for_bank(row)
                for row in load_catalog_records(settings, active=True)
            ]
    code_versions = [string_value(row.get("code_version")) for row in records]
    if not records:
        raise BankError("Refusing to accept an empty active catalog.")
    if len(code_versions) != len(set(code_versions)):
        raise BankError("Refusing to accept active catalog with duplicate CodeVersion.")
    identity_conflicts = identity_conflicts_in_catalog(records)
    if identity_conflicts:
        raise BankError("Refusing to accept active catalog with identity conflicts.")

    current_timestamp = timestamp or compact_timestamp()
    accepted_records_path = accepted_catalog_records_path(settings)
    write_jsonl(accepted_records_path, records)
    sha256 = sha256_rows(records)
    accepted = {
        "timestamp": current_timestamp,
        "snapshot_path": relative_to_data_root(settings, snapshot_path)
        if snapshot_path is not None
        else None,
        "records_path": relative_to_data_root(settings, accepted_records_path),
        "total_records": len(records),
        "unique_code_versions": len(set(code_versions)),
        "sha256": sha256,
        "accepted_at": utc_now(),
    }
    write_json(accepted_catalog_path(settings), accepted)
    return accepted


def build_update_plan(
    settings: Settings,
    *,
    timestamp: str | None = None,
    allow_large_delta: bool = False,
    candidate_records_path: Path | None = None,
    candidate_snapshot_path: Path | None = None,
    transaction_id: str | None = None,
) -> BankPlanSummary:
    plan_transaction_id = transaction_id or timestamp or compact_timestamp()
    current_records_path = candidate_records_path or settings.paths.indexes / "catalog-active.jsonl"
    current_records = load_candidate_records(current_records_path)
    previous_records = read_accepted_catalog_records(settings)
    plan_dir = bank_plans_root(settings) / plan_transaction_id
    plan_path = plan_dir / "plan.json"
    markdown_path = plan_dir / "plan.md"
    if plan_path.exists():
        raise BankError(f"Plan already exists: {plan_path}")

    actions = reconcile_catalogs(settings, previous_records, current_records)
    warning_context = {
        **actions,
        "previous_total": len(previous_records),
        "candidate_total": len(current_records),
    }
    warnings = warnings_for_plan(settings, warning_context, allow_large_delta)
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": f"plan-{plan_transaction_id}",
        "transaction_id": plan_transaction_id,
        "created_at": utc_now(),
        "state": "created",
        "previous_accepted_catalog_sha256": accepted_catalog_sha256(settings),
        "candidate_catalog_sha256": sha256_file(current_records_path),
        "candidate_catalog_records_path": current_records_path.as_posix(),
        "candidate_snapshot_path": candidate_snapshot_path.as_posix()
        if candidate_snapshot_path is not None
        else current_records_path.parent.as_posix(),
        "previous_total": len(previous_records),
        "candidate_total": len(current_records),
        "current_total": len(current_records),
        "actions": actions,
        "warnings": warnings,
        "requires_manual_review": bool(warnings),
    }
    plan.update(actions)
    write_plan_with_hash(plan_path, plan)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_plan_markdown(plan), encoding="utf-8")
    return BankPlanSummary(
        plan_path=plan_path,
        markdown_path=markdown_path,
        total_actions=sum(len(actions[key]) for key in action_keys()),
        requires_manual_review=bool(warnings),
    )


def reconcile_catalogs(
    settings: Settings,
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_by_cv = by_code_version(previous_records)
    current_by_cv = by_code_version(current_records)
    local_active = local_code_versions(bank_active_root(settings))
    local_legacy = local_code_versions(bank_legacy_root(settings))
    unchanged: list[str] = []
    metadata_changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    reactivated: list[str] = []
    identity_conflicts = identity_conflicts_in_catalog(current_records)

    for code_version, current in sorted(current_by_cv.items()):
        previous = previous_by_cv.get(code_version)
        if previous is None:
            added.append(code_version)
            if code_version in local_legacy:
                reactivated.append(code_version)
            continue
        if same_identity(previous, current):
            if metadata_changed_between(previous, current):
                metadata_changed.append(code_version)
            else:
                unchanged.append(code_version)
        else:
            identity_conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_source_record_id_changed",
                    "code_version": code_version,
                    "previous_source_record_id": source_record_id_from_catalog(previous),
                    "current_source_record_id": source_record_id_from_catalog(current),
                }
            )

    for code_version in sorted(set(previous_by_cv) - set(current_by_cv)):
        removed.append(code_version)

    expected = set(current_by_cv)
    missing_locally = sorted(expected - local_active - set(reactivated))
    orphaned_local = sorted(local_active - expected)
    return {
        "unchanged": unchanged,
        "added": added,
        "missing_locally": missing_locally,
        "removed_from_catalog": removed,
        "reactivated": reactivated,
        "metadata_changed": metadata_changed,
        "identity_conflicts": identity_conflicts,
        "silent_source_candidates": [],
        "orphaned_local": orphaned_local,
        "unexpected_local": orphaned_local,
        "replacement_candidates": replacement_candidates(
            previous_by_cv,
            current_by_cv,
            removed,
            added,
        ),
    }


def stage_update(
    settings: Settings,
    client: ClinrecApiClient,
    plan_path: Path,
    *,
    force: bool = False,
    retry_failed: bool = False,
    allow_identity_conflict: bool = False,
    dry_run: bool = False,
    verify_existing: bool = False,
) -> BankStageSummary:
    plan = load_verified_plan(plan_path)
    if plan.get("identity_conflicts") and not allow_identity_conflict:
        raise BankError("Plan contains identity conflicts; explicit override is required.")
    if plan.get("state") not in {"created", "staged", "ready_to_apply"}:
        raise BankError(f"Plan state is not stageable: {plan.get('state')}")
    verify_candidate_hash(plan)

    transaction_id = string_value(plan["transaction_id"])
    required = required_staging_set(plan, verify_existing=verify_existing)
    candidate_rows = candidate_rows_for_plan(plan)
    selected_rows = [
        row for row in candidate_rows if string_value(row.get("code_version")) in required
    ]
    summary_path = bank_staging_root(settings) / transaction_id / "staging-summary.json"
    if dry_run:
        summary = stage_summary_payload(plan, required, [], not_attempted=sorted(required))
        write_json(summary_path, summary)
        return stage_summary_from_payload(summary_path, summary)

    download_summary = download_current_documents(
        settings,
        client,
        BankRecordFilter(
            code_versions=sorted(required),
            force=force,
            retry_failed=retry_failed,
            timestamp=transaction_id,
        ),
        destination_root=bank_staging_root(settings) / transaction_id,
        records_override=selected_rows,
    )
    attempted = {document.code_version for document in download_summary.documents}
    not_attempted = sorted(required - attempted)
    documents = [
        {
            "code_version": document.code_version,
            "status": document.status,
            "manifest": document.manifest_path.as_posix(),
            "error": document.error,
        }
        for document in download_summary.documents
    ]
    validatable = {
        document.code_version
        for document in download_summary.documents
        if document.status in {"downloaded", "already_valid"}
    }
    invalid = strict_staging_failures(settings, transaction_id, validatable)
    summary = stage_summary_payload(
        plan,
        required,
        documents,
        not_attempted=not_attempted,
        invalid=invalid,
    )
    write_json(summary_path, summary)
    if summary["failed"] == 0 and summary["not_attempted"] == 0:
        update_plan_state(plan_path, "staged")
    return stage_summary_from_payload(summary_path, summary)


def apply_update_plan(
    settings: Settings,
    plan_path: Path,
    *,
    allow_manual_review: bool = False,
    accept_catalog: bool = True,
    allow_identity_conflict: bool = False,
    resume: bool = False,
    rollback_on_error: bool = True,
) -> BankApplySummary:
    plan = load_verified_plan(plan_path)
    if plan.get("requires_manual_review") and not allow_manual_review:
        raise BankError("Plan requires manual review before apply.")
    if plan.get("identity_conflicts") and not allow_identity_conflict:
        raise BankError("Plan contains identity conflicts; explicit override is required.")
    if plan.get("state") not in {"staged", "ready_to_apply", "applying"}:
        raise BankError(f"Plan state is not applicable: {plan.get('state')}")
    verify_candidate_hash(plan)
    verify_previous_catalog_hash(settings, plan)

    transaction_id = string_value(plan["transaction_id"])
    if journal_path(settings, transaction_id).exists():
        journal = read_journal(settings, transaction_id)
        if journal.get("state") == "completed":
            raise BankError("Plan cannot be applied twice.")
        if not resume and journal.get("state") not in {"created", "applying", "failed"}:
            raise BankError("Existing transaction requires --resume or rollback.")

    required = required_staging_set(plan, verify_existing=False)
    invalid = strict_staging_failures(settings, transaction_id, required)
    if invalid:
        raise BankError(f"Required staging is incomplete or invalid: {invalid}")

    create_journal(
        settings,
        transaction_id=transaction_id,
        plan_id=string_value(plan["plan_id"]),
        candidate_catalog_sha256=string_value(plan["candidate_catalog_sha256"]),
        previous_catalog_sha256=plan.get("previous_accepted_catalog_sha256"),
    )
    set_journal_state(settings, transaction_id, "applying")
    update_plan_state(plan_path, "applying")

    moved_to_legacy = 0
    reactivated = 0
    promoted = 0
    try:
        for code_version in plan_actions(plan, "removed_from_catalog"):
            if tx_move_active_to_legacy(settings, transaction_id, code_version=code_version):
                write_lifecycle(settings, code_version, plan)
                moved_to_legacy += 1

        for code_version in plan_actions(plan, "reactivated"):
            remove_legacy_for_reactivation(settings, transaction_id, code_version=code_version)
            if promote_staged_to_active(
                settings,
                transaction_id,
                code_version=code_version,
                staging_document=bank_staging_root(settings) / transaction_id / code_version,
            ):
                reactivated += 1

        for code_version in sorted(
            set(plan_actions(plan, "added")) | set(plan_actions(plan, "missing_locally"))
        ):
            if code_version in set(plan_actions(plan, "reactivated")):
                continue
            if promote_staged_to_active(
                settings,
                transaction_id,
                code_version=code_version,
                staging_document=bank_staging_root(settings) / transaction_id / code_version,
            ):
                promoted += 1

        update_metadata_sidecars(settings, transaction_id, plan)

        from clinrec.bank.qa import run_bank_qa

        qa_summary = run_bank_qa(
            settings,
            BankRecordFilter(all_records=True),
            against="candidate",
            plan_path=plan_path,
        )
        if qa_summary.fatal or qa_summary.errors:
            raise BankError("Post-apply candidate QA failed.")
        if accept_catalog:
            candidate_path = Path(string_value(plan["candidate_catalog_records_path"]))
            accept_current_catalog(
                settings,
                timestamp=transaction_id,
                snapshot_path=Path(string_value(plan["candidate_snapshot_path"])),
                records_path=candidate_path,
            )
        cleanup_empty_staging(settings, transaction_id)
        set_journal_state(settings, transaction_id, "completed")
        update_plan_state(plan_path, "applied")
    except Exception:
        if rollback_on_error:
            rollback_transaction(settings, transaction_id)
            update_plan_state(plan_path, "rolled_back")
        else:
            set_journal_state(settings, transaction_id, "failed")
            update_plan_state(plan_path, "failed")
        raise

    return BankApplySummary(
        applied=promoted + moved_to_legacy + reactivated,
        moved_to_legacy=moved_to_legacy,
        reactivated=reactivated,
        plan_path=plan_path,
        transaction_id=transaction_id,
    )


def bank_bootstrap(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    force: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    if not apply:
        raise BankError("bank-bootstrap requires --apply for transactional bootstrap.")
    candidate = fetch_candidate_catalog(settings, client)
    plan_summary = build_update_plan(
        settings,
        transaction_id=candidate.transaction_id,
        candidate_records_path=candidate.active_records_path,
        candidate_snapshot_path=candidate.root,
    )
    stage_summary = stage_update(settings, client, plan_summary.plan_path, force=force)
    if stage_summary.failed or stage_summary.not_attempted:
        raise BankError("Bootstrap staging failed; active bank was not changed.")
    apply_summary = apply_update_plan(settings, plan_summary.plan_path, allow_manual_review=True)
    reference_summary = update_references(settings, client)
    enrich_summary = enrich_developers(settings, BankRecordFilter(all_records=True))
    from clinrec.bank.qa import run_bank_qa

    final_qa = run_bank_qa(settings, BankRecordFilter(all_records=True), against="accepted")
    return {
        "catalog_active_records": candidate.active_total_records,
        "transaction_id": candidate.transaction_id,
        "plan": str(plan_summary.plan_path),
        "staging_summary": str(stage_summary.summary_path),
        "downloaded": stage_summary.downloaded,
        "applied": apply_summary.applied,
        "qa_fatal": final_qa.fatal,
        "qa_errors": final_qa.errors,
        "accepted": read_json_file(accepted_catalog_path(settings)),
        "references": str(reference_summary.report_path),
        "reference_warnings": reference_summary.warnings,
        "developer_enrichment_updated": enrich_summary.updated,
    }


def bank_update(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    apply: bool = False,
    verify_existing: bool = False,
    allow_large_delta: bool = False,
) -> dict[str, Any]:
    if apply:
        raise BankError("bank-update --apply is disabled; use plan, stage, and apply commands.")
    candidate = fetch_candidate_catalog(settings, client)
    plan_summary = build_update_plan(
        settings,
        transaction_id=candidate.transaction_id,
        candidate_records_path=candidate.active_records_path,
        candidate_snapshot_path=candidate.root,
        allow_large_delta=allow_large_delta,
    )
    _ = verify_existing
    return {
        "transaction_id": candidate.transaction_id,
        "candidate": str(candidate.root),
        "plan": str(plan_summary.plan_path),
        "requires_manual_review": plan_summary.requires_manual_review,
    }


def load_verified_plan(path: Path) -> dict[str, Any]:
    plan = read_json_file(path)
    if not plan:
        raise BankError(f"Plan is missing or invalid: {path}")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise BankError("Plan schema_version is invalid.")
    expected_hash = plan.get("plan_sha256")
    candidate = dict(plan)
    candidate.pop("plan_sha256", None)
    if expected_hash != sha256_json(candidate):
        raise BankError("Plan hash mismatch; refusing to apply modified plan.")
    return plan


def write_plan_with_hash(path: Path, plan: dict[str, Any]) -> None:
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    payload["plan_sha256"] = sha256_json(payload)
    atomic_write_json(path, payload)


def update_plan_state(path: Path, state: str) -> None:
    plan = load_verified_plan(path)
    plan["state"] = state
    plan["updated_at"] = utc_now()
    write_plan_with_hash(path, plan)


def verify_candidate_hash(plan: dict[str, Any]) -> None:
    path = Path(string_value(plan.get("candidate_catalog_records_path")))
    if not path.exists():
        raise BankError(f"Candidate catalog is missing: {path}")
    if sha256_file(path) != plan.get("candidate_catalog_sha256"):
        raise BankError("Candidate catalog hash mismatch.")


def verify_previous_catalog_hash(settings: Settings, plan: dict[str, Any]) -> None:
    expected = plan.get("previous_accepted_catalog_sha256")
    current = accepted_catalog_sha256(settings)
    if expected != current:
        raise BankError("Previous accepted catalog hash mismatch.")


def candidate_rows_for_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return load_candidate_records(Path(string_value(plan["candidate_catalog_records_path"])))


def required_staging_set(plan: dict[str, Any], *, verify_existing: bool) -> set[str]:
    selected = (
        set(plan_actions(plan, "added"))
        | set(plan_actions(plan, "missing_locally"))
        | set(plan_actions(plan, "reactivated"))
        | set(plan_actions(plan, "silent_source_candidates"))
    )
    if verify_existing:
        selected |= set(plan_actions(plan, "unchanged"))
    return selected


def strict_staging_failures(
    settings: Settings,
    transaction_id: str,
    required: set[str],
) -> dict[str, list[str]]:
    failures: dict[str, list[str]] = {}
    staging_root = bank_staging_root(settings) / transaction_id
    for code_version in sorted(required):
        issues = current_validation_issues(staging_root / code_version, code_version)
        if issues:
            failures[code_version] = issues
    return failures


def stage_summary_payload(
    plan: dict[str, Any],
    required: set[str],
    documents: list[dict[str, Any]],
    *,
    not_attempted: list[str],
    invalid: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    invalid = invalid or {}
    failed_documents = [
        row for row in documents if row["status"] not in {"downloaded", "already_valid"}
    ]
    failed = len(failed_documents) + len(invalid)
    return {
        "transaction_id": plan["transaction_id"],
        "plan_id": plan["plan_id"],
        "planned": len(required),
        "attempted": len(documents),
        "downloaded": sum(1 for row in documents if row["status"] == "downloaded"),
        "already_valid": sum(1 for row in documents if row["status"] == "already_valid"),
        "failed": failed,
        "not_attempted": len(not_attempted),
        "circuit_open": any(row["status"] == "circuit_open" for row in documents),
        "identity_conflicts": plan_actions(plan, "identity_conflicts"),
        "documents": documents,
        "invalid": invalid,
        "blocking_conditions": blocking_conditions(failed, not_attempted, invalid),
    }


def stage_summary_from_payload(path: Path, payload: dict[str, Any]) -> BankStageSummary:
    return BankStageSummary(
        transaction_id=string_value(payload["transaction_id"]),
        plan_id=string_value(payload["plan_id"]),
        summary_path=path,
        planned=int(payload["planned"]),
        attempted=int(payload["attempted"]),
        downloaded=int(payload["downloaded"]),
        already_valid=int(payload["already_valid"]),
        failed=int(payload["failed"]),
        not_attempted=int(payload["not_attempted"]),
        circuit_open=bool(payload["circuit_open"]),
    )


def blocking_conditions(
    failed: int,
    not_attempted: list[str],
    invalid: dict[str, list[str]],
) -> list[str]:
    conditions: list[str] = []
    if failed:
        conditions.append("failed_documents")
    if not_attempted:
        conditions.append("not_attempted_documents")
    if invalid:
        conditions.append("invalid_staging_documents")
    return conditions


def update_metadata_sidecars(settings: Settings, transaction_id: str, plan: dict[str, Any]) -> None:
    records = {string_value(row.get("code_version")): row for row in candidate_rows_for_plan(plan)}
    for code_version in plan_actions(plan, "metadata_changed"):
        document_root = bank_active_root(settings) / code_version
        catalog_path = document_root / "current" / "catalog-record.json"
        ensure_area_backup(
            settings,
            transaction_id,
            area="active",
            code_version=code_version,
            source=document_root,
        )
        archive_catalog_sidecar(settings, code_version, catalog_path, transaction_id)
        write_json(catalog_path, records[code_version])


def archive_catalog_sidecar(
    settings: Settings,
    code_version: str,
    catalog_path: Path,
    transaction_id: str,
) -> None:
    if not catalog_path.exists():
        return
    archive = (
        bank_history_root(settings)
        / code_version
        / compact_timestamp()
        / "catalog-record.json"
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(catalog_path.read_bytes())
    write_json(
        archive.parent / "event.json",
        {
            "event_type": "metadata_changed",
            "recorded_at": utc_now(),
            "transaction_id": transaction_id,
        },
    )


def cleanup_empty_staging(settings: Settings, transaction_id: str) -> None:
    root = bank_staging_root(settings) / transaction_id
    if root.exists() and not any(root.iterdir()):
        root.rmdir()


def write_lifecycle(settings: Settings, code_version: str, plan: dict[str, Any]) -> None:
    target = bank_legacy_root(settings) / code_version
    lifecycle_path = target / "lifecycle.json"
    existing = read_json_file(lifecycle_path)
    events_value = existing.get("events")
    events: list[Any] = events_value if isinstance(events_value, list) else []
    events.append(
        {
            "event_type": "removed_from_catalog",
            "recorded_at": utc_now(),
            "transaction_id": plan["transaction_id"],
        }
    )
    write_json(
        lifecycle_path,
        {
            "code_version": code_version,
            "first_seen_active_at": existing.get("first_seen_active_at"),
            "last_seen_active_at": utc_now(),
            "removed_from_active_catalog_at": utc_now(),
            "removal_snapshot": plan.get("transaction_id"),
            "replacement_status": replacement_status_for(code_version, plan),
            "replacement_candidates": (plan.get("replacement_candidates") or {}).get(
                code_version,
                [],
            ),
            "events": events,
        },
    )


def identity_conflicts_in_catalog(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_id: dict[int, set[str]] = {}
    by_cv: dict[str, set[int]] = {}
    for record in records:
        code_version = string_value(record.get("code_version"))
        source_record_id = source_record_id_from_catalog(record)
        code = record.get("code")
        version = record.get("version")
        if source_record_id is not None:
            by_id.setdefault(source_record_id, set()).add(code_version)
            by_cv.setdefault(code_version, set()).add(source_record_id)
        if code is not None and version is not None and code_version != f"{code}_{version}":
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_mismatch",
                    "code_version": code_version,
                    "expected": f"{code}_{version}",
                }
            )
    for source_record_id, code_versions in sorted(by_id.items()):
        if len(code_versions) > 1:
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "source_record_id_multiple_code_versions",
                    "source_record_id": source_record_id,
                    "code_versions": sorted(code_versions),
                }
            )
    for code_version, source_record_ids in sorted(by_cv.items()):
        if len(source_record_ids) > 1:
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_multiple_source_record_ids",
                    "code_version": code_version,
                    "source_record_ids": sorted(source_record_ids),
                }
            )
    return conflicts


def replacement_candidates(
    previous_by_cv: dict[str, dict[str, Any]],
    current_by_cv: dict[str, dict[str, Any]],
    removed: list[str],
    added: list[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for removed_code_version in removed:
        removed_record = previous_by_cv.get(removed_code_version, {})
        candidates: list[dict[str, Any]] = []
        for added_code_version in added:
            added_record = current_by_cv.get(added_code_version, {})
            score = replacement_score(removed_record, added_record)
            if score > 0:
                candidates.append(
                    {
                        "code_version": added_code_version,
                        "score": score,
                        "status": "probable_replacement"
                        if score >= 4
                        else "manual_review_required",
                    }
                )
        result[removed_code_version] = sorted(
            candidates or [{"status": "no_replacement_candidate", "score": 0}],
            key=lambda item: item["score"],
            reverse=True,
        )
    return result


def replacement_score(left: dict[str, Any], right: dict[str, Any]) -> int:
    score = 0
    if left.get("code") == right.get("code"):
        score += 1
    if normalize_title(left.get("name")) == normalize_title(right.get("name")):
        score += 2
    for field in ("age_category", "publish_date"):
        if left.get(field) and left.get(field) == right.get(field):
            score += 1
    if stable_json_dumps(left.get("mkbs")) == stable_json_dumps(right.get("mkbs")):
        score += 1
    if stable_json_dumps(left.get("developers")) == stable_json_dumps(right.get("developers")):
        score += 1
    return score


def warnings_for_plan(
    settings: Settings,
    plan: dict[str, Any],
    allow_large_delta: bool,
) -> list[str]:
    warnings: list[str] = []
    previous_total = int(plan.get("previous_total") or 0)
    candidate_total = int(plan.get("candidate_total") or plan.get("current_total") or 0)
    removed = len(plan.get("removed_from_catalog") or [])
    identity_conflicts = len(plan.get("identity_conflicts") or [])
    if candidate_total == 0:
        warnings.append("catalog_change_requires_manual_review")
    if previous_total and candidate_total < previous_total and not allow_large_delta:
        drop_percent = ((previous_total - candidate_total) / previous_total) * 100
        if drop_percent > settings.bank.max_catalog_drop_percent:
            warnings.append("catalog_change_requires_manual_review")
    if previous_total and removed and not allow_large_delta:
        removed_percent = (removed / previous_total) * 100
        if removed_percent > settings.bank.max_catalog_drop_percent:
            warnings.append("catalog_change_requires_manual_review")
    if identity_conflicts > settings.bank.max_identity_conflicts:
        warnings.append("identity_conflict")
    if removed and settings.bank.require_manual_apply_on_removed:
        warnings.append("manual_review_required")
    return sorted(set(warnings))


def by_code_version(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        string_value(row.get("code_version")): row
        for row in records
        if row.get("code_version")
    }


def local_code_versions(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        source_record_id_from_catalog(left) == source_record_id_from_catalog(right)
        and string_value(left.get("code_version")) == string_value(right.get("code_version"))
    )


def metadata_changed_between(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in METADATA_FIELDS:
        left_value = (
            normalize_title(left.get(field))
            if field == "name"
            else stable_json_dumps(left.get(field))
        )
        right_value = (
            normalize_title(right.get(field))
            if field == "name"
            else stable_json_dumps(right.get(field))
        )
        if left_value != right_value:
            return True
    return False


def action_keys() -> tuple[str, ...]:
    return (
        "added",
        "missing_locally",
        "metadata_changed",
        "removed_from_catalog",
        "reactivated",
        "identity_conflicts",
        "silent_source_candidates",
        "orphaned_local",
    )


def plan_actions(plan: dict[str, Any], key: str) -> list[Any]:
    actions = plan.get("actions") if isinstance(plan.get("actions"), dict) else {}
    value = actions.get(key, plan.get(key)) if isinstance(actions, dict) else plan.get(key)
    return list(value) if isinstance(value, list) else []


def render_plan_markdown(plan: dict[str, Any]) -> str:
    lines = ["# Bank update plan", ""]
    for key in ("previous_total", "candidate_total", *action_keys()):
        value = plan_actions(plan, key) if key in action_keys() else plan.get(key)
        lines.append(f"- {key}: {len(value) if isinstance(value, list) else value}")
    if plan.get("warnings"):
        lines.append(f"- warnings: {', '.join(plan['warnings'])}")
    return "\n".join(lines) + "\n"


def replacement_status_for(code_version: str, plan: dict[str, Any]) -> str:
    candidates = (plan.get("replacement_candidates") or {}).get(code_version) or []
    if not candidates:
        return "unresolved"
    return string_value(candidates[0].get("status")) or "unresolved"


def sha256_rows(rows: list[dict[str, Any]]) -> str:
    content = "\n".join(stable_json_dumps(row) for row in rows)
    return sha256_json(content)


def manifest_identity_state(manifest: dict[str, Any]) -> str:
    catalog_id = manifest.get("catalog_source_record_id")
    document_id = manifest.get("document_db_id")
    return db_id_state(
        int(catalog_id) if isinstance(catalog_id, int) else None,
        int(document_id) if isinstance(document_id, int) else None,
    )
