from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.api.client import ClinrecApiClient, JsonPayloadResult
from clinrec.bank.common import (
    BankError,
    BankRecordFilter,
    add_catalog_status_fields,
    append_checkpoint,
    bank_root,
    catalog_record_for_bank,
    compact_timestamp,
    current_dir,
    db_id_state,
    existing_manifest_matches,
    load_catalog_records,
    manifest_for_raw_json,
    minimal_validate_raw_document,
    parse_code_version_or_raise,
    read_json_file,
    refresh_bank_manifest,
    selected_active_records,
    source_record_id_from_catalog,
    string_value,
    utc_now,
)
from clinrec.models.external import ExternalApiError


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
    *,
    destination_root: Path | None = None,
    records_override: list[dict[str, Any]] | None = None,
) -> BankDownloadSummary:
    timestamp = compact_timestamp(options.timestamp)
    records = (
        records_override
        if records_override is not None
        else selected_active_records(settings, options)
    )
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

    effective_destination_root = destination_root
    if destination_root is None and not options.unsafe_direct_active_write:
        effective_destination_root = (
            bank_root(settings) / "maintenance" / "download-current" / timestamp
        )

    documents: list[BankDownloadDocumentSummary] = []
    for record in records:
        code_version = string_value(record.get("code_version"))
        try:
            document = download_one_current(
                settings,
                client,
                record,
                options,
                destination_root=effective_destination_root,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            document = failed_current_summary(
                settings,
                code_version,
                str(exc),
                destination_root=effective_destination_root,
            )
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
        references_index_path=None,
    )


def download_one_current(
    settings: Any,
    client: ClinrecApiClient,
    catalog_record: dict[str, Any],
    options: BankRecordFilter,
    *,
    destination_root: Path | None = None,
) -> BankDownloadDocumentSummary:
    code_version = string_value(catalog_record.get("code_version"))
    if not code_version:
        raise BankError("Catalog record does not contain code_version")
    target_dir = target_current_dir(settings, code_version, destination_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "getclinrec.json"
    manifest_path = target_dir / "manifest.json"
    manifest = read_json_file(manifest_path)
    bank_catalog_record = catalog_record_for_bank(catalog_record)
    catalog_source_record_id = source_record_id_from_catalog(bank_catalog_record)

    if not options.force and existing_manifest_matches(target_path, manifest):
        content = target_path.read_bytes()
        info, errors = minimal_validate_raw_document(content, expected_code_version=code_version)
        if info is not None and not errors:
            write_current_sidecars(target_dir, bank_catalog_record, info)
            write_json(
                manifest_path,
                add_catalog_status_fields(
                    {
                        **manifest,
                        "catalog_source_record_id": catalog_source_record_id,
                        "document_db_id": info.db_id,
                        "db_id_state": db_id_state(catalog_source_record_id, info.db_id),
                        "db_id_match": catalog_source_record_id == info.db_id
                        if catalog_source_record_id is not None and info.db_id is not None
                        else None,
                    },
                    bank_catalog_record,
                    info.payload,
                ),
            )
            write_bank_manifest(
                settings,
                code_version,
                target_dir.parent,
                "valid",
                destination_root,
            )
            return BankDownloadDocumentSummary(
                code_version=code_version,
                document_dir=target_dir.parent,
                manifest_path=manifest_path,
                status="already_valid",
            )

    result = client.fetch_clinrec_payload(code_version)
    if isinstance(result, ExternalApiError):
        status = "circuit_open" if result.kind.value == "circuit_open" else "failed"
        write_attempt(
            target_dir,
            endpoint=result.endpoint,
            http_status=result.status_code,
            error_kind=result.kind.value,
            message=result.message,
            content_type=result.content_type,
            response_size=result.response_size_bytes,
            attempts=result.attempts,
            retry_after=result.retry_after,
            safe_body_preview=result.safe_body_preview,
        )
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
        write_attempt(
            target_dir,
            endpoint=result.endpoint,
            http_status=result.status_code,
            error_kind="validation_failure",
            message=error,
            content_type=result.content_type,
            response_size=result.response_size_bytes,
            attempts=result.attempts,
            retry_after=None,
            safe_body_preview=result.raw_content[:4096].decode("utf-8", errors="replace"),
        )
        return BankDownloadDocumentSummary(
            code_version=code_version,
            document_dir=target_dir.parent,
            manifest_path=manifest_path,
            status="failed",
            error=error,
        )

    if destination_root is None and options.force and target_path.exists():
        ensure_direct_identity_is_unchanged(target_path, result.raw_content, code_version)

    history_path = preserve_silent_source_change(
        settings,
        target_path,
        result.raw_content,
        code_version,
        options.force,
    )
    part_path.replace(target_path)
    write_json(
        manifest_path,
        add_catalog_status_fields(
            {
                **manifest_for_raw_json(
                    code_version=code_version,
                    code=info.code,
                    version=info.version,
                    status=info.status,
                    source="GetClinrec2",
                    http_status=result.status_code,
                    content_type=result.content_type,
                    raw_content=result.raw_content,
                    validation="valid",
                    catalog_source_record_id=catalog_source_record_id,
                    document_db_id=info.db_id,
                ),
                **(
                    {
                        "silent_source_change": True,
                        "previous_raw_path": history_path.as_posix(),
                    }
                    if history_path is not None
                    else {}
                ),
            },
            bank_catalog_record,
            info.payload,
        ),
    )
    write_current_sidecars(target_dir, bank_catalog_record, info)
    write_bank_manifest(settings, code_version, target_dir.parent, "valid", destination_root)
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
) -> None:
    _ = info
    write_json(target_dir / "catalog-record.json", catalog_record)


def failed_current_summary(
    settings: Any,
    code_version: str,
    error: str,
    *,
    destination_root: Path | None = None,
) -> BankDownloadDocumentSummary:
    target_dir = target_current_dir(settings, code_version, destination_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.json"
    write_attempt(
        target_dir,
        endpoint="GetClinrec2",
        http_status=None,
        error_kind="exception",
        message=error,
        content_type=None,
        response_size=0,
        attempts=0,
        retry_after=None,
        safe_body_preview=None,
    )
    return BankDownloadDocumentSummary(
        code_version=code_version,
        document_dir=target_dir.parent,
        manifest_path=manifest_path,
        status="failed",
        error=error,
    )


def write_attempt(
    target_dir: Path,
    *,
    endpoint: str,
    http_status: int | None,
    error_kind: str,
    message: str,
    content_type: str | None,
    response_size: int,
    attempts: int,
    retry_after: str | None,
    safe_body_preview: str | None,
) -> Path:
    attempts_dir = target_dir / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempt_path = attempts_dir / f"{compact_timestamp()}.json"
    write_json(
        attempt_path,
        {
            "attempted_at": utc_now(),
            "endpoint": endpoint,
            "http_status": http_status,
            "error_kind": error_kind,
            "message": message,
            "content_type": content_type,
            "response_size": response_size,
            "attempts": attempts,
            "retry_after": retry_after,
            "safe_body_preview": safe_body_preview,
        },
    )
    return attempt_path


def target_current_dir(
    settings: Any,
    code_version: str,
    destination_root: Path | None,
) -> Path:
    if destination_root is None:
        return current_dir(settings, code_version)
    return destination_root / code_version / "current"


def write_bank_manifest(
    settings: Any,
    code_version: str,
    document_dir: Path,
    current_status: str,
    destination_root: Path | None,
) -> None:
    if destination_root is None:
        refresh_bank_manifest(settings, code_version, current_status=current_status)
        return
    code, version = parse_code_version_or_raise(code_version)
    write_json(
        document_dir / "bank-manifest.json",
        {
            "code_version": code_version,
            "code": code,
            "version": version,
            "current_status": current_status,
            "previous_status": "not_checked",
            "pdf_status": "not_requested",
            "updated_at": utc_now(),
        },
    )


def preserve_silent_source_change(
    settings: Any,
    target_path: Path,
    new_content: bytes,
    code_version: str,
    force: bool,
) -> Path | None:
    if not force or not target_path.exists():
        return None
    old_content = target_path.read_bytes()
    if old_content == new_content:
        return None
    old_info, old_errors = minimal_validate_raw_document(
        old_content,
        expected_code_version=code_version,
    )
    new_info, new_errors = minimal_validate_raw_document(
        new_content,
        expected_code_version=code_version,
    )
    if old_info is None or new_info is None or old_errors or new_errors:
        return None
    if old_info.db_id != new_info.db_id or old_info.code_version != new_info.code_version:
        return None
    history_root = (
        settings.paths.data_root
        / "bank"
        / "history"
        / code_version
        / compact_timestamp()
        / "direct-force-replacement"
    )
    history_path = history_root / "getclinrec.json"
    history_root.mkdir(parents=True, exist_ok=True)
    history_path.write_bytes(old_content)
    current_root = target_path.parent
    document_root = current_root.parent
    for name in ("manifest.json", "catalog-record.json"):
        source = current_root / name
        if source.exists():
            (history_root / name).write_bytes(source.read_bytes())
    bank_manifest = document_root / "bank-manifest.json"
    if bank_manifest.exists():
        (history_root / "bank-manifest.json").write_bytes(bank_manifest.read_bytes())
    write_json(
        history_root / "event.json",
        {
            "event_type": "direct_force_replacement",
            "code_version": code_version,
            "recorded_at": utc_now(),
            "old_sha256": manifest_for_raw_json(
                code_version=code_version,
                code=old_info.code,
                version=old_info.version,
                status=old_info.status,
                source="GetClinrec2",
                http_status=200,
                content_type="application/json",
                raw_content=old_content,
                validation="valid",
                document_db_id=old_info.db_id,
            )["sha256"],
            "new_sha256": manifest_for_raw_json(
                code_version=code_version,
                code=new_info.code,
                version=new_info.version,
                status=new_info.status,
                source="GetClinrec2",
                http_status=200,
                content_type="application/json",
                raw_content=new_content,
                validation="valid",
                document_db_id=new_info.db_id,
            )["sha256"],
        },
    )
    return Path(history_path)


def ensure_direct_identity_is_unchanged(
    target_path: Path,
    new_content: bytes,
    code_version: str,
) -> None:
    old_content = target_path.read_bytes()
    old_info, old_errors = minimal_validate_raw_document(
        old_content,
        expected_code_version=code_version,
    )
    new_info, new_errors = minimal_validate_raw_document(
        new_content,
        expected_code_version=code_version,
    )
    if old_info is None or new_info is None or old_errors or new_errors:
        return
    if old_info.db_id != new_info.db_id:
        raise BankError(
            "Refusing unsafe direct active replacement because document db_id changed."
        )


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
