from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.bank.accepted import load_accepted_generation
from clinrec.bank.common import (
    PLAN_SCHEMA_VERSION,
    BankError,
    BankRecordFilter,
    accepted_catalog_path,
    bank_active_root,
    bank_document_root,
    bank_legacy_root,
    bank_reports_root,
    bank_root,
    bank_staging_root,
    compact_timestamp,
    current_validation_issues,
    filter_catalog_records,
    first_present,
    has_selection,
    minimal_validate_raw_document,
    read_json_file,
    read_jsonl,
    string_value,
    write_jsonl,
)
from clinrec.bank.reconcile_helpers import plan_actions


@dataclass(frozen=True)
class BankQaSummary:
    expected: int
    folders: int
    valid_current_json: int
    valid_manifests: int
    fatal: int
    errors: int
    completeness_path: Path
    report_markdown_path: Path
    previous_relations_path: Path
    anomalies_path: Path
    bank_manifest_path: Path


def run_bank_qa(
    settings: Any,
    options: BankRecordFilter | None = None,
    *,
    against: str = "accepted",
    phase: str | None = None,
    plan_path: Path | None = None,
) -> BankQaSummary:
    effective_options = options or BankRecordFilter(all_records=True)
    full_run = is_full_qa(effective_options)
    expected_source_rows, candidate_plan = expected_rows_for_mode(
        settings,
        against=against,
        plan_path=plan_path,
    )
    candidate_phase = phase or ("staged" if against == "candidate" else "accepted")
    if against == "candidate" and candidate_phase not in {"staged", "applied"}:
        raise BankError("--phase must be staged or applied for candidate QA.")
    selected_rows = (
        filter_catalog_records(expected_source_rows, effective_options)
        if has_selection(effective_options) and not full_run
        else expected_source_rows
    )
    expected = {string_value(row.get("code_version")) for row in selected_rows}
    expected.discard("")
    expected_by_code_version = {
        string_value(row.get("code_version")): row
        for row in selected_rows
        if string_value(row.get("code_version"))
    }
    candidate_transaction_id = (
        string_value(candidate_plan.get("transaction_id")) if candidate_plan else ""
    )
    candidate_staging_root = (
        bank_staging_root(settings) / candidate_transaction_id
        if candidate_transaction_id
        else bank_staging_root(settings)
    )
    required_staging: set[str] = set()
    planned_removed: set[str] = set()
    if candidate_plan and candidate_phase == "staged":
        from clinrec.bank.reconcile import required_staging_set, verify_candidate_hash

        verify_candidate_hash(candidate_plan)
        required_staging = required_staging_set(candidate_plan, verify_existing=False)
        planned_removed = set(plan_actions(candidate_plan, "removed_from_catalog"))

    active_root = bank_active_root(settings)
    active_folders = {
        path.name
        for path in active_root.iterdir()
        if path.is_dir()
    } if active_root.exists() else set()
    staging_folders = local_folders(candidate_staging_root)
    represented_folders = (
        (active_folders - planned_removed) | staging_folders
        if against == "candidate" and candidate_phase == "staged"
        else active_folders
    )
    folders = represented_folders if full_run else represented_folders & expected
    legacy_folders = local_folders(bank_legacy_root(settings))
    active_legacy_duplicates = sorted(active_folders & legacy_folders)
    staging_root = bank_staging_root(settings)
    staging_entries = list(staging_root.iterdir()) if staging_root.exists() else []
    part_files = [
        path.as_posix()
        for path in sorted(bank_root(settings).rglob("*.part"))
        if path.is_file()
    ] if bank_root(settings).exists() else []
    accepted_exists = accepted_catalog_path(settings).exists()
    unexpected_represented_folders = (
        sorted(represented_folders - expected)
        if against == "candidate" and full_run
        else []
    )
    unexpected_staging_folders = (
        sorted(staging_folders - required_staging)
        if against == "candidate" and candidate_phase == "staged"
        else []
    )
    missing_required_staging_folders = (
        sorted(required_staging - staging_folders)
        if against == "candidate" and candidate_phase == "staged"
        else []
    )

    valid_current: set[str] = set()
    failed_current: list[str] = []
    valid_catalog_records: set[str] = set()
    valid_manifests: set[str] = set()
    identity_conflicts: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    anomaly_rows: list[dict[str, Any]] = []
    bank_manifest_rows: list[dict[str, Any]] = []

    for code_version in sorted(expected):
        document_dir = document_root_for_expected(
            settings,
            code_version,
            against=against,
            phase=candidate_phase,
            required_staging=required_staging,
            candidate_staging_root=candidate_staging_root,
        )
        manifest_path = document_dir / "current" / "manifest.json"
        current_manifest = read_json_file(manifest_path)
        strict_issues = current_validation_issues(document_dir, code_version)
        if not strict_issues:
            valid_current.add(code_version)
        else:
            failed_current.append(code_version)
        if validate_catalog_record(
            document_dir / "current" / "catalog-record.json",
            code_version,
            expected_row=expected_by_code_version.get(code_version),
        ):
            valid_catalog_records.add(code_version)
        bank_manifest = read_json_file(document_dir / "bank-manifest.json")
        if validate_bank_manifest(bank_manifest, code_version):
            valid_manifests.add(code_version)
            bank_manifest_rows.append(bank_manifest)
        identity_issue = validate_identity_manifest(code_version, current_manifest)
        if identity_issue is not None:
            identity_conflicts.append(identity_issue)
            anomaly_rows.append(identity_issue)

        relation = read_json_file(document_dir / "previous" / "relation.json")
        if relation:
            relation_rows.append(relation)
            relation_status = string_value(relation.get("relation_status"))
            warnings = (
                relation.get("warnings")
                if isinstance(relation.get("warnings"), list)
                else []
            )
            if relation_status in {
                "parallel_active_versions",
                "metadata_conflict",
                "previous_temporary_failure",
                "unknown_status_pair",
            } or warnings:
                anomaly_rows.append(
                    {
                        "code_version": code_version,
                        "relation_status": relation_status,
                        "warnings": warnings,
                    }
                )

    planned_reactivated = (
        set(plan_actions(candidate_plan, "reactivated"))
        if candidate_plan is not None
        else set()
    )
    legacy_missing_lifecycle = []
    for code_version in sorted(legacy_folders):
        if code_version in planned_reactivated and candidate_phase == "staged":
            continue
        if not (bank_legacy_root(settings) / code_version / "lifecycle.json").exists():
            legacy_missing_lifecycle.append(code_version)
    missing_folders = sorted(expected - folders)
    unexpected_folders = (
        sorted(active_folders - expected)
        if full_run and against == "accepted"
        else []
    )
    missing_current_json = sorted(expected - valid_current)
    relation_counts = Counter(string_value(row.get("relation_status")) for row in relation_rows)
    reference_status = (
        "available"
        if (settings.paths.data_root / "bank" / "references" / "nko-current.jsonl").exists()
        else "missing_optional"
    )
    completeness = {
        "qa_against": against,
        "qa_phase": candidate_phase,
        "catalog_total_records": len(expected_source_rows),
        "active_expected": len(expected),
        "active_present": len(folders),
        "legacy_count": len(legacy_folders),
        "added": [],
        "removed": [],
        "reactivated": [],
        "metadata_changed": [],
        "expected_unique": len(expected),
        "folders": len(folders),
        "valid_current_json": len(valid_current),
        "valid_catalog_records": len(valid_catalog_records),
        "valid_developers": None,
        "valid_manifests": len(valid_manifests),
        "checked_previous": len(relation_rows),
        "confirmed_predecessors": relation_counts.get("confirmed_predecessor", 0),
        "parallel_active_versions": relation_counts.get("parallel_active_versions", 0),
        "metadata_conflicts": relation_counts.get("metadata_conflict", 0),
        "previous_unavailable": relation_counts.get("previous_unavailable", 0),
        "temporary_failures": relation_counts.get("previous_temporary_failure", 0),
        "identity_conflicts": len(identity_conflicts),
        "catalog_db_id_mismatch": identity_conflicts,
        "missing_folders": missing_folders,
        "unexpected_folders": unexpected_folders,
        "unexpected_active_folders": unexpected_folders,
        "missing_current_json": missing_current_json,
        "failed_current_json": sorted(failed_current),
        "active_legacy_duplicates": active_legacy_duplicates,
        "staging_empty": not staging_entries if against == "accepted" else True,
        "transaction_staging_empty": not staging_folders,
        "staging_entries": [path.as_posix() for path in staging_entries],
        "unexpected_staging_folders": unexpected_staging_folders,
        "missing_required_staging_folders": missing_required_staging_folders,
        "unexpected_represented_folders": unexpected_represented_folders,
        "part_files": part_files,
        "accepted_catalog_exists": accepted_exists,
        "candidate_plan": plan_path.as_posix() if plan_path is not None else None,
        "legacy_missing_lifecycle": legacy_missing_lifecycle,
        "reference_status": reference_status,
        "full_run": full_run,
    }
    fatal = sum(
        1
        for condition in (
            bool(missing_folders),
            bool(unexpected_folders),
            expected != valid_current,
            expected != valid_manifests,
            bool(active_legacy_duplicates),
            bool(staging_entries) and against == "accepted",
            bool(unexpected_staging_folders),
            bool(missing_required_staging_folders),
            bool(unexpected_represented_folders),
            against == "candidate" and candidate_phase == "applied" and bool(staging_folders),
            bool(part_files),
            full_run and against == "accepted" and not accepted_exists,
        )
        if condition
    )
    errors = sum(
        1
        for condition in (
            expected != valid_catalog_records,
            bool(identity_conflicts),
            bool(legacy_missing_lifecycle),
        )
        if condition
    )

    reports_root = bank_reports_root(settings)
    report_dir = qa_report_dir(
        reports_root,
        effective_options,
        full_run,
        against=against,
        transaction_id=candidate_transaction_id,
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    completeness_path = report_dir / "completeness.json"
    report_markdown_path = report_dir / "completeness.md"
    previous_relations_path = report_dir / "previous-relations.jsonl"
    anomalies_path = report_dir / "anomalies.jsonl"
    write_global_bank_manifest = full_run and against == "accepted" and fatal == 0 and errors == 0
    bank_manifest_path = (
        reports_root.parent / "bank-manifest.jsonl"
        if write_global_bank_manifest
        else report_dir / "bank-manifest.jsonl"
    )

    write_json(completeness_path, completeness)
    report_markdown_path.write_text(render_completeness_markdown(completeness), encoding="utf-8")
    write_jsonl(previous_relations_path, relation_rows)
    write_jsonl(anomalies_path, anomaly_rows)
    write_jsonl(bank_manifest_path, bank_manifest_rows)
    if write_global_bank_manifest:
        write_json(reports_root / "latest-full.json", completeness)

    return BankQaSummary(
        expected=len(expected),
        folders=len(folders),
        valid_current_json=len(valid_current),
        valid_manifests=len(valid_manifests),
        fatal=fatal,
        errors=errors,
        completeness_path=completeness_path,
        report_markdown_path=report_markdown_path,
        previous_relations_path=previous_relations_path,
        anomalies_path=anomalies_path,
        bank_manifest_path=bank_manifest_path,
    )


def expected_rows_for_mode(
    settings: Any,
    *,
    against: str,
    plan_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if against == "accepted":
        try:
            generation = load_accepted_generation(settings)
        except BankError:
            if accepted_catalog_path(settings).exists():
                raise
            return [], None
        return read_jsonl(generation.catalog_path), None
    if against != "candidate":
        raise BankError(f"Unsupported bank QA mode: {against}")
    if plan_path is None:
        raise BankError("--plan is required for candidate QA.")
    from clinrec.bank.reconcile import candidate_rows_for_plan, load_verified_plan

    plan = load_verified_plan(plan_path)
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise BankError("Candidate QA plan schema is invalid.")
    return candidate_rows_for_plan(plan), plan


def document_root_for_expected(
    settings: Any,
    code_version: str,
    *,
    against: str,
    phase: str,
    required_staging: set[str],
    candidate_staging_root: Path,
) -> Path:
    active_root = bank_document_root(settings, code_version)
    if against == "candidate" and phase == "staged" and code_version in required_staging:
        return candidate_staging_root / code_version
    return active_root


def validate_current_json(path: Path, manifest: dict[str, Any], code_version: str) -> bool:
    if not path.exists() or not manifest:
        return False
    raw_content = path.read_bytes()
    if manifest.get("sha256") and manifest.get("sha256") != sha256(raw_content):
        return False
    if manifest.get("size") and manifest.get("size") != len(raw_content):
        return False
    info, errors = minimal_validate_raw_document(raw_content, expected_code_version=code_version)
    return info is not None and not errors


def validate_catalog_record(
    path: Path,
    code_version: str,
    *,
    expected_row: dict[str, Any] | None = None,
) -> bool:
    payload = read_json_file(path)
    source_record_id = first_present(payload, "source_record_id", "SourceRecordId")
    if payload.get("code_version") != code_version or source_record_id is None:
        return False
    if expected_row is None:
        return True
    for field in ("source_record_id", "code_version", "code", "version"):
        if string_value(payload.get(field)) != string_value(expected_row.get(field)):
            return False
    if normalize_name(payload.get("name")) != normalize_name(expected_row.get("name")):
        return False
    return True


def normalize_name(value: Any) -> str:
    return " ".join(string_value(value).split()).casefold()


def validate_bank_manifest(payload: dict[str, Any], code_version: str) -> bool:
    return (
        payload.get("code_version") == code_version
        and payload.get("current_status") == "valid"
        and payload.get("pdf_status") == "not_requested"
    )


def validate_identity_manifest(
    code_version: str,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    catalog_source_record_id = manifest.get("catalog_source_record_id")
    document_db_id = manifest.get("document_db_id")
    if (
        isinstance(catalog_source_record_id, int)
        and isinstance(document_db_id, int)
        and catalog_source_record_id == document_db_id
    ):
        return None
    if catalog_source_record_id is None or document_db_id is None:
        return None
    return {
        "code": "catalog_db_id_mismatch",
        "kind": "identity_conflict",
        "code_version": code_version,
        "catalog_source_record_id": catalog_source_record_id,
        "document_db_id": document_db_id,
        "db_id_match": manifest.get("db_id_match"),
    }


def render_completeness_markdown(completeness: dict[str, Any]) -> str:
    lines = [
        "# Bank completeness",
        "",
        f"- catalog_total_records: {completeness['catalog_total_records']}",
        f"- active_expected: {completeness['active_expected']}",
        f"- active_present: {completeness['active_present']}",
        f"- legacy_count: {completeness['legacy_count']}",
        f"- expected_unique: {completeness['expected_unique']}",
        f"- folders: {completeness['folders']}",
        f"- valid_current_json: {completeness['valid_current_json']}",
        f"- valid_manifests: {completeness['valid_manifests']}",
        f"- confirmed_predecessors: {completeness['confirmed_predecessors']}",
        f"- parallel_active_versions: {completeness['parallel_active_versions']}",
        f"- metadata_conflicts: {completeness['metadata_conflicts']}",
        f"- previous_unavailable: {completeness['previous_unavailable']}",
        f"- temporary_failures: {completeness['temporary_failures']}",
        f"- identity_conflicts: {completeness['identity_conflicts']}",
    ]
    return "\n".join(lines) + "\n"


def is_full_qa(options: BankRecordFilter) -> bool:
    return options.all_records and not any(
        (options.code_versions, options.code, options.from_code, options.to_code)
    )


def local_folders(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def qa_report_dir(
    reports_root: Path,
    options: BankRecordFilter,
    full_run: bool,
    *,
    against: str,
    transaction_id: str,
) -> Path:
    timestamp = compact_timestamp(options.timestamp)
    if against == "candidate":
        transaction_scope = transaction_id or "unknown-transaction"
        return reports_root / "candidate" / transaction_scope / timestamp
    if full_run:
        return reports_root / "full" / timestamp
    return reports_root / "scoped" / qa_scope(options) / timestamp


def qa_scope(options: BankRecordFilter) -> str:
    if options.code_versions:
        return "code-version-" + "-".join(sorted(options.code_versions))
    if options.code is not None:
        return f"code-{options.code}"
    if options.from_code is not None or options.to_code is not None:
        return f"code-range-{options.from_code or 'start'}-{options.to_code or 'end'}"
    return "selection"


def sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def read_report_rows(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)
