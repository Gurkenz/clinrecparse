from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import to_int, write_json
from clinrec.bank.common import (
    BankRecordFilter,
    bank_reports_root,
    current_dir,
    filter_catalog_records,
    has_selection,
    load_catalog_records,
    minimal_validate_raw_document,
    read_json_file,
    source_record_id_from_catalog,
    string_value,
    write_jsonl,
)


@dataclass(frozen=True)
class IdentityAnalysisSummary:
    unique_db_ids: int
    duplicate_db_ids: int
    duplicate_code_versions: int
    db_id_to_many_code_versions: int
    code_version_to_many_db_ids: int
    mismatches: int
    report_path: Path
    pairs_path: Path


def analyze_identities(
    settings: Any,
    options: BankRecordFilter | None = None,
) -> IdentityAnalysisSummary:
    effective_options = options or BankRecordFilter(all_records=True)
    catalog_rows = load_catalog_records(settings, active=True)
    selected_rows = (
        filter_catalog_records(catalog_rows, effective_options)
        if has_selection(effective_options)
        else catalog_rows
    )
    pairs = [identity_pair(settings, row) for row in selected_rows]
    report = build_identity_report(pairs)

    reports_root = bank_reports_root(settings)
    reports_root.mkdir(parents=True, exist_ok=True)
    report_path = reports_root / "identity-analysis.json"
    pairs_path = reports_root / "identity-pairs.jsonl"
    write_json(report_path, report)
    write_jsonl(pairs_path, pairs)

    return IdentityAnalysisSummary(
        unique_db_ids=report["unique_db_ids"],
        duplicate_db_ids=len(report["duplicate_db_ids"]),
        duplicate_code_versions=len(report["duplicate_code_versions"]),
        db_id_to_many_code_versions=len(report["db_id_to_many_code_versions"]),
        code_version_to_many_db_ids=len(report["code_version_to_many_db_ids"]),
        mismatches=report["catalog_document_db_id_mismatches"],
        report_path=report_path,
        pairs_path=pairs_path,
    )


def identity_pair(settings: Any, catalog_row: dict[str, Any]) -> dict[str, Any]:
    code_version = string_value(catalog_row.get("code_version"))
    manifest = read_json_file(current_dir(settings, code_version) / "manifest.json")
    document_db_id = to_int(manifest.get("document_db_id"))
    raw_path = current_dir(settings, code_version) / "getclinrec.json"
    if document_db_id is None and raw_path.exists():
        info, _errors = minimal_validate_raw_document(
            raw_path.read_bytes(),
            expected_code_version=code_version,
        )
        if info is not None:
            document_db_id = info.db_id

    catalog_source_record_id = source_record_id_from_catalog(catalog_row)
    return {
        "code_version": code_version,
        "code": to_int(catalog_row.get("code")),
        "version": to_int(catalog_row.get("version")),
        "catalog_source_record_id": catalog_source_record_id,
        "document_db_id": document_db_id,
        "db_id_match": (
            catalog_source_record_id == document_db_id
            if catalog_source_record_id is not None and document_db_id is not None
            else None
        ),
        "publish_date": catalog_row.get("publish_date"),
        "created_date": catalog_row.get("created_date"),
    }


def build_identity_report(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    db_id_to_code_versions: dict[int, set[str]] = defaultdict(set)
    code_version_to_db_ids: dict[str, set[int]] = defaultdict(set)
    code_version_counts: dict[str, int] = defaultdict(int)
    pair_rows: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    for pair in pairs:
        code_version = string_value(pair.get("code_version"))
        document_db_id = to_int(pair.get("document_db_id"))
        code_version_counts[code_version] += 1
        if document_db_id is not None:
            db_id_to_code_versions[document_db_id].add(code_version)
            code_version_to_db_ids[code_version].add(document_db_id)
        pair_rows.append(
            {
                "db_id": document_db_id,
                "code_version": code_version,
                "catalog_source_record_id": pair.get("catalog_source_record_id"),
                "db_id_match": pair.get("db_id_match"),
            }
        )
        if pair.get("db_id_match") is not True:
            mismatches.append(pair)

    duplicate_db_ids = {
        str(db_id): sorted(code_versions)
        for db_id, code_versions in sorted(db_id_to_code_versions.items())
        if len(code_versions) > 1
    }
    duplicate_code_versions = {
        code_version: count
        for code_version, count in sorted(code_version_counts.items())
        if count > 1
    }
    code_version_to_many_db_ids = {
        code_version: sorted(db_ids)
        for code_version, db_ids in sorted(code_version_to_db_ids.items())
        if len(db_ids) > 1
    }
    return {
        "unique_db_ids": len(db_id_to_code_versions),
        "duplicate_db_ids": duplicate_db_ids,
        "duplicate_code_versions": duplicate_code_versions,
        "db_id_code_version_pairs": pair_rows,
        "db_id_to_many_code_versions": duplicate_db_ids,
        "code_version_to_many_db_ids": code_version_to_many_db_ids,
        "created_date_db_id_monotonicity": monotonicity_report(pairs, "created_date"),
        "publish_date_db_id_monotonicity": monotonicity_report(pairs, "publish_date"),
        "catalog_document_db_id_matches": sum(
            1 for pair in pairs if pair.get("db_id_match") is True
        ),
        "catalog_document_db_id_mismatches": len(mismatches),
        "mismatches": mismatches,
    }


def monotonicity_report(pairs: list[dict[str, Any]], date_key: str) -> dict[str, Any]:
    sortable = [
        (string_value(pair.get(date_key)), to_int(pair.get("document_db_id")), pair)
        for pair in pairs
        if string_value(pair.get(date_key)) and to_int(pair.get("document_db_id")) is not None
    ]
    sortable.sort(key=lambda item: (item[0], item[1] or 0))
    violations: list[dict[str, Any]] = []
    previous_db_id: int | None = None
    previous_pair: dict[str, Any] | None = None
    for _date_value, db_id, pair in sortable:
        if previous_db_id is not None and db_id is not None and db_id < previous_db_id:
            violations.append(
                {
                    "previous": previous_pair,
                    "current": pair,
                    "date_key": date_key,
                }
            )
        if db_id is not None:
            previous_db_id = db_id
            previous_pair = pair
    return {
        "checked": len(sortable),
        "violations": violations,
    }
