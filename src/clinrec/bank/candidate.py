from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import CatalogSyncSummary, sync_catalog, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    CANDIDATE_MANIFEST_SCHEMA_VERSION,
    BankError,
    bank_candidates_root,
    catalog_record_for_bank,
    compact_timestamp,
    read_json_file,
    read_jsonl,
    sha256_file,
    source_record_id_from_catalog,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.config import Settings


@dataclass(frozen=True)
class CandidateCatalogSummary:
    transaction_id: str
    root: Path
    active_records_path: Path
    all_statuses_records_path: Path
    manifest_path: Path
    active_total_records: int
    active_unique_code_versions: int
    active_index_sha256: str
    validation_status: str
    validation_issues: list[dict[str, Any]]


def fetch_candidate_catalog(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    transaction_id: str | None = None,
    include_code_versions: set[str] | None = None,
    pilot: bool = False,
) -> CandidateCatalogSummary:
    current_transaction_id = transaction_id or compact_timestamp()
    candidate_root = bank_candidates_root(settings) / current_transaction_id
    if candidate_root.exists():
        raise BankError(f"Candidate transaction already exists: {current_transaction_id}")

    sync_summary = sync_catalog(settings, client, timestamp=current_transaction_id)
    return materialize_candidate_from_sync(
        settings,
        sync_summary,
        transaction_id=current_transaction_id,
        include_code_versions=include_code_versions,
        pilot=pilot,
    )


def materialize_candidate_from_sync(
    settings: Settings,
    sync_summary: CatalogSyncSummary,
    *,
    transaction_id: str,
    include_code_versions: set[str] | None = None,
    pilot: bool = False,
) -> CandidateCatalogSummary:
    candidate_root = bank_candidates_root(settings) / transaction_id
    if candidate_root.exists():
        raise BankError(f"Candidate transaction already exists: {transaction_id}")
    pages_root = candidate_root / "pages"
    pages_root.mkdir(parents=True, exist_ok=True)

    active_rows = [
        catalog_record_for_bank(row) for row in read_jsonl(sync_summary.active_index_path)
    ]
    all_statuses_rows = [
        catalog_record_for_bank(row) for row in read_jsonl(sync_summary.all_statuses_index_path)
    ]
    if include_code_versions is not None:
        active_rows = [
            row
            for row in active_rows
            if string_value(row.get("code_version")) in include_code_versions
        ]
        all_statuses_rows = [
            row
            for row in all_statuses_rows
            if string_value(row.get("code_version")) in include_code_versions
        ]

    request_source = sync_summary.active.snapshot_dir / "request.json"
    if request_source.exists():
        shutil.copy2(request_source, candidate_root / "request.json")
    copy_pages(sync_summary.active.snapshot_dir, pages_root, prefix="page")
    copy_pages(sync_summary.all_statuses.snapshot_dir, pages_root, prefix="all-statuses-page")

    active_records_path = candidate_root / "catalog-active.jsonl"
    all_statuses_records_path = candidate_root / "catalog-all-statuses.jsonl"
    write_jsonl(active_records_path, active_rows)
    write_jsonl(all_statuses_records_path, all_statuses_rows)

    validation_issues = validate_candidate_rows(active_rows)
    validation_status = "valid" if not validation_issues else "invalid"
    manifest_path = candidate_root / "manifest.json"
    active_sha = sha256_file(active_records_path)
    all_statuses_sha = sha256_file(all_statuses_records_path)
    write_json(
        manifest_path,
        {
            "schema_version": CANDIDATE_MANIFEST_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "created_at": utc_now(),
            "active_total_records": len(active_rows),
            "active_unique_code_versions": len(
                {string_value(row.get("code_version")) for row in active_rows}
            ),
            "active_index_sha256": active_sha,
            "all_statuses_index_sha256": all_statuses_sha,
            "snapshot_paths": {
                "active": active_records_path.as_posix(),
                "all_statuses": all_statuses_records_path.as_posix(),
                "source_snapshot": sync_summary.snapshot_root.as_posix(),
            },
            "validation_status": validation_status,
            "validation_issues": validation_issues,
            "pilot": pilot,
        },
    )
    if validation_status != "valid":
        raise BankError(f"Candidate catalog is invalid: {validation_issues}")
    return CandidateCatalogSummary(
        transaction_id=transaction_id,
        root=candidate_root,
        active_records_path=active_records_path,
        all_statuses_records_path=all_statuses_records_path,
        manifest_path=manifest_path,
        active_total_records=len(active_rows),
        active_unique_code_versions=len(
            {string_value(row.get("code_version")) for row in active_rows}
        ),
        active_index_sha256=active_sha,
        validation_status=validation_status,
        validation_issues=validation_issues,
    )


def load_candidate_records(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    return [catalog_record_for_bank(row) for row in rows]


def load_candidate_manifest(candidate_root: Path) -> dict[str, Any]:
    manifest = read_json_file(candidate_root / "manifest.json")
    if manifest.get("schema_version") != CANDIDATE_MANIFEST_SCHEMA_VERSION:
        raise BankError(f"Invalid candidate manifest schema: {candidate_root}")
    return manifest


def copy_pages(snapshot_subset_root: Path, pages_root: Path, *, prefix: str) -> None:
    for index, source in enumerate(sorted(snapshot_subset_root.glob("page-*.json")), start=1):
        target = pages_root / f"{prefix}-{index:04d}.json"
        shutil.copy2(source, target)


def validate_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not rows:
        issues.append({"code": "empty_catalog", "severity": "error"})
    code_versions: dict[str, int] = {}
    source_ids: dict[int, set[str]] = {}
    for row in rows:
        code_version = string_value(row.get("code_version"))
        code = row.get("code")
        version = row.get("version")
        expected = f"{code}_{version}" if code is not None and version is not None else ""
        if expected and code_version != expected:
            issues.append(
                {
                    "code": "code_version_mismatch",
                    "severity": "error",
                    "code_version": code_version,
                    "expected": expected,
                }
            )
        code_versions[code_version] = code_versions.get(code_version, 0) + 1
        source_record_id = source_record_id_from_catalog(row)
        if source_record_id is not None:
            source_ids.setdefault(source_record_id, set()).add(code_version)
    for code_version, count in sorted(code_versions.items()):
        if count > 1:
            issues.append(
                {
                    "code": "duplicate_code_version",
                    "severity": "error",
                    "code_version": code_version,
                    "count": count,
                }
            )
    for source_record_id, versions in sorted(source_ids.items()):
        if len(versions) > 1:
            issues.append(
                {
                    "code": "duplicate_source_record_id",
                    "severity": "error",
                    "source_record_id": source_record_id,
                    "code_versions": sorted(versions),
                }
            )
    return issues
