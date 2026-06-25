from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clinrec.api.catalog_sync import validate_reference_organizations, write_json
from clinrec.api.client import ClinrecApiClient, JsonPayloadResult
from clinrec.bank.common import (
    BankError,
    BankRecordFilter,
    append_checkpoint,
    bank_references_root,
    compact_timestamp,
    current_dir,
    existing_manifest_matches,
    first_present,
    list_value,
    load_catalog_records,
    manifest_for_raw_json,
    minimal_validate_raw_document,
    read_json_file,
    read_jsonl,
    refresh_bank_manifest,
    selected_active_records,
    sha256_bytes,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.models.external import ExternalApiError, NkoListResponse, ReferenceOrganization


@dataclass(frozen=True)
class BankDownloadDocumentSummary:
    code_version: str
    document_dir: Path
    manifest_path: Path
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BankDownloadSummary:
    timestamp: str
    planned: int
    downloaded: int
    skipped: int
    failed: int
    dry_run: bool
    documents: list[BankDownloadDocumentSummary]
    candidates_preview: list[str]
    references_index_path: Path | None = None


def download_current_documents(
    settings: Any,
    client: ClinrecApiClient | None,
    options: BankRecordFilter,
) -> BankDownloadSummary:
    timestamp = compact_timestamp(options.timestamp)
    records = selected_active_records(settings, options)
    preview = [string_value(record.get("code_version")) for record in records[:20]]
    if options.dry_run:
        return BankDownloadSummary(
            timestamp=timestamp,
            planned=len(records),
            downloaded=0,
            skipped=0,
            failed=0,
            dry_run=True,
            documents=[],
            candidates_preview=preview,
        )
    if client is None:
        raise BankError("HTTP client is required unless --dry-run is used.")

    nko_index_path, nko_index = ensure_bank_references(settings, client, force=options.force)
    documents: list[BankDownloadDocumentSummary] = []
    for record in records:
        code_version = string_value(record.get("code_version"))
        try:
            document = download_one_current(settings, client, record, nko_index, options)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            document = failed_current_summary(settings, code_version, str(exc))
        documents.append(document)
        append_checkpoint(
            settings,
            "bank-download-current",
            {
                "code_version": code_version,
                "status": document.status,
                "error": document.error,
            },
        )
        if document.status == "circuit_open":
            break

    return BankDownloadSummary(
        timestamp=timestamp,
        planned=len(records),
        downloaded=sum(1 for document in documents if document.status == "downloaded"),
        skipped=sum(1 for document in documents if document.status == "already_valid"),
        failed=sum(
            1
            for document in documents
            if document.status not in {"downloaded", "already_valid"}
        ),
        dry_run=False,
        documents=documents,
        candidates_preview=preview,
        references_index_path=nko_index_path,
    )


def ensure_bank_references(
    settings: Any,
    client: ClinrecApiClient,
    *,
    force: bool,
) -> tuple[Path, dict[str, dict[str, Any]]]:
    references_dir = bank_references_root(settings)
    raw_path = references_dir / "getnkolist.json"
    index_path = references_dir / "nko.jsonl"
    if not force and raw_path.exists() and index_path.exists():
        return index_path, nko_rows_by_id(read_jsonl(index_path))

    result = client.fetch_nko_list_payload()
    if isinstance(result, ExternalApiError):
        raise BankError(f"GetNkoList failed: {result.message}")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(result.raw_content)
    try:
        response = NkoListResponse.model_validate(result.payload)
    except ValidationError as exc:
        raise BankError(f"GetNkoList response validation failed: {exc.errors()}") from exc

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
    write_jsonl(index_path, rows)
    write_json(
        references_dir / "manifest.json",
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
    return index_path, nko_rows_by_id(rows)


def download_one_current(
    settings: Any,
    client: ClinrecApiClient,
    catalog_record: dict[str, Any],
    nko_index: dict[str, dict[str, Any]],
    options: BankRecordFilter,
) -> BankDownloadDocumentSummary:
    code_version = string_value(catalog_record.get("code_version"))
    if not code_version:
        raise BankError("Catalog record does not contain code_version")
    target_dir = current_dir(settings, code_version)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "getclinrec.json"
    manifest_path = target_dir / "manifest.json"
    manifest = read_json_file(manifest_path)

    if not options.force and existing_manifest_matches(target_path, manifest):
        content = target_path.read_bytes()
        info, errors = minimal_validate_raw_document(content, expected_code_version=code_version)
        if info is not None and not errors:
            write_current_sidecars(target_dir, catalog_record, info, nko_index)
            refresh_bank_manifest(settings, code_version, current_status="valid")
            return BankDownloadDocumentSummary(
                code_version=code_version,
                document_dir=target_dir.parent,
                manifest_path=manifest_path,
                status="already_valid",
            )

    result = client.fetch_clinrec_payload(code_version)
    if isinstance(result, ExternalApiError):
        status = "circuit_open" if result.kind.value == "circuit_open" else "failed"
        write_json(
            manifest_path,
            {
                "code_version": code_version,
                "source": "GetClinrec2",
                "http_status": result.status_code,
                "content_type": result.content_type,
                "size": 0,
                "sha256": None,
                "downloaded_at": utc_now(),
                "validation": status,
                "error": result.message,
            },
        )
        refresh_bank_manifest(settings, code_version, current_status=status)
        return BankDownloadDocumentSummary(
            code_version=code_version,
            document_dir=target_dir.parent,
            manifest_path=manifest_path,
            status=status,
            error=result.message,
        )

    part_path = target_path.with_suffix(target_path.suffix + ".part")
    part_path.write_bytes(result.raw_content)
    info, errors = minimal_validate_raw_document(
        part_path.read_bytes(),
        expected_code_version=code_version,
    )
    if info is None:
        part_path.unlink(missing_ok=True)
        error = "; ".join(errors)
        write_json(
            manifest_path,
            manifest_for_raw_json(
                code_version=code_version,
                code=int(catalog_record.get("code") or 0),
                version=int(catalog_record.get("version") or 0),
                status=None,
                source="GetClinrec2",
                http_status=result.status_code,
                content_type=result.content_type,
                raw_content=result.raw_content,
                validation="invalid",
                error=error,
            ),
        )
        refresh_bank_manifest(settings, code_version, current_status="invalid")
        return BankDownloadDocumentSummary(
            code_version=code_version,
            document_dir=target_dir.parent,
            manifest_path=manifest_path,
            status="failed",
            error=error,
        )

    part_path.replace(target_path)
    write_json(
        manifest_path,
        manifest_for_raw_json(
            code_version=code_version,
            code=info.code,
            version=info.version,
            status=info.status,
            source="GetClinrec2",
            http_status=result.status_code,
            content_type=result.content_type,
            raw_content=result.raw_content,
            validation="valid",
        ),
    )
    write_current_sidecars(target_dir, catalog_record, info, nko_index)
    refresh_bank_manifest(settings, code_version, current_status="valid")
    return BankDownloadDocumentSummary(
        code_version=code_version,
        document_dir=target_dir.parent,
        manifest_path=manifest_path,
        status="downloaded",
    )


def write_current_sidecars(
    target_dir: Path,
    catalog_record: dict[str, Any],
    info: Any,
    nko_index: dict[str, dict[str, Any]],
) -> None:
    write_json(target_dir / "catalog-record.json", catalog_record)
    association_ids = info.association_ids
    resolved: list[dict[str, Any]] = []
    unresolved: list[Any] = []
    for association_id in association_ids:
        row = nko_index.get(str(association_id))
        if row is None:
            unresolved.append(association_id)
        else:
            resolved.append(row)
    write_json(
        target_dir / "developers.json",
        {
            "catalog_developers": list_value(catalog_record.get("developers")),
            "association_ids": association_ids,
            "resolved_associations": resolved,
            "unresolved_association_ids": unresolved,
        },
    )


def failed_current_summary(
    settings: Any,
    code_version: str,
    error: str,
) -> BankDownloadDocumentSummary:
    target_dir = current_dir(settings, code_version)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.json"
    write_json(
        manifest_path,
        {
            "code_version": code_version,
            "source": "GetClinrec2",
            "downloaded_at": utc_now(),
            "validation": "failed",
            "error": error,
        },
    )
    refresh_bank_manifest(settings, code_version, current_status="failed")
    return BankDownloadDocumentSummary(
        code_version=code_version,
        document_dir=target_dir.parent,
        manifest_path=manifest_path,
        status="failed",
        error=error,
    )


def nko_rows_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        organization_id = first_present(row, "id", "Id", "ID")
        if organization_id is not None:
            result[str(organization_id)] = row
    return result


def raw_result_from_fixture(
    code_version: str,
    content: bytes,
    *,
    content_type: str = "application/json",
) -> JsonPayloadResult:
    payload = json.loads(content.decode("utf-8"))
    return JsonPayloadResult(
        endpoint="GetClinrec2",
        status_code=200,
        content_type=content_type,
        payload=payload,
        raw_content=content,
        response_size_bytes=len(content),
        duration_seconds=0.0,
        code_version=code_version,
    )


def active_catalog_total(settings: Any) -> int:
    return len(load_catalog_records(settings, active=True))
