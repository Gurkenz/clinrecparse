from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clinrec.api.catalog_sync import validate_reference_organizations, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    BankRecordFilter,
    bank_active_root,
    bank_legacy_root,
    bank_references_root,
    compact_timestamp,
    first_present,
    load_catalog_records,
    minimal_validate_raw_document,
    parse_code_version_or_raise,
    read_json_file,
    read_jsonl,
    sha256_bytes,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.config import Settings
from clinrec.models.external import ExternalApiError, NkoListResponse, ReferenceOrganization


@dataclass(frozen=True)
class BankReferenceUpdateSummary:
    timestamp: str
    report_path: Path
    current_path: Path
    history_path: Path
    snapshot_path: Path | None
    new: int
    updated: int
    unchanged: int
    missing_from_latest_reference: int
    warnings: list[str]


@dataclass(frozen=True)
class BankDeveloperEnrichmentSummary:
    documents: int
    updated: int
    unresolved: int
    reference_status: str


def update_references(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    timestamp: str | None = None,
) -> BankReferenceUpdateSummary:
    current_timestamp = compact_timestamp(timestamp)
    references_root = bank_references_root(settings)
    snapshots_root = references_root / "snapshots" / current_timestamp
    current_path = references_root / "nko-current.jsonl"
    legacy_current_path = references_root / "nko.jsonl"
    history_path = references_root / "nko-history.jsonl"
    report_path = references_root / "update-report.json"
    raw_path = snapshots_root / "getnkolist.json"
    warnings: list[str] = []

    result = client.fetch_nko_list_payload()
    if isinstance(result, ExternalApiError):
        warnings.append("nko_fetch_failed")
        report = reference_failure_report(
            current_timestamp,
            warnings,
            result.model_dump(mode="json"),
            current_path,
        )
        write_json(report_path, report)
        return BankReferenceUpdateSummary(
            timestamp=current_timestamp,
            report_path=report_path,
            current_path=current_path,
            history_path=history_path,
            snapshot_path=None,
            new=0,
            updated=0,
            unchanged=0,
            missing_from_latest_reference=0,
            warnings=warnings,
        )

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(result.raw_content)
    try:
        response = NkoListResponse.model_validate(result.payload)
    except ValidationError as exc:
        warnings.append("nko_validation_failed")
        report = reference_failure_report(
            current_timestamp,
            warnings,
            {"errors": exc.errors()},
            current_path,
        )
        write_json(report_path, report)
        return BankReferenceUpdateSummary(
            timestamp=current_timestamp,
            report_path=report_path,
            current_path=current_path,
            history_path=history_path,
            snapshot_path=raw_path,
            new=0,
            updated=0,
            unchanged=0,
            missing_from_latest_reference=0,
            warnings=warnings,
        )

    organizations = [
        ReferenceOrganization(
            id=item.id,
            name=item.name,
            short_name=item.short_name,
            raw_short_name=item.raw_short_name,
            engname=item.engname,
            engshortname=item.engshortname,
            profile=item.profile,
            url=item.url,
        )
        for item in response.d.data
    ]
    issues = validate_reference_organizations(organizations)
    rows = [organization.model_dump(mode="json") for organization in organizations]
    latest_by_id = rows_by_id(rows)
    previous_history = {string_value(row.get("nko_id")): row for row in read_jsonl(history_path)}
    previous_current = rows_by_id(read_jsonl(current_path))

    history_rows: list[dict[str, Any]] = []
    new = updated = unchanged = 0
    for nko_id, row in sorted(latest_by_id.items()):
        previous = previous_history.get(nko_id)
        previous_raw = previous_current.get(nko_id)
        raw_changed = stable_json(previous_raw) != stable_json(row)
        if previous is None:
            new += 1
            previous_values: list[dict[str, Any]] = []
            first_seen_at = utc_now()
        else:
            previous_values = list_value(previous.get("previous_values"))
            first_seen_at = string_value(previous.get("first_seen_at")) or utc_now()
            if raw_changed:
                updated += 1
                if previous_raw:
                    previous_values.append(
                        {
                            "recorded_at": string_value(previous.get("last_seen_at")) or utc_now(),
                            "raw": previous_raw,
                        }
                    )
            else:
                unchanged += 1
        history_rows.append(
            {
                "nko_id": nko_id,
                "first_seen_at": first_seen_at,
                "last_seen_at": utc_now(),
                "missing_from_latest": False,
                "raw_current": row,
                "previous_values": previous_values,
            }
        )

    missing = 0
    for nko_id, previous in sorted(previous_history.items()):
        if nko_id in latest_by_id:
            continue
        missing += 1
        history_rows.append(
            {
                **previous,
                "nko_id": nko_id,
                "last_seen_at": string_value(previous.get("last_seen_at")) or utc_now(),
                "missing_from_latest": True,
            }
        )

    write_jsonl(current_path, rows)
    write_jsonl(legacy_current_path, rows)
    write_jsonl(history_path, history_rows)
    write_json(
        references_root / "manifest.json",
        {
            "source": "GetNkoList",
            "http_status": result.status_code,
            "content_type": result.content_type,
            "size": len(result.raw_content),
            "sha256": sha256_bytes(result.raw_content),
            "downloaded_at": utc_now(),
            "records": len(rows),
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
    )
    write_json(
        report_path,
        {
            "timestamp": current_timestamp,
            "snapshot_path": raw_path.as_posix(),
            "records": len(rows),
            "new": new,
            "updated": updated,
            "unchanged": unchanged,
            "missing_from_latest_reference": missing,
            "warnings": warnings,
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
    )
    return BankReferenceUpdateSummary(
        timestamp=current_timestamp,
        report_path=report_path,
        current_path=current_path,
        history_path=history_path,
        snapshot_path=raw_path,
        new=new,
        updated=updated,
        unchanged=unchanged,
        missing_from_latest_reference=missing,
        warnings=warnings,
    )


def enrich_developers(
    settings: Settings,
    options: BankRecordFilter | None = None,
) -> BankDeveloperEnrichmentSummary:
    reference_path = bank_references_root(settings) / "nko-current.jsonl"
    references = rows_by_id(read_jsonl(reference_path))
    reference_status = "available" if references else "missing"
    roots = selected_document_roots(settings, options)
    updated = 0
    unresolved_total = 0
    for document_root in roots:
        current_root = document_root / "current"
        raw_path = current_root / "getclinrec.json"
        catalog_path = current_root / "catalog-record.json"
        if not raw_path.exists():
            continue
        code_version = document_root.name
        info, _errors = minimal_validate_raw_document(
            raw_path.read_bytes(),
            expected_code_version=code_version,
        )
        catalog_record = read_json_file(catalog_path)
        association_ids = info.association_ids if info is not None else []
        resolved = []
        unresolved = []
        for association_id in association_ids:
            row = references.get(string_value(association_id))
            if row is None:
                unresolved.append(association_id)
            else:
                resolved.append(row)
        write_json(
            current_root / "developers.json",
            {
                "catalog_developers": catalog_record.get("developers") or [],
                "association_ids": association_ids,
                "resolved_associations": resolved,
                "unresolved_association_ids": unresolved,
                "reference_status": reference_status,
                "updated_at": utc_now(),
            },
        )
        updated += 1
        unresolved_total += len(unresolved)
    return BankDeveloperEnrichmentSummary(
        documents=len(roots),
        updated=updated,
        unresolved=unresolved_total,
        reference_status=reference_status,
    )


def selected_document_roots(
    settings: Settings,
    options: BankRecordFilter | None,
) -> list[Path]:
    all_roots = sorted(
        document_roots(bank_active_root(settings))
        + document_roots(bank_legacy_root(settings))
    )
    if options is None or options.all_records or not has_reference_selection(options):
        return all_roots

    selected = set(options.code_versions or [])
    if options.code is not None or options.from_code is not None or options.to_code is not None:
        catalog_rows = load_catalog_records(settings, active=True)
        for row in catalog_rows:
            code_version = string_value(row.get("code_version"))
            if not code_version:
                continue
            code, _version = parse_code_version_or_raise(code_version)
            if options.code is not None and code != options.code:
                continue
            if options.from_code is not None and code < options.from_code:
                continue
            if options.to_code is not None and code > options.to_code:
                continue
            selected.add(code_version)
    return [root for root in all_roots if root.name in selected]


def document_roots(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.iterdir() if path.is_dir()]


def rows_by_id(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        organization_id = first_present(row, "id", "Id", "ID", "nko_id", "NkoId")
        if organization_id is not None:
            result[string_value(organization_id)] = row
    return result


def reference_failure_report(
    timestamp: str,
    warnings: list[str],
    context: dict[str, Any],
    current_path: Path,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "records": len(read_jsonl(current_path)),
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "missing_from_latest_reference": 0,
        "warnings": warnings,
        "fallback_reference_path": current_path.as_posix() if current_path.exists() else None,
        "context": context,
    }


def has_reference_selection(options: BankRecordFilter) -> bool:
    return bool(options.code_versions) or any(
        value is not None for value in (options.code, options.from_code, options.to_code)
    )


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]
