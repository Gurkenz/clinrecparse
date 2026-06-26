from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
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
    plan_path: Path | None = None,
) -> BankQaSummary:
    effective_options = options or BankRecordFilter(all_records=True)
    full_run = is_full_qa(effective_options)
    expected_source_rows, candidate_plan = expected_rows_for_mode(
        settings,
        against=against,
        plan_path=plan_path,
    )
    selected_rows = (
        filter_catalog_records(expected_source_rows, effective_options)
        if has_selection(effective_options) and not full_run
        else expected_source_rows
    )
    expected = {string_value(row.get("code_version")) for row in selected_rows}
    expected.discard("")
    candidate_transaction_id = (
        string_value(candidate_plan.get("transaction_id")) if candidate_plan else ""
    )
    candidate_staging_root = (
        bank_staging_root(settings) / candidate_transaction_id
        if candidate_transaction_id
        else bank_staging_root(settings)
    )

    active_root = bank_active_root(settings)
    active_folders = {
        path.name
        for path in active_root.iterdir()
        if path.is_dir()
    } if active_root.exists() else set()
    staging_folders = local_folders(candidate_staging_root)
    represented_folders = (
        active_folders | staging_folders if against == "candidate" else active_folders
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

    legacy_missing_lifecycle = [
        code_version
        for code_version in sorted(legacy_folders)
        if not (bank_legacy_root(settings) / code_version / "lifecycle.json").exists()
    ]
    missing_folders = sorted(expected - folders)
    unexpected_folders = sorted(active_folders - expected) if full_run else []
    missing_current_json = sorted(expected - valid_current)
    relation_counts = Counter(string_value(row.get("relation_status")) for row in relation_rows)
    reference_status = (
        "available"
        if (settings.paths.data_root / "bank" / "references" / "nko-current.jsonl").exists()
        else "missing_optional"
    )
    completeness = {
        "qa_against": against,
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
        "staging_entries": [path.as_posix() for path in staging_entries],
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
    bank_manifest_path = (
        reports_root.parent / "bank-manifest.jsonl"
        if full_run
        else report_dir / "bank-manifest.jsonl"
    )

    write_json(completeness_path, completeness)
    report_markdown_path.write_text(render_completeness_markdown(completeness), encoding="utf-8")
    write_jsonl(previous_relations_path, relation_rows)
    write_jsonl(anomalies_path, anomaly_rows)
    write_jsonl(bank_manifest_path, bank_manifest_rows)
    if full_run and against == "accepted":
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
        return read_jsonl(bank_root(settings) / "state" / "accepted-catalog-records.jsonl"), None
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
    candidate_staging_root: Path,
) -> Path:
    active_root = bank_document_root(settings, code_version)
    if against == "candidate" and not active_root.exists():
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


def validate_catalog_record(path: Path, code_version: str) -> bool:
    payload = read_json_file(path)
    source_record_id = first_present(payload, "source_record_id", "SourceRecordId")
    return (
        payload.get("code_version") == code_version
        and source_record_id is not None
    )


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
