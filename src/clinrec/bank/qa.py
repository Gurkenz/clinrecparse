from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.bank.common import (
    BankRecordFilter,
    bank_active_root,
    bank_document_root,
    bank_reports_root,
    current_dir,
    filter_catalog_records,
    has_selection,
    load_catalog_records,
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


def run_bank_qa(settings: Any, options: BankRecordFilter | None = None) -> BankQaSummary:
    effective_options = options or BankRecordFilter(all_records=True)
    active_rows = load_catalog_records(settings, active=True)
    selected_rows = (
        filter_catalog_records(active_rows, effective_options)
        if has_selection(effective_options)
        else active_rows
    )
    expected = {string_value(row.get("code_version")) for row in selected_rows}
    expected.discard("")

    active_root = bank_active_root(settings)
    folders = {
        path.name
        for path in active_root.iterdir()
        if path.is_dir()
    } if active_root.exists() else set()
    if has_selection(effective_options):
        folders = folders & expected

    valid_current: set[str] = set()
    failed_current: list[str] = []
    valid_catalog_records: set[str] = set()
    valid_developers: set[str] = set()
    valid_manifests: set[str] = set()
    identity_conflicts: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    anomaly_rows: list[dict[str, Any]] = []
    bank_manifest_rows: list[dict[str, Any]] = []

    for code_version in sorted(expected):
        document_dir = bank_document_root(settings, code_version)
        current_path = current_dir(settings, code_version) / "getclinrec.json"
        manifest_path = current_dir(settings, code_version) / "manifest.json"
        current_manifest = read_json_file(manifest_path)
        if validate_current_json(current_path, current_manifest, code_version):
            valid_current.add(code_version)
        else:
            failed_current.append(code_version)
        if validate_catalog_record(
            current_dir(settings, code_version) / "catalog-record.json",
            code_version,
        ):
            valid_catalog_records.add(code_version)
        if validate_developers(current_dir(settings, code_version) / "developers.json"):
            valid_developers.add(code_version)
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

    missing_folders = sorted(expected - folders)
    unexpected_folders = sorted(folders - expected)
    missing_current_json = sorted(expected - valid_current)
    relation_counts = Counter(string_value(row.get("relation_status")) for row in relation_rows)
    completeness = {
        "catalog_total_records": len(active_rows),
        "expected_unique": len(expected),
        "folders": len(folders),
        "valid_current_json": len(valid_current),
        "valid_catalog_records": len(valid_catalog_records),
        "valid_developers": len(valid_developers),
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
        "missing_current_json": missing_current_json,
        "failed_current_json": sorted(failed_current),
    }
    fatal = sum(
        1
        for condition in (
            bool(missing_folders),
            bool(unexpected_folders),
            expected != valid_current,
            expected != valid_manifests,
        )
        if condition
    )
    errors = sum(
        1
        for condition in (
            expected != valid_catalog_records,
            expected != valid_developers,
            bool(identity_conflicts),
        )
        if condition
    )

    reports_root = bank_reports_root(settings)
    reports_root.mkdir(parents=True, exist_ok=True)
    completeness_path = reports_root / "completeness.json"
    report_markdown_path = reports_root / "completeness.md"
    previous_relations_path = reports_root / "previous-relations.jsonl"
    anomalies_path = reports_root / "anomalies.jsonl"
    bank_manifest_path = bank_reports_root(settings).parent / "bank-manifest.jsonl"

    write_json(completeness_path, completeness)
    report_markdown_path.write_text(render_completeness_markdown(completeness), encoding="utf-8")
    write_jsonl(previous_relations_path, relation_rows)
    write_jsonl(anomalies_path, anomaly_rows)
    write_jsonl(bank_manifest_path, bank_manifest_rows)

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
    return payload.get("code_version") == code_version


def validate_developers(path: Path) -> bool:
    payload = read_json_file(path)
    return all(
        key in payload
        for key in (
            "catalog_developers",
            "association_ids",
            "resolved_associations",
            "unresolved_association_ids",
        )
    )


def validate_bank_manifest(payload: dict[str, Any], code_version: str) -> bool:
    return (
        payload.get("code_version") == code_version
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


def sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def read_report_rows(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)
