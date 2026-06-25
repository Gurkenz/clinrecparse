from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.api.version_discovery import (
    TEMPORARY_AVAILABILITY,
    VersionCandidate,
    classify_success_payload,
    read_availability_index,
    read_jsonl,
)
from clinrec.config import Settings
from clinrec.models.external import ExternalApiError, VersionAvailability, VersionAvailabilityRecord


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadOptions:
    code_versions: list[str] | None = None
    code: int | None = None
    from_code: int | None = None
    to_code: int | None = None
    all_versions: bool = False
    force: bool = False
    dry_run: bool = False
    retry_failed: bool = False
    timestamp: str | None = None


@dataclass(frozen=True)
class DownloadedDocumentSummary:
    code_version: str
    document_dir: Path
    manifest_path: Path
    status: str
    json_status: str
    pdf_status: str


@dataclass(frozen=True)
class DownloadSummary:
    timestamp: str
    planned: int
    downloaded: int
    skipped: int
    partial: int
    failed: int
    dry_run: bool
    documents: list[DownloadedDocumentSummary]
    candidates_preview: list[str]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def download_documents(
    settings: Settings,
    client: ClinrecApiClient | None,
    options: DownloadOptions,
) -> DownloadSummary:
    if not has_selection_filter(options):
        raise DownloadError(
            "Refusing to download the full corpus without a filter. "
            "Use --all, --code-version, --code, or --from-code/--to-code."
        )

    timestamp = options.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidates = filter_download_candidates(load_available_records(settings, options), options)

    if options.dry_run:
        return DownloadSummary(
            timestamp=timestamp,
            planned=len(candidates),
            downloaded=0,
            skipped=0,
            partial=0,
            failed=0,
            dry_run=True,
            documents=[],
            candidates_preview=[record.requested_code_version for record in candidates[:20]],
        )

    if client is None:
        raise DownloadError("HTTP client is required unless --dry-run is used.")
    run_health_probe_if_needed(client, candidates, options)

    catalog_records = load_catalog_records(default_catalog_index_path(settings))
    documents: list[DownloadedDocumentSummary] = []
    for record in candidates:
        documents.append(
            download_one_json_document(settings, client, record, catalog_records, options)
        )

    counts = count_document_statuses(documents)
    return DownloadSummary(
        timestamp=timestamp,
        planned=len(candidates),
        downloaded=counts["downloaded"],
        skipped=counts["skipped"],
        partial=counts["partial"],
        failed=counts["failed"],
        dry_run=False,
        documents=documents,
        candidates_preview=[record.requested_code_version for record in candidates[:20]],
    )


def download_pdfs(
    settings: Settings,
    client: ClinrecApiClient | None,
    options: DownloadOptions,
) -> DownloadSummary:
    if not has_selection_filter(options):
        raise DownloadError(
            "Refusing to download PDFs without a filter. "
            "Use --all, --code-version, --code, or --from-code/--to-code."
        )

    timestamp = options.timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidates = filter_download_candidates(load_available_records(settings, options), options)
    if options.dry_run:
        return DownloadSummary(
            timestamp=timestamp,
            planned=len(candidates),
            downloaded=0,
            skipped=0,
            partial=0,
            failed=0,
            dry_run=True,
            documents=[],
            candidates_preview=[record.requested_code_version for record in candidates[:20]],
        )
    if client is None:
        raise DownloadError("HTTP client is required unless --dry-run is used.")
    run_health_probe_if_needed(client, candidates, options)

    catalog_records = load_catalog_records(default_catalog_index_path(settings))
    documents = [
        download_one_pdf_document(settings, client, record, catalog_records, options)
        for record in candidates
    ]
    counts = count_document_statuses(documents)
    return DownloadSummary(
        timestamp=timestamp,
        planned=len(candidates),
        downloaded=counts["downloaded"],
        skipped=counts["skipped"],
        partial=counts["partial"],
        failed=counts["failed"],
        dry_run=False,
        documents=documents,
        candidates_preview=[record.requested_code_version for record in candidates[:20]],
    )


def download_one_json_document(
    settings: Settings,
    client: ClinrecApiClient,
    record: VersionAvailabilityRecord,
    catalog_records: dict[str, dict[str, Any]],
    options: DownloadOptions,
) -> DownloadedDocumentSummary:
    document_dir = document_directory(settings, record)
    source_dir = document_dir / "source"
    for directory in (
        source_dir,
        document_dir / "parsed",
        document_dir / "assets",
        document_dir / "qa",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = document_dir / "manifest.json"
    manifest = read_manifest(manifest_path)

    catalog_record = catalog_records.get(record.requested_code_version)
    catalog_path = source_dir / "catalog-record.json"
    if catalog_record is None:
        catalog_path.write_text("null\n", encoding="utf-8")
        catalog_record_status = "missing"
    else:
        catalog_path.write_text(
            json.dumps(catalog_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        catalog_record_status = "saved"

    json_info = ensure_json_source(
        client,
        record,
        source_dir / "getclinrec.json",
        manifest.get("json"),
        options.force,
    )
    pdf_info = pdf_not_requested_info(source_dir / "official.pdf", manifest.get("pdf"))

    document_status = document_status_from_parts(json_info["status"], pdf_info["status"])
    manifest_payload = {
        "code": record.code,
        "version": record.version,
        "code_version": record.requested_code_version,
        "status": document_status,
        "catalog_record": {
            "path": "source/catalog-record.json",
            "status": catalog_record_status,
        },
        "json": json_info,
        "pdf": pdf_info,
    }
    write_json(manifest_path, manifest_payload)
    write_http_metadata(source_dir / "http-metadata.json", manifest_payload)

    return DownloadedDocumentSummary(
        code_version=record.requested_code_version,
        document_dir=document_dir,
        manifest_path=manifest_path,
        status=document_status,
        json_status=str(json_info["status"]),
        pdf_status=str(pdf_info["status"]),
    )


def download_one_pdf_document(
    settings: Settings,
    client: ClinrecApiClient,
    record: VersionAvailabilityRecord,
    catalog_records: dict[str, dict[str, Any]],
    options: DownloadOptions,
) -> DownloadedDocumentSummary:
    document_dir = document_directory(settings, record)
    source_dir = document_dir / "source"
    for directory in (
        source_dir,
        document_dir / "parsed",
        document_dir / "assets",
        document_dir / "qa",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = document_dir / "manifest.json"
    manifest = read_manifest(manifest_path)
    catalog_record = catalog_records.get(record.requested_code_version)
    catalog_path = source_dir / "catalog-record.json"
    if catalog_record is None:
        catalog_path.write_text("null\n", encoding="utf-8")
        catalog_record_status = "missing"
    else:
        catalog_path.write_text(
            json.dumps(catalog_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        catalog_record_status = "saved"

    json_info = json_manifest_or_missing(source_dir / "getclinrec.json", manifest.get("json"))
    pdf_info = ensure_pdf_source(
        client,
        record,
        source_dir / "official.pdf",
        manifest.get("pdf"),
        options.force,
    )
    document_status = pdf_info["status"]
    manifest_payload = {
        "code": record.code,
        "version": record.version,
        "code_version": record.requested_code_version,
        "status": document_status,
        "catalog_record": {
            "path": "source/catalog-record.json",
            "status": catalog_record_status,
        },
        "json": json_info,
        "pdf": pdf_info,
    }
    write_json(manifest_path, manifest_payload)
    write_http_metadata(source_dir / "http-metadata.json", manifest_payload)
    return DownloadedDocumentSummary(
        code_version=record.requested_code_version,
        document_dir=document_dir,
        manifest_path=manifest_path,
        status=document_status,
        json_status=str(json_info["status"]),
        pdf_status=str(pdf_info["status"]),
    )


def ensure_json_source(
    client: ClinrecApiClient,
    record: VersionAvailabilityRecord,
    path: Path,
    previous: Any,
    force: bool,
) -> dict[str, Any]:
    if not force and is_existing_file_valid(path, previous, kind="json"):
        return existing_file_info(path, previous, "already_valid")

    result = client.fetch_clinrec_payload(record.requested_code_version)
    if isinstance(result, ExternalApiError):
        return failed_info("source/getclinrec.json", "failed", result.message, result.status_code)

    part_path = path.with_suffix(path.suffix + ".part")
    write_part_bytes(part_path, result.raw_content)
    if not is_valid_json_file(part_path):
        part_path.unlink(missing_ok=True)
        return failed_info("source/getclinrec.json", "failed", "Downloaded JSON is invalid", None)
    candidate = VersionCandidate(code=record.code, version=record.version)
    validated = classify_success_payload(
        result,
        candidate,
        checked_at=utc_now(),
        attempts=result.attempts,
    )
    if validated.availability != VersionAvailability.AVAILABLE_JSON:
        part_path.unlink(missing_ok=True)
        return failed_info(
            "source/getclinrec.json",
            "failed",
            validated.error or "Downloaded JSON failed semantic validation",
            result.status_code,
        )
    part_path.replace(path)
    return file_info(path, "downloaded", fetched_at=utc_now())


def ensure_pdf_source(
    client: ClinrecApiClient,
    record: VersionAvailabilityRecord,
    path: Path,
    previous: Any,
    force: bool,
) -> dict[str, Any]:
    if not force and is_existing_file_valid(path, previous, kind="pdf"):
        return existing_file_info(path, previous, "already_valid")

    result = client.fetch_pdf(record.requested_code_version)
    if isinstance(result, ExternalApiError):
        status = "unavailable" if result.status_code in {403, 404} else "failed"
        return failed_info("source/official.pdf", status, result.message, result.status_code)

    part_path = path.with_suffix(path.suffix + ".part")
    write_part_bytes(part_path, result.content)
    if not is_valid_pdf_file(part_path):
        part_path.unlink(missing_ok=True)
        return failed_info(
            "source/official.pdf",
            "failed",
            "Downloaded PDF is invalid",
            result.status_code,
        )
    part_path.replace(path)
    return file_info(path, "downloaded", fetched_at=utc_now())


def load_available_records(
    settings: Settings,
    options: DownloadOptions | None = None,
) -> list[VersionAvailabilityRecord]:
    index_path = settings.paths.indexes / "version-availability.jsonl"
    if not index_path.exists():
        raise DownloadError(
            "version-availability.jsonl is missing. Run 'clinrec discover-versions' first."
        )
    retry_failed = bool(options and options.retry_failed)
    records = [
        record
        for record in read_availability_index(index_path).values()
        if record.availability == VersionAvailability.AVAILABLE_JSON
        or (retry_failed and record.availability in TEMPORARY_AVAILABILITY)
    ]
    return sorted(records, key=lambda item: (item.code, item.version))


def filter_download_candidates(
    records: list[VersionAvailabilityRecord],
    options: DownloadOptions,
) -> list[VersionAvailabilityRecord]:
    filtered = records
    if options.code_versions:
        selected = set(options.code_versions)
        filtered = [record for record in filtered if record.requested_code_version in selected]
    if options.code is not None:
        filtered = [record for record in filtered if record.code == options.code]
    if options.from_code is not None:
        filtered = [record for record in filtered if record.code >= options.from_code]
    if options.to_code is not None:
        filtered = [record for record in filtered if record.code <= options.to_code]
    return filtered


def run_health_probe_if_needed(
    client: ClinrecApiClient,
    candidates: list[VersionAvailabilityRecord],
    options: DownloadOptions,
) -> None:
    if not options.all_versions and len(candidates) <= 20:
        return
    probe_error = client.health_probe()
    if probe_error is not None:
        raise DownloadError(f"Health probe failed before download: {probe_error.message}")


def document_directory(settings: Settings, record: VersionAvailabilityRecord) -> Path:
    return settings.paths.documents / str(record.code) / record.requested_code_version


def has_selection_filter(options: DownloadOptions) -> bool:
    return bool(options.all_versions or options.code_versions) or any(
        value is not None for value in (options.code, options.from_code, options.to_code)
    )


def load_catalog_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows = read_jsonl(path)
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        code_version = row.get("code_version")
        if isinstance(code_version, str):
            records[code_version] = row
    return records


def default_catalog_index_path(settings: Settings) -> Path:
    for name in ("catalog-all-statuses.jsonl", "catalog-active.jsonl", "catalog.jsonl"):
        path = settings.paths.indexes / name
        if path.exists():
            return path
    return settings.paths.indexes / "catalog-all-statuses.jsonl"


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def file_info(path: Path, status: str, *, fetched_at: str) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": normalized_source_path(path),
        "sha256": sha256_bytes(content),
        "size": len(content),
        "fetched_at": fetched_at,
        "status": status,
    }


def existing_file_info(path: Path, previous: Any, status: str) -> dict[str, Any]:
    content = path.read_bytes()
    fetched_at = previous.get("fetched_at") if isinstance(previous, dict) else None
    return {
        "path": normalized_source_path(path),
        "sha256": sha256_bytes(content),
        "size": len(content),
        "fetched_at": fetched_at,
        "status": status,
    }


def failed_info(
    relative_path: str,
    status: str,
    error: str,
    http_status: int | None,
) -> dict[str, Any]:
    return {
        "path": relative_path,
        "sha256": None,
        "size": 0,
        "fetched_at": utc_now(),
        "status": status,
        "http_status": http_status,
        "error": error,
    }


def json_manifest_or_missing(path: Path, previous: Any) -> dict[str, Any]:
    if is_existing_file_valid(path, previous, kind="json"):
        return existing_file_info(path, previous, "already_valid")
    return {
        "path": normalized_source_path(path),
        "sha256": None,
        "size": 0,
        "fetched_at": None,
        "status": "missing",
    }


def pdf_not_requested_info(path: Path, previous: Any) -> dict[str, Any]:
    if is_existing_file_valid(path, previous, kind="pdf"):
        status = "already_valid" if isinstance(previous, dict) else "downloaded"
        return existing_file_info(path, previous, status)
    return {
        "path": normalized_source_path(path),
        "sha256": None,
        "size": 0,
        "fetched_at": None,
        "status": "not_requested",
    }


def normalized_source_path(path: Path) -> str:
    return f"source/{path.name}"


def is_existing_file_valid(path: Path, previous: Any, *, kind: str) -> bool:
    if not path.exists() or not isinstance(previous, dict):
        return False
    if kind == "json" and not is_valid_json_file(path):
        return False
    if kind == "pdf" and not is_valid_pdf_file(path):
        return False
    content = path.read_bytes()
    return previous.get("sha256") == sha256_bytes(content) and previous.get("size") == len(content)


def is_valid_json_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def is_valid_pdf_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    prefix = path.read_bytes()[:5]
    return prefix == b"%PDF-"


def write_part_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def document_status_from_parts(json_status: str, pdf_status: str) -> str:
    _ = pdf_status
    if json_status in {"downloaded", "already_valid"}:
        return "downloaded"
    return "failed"


def count_document_statuses(documents: list[DownloadedDocumentSummary]) -> dict[str, int]:
    counts = {"downloaded": 0, "skipped": 0, "partial": 0, "failed": 0}
    for document in documents:
        if document.status == "downloaded":
            if "downloaded" in {document.json_status, document.pdf_status}:
                counts["downloaded"] += 1
            else:
                counts["skipped"] += 1
        elif document.status == "already_valid":
            counts["skipped"] += 1
        elif document.status == "not_requested":
            counts["skipped"] += 1
        elif document.status == "partial":
            counts["partial"] += 1
        else:
            counts["failed"] += 1
    return counts


def write_http_metadata(path: Path, manifest_payload: dict[str, Any]) -> None:
    metadata = {
        "code": manifest_payload["code"],
        "version": manifest_payload["version"],
        "code_version": manifest_payload["code_version"],
        "json": {
            "status": manifest_payload["json"].get("status"),
            "size": manifest_payload["json"].get("size"),
            "fetched_at": manifest_payload["json"].get("fetched_at"),
        },
        "pdf": {
            "status": manifest_payload["pdf"].get("status"),
            "size": manifest_payload["pdf"].get("size"),
            "fetched_at": manifest_payload["pdf"].get("fetched_at"),
            "http_status": manifest_payload["pdf"].get("http_status"),
            "error": manifest_payload["pdf"].get("error"),
        },
    }
    write_json(path, metadata)
