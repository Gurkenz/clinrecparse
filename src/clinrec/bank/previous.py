from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from clinrec.api.catalog_sync import to_int, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    BankError,
    BankRecordFilter,
    append_checkpoint,
    catalog_record_for_bank,
    current_dir,
    existing_manifest_matches,
    first_non_empty,
    first_present,
    list_value,
    load_catalog_by_code_version,
    manifest_for_raw_json,
    minimal_validate_raw_document,
    previous_candidate_dir,
    previous_dir,
    read_json_file,
    refresh_bank_manifest,
    selected_active_records,
    source_record_id_from_catalog,
    string_value,
    utc_now,
)
from clinrec.models.external import ApiErrorKind, ExternalApiError


@dataclass(frozen=True)
class BankPreviousDocumentSummary:
    code_version: str
    previous_code_version: str | None
    relation_status: str
    relation_path: Path
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BankPreviousSummary:
    timestamp: str
    planned: int
    checked: int
    skipped: int
    failed: int
    dry_run: bool
    documents: list[BankPreviousDocumentSummary]
    candidates_preview: list[str]


def check_previous_documents(
    settings: Any,
    client: ClinrecApiClient | None,
    options: BankRecordFilter,
) -> BankPreviousSummary:
    records = selected_active_records(settings, options)
    preview = previous_candidates_preview(records)
    if options.dry_run:
        return BankPreviousSummary(
            timestamp=options.timestamp or utc_now(),
            planned=len(records),
            checked=0,
            skipped=0,
            failed=0,
            dry_run=True,
            documents=[],
            candidates_preview=preview,
        )
    if client is None:
        raise BankError("HTTP client is required unless --dry-run is used.")

    all_catalog = load_catalog_by_code_version(settings, active=False)
    documents: list[BankPreviousDocumentSummary] = []
    for record in records:
        code_version = string_value(record.get("code_version"))
        try:
            document = check_one_previous(settings, client, record, all_catalog, options)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            document = write_relation_failure(settings, code_version, str(exc))
        documents.append(document)
        append_checkpoint(
            settings,
            "bank-check-previous",
            {
                "code_version": code_version,
                "previous_code_version": document.previous_code_version,
                "relation_status": document.relation_status,
                "status": document.status,
                "error": document.error,
            },
        )
        if document.status == "circuit_open":
            break

    return BankPreviousSummary(
        timestamp=options.timestamp or utc_now(),
        planned=len(records),
        checked=sum(1 for document in documents if document.status == "checked"),
        skipped=sum(1 for document in documents if document.status == "skipped"),
        failed=sum(1 for document in documents if document.status not in {"checked", "skipped"}),
        dry_run=False,
        documents=documents,
        candidates_preview=preview,
    )


def check_one_previous(
    settings: Any,
    client: ClinrecApiClient,
    current_record: dict[str, Any],
    all_catalog: dict[str, dict[str, Any]],
    options: BankRecordFilter,
) -> BankPreviousDocumentSummary:
    current_code_version = string_value(current_record.get("code_version"))
    relation_path = previous_dir(settings, current_code_version) / "relation.json"
    relation_path.parent.mkdir(parents=True, exist_ok=True)
    existing_relation = read_json_file(relation_path)
    if (
        existing_relation
        and not options.force
        and (
            existing_relation.get("relation_status") != "previous_temporary_failure"
            or not options.retry_failed
        )
    ):
        refresh_bank_manifest(
            settings,
            current_code_version,
            previous_status=string_value(existing_relation.get("relation_status")),
        )
        return BankPreviousDocumentSummary(
            code_version=current_code_version,
            previous_code_version=nullable_string(existing_relation.get("previous_code_version")),
            relation_status=string_value(existing_relation.get("relation_status")),
            relation_path=relation_path,
            status="skipped",
        )

    previous_code_version = previous_code_version_for_record(current_record)
    if previous_code_version is None:
        relation = base_relation(current_code_version, None, current_record)
        relation["relation_status"] = "no_lower_version"
        relation["warnings"] = []
        write_json(relation_path, relation)
        refresh_bank_manifest(settings, current_code_version, previous_status="no_lower_version")
        return BankPreviousDocumentSummary(
            code_version=current_code_version,
            previous_code_version=None,
            relation_status="no_lower_version",
            relation_path=relation_path,
            status="checked",
        )

    current_json_path = current_dir(settings, current_code_version) / "getclinrec.json"
    if not current_json_path.exists():
        relation = base_relation(current_code_version, previous_code_version, current_record)
        relation.update(
            {
                "relation_status": "current_missing",
                "warnings": ["missing_current_json"],
            }
        )
        write_json(relation_path, relation)
        refresh_bank_manifest(settings, current_code_version, previous_status="current_missing")
        return BankPreviousDocumentSummary(
            code_version=current_code_version,
            previous_code_version=previous_code_version,
            relation_status="current_missing",
            relation_path=relation_path,
            status="failed",
            error="current/getclinrec.json is missing",
        )

    current_info, current_errors = minimal_validate_raw_document(
        current_json_path.read_bytes(),
        expected_code_version=current_code_version,
    )
    if current_info is None:
        relation = base_relation(current_code_version, previous_code_version, current_record)
        relation.update(
            {
                "relation_status": "current_invalid",
                "warnings": ["invalid_current_json"],
                "error": "; ".join(current_errors),
            }
        )
        write_json(relation_path, relation)
        refresh_bank_manifest(settings, current_code_version, previous_status="current_invalid")
        return BankPreviousDocumentSummary(
            code_version=current_code_version,
            previous_code_version=previous_code_version,
            relation_status="current_invalid",
            relation_path=relation_path,
            status="failed",
            error="; ".join(current_errors),
        )

    target_dir = previous_candidate_dir(settings, current_code_version, previous_code_version)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "getclinrec.json"
    manifest_path = target_dir / "manifest.json"
    previous_catalog = catalog_record_for_bank(all_catalog.get(previous_code_version, {}))
    previous_info = None
    previous_errors: list[str] = []
    result_status = "checked"

    manifest = read_json_file(manifest_path)
    if not options.force and existing_manifest_matches(target_path, manifest):
        previous_info, previous_errors = minimal_validate_raw_document(
            target_path.read_bytes(),
            expected_code_version=previous_code_version,
        )
    else:
        result = client.fetch_clinrec_payload(previous_code_version)
        if isinstance(result, ExternalApiError):
            relation_status = relation_status_for_error(result)
            relation = base_relation(current_code_version, previous_code_version, current_record)
            relation.update(
                {
                    "current_status": current_info.status,
                    "previous_status": result.status_code,
                    "relation_status": relation_status,
                    "warnings": warnings_for_unavailable(relation_status),
                    "error": result.message,
                    "http_status": result.status_code,
                    "api_error_kind": result.kind.value,
                }
            )
            write_json(relation_path, relation)
            refresh_bank_manifest(settings, current_code_version, previous_status=relation_status)
            return BankPreviousDocumentSummary(
                code_version=current_code_version,
                previous_code_version=previous_code_version,
                relation_status=relation_status,
                relation_path=relation_path,
                status="circuit_open" if result.kind == ApiErrorKind.CIRCUIT_OPEN else "checked",
                error=result.message,
            )

        part_path = target_path.with_suffix(target_path.suffix + ".part")
        part_path.write_bytes(result.raw_content)
        previous_info, previous_errors = minimal_validate_raw_document(
            part_path.read_bytes(),
            expected_code_version=previous_code_version,
        )
        if previous_info is None:
            part_path.unlink(missing_ok=True)
            relation_status = "previous_temporary_failure"
            relation = base_relation(current_code_version, previous_code_version, current_record)
            relation.update(
                {
                    "current_status": current_info.status,
                    "previous_status": None,
                    "relation_status": relation_status,
                    "warnings": ["previous_invalid_json"],
                    "error": "; ".join(previous_errors),
                }
            )
            write_json(relation_path, relation)
            refresh_bank_manifest(settings, current_code_version, previous_status=relation_status)
            return BankPreviousDocumentSummary(
                code_version=current_code_version,
                previous_code_version=previous_code_version,
                relation_status=relation_status,
                relation_path=relation_path,
                status=result_status,
                error="; ".join(previous_errors),
            )
        part_path.replace(target_path)
        write_json(
            manifest_path,
            manifest_for_raw_json(
                code_version=previous_code_version,
                code=previous_info.code,
                version=previous_info.version,
                status=previous_info.status,
                source="GetClinrec2",
                http_status=result.status_code,
                content_type=result.content_type,
                raw_content=result.raw_content,
                validation="valid",
                catalog_source_record_id=source_record_id_from_catalog(previous_catalog),
                document_db_id=previous_info.db_id,
            ),
        )
        write_json(target_dir / "catalog-record.json", previous_catalog or None)

    if previous_info is None:
        relation_status = "previous_temporary_failure"
        relation = base_relation(current_code_version, previous_code_version, current_record)
        relation.update(
            {
                "current_status": current_info.status,
                "previous_status": None,
                "relation_status": relation_status,
                "warnings": ["previous_invalid_json"],
                "error": "; ".join(previous_errors),
            }
        )
    else:
        relation = build_relation(
            current_info=current_info,
            previous_info=previous_info,
            current_catalog=current_record,
            previous_catalog=previous_catalog,
        )
        relation_status = string_value(relation["relation_status"])

    write_json(relation_path, relation)
    refresh_bank_manifest(settings, current_code_version, previous_status=relation_status)
    return BankPreviousDocumentSummary(
        code_version=current_code_version,
        previous_code_version=previous_code_version,
        relation_status=relation_status,
        relation_path=relation_path,
        status=result_status,
    )


def previous_code_version_for_record(record: dict[str, Any]) -> str | None:
    code = to_int(record.get("code"))
    version = to_int(record.get("version"))
    if code is None or version is None or version <= 1:
        return None
    return f"{code}_{version - 1}"


def previous_candidates_preview(records: list[dict[str, Any]]) -> list[str]:
    preview: list[str] = []
    for record in records[:20]:
        code_version = previous_code_version_for_record(record)
        if code_version is not None:
            preview.append(code_version)
    return preview


def build_relation(
    *,
    current_info: Any,
    previous_info: Any,
    current_catalog: dict[str, Any],
    previous_catalog: dict[str, Any],
) -> dict[str, Any]:
    code_equal = current_info.code == previous_info.code
    version_delta = current_info.version - previous_info.version
    adult_equal = equality_or_unknown(current_info.adult, previous_info.adult)
    child_equal = equality_or_unknown(current_info.child, previous_info.child)
    age_category_equal = equality_or_unknown(
        first_non_empty(current_info.age_category, current_catalog.get("age_category")),
        first_non_empty(previous_info.age_category, previous_catalog.get("age_category")),
    )
    title_similarity = round(float(fuzz.token_set_ratio(current_info.name, previous_info.name)), 1)
    mkb_overlap = overlap_ratio(
        normalized_values(first_non_empty(current_info.mkbs, current_catalog.get("mkbs"))),
        normalized_values(first_non_empty(previous_info.mkbs, previous_catalog.get("mkbs"))),
    )
    developer_overlap = overlap_ratio(
        normalized_values(current_catalog.get("developers")),
        normalized_values(previous_catalog.get("developers")),
    )
    warnings: list[str] = []
    relation_status = classify_relation(
        current_status=current_info.status,
        previous_status=previous_info.status,
        code_equal=code_equal,
        version_delta=version_delta,
        age_conflict=False in {adult_equal, child_equal, age_category_equal},
        title_similarity=title_similarity,
        mkb_overlap=mkb_overlap,
        warnings=warnings,
    )
    return {
        "current_code_version": current_info.code_version,
        "previous_code_version": previous_info.code_version,
        "current_status": current_info.status,
        "previous_status": previous_info.status,
        "current_status_class": status_class(current_info.status),
        "previous_status_class": status_class(previous_info.status),
        "relation_status": relation_status,
        "code_equal": code_equal,
        "version_delta": version_delta,
        "adult_equal": adult_equal,
        "child_equal": child_equal,
        "age_category_equal": age_category_equal,
        "title_similarity": title_similarity,
        "mkb_overlap": mkb_overlap,
        "developer_overlap": developer_overlap,
        "warnings": warnings,
    }


def classify_relation(
    *,
    current_status: int | None,
    previous_status: int | None,
    code_equal: bool,
    version_delta: int,
    age_conflict: bool,
    title_similarity: float,
    mkb_overlap: float,
    warnings: list[str],
) -> str:
    if current_status not in {0, 4} or previous_status not in {0, 4}:
        warnings.append("unknown_status")
        return "unknown_status_pair"
    if current_status == 0 and previous_status == 0:
        warnings.extend(["parallel_active_versions", "source_identification_anomaly"])
        return "parallel_active_versions"
    if (
        code_equal
        and version_delta == 1
        and current_status == 0
        and previous_status == 4
        and not age_conflict
        and (title_similarity >= 80.0 or mkb_overlap > 0.0)
    ):
        return "confirmed_predecessor"
    if current_status == 0 and previous_status == 4:
        warnings.append("metadata_conflict")
        return "metadata_conflict"
    warnings.append("unknown_status")
    return "unknown_status_pair"


def relation_status_for_error(error: ExternalApiError) -> str:
    if error.status_code in {403, 404}:
        return "previous_unavailable"
    if (
        error.status_code == 429
        or (error.status_code is not None and error.status_code >= 500)
        or error.kind
        in {
            ApiErrorKind.REQUEST_ERROR,
            ApiErrorKind.INVALID_JSON,
            ApiErrorKind.HTML_ERROR,
            ApiErrorKind.EMPTY_RESPONSE,
            ApiErrorKind.UNEXPECTED_CONTENT_TYPE,
            ApiErrorKind.CIRCUIT_OPEN,
            ApiErrorKind.RATE_LIMITED_429,
        }
    ):
        return "previous_temporary_failure"
    return "previous_temporary_failure"


def base_relation(
    current_code_version: str,
    previous_code_version: str | None,
    current_record: dict[str, Any],
) -> dict[str, Any]:
    return {
        "current_code_version": current_code_version,
        "previous_code_version": previous_code_version,
        "current_status": to_int(current_record.get("status")),
        "previous_status": None,
        "code_equal": None,
        "version_delta": None,
        "adult_equal": None,
        "child_equal": None,
        "age_category_equal": None,
        "title_similarity": None,
        "mkb_overlap": None,
        "developer_overlap": None,
    }


def write_relation_failure(
    settings: Any,
    code_version: str,
    error: str,
) -> BankPreviousDocumentSummary:
    relation_path = previous_dir(settings, code_version) / "relation.json"
    relation_path.parent.mkdir(parents=True, exist_ok=True)
    relation = {
        "current_code_version": code_version,
        "previous_code_version": None,
        "relation_status": "failed",
        "warnings": ["relation_exception"],
        "error": error,
    }
    write_json(relation_path, relation)
    refresh_bank_manifest(settings, code_version, previous_status="failed")
    return BankPreviousDocumentSummary(
        code_version=code_version,
        previous_code_version=None,
        relation_status="failed",
        relation_path=relation_path,
        status="failed",
        error=error,
    )


def equality_or_unknown(left: Any, right: Any) -> bool | None:
    if left is None or right is None:
        return None
    return bool(left == right)


def normalized_values(value: Any) -> set[str]:
    result: set[str] = set()
    for item in list_value(value):
        if isinstance(item, dict):
            candidate = first_non_empty(
                first_present(item, "code", "Code", "CodeMKB", "mkb", "MKB"),
                first_present(item, "name", "Name", "title", "Title", "id", "Id", "ID"),
            )
        else:
            candidate = item
        if candidate is not None and str(candidate).strip():
            result.add(json.dumps(candidate, ensure_ascii=False, sort_keys=True))
    return result


def overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return round(len(left & right) / len(left | right), 4)


def status_class(status: int | None) -> str:
    if status == 0:
        return "active"
    if status == 4:
        return "inactive"
    return "unknown_status"


def warnings_for_unavailable(relation_status: str) -> list[str]:
    if relation_status == "previous_unavailable":
        return []
    return ["previous_temporary_failure"]


def nullable_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
