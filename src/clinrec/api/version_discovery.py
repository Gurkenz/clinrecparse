from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clinrec.api.catalog_sync import split_code_version, to_int, write_json
from clinrec.api.client import ClinrecApiClient, JsonPayloadResult
from clinrec.config import Settings
from clinrec.models.external import (
    ApiErrorKind,
    CatalogResponse,
    ClinrecResponse,
    ExternalApiError,
    VersionAvailability,
    VersionAvailabilityRecord,
)

TEMPORARY_AVAILABILITY = {
    VersionAvailability.SERVER_ERROR,
    VersionAvailability.TIMEOUT,
    VersionAvailability.INVALID_JSON,
    VersionAvailability.HTML_ERROR,
    VersionAvailability.EMPTY_RESPONSE,
    VersionAvailability.ID_MISMATCH,
}
UNAVAILABLE_AVAILABILITY = {
    VersionAvailability.FORBIDDEN_403,
    VersionAvailability.NOT_FOUND_404,
}


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryOptions:
    code: int | None = None
    from_code: int | None = None
    to_code: int | None = None
    force: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    timestamp: str | None = None


@dataclass(frozen=True)
class VersionCandidate:
    code: int
    version: int

    @property
    def code_version(self) -> str:
        return f"{self.code}_{self.version}"


@dataclass(frozen=True)
class DiscoverySummary:
    timestamp: str
    index_path: Path
    report_path: Path
    planned: int
    checked: int
    skipped: int
    codes: int
    dry_run: bool
    availability_counts: dict[str, int]
    candidates_preview: list[str]
    anomalies: list[dict[str, Any]]


def utc_checked_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def discover_versions(
    settings: Settings,
    client: ClinrecApiClient | None,
    options: DiscoveryOptions,
) -> DiscoverySummary:
    timestamp = options.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    index_path = settings.paths.indexes / "version-availability.jsonl"
    report_path = settings.paths.reports / f"version-discovery-{timestamp}.json"

    candidates = filter_candidates(build_version_candidates(settings), options)
    existing = read_availability_index(index_path)
    candidates_to_check = [
        candidate
        for candidate in candidates
        if should_check(candidate, existing.get(candidate.code_version), settings, options)
    ]
    skipped = len(candidates) - len(candidates_to_check)
    anomalies: list[dict[str, Any]] = []

    if options.dry_run:
        summary = build_summary(
            timestamp=timestamp,
            index_path=index_path,
            report_path=report_path,
            planned=len(candidates),
            checked=0,
            skipped=skipped,
            codes=len({candidate.code for candidate in candidates}),
            dry_run=True,
            records=list(existing.values()),
            candidates_preview=[candidate.code_version for candidate in candidates[:20]],
            anomalies=anomalies,
        )
        write_discovery_report(summary, options)
        return summary

    if client is None:
        raise DiscoveryError("HTTP client is required unless --dry-run is used.")

    current = dict(existing)
    checked = 0
    for code in sorted({candidate.code for candidate in candidates_to_check}):
        code_candidates = [candidate for candidate in candidates_to_check if candidate.code == code]
        for candidate in code_candidates:
            previous = current.get(candidate.code_version)
            result = check_candidate(client, candidate, previous)
            current[candidate.code_version] = result
            checked += 1
            if result.availability == VersionAvailability.ID_MISMATCH:
                anomalies.append(
                    {
                        "code": "id_mismatch",
                        "requested_code_version": result.requested_code_version,
                        "error": result.error,
                    }
                )
        write_availability_index(index_path, current)

    summary = build_summary(
        timestamp=timestamp,
        index_path=index_path,
        report_path=report_path,
        planned=len(candidates),
        checked=checked,
        skipped=skipped,
        codes=len({candidate.code for candidate in candidates}),
        dry_run=False,
        records=list(current.values()),
        candidates_preview=[candidate.code_version for candidate in candidates[:20]],
        anomalies=anomalies,
    )
    write_discovery_report(summary, options)
    return summary


def build_version_candidates(settings: Settings) -> list[VersionCandidate]:
    max_versions: dict[int, int] = {}
    for code, version in read_catalog_index_candidates(settings.paths.indexes / "catalog.jsonl"):
        max_versions[code] = max(max_versions.get(code, 0), version)
    for code, version in read_raw_catalog_candidates(settings.paths.snapshots / "catalog"):
        max_versions[code] = max(max_versions.get(code, 0), version)
    for code, version in read_availability_candidates(
        settings.paths.indexes / "version-availability.jsonl"
    ):
        max_versions[code] = max(max_versions.get(code, 0), version)

    if not max_versions:
        raise DiscoveryError("No catalog candidates found. Run 'clinrec sync-catalog' first.")

    candidates: list[VersionCandidate] = []
    for code, max_version in sorted(max_versions.items()):
        if code <= 0 or max_version <= 0:
            continue
        candidates.extend(
            VersionCandidate(code=code, version=version)
            for version in range(1, max_version + 1)
        )
    return candidates


def filter_candidates(
    candidates: list[VersionCandidate],
    options: DiscoveryOptions,
) -> list[VersionCandidate]:
    filtered = candidates
    if options.code is not None:
        filtered = [candidate for candidate in filtered if candidate.code == options.code]
    if options.from_code is not None:
        filtered = [candidate for candidate in filtered if candidate.code >= options.from_code]
    if options.to_code is not None:
        filtered = [candidate for candidate in filtered if candidate.code <= options.to_code]
    return filtered


def should_check(
    candidate: VersionCandidate,
    previous: VersionAvailabilityRecord | None,
    settings: Settings,
    options: DiscoveryOptions,
) -> bool:
    if previous is None:
        return True
    if options.force:
        return True
    if previous.availability == VersionAvailability.AVAILABLE_JSON:
        return False
    if previous.availability in TEMPORARY_AVAILABILITY:
        return options.retry_failed
    if previous.availability in UNAVAILABLE_AVAILABILITY:
        return is_unavailable_expired(previous, settings)
    return True


def check_candidate(
    client: ClinrecApiClient,
    candidate: VersionCandidate,
    previous: VersionAvailabilityRecord | None = None,
) -> VersionAvailabilityRecord:
    checked_at = utc_checked_at()
    attempts = (previous.attempts + 1) if previous else 1
    result = client.fetch_clinrec_payload(candidate.code_version)
    if isinstance(result, ExternalApiError):
        availability = classify_external_error(result)
        return VersionAvailabilityRecord(
            requested_code_version=candidate.code_version,
            code=candidate.code,
            version=candidate.version,
            availability=availability,
            http_status=result.status_code,
            checked_at=checked_at,
            attempts=attempts,
            error=result.message,
        )

    return classify_success_payload(result, candidate, checked_at, attempts)


def classify_success_payload(
    result: JsonPayloadResult,
    candidate: VersionCandidate,
    checked_at: str,
    attempts: int,
) -> VersionAvailabilityRecord:
    try:
        response = ClinrecResponse.model_validate(result.payload)
    except ValidationError as exc:
        return VersionAvailabilityRecord(
            requested_code_version=candidate.code_version,
            code=candidate.code,
            version=candidate.version,
            availability=VersionAvailability.INVALID_JSON,
            http_status=result.status_code,
            checked_at=checked_at,
            attempts=attempts,
            error=f"Response schema validation failed: {exc.errors()}",
        )

    document = response.obj
    response_code_version = document.code_version or document.id
    response_code = to_int(document.code)
    response_version = to_int(document.version)
    title = document.title
    mismatch_reasons: list[str] = []
    if response_code_version != candidate.code_version:
        mismatch_reasons.append(f"id/code_version={response_code_version!r}")
    if response_code != candidate.code:
        mismatch_reasons.append(f"code={response_code!r}")
    if response_version != candidate.version:
        mismatch_reasons.append(f"version={response_version!r}")
    if not document.sections:
        mismatch_reasons.append("missing_sections")
    if not title or not title.strip():
        mismatch_reasons.append("missing_title")

    if mismatch_reasons:
        return VersionAvailabilityRecord(
            requested_code_version=candidate.code_version,
            code=candidate.code,
            version=candidate.version,
            availability=VersionAvailability.ID_MISMATCH,
            http_status=result.status_code,
            checked_at=checked_at,
            attempts=attempts,
            title=title,
            error="; ".join(mismatch_reasons),
        )

    return VersionAvailabilityRecord(
        requested_code_version=candidate.code_version,
        code=candidate.code,
        version=candidate.version,
        availability=VersionAvailability.AVAILABLE_JSON,
        http_status=result.status_code,
        checked_at=checked_at,
        attempts=attempts,
        title=title,
    )


def classify_external_error(error: ExternalApiError) -> VersionAvailability:
    if error.status_code == 403:
        return VersionAvailability.FORBIDDEN_403
    if error.status_code == 404:
        return VersionAvailability.NOT_FOUND_404
    if error.status_code is not None and error.status_code >= 500:
        return VersionAvailability.SERVER_ERROR
    if getattr(error, "error_type", None) == "timeout":
        return VersionAvailability.TIMEOUT
    if error.kind == ApiErrorKind.INVALID_JSON:
        return VersionAvailability.INVALID_JSON
    if error.kind == ApiErrorKind.HTML_ERROR:
        return VersionAvailability.HTML_ERROR
    if error.kind == ApiErrorKind.EMPTY_RESPONSE:
        return VersionAvailability.EMPTY_RESPONSE
    if error.kind == ApiErrorKind.UNEXPECTED_CONTENT_TYPE:
        return VersionAvailability.INVALID_JSON
    if error.kind == ApiErrorKind.REQUEST_ERROR:
        return VersionAvailability.SERVER_ERROR
    return VersionAvailability.SERVER_ERROR


def read_catalog_index_candidates(path: Path) -> list[tuple[int, int]]:
    if not path.exists():
        return []
    candidates: list[tuple[int, int]] = []
    for row in read_jsonl(path):
        code = to_int(row.get("code"))
        version = to_int(row.get("version"))
        if code is not None and version is not None:
            candidates.append((code, version))
    return candidates


def read_raw_catalog_candidates(snapshot_root: Path) -> list[tuple[int, int]]:
    if not snapshot_root.exists():
        return []
    latest = latest_snapshot_dir(snapshot_root)
    if latest is None:
        return []
    candidates: list[tuple[int, int]] = []
    for subset in ("active", "all-statuses"):
        subset_dir = latest / subset
        for page_path in sorted(subset_dir.glob("page-*.json")):
            payload = json.loads(page_path.read_text(encoding="utf-8"))
            response = CatalogResponse.model_validate(payload)
            for record in response.data:
                code = to_int(record.code)
                version = to_int(record.version)
                if code is None or version is None:
                    parsed_code, parsed_version = split_code_version(record.code_version)
                    code = code if code is not None else parsed_code
                    version = version if version is not None else parsed_version
                if code is not None and version is not None:
                    candidates.append((code, version))
    return candidates


def read_availability_candidates(path: Path) -> list[tuple[int, int]]:
    candidates: list[tuple[int, int]] = []
    for record in read_availability_index(path).values():
        candidates.append((record.code, record.version))
    return candidates


def read_availability_index(path: Path) -> dict[str, VersionAvailabilityRecord]:
    if not path.exists():
        return {}
    records: dict[str, VersionAvailabilityRecord] = {}
    for row in read_jsonl(path):
        record = VersionAvailabilityRecord.model_validate(row)
        records[record.requested_code_version] = record
    return records


def write_availability_index(
    path: Path,
    records: dict[str, VersionAvailabilityRecord],
) -> None:
    rows = [
        record.model_dump(mode="json")
        for record in sorted(records.values(), key=lambda item: (item.code, item.version))
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp_path.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_snapshot_dir(snapshot_root: Path) -> Path | None:
    snapshots = [path for path in snapshot_root.iterdir() if path.is_dir()]
    if not snapshots:
        return None
    return sorted(snapshots, key=lambda path: path.name)[-1]


def is_unavailable_expired(record: VersionAvailabilityRecord, settings: Settings) -> bool:
    ttl_days = settings.discovery.unavailable_retry_ttl_days
    if ttl_days <= 0:
        return True
    try:
        checked_at = datetime.fromisoformat(record.checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(UTC) - checked_at >= timedelta(days=ttl_days)


def build_summary(
    *,
    timestamp: str,
    index_path: Path,
    report_path: Path,
    planned: int,
    checked: int,
    skipped: int,
    codes: int,
    dry_run: bool,
    records: list[VersionAvailabilityRecord],
    candidates_preview: list[str],
    anomalies: list[dict[str, Any]],
) -> DiscoverySummary:
    counts = Counter(record.availability.value for record in records)
    return DiscoverySummary(
        timestamp=timestamp,
        index_path=index_path,
        report_path=report_path,
        planned=planned,
        checked=checked,
        skipped=skipped,
        codes=codes,
        dry_run=dry_run,
        availability_counts=dict(sorted(counts.items())),
        candidates_preview=candidates_preview,
        anomalies=anomalies,
    )


def write_discovery_report(summary: DiscoverySummary, options: DiscoveryOptions) -> None:
    write_json(
        summary.report_path,
        {
            "timestamp": summary.timestamp,
            "filters": {
                "code": options.code,
                "from_code": options.from_code,
                "to_code": options.to_code,
                "force": options.force,
                "retry_failed": options.retry_failed,
                "dry_run": options.dry_run,
            },
            "planned": summary.planned,
            "checked": summary.checked,
            "skipped": summary.skipped,
            "codes": summary.codes,
            "availability_counts": summary.availability_counts,
            "checkpoint_records": sum(summary.availability_counts.values()),
            "candidates_preview": summary.candidates_preview,
            "anomalies": summary.anomalies,
        },
    )
