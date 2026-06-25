from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clinrec.api.client import ClinrecApiClient, JsonPayloadResult
from clinrec.config import Settings
from clinrec.models.external import (
    CatalogQaReport,
    CatalogRecord,
    CatalogResponse,
    ExternalApiError,
    NkoListResponse,
    NormalizedCatalogRecord,
    QaIssue,
    ReferenceOrganization,
)

INITIAL_PAGE_SIZE = 1000
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$")
MICROSOFT_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogSubsetSummary:
    name: str
    snapshot_dir: Path
    pages: int
    records: int
    total_records: int | None
    issues: int


@dataclass(frozen=True)
class CatalogSyncSummary:
    timestamp: str
    snapshot_root: Path
    index_path: Path
    qa_report_path: Path
    active: CatalogSubsetSummary
    all_statuses: CatalogSubsetSummary
    normalized_records: int
    issues: list[QaIssue]


@dataclass(frozen=True)
class ReferenceSyncSummary:
    timestamp: str
    snapshot_dir: Path
    raw_path: Path
    index_path: Path
    qa_report_path: Path
    organizations: int
    issues: list[QaIssue]


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_catalog_request(
    *,
    active_only: bool,
    page: int,
    page_size: int = INITIAL_PAGE_SIZE,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    if active_only:
        filters.append(
            {
                "fieldName": "status",
                "filterType": 1,
                "filterValueType": 2,
                "value1": 0,
                "value2": "",
                "values": [],
            }
        )

    return {
        "columns": [],
        "currentPage": page,
        "filters": filters,
        "pageSize": page_size,
        "sortOption": {
            "fieldName": "publishdate",
            "sortType": 2,
        },
        "useANDoperator": True,
    }


def sync_catalog(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    timestamp: str | None = None,
) -> CatalogSyncSummary:
    current_timestamp = timestamp or utc_timestamp()
    snapshot_root = settings.paths.snapshots / "catalog" / current_timestamp
    qa_report_path = settings.paths.reports / f"catalog-qa-{current_timestamp}.json"
    report_issues: list[QaIssue] = []
    active: CatalogSubsetSummary | None = None
    all_statuses: CatalogSubsetSummary | None = None

    try:
        active = _sync_catalog_subset(
            settings,
            client,
            timestamp=current_timestamp,
            subset_name="active",
            active_only=True,
            issues=report_issues,
        )
        all_statuses = _sync_catalog_subset(
            settings,
            client,
            timestamp=current_timestamp,
            subset_name="all-statuses",
            active_only=False,
            issues=report_issues,
        )
    except SyncError:
        _write_catalog_qa_report(
            qa_report_path,
            current_timestamp,
            active.records if active else 0,
            all_statuses.records if all_statuses else 0,
            report_issues,
        )
        raise

    all_records = _read_subset_records(all_statuses.snapshot_dir)
    normalized_records = [normalize_catalog_record(record) for record in all_records]
    report_issues.extend(validate_catalog_records(normalized_records))

    settings.paths.indexes.mkdir(parents=True, exist_ok=True)
    index_path = settings.paths.indexes / "catalog.jsonl"
    write_jsonl(index_path, [record.model_dump(mode="json") for record in normalized_records])

    report = CatalogQaReport(
        timestamp=current_timestamp,
        active_records=active.records,
        all_statuses_records=all_statuses.records,
        issues=report_issues,
    )
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    write_json(qa_report_path, report.model_dump(mode="json"))

    return CatalogSyncSummary(
        timestamp=current_timestamp,
        snapshot_root=snapshot_root,
        index_path=index_path,
        qa_report_path=qa_report_path,
        active=CatalogSubsetSummary(
            **{**active.__dict__, "issues": _subset_issue_count(report_issues, "active")}
        ),
        all_statuses=CatalogSubsetSummary(
            **{
                **all_statuses.__dict__,
                "issues": _subset_issue_count(report_issues, "all-statuses"),
            }
        ),
        normalized_records=len(normalized_records),
        issues=report_issues,
    )


def sync_references(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    timestamp: str | None = None,
) -> ReferenceSyncSummary:
    current_timestamp = timestamp or utc_timestamp()
    snapshot_dir = settings.paths.snapshots / "references" / current_timestamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    raw_path = snapshot_dir / "nko-list.json"
    qa_report_path = settings.paths.reports / f"references-qa-{current_timestamp}.json"
    issues: list[QaIssue] = []

    result = client.fetch_nko_list_payload()
    if isinstance(result, ExternalApiError):
        issues.append(
            QaIssue(
                severity="error",
                code="nko_fetch_failed",
                message=result.message,
                context=result.model_dump(mode="json"),
            )
        )
        settings.paths.reports.mkdir(parents=True, exist_ok=True)
        write_json(qa_report_path, {"timestamp": current_timestamp, "issues": _dump_issues(issues)})
        raise SyncError(result.message)

    raw_path.write_bytes(result.raw_content)
    try:
        response = NkoListResponse.model_validate(result.payload)
    except ValidationError as exc:
        issues.append(
            QaIssue(
                severity="error",
                code="nko_validation_failed",
                message="NKO response validation failed",
                context={"errors": exc.errors()},
            )
        )
        settings.paths.reports.mkdir(parents=True, exist_ok=True)
        write_json(qa_report_path, {"timestamp": current_timestamp, "issues": _dump_issues(issues)})
        raise SyncError("NKO response validation failed") from exc

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
    issues.extend(validate_reference_organizations(organizations))

    settings.paths.references.mkdir(parents=True, exist_ok=True)
    index_path = settings.paths.references / "nko-organizations.jsonl"
    write_jsonl(
        index_path,
        [organization.model_dump(mode="json") for organization in organizations],
    )

    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    write_json(
        qa_report_path,
        {
            "timestamp": current_timestamp,
            "organizations": len(organizations),
            "issues": _dump_issues(issues),
        },
    )

    return ReferenceSyncSummary(
        timestamp=current_timestamp,
        snapshot_dir=snapshot_dir,
        raw_path=raw_path,
        index_path=index_path,
        qa_report_path=qa_report_path,
        organizations=len(organizations),
        issues=issues,
    )


def _sync_catalog_subset(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    timestamp: str,
    subset_name: str,
    active_only: bool,
    issues: list[QaIssue],
) -> CatalogSubsetSummary:
    subset_dir = settings.paths.snapshots / "catalog" / timestamp / subset_name
    subset_dir.mkdir(parents=True, exist_ok=True)

    first_request = build_catalog_request(active_only=active_only, page=1)
    write_json(subset_dir / "request.json", first_request)

    pages: list[CatalogResponse] = []
    page_files: list[str] = []
    page = 1
    total_records: int | None = None
    actual_page_size: int | None = None

    while True:
        request_payload = build_catalog_request(
            active_only=active_only,
            page=page,
            page_size=actual_page_size or INITIAL_PAGE_SIZE,
        )
        result = client.fetch_catalog_payload(request_payload)
        if isinstance(result, ExternalApiError):
            issues.append(
                QaIssue(
                    severity="error",
                    code="catalog_fetch_failed",
                    message=result.message,
                    context={"subset": subset_name, **result.model_dump(mode="json")},
                )
            )
            _write_catalog_manifest(
                subset_dir,
                subset_name,
                page_files,
                pages,
                total_records,
                issues,
            )
            raise SyncError(f"{subset_name} catalog fetch failed: {result.message}")

        page_path = subset_dir / f"page-{page:04d}.json"
        page_path.write_bytes(result.raw_content)
        page_files.append(page_path.name)

        response = _validate_catalog_payload(result, subset_name, page, issues)
        pages.append(response)

        if response.total is None:
            issues.append(
                QaIssue(
                    severity="error",
                    code="missing_total_records",
                    message="Catalog response does not contain TotalRecords",
                    context={"subset": subset_name, "page": page},
                )
            )
            _write_catalog_manifest(
                subset_dir,
                subset_name,
                page_files,
                pages,
                total_records,
                issues,
            )
            raise SyncError("Catalog response does not contain TotalRecords")

        total_records = response.total
        if response.current_page is not None and response.current_page != page:
            issues.append(
                QaIssue(
                    severity="error",
                    code="invalid_page_number",
                    message="Catalog response page number does not match requested page",
                    context={
                        "subset": subset_name,
                        "requested_page": page,
                        "actual_page": response.current_page,
                    },
                )
            )

        if actual_page_size is None:
            actual_page_size = response.page_size or len(response.data) or INITIAL_PAGE_SIZE
            if response.page_size is None:
                issues.append(
                    QaIssue(
                        severity="warning",
                        code="missing_page_size",
                        message="Catalog response does not expose PageSize; inferred page size",
                        context={"subset": subset_name, "inferred_page_size": actual_page_size},
                    )
                )

        expected_pages = max(1, math.ceil(total_records / actual_page_size))
        if page >= expected_pages:
            break
        page += 1

    record_count = sum(len(response.data) for response in pages)
    if total_records is not None and record_count != total_records:
        issues.append(
            QaIssue(
                severity="error",
                code="record_count_mismatch",
                message="Fetched record count does not match TotalRecords",
                context={
                    "subset": subset_name,
                    "fetched": record_count,
                    "total_records": total_records,
                },
            )
        )

    expected_page_numbers = list(range(1, len(pages) + 1))
    actual_page_numbers = [
        response.current_page if response.current_page is not None else index
        for index, response in enumerate(pages, start=1)
    ]
    if actual_page_numbers != expected_page_numbers:
        issues.append(
            QaIssue(
                severity="error",
                code="missing_or_unordered_pages",
                message="Catalog pages are missing or out of order",
                context={
                    "subset": subset_name,
                    "expected": expected_page_numbers,
                    "actual": actual_page_numbers,
                },
            )
        )

    _write_catalog_manifest(subset_dir, subset_name, page_files, pages, total_records, issues)
    return CatalogSubsetSummary(
        name=subset_name,
        snapshot_dir=subset_dir,
        pages=len(pages),
        records=record_count,
        total_records=total_records,
        issues=_subset_issue_count(issues, subset_name),
    )


def _validate_catalog_payload(
    result: JsonPayloadResult,
    subset_name: str,
    page: int,
    issues: list[QaIssue],
) -> CatalogResponse:
    try:
        return CatalogResponse.model_validate(result.payload)
    except ValidationError as exc:
        issues.append(
            QaIssue(
                severity="error",
                code="catalog_validation_failed",
                message="Catalog response validation failed",
                context={"subset": subset_name, "page": page, "errors": exc.errors()},
            )
        )
        raise SyncError("Catalog response validation failed") from exc


def _write_catalog_manifest(
    subset_dir: Path,
    subset_name: str,
    page_files: list[str],
    pages: list[CatalogResponse],
    total_records: int | None,
    issues: list[QaIssue],
) -> None:
    write_json(
        subset_dir / "manifest.json",
        {
            "subset": subset_name,
            "pages": page_files,
            "page_count": len(page_files),
            "record_count": sum(len(page.data) for page in pages),
            "total_records": total_records,
            "issues": [
                issue.model_dump(mode="json")
                for issue in issues
                if issue.context.get("subset") == subset_name
            ],
        },
    )


def _write_catalog_qa_report(
    qa_report_path: Path,
    timestamp: str,
    active_records: int,
    all_statuses_records: int,
    issues: list[QaIssue],
) -> None:
    report = CatalogQaReport(
        timestamp=timestamp,
        active_records=active_records,
        all_statuses_records=all_statuses_records,
        issues=issues,
    )
    write_json(qa_report_path, report.model_dump(mode="json"))


def _read_subset_records(subset_dir: Path) -> list[CatalogRecord]:
    records: list[CatalogRecord] = []
    for page_path in sorted(subset_dir.glob("page-*.json")):
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        response = CatalogResponse.model_validate(payload)
        records.extend(response.data)
    return records


def normalize_catalog_record(record: CatalogRecord) -> NormalizedCatalogRecord:
    code_version = record.code_version
    code, version = split_code_version(code_version)
    code = to_int(record.code) if record.code is not None else code
    version = to_int(record.version) if record.version is not None else version
    publish_date = parse_source_date(record.publish_date or record.publish_date_str)
    created_date = parse_source_date(record.created or record.created_str)
    return NormalizedCatalogRecord(
        source_record_id=to_int(record.source_record_id),
        code=code,
        version=version,
        code_version=code_version,
        name=record.title or "",
        status=to_int(record.status),
        apply_status=record.apply_status,
        apply_status_calculated=to_int(record.apply_status_calculated),
        npc_approved=record.npc_approved,
        age_category=to_int(record.age_category),
        age_category_name=record.age_category_name,
        publish_date=publish_date,
        created_date=created_date,
        prev_cr_id=to_int(record.prev_cr_id),
        developers=as_list(record.developers),
        mkbs=as_list(record.mkbs),
        specialities=record.specialities,
    )


def validate_catalog_records(records: list[NormalizedCatalogRecord]) -> list[QaIssue]:
    issues: list[QaIssue] = []
    source_ids = [
        record.source_record_id for record in records if record.source_record_id is not None
    ]
    for source_id, count in Counter(source_ids).items():
        if count > 1:
            issues.append(
                QaIssue(
                    severity="error",
                    code="duplicate_source_record_id",
                    message="source_record_id is not unique",
                    context={"source_record_id": source_id, "count": count},
                )
            )

    by_code_version: dict[str, list[NormalizedCatalogRecord]] = defaultdict(list)
    for record in records:
        by_code_version[record.code_version].append(record)
        if not record.name.strip():
            issues.append(
                QaIssue(
                    severity="error",
                    code="empty_name",
                    message="Catalog record has empty Name",
                    context={"code_version": record.code_version},
                )
            )
        if record.code is not None and record.version is not None:
            expected = f"{record.code}_{record.version}"
            if record.code_version != expected:
                issues.append(
                    QaIssue(
                        severity="error",
                        code="code_version_mismatch",
                        message="CodeVersion does not match Code + '_' + Version",
                        context={
                            "code_version": record.code_version,
                            "expected": expected,
                            "code": record.code,
                            "version": record.version,
                        },
                    )
                )

    for code_version, duplicates in by_code_version.items():
        if len(duplicates) <= 1:
            continue
        names = {record.name for record in duplicates}
        statuses = {record.status for record in duplicates}
        issue_code = (
            "conflicting_code_version"
            if len(names) > 1 or len(statuses) > 1
            else "duplicate_code_version"
        )
        issues.append(
            QaIssue(
                severity="error",
                code=issue_code,
                message="code_version is duplicated",
                context={
                    "code_version": code_version,
                    "count": len(duplicates),
                    "names": sorted(names),
                    "statuses": sorted(str(status) for status in statuses),
                },
            )
        )

    return issues


def validate_reference_organizations(organizations: list[ReferenceOrganization]) -> list[QaIssue]:
    issues: list[QaIssue] = []
    ids = [organization.id for organization in organizations if organization.id is not None]
    for organization_id, count in Counter(ids).items():
        if count > 1:
            issues.append(
                QaIssue(
                    severity="error",
                    code="duplicate_organization_id",
                    message="Organization id is duplicated",
                    context={"id": organization_id, "count": count},
                )
            )
    names = [organization.name.strip() for organization in organizations]
    for name, count in Counter(names).items():
        if name and count > 1:
            issues.append(
                QaIssue(
                    severity="warning",
                    code="duplicate_organization_name",
                    message="Organization name is duplicated",
                    context={"name": name, "count": count},
                )
            )
    return issues


def parse_source_date(value: str | None) -> str | None:
    if not value:
        return None

    trimmed = value.strip()
    date_match = DATE_PREFIX_RE.match(trimmed)
    if date_match:
        return date_match.group(1)

    match = MICROSOFT_DATE_RE.fullmatch(trimmed)
    if match:
        epoch_ms = int(match.group(1))
        return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).date().isoformat()

    try:
        parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
    except ValueError:
        return None

    return parsed.date().isoformat()


def split_code_version(code_version: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"(\d+)_(\d+)", code_version)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def to_int(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _subset_issue_count(issues: list[QaIssue], subset_name: str) -> int:
    return sum(1 for issue in issues if issue.context.get("subset") == subset_name)


def _dump_issues(issues: list[QaIssue]) -> list[dict[str, Any]]:
    return [issue.model_dump(mode="json") for issue in issues]
