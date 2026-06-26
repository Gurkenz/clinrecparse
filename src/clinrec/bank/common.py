from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import split_code_version, to_int, write_json
from clinrec.config import Settings


class BankError(RuntimeError):
    pass


TEMPORARY_RELATION_STATUSES = {
    "previous_temporary_failure",
}


@dataclass(frozen=True)
class BankRecordFilter:
    code_versions: list[str] | None = None
    code: int | None = None
    from_code: int | None = None
    to_code: int | None = None
    all_records: bool = False
    force: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    timestamp: str | None = None


@dataclass(frozen=True)
class RawDocumentInfo:
    code_version: str
    code: int
    version: int
    db_id: int | None
    name: str
    status: int | None
    adult: bool | None
    child: bool | None
    age_category: Any
    mkbs: list[Any]
    association_ids: list[Any]
    payload: dict[str, Any]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compact_timestamp(value: str | None = None) -> str:
    if value:
        return value
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def bank_root(settings: Settings) -> Path:
    return settings.paths.data_root / "bank"


def bank_active_root(settings: Settings) -> Path:
    return bank_root(settings) / "active"


def bank_references_root(settings: Settings) -> Path:
    return bank_root(settings) / "references"


def bank_reports_root(settings: Settings) -> Path:
    return bank_root(settings) / "reports"


def bank_checkpoints_root(settings: Settings) -> Path:
    return bank_root(settings) / "checkpoints"


def bank_document_root(settings: Settings, code_version: str) -> Path:
    return bank_active_root(settings) / code_version


def current_dir(settings: Settings, code_version: str) -> Path:
    return bank_document_root(settings, code_version) / "current"


def previous_dir(settings: Settings, code_version: str) -> Path:
    return bank_document_root(settings, code_version) / "previous"


def previous_candidate_dir(
    settings: Settings,
    current_code_version: str,
    previous_code_version: str,
) -> Path:
    return previous_dir(settings, current_code_version) / previous_code_version


def catalog_index_path(settings: Settings, *, active: bool) -> Path:
    name = "catalog-active.jsonl" if active else "catalog-all-statuses.jsonl"
    return settings.paths.indexes / name


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_checkpoint(settings: Settings, stage: str, row: dict[str, Any]) -> None:
    path = bank_checkpoints_root(settings) / f"{stage}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checkpointed_at": utc_now(), "stage": stage, **row}
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_catalog_records(settings: Settings, *, active: bool) -> list[dict[str, Any]]:
    path = catalog_index_path(settings, active=active)
    if not path.exists():
        kind = "active" if active else "all-statuses"
        raise BankError(f"{path} is missing. Run 'clinrec bank-sync-catalog' first ({kind}).")
    return read_jsonl(path)


def load_catalog_by_code_version(settings: Settings, *, active: bool) -> dict[str, dict[str, Any]]:
    rows = load_catalog_records(settings, active=active)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code_version = row.get("code_version")
        if isinstance(code_version, str):
            result[code_version] = row
    return result


def ensure_selection(options: BankRecordFilter, *, command: str) -> None:
    if has_selection(options):
        return
    raise BankError(
        f"Refusing to run {command} without a selection. "
        "Use --all, --code-version, --code, or --from-code/--to-code."
    )


def has_selection(options: BankRecordFilter) -> bool:
    return bool(options.all_records or options.code_versions) or any(
        value is not None for value in (options.code, options.from_code, options.to_code)
    )


def filter_catalog_records(
    rows: list[dict[str, Any]],
    options: BankRecordFilter,
) -> list[dict[str, Any]]:
    filtered = rows
    if options.code_versions:
        selected = set(options.code_versions)
        filtered = [
            row for row in filtered if string_value(row.get("code_version")) in selected
        ]
    if options.code is not None:
        filtered = [row for row in filtered if to_int(row.get("code")) == options.code]
    if options.from_code is not None:
        filtered = [
            row
            for row in filtered
            if (code := to_int(row.get("code"))) is not None and code >= options.from_code
        ]
    if options.to_code is not None:
        filtered = [
            row
            for row in filtered
            if (code := to_int(row.get("code"))) is not None and code <= options.to_code
        ]
    return sorted(
        filtered,
        key=lambda row: (to_int(row.get("code")) or 0, to_int(row.get("version")) or 0),
    )


def selected_active_records(settings: Settings, options: BankRecordFilter) -> list[dict[str, Any]]:
    ensure_selection(options, command="bank command")
    return filter_catalog_records(load_catalog_records(settings, active=True), options)


def parse_code_version_or_raise(code_version: str) -> tuple[int, int]:
    code, version = split_code_version(code_version)
    if code is None or version is None:
        raise BankError(f"Invalid CodeVersion: {code_version!r}")
    return code, version


def minimal_validate_raw_document(
    raw_content: bytes,
    *,
    expected_code_version: str,
) -> tuple[RawDocumentInfo | None, list[str]]:
    try:
        payload_value = json.loads(raw_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"invalid JSON: {exc}"]
    if not isinstance(payload_value, dict):
        return None, ["root is not an object"]

    payload: dict[str, Any] = payload_value
    obj = mapping_value(first_present(payload, "obj", "Obj", "data", "Data"))
    expected_code, expected_version = parse_code_version_or_raise(expected_code_version)

    code_version = string_value(
        first_non_empty(
            first_present(payload, "id", "Id", "ID", "code_version", "CodeVersion"),
            first_present(obj, "id", "Id", "ID", "code_version", "CodeVersion"),
        )
    )
    code = to_int(
        first_non_empty(
            first_present(payload, "code", "Code"),
            first_present(obj, "code", "Code"),
        )
    )
    version = to_int(
        first_non_empty(
            first_present(payload, "version", "Version", "ver", "Ver"),
            first_present(obj, "version", "Version", "ver", "Ver"),
        )
    )
    name = string_value(
        first_non_empty(
            first_present(payload, "name", "Name", "title", "Title"),
            first_present(obj, "name", "Name", "title", "Title"),
        )
    )
    status_value = first_non_empty(
        first_present(payload, "status", "Status"),
        first_present(obj, "status", "Status"),
    )
    status = to_int(status_value)
    db_id = to_int(first_present(payload, "db_id", "dbId", "DbId", "DB_ID"))
    sections = first_present(obj, "sections", "Sections")

    errors: list[str] = []
    if code_version != expected_code_version:
        errors.append(f"id/code_version mismatch: {code_version!r}")
    if code != expected_code:
        errors.append(f"code mismatch: {code!r}")
    if version != expected_version:
        errors.append(f"version mismatch: {version!r}")
    if not name:
        errors.append("name is empty")
    if status_value is None:
        errors.append("status is missing")
    if not obj:
        errors.append("obj is not an object")
    if not isinstance(sections, list) or not sections:
        errors.append("obj.sections is not a non-empty array")
    if errors:
        return None, errors

    return (
        RawDocumentInfo(
            code_version=expected_code_version,
            code=expected_code,
            version=expected_version,
            db_id=db_id,
            name=name,
            status=status,
            adult=bool_or_none(
                first_non_empty(
                    first_present(payload, "adult", "Adult"),
                    first_present(obj, "adult", "Adult"),
                )
            ),
            child=bool_or_none(
                first_non_empty(
                    first_present(payload, "child", "Child"),
                    first_present(obj, "child", "Child"),
                )
            ),
            age_category=first_non_empty(
                first_present(payload, "age_category", "AgeCategory", "age", "Age"),
                first_present(obj, "age_category", "AgeCategory", "age", "Age"),
            ),
            mkbs=list_value(
                first_non_empty(
                    first_present(payload, "mkbs", "MKBs", "Mkbs", "Mkb"),
                    first_present(obj, "mkbs", "MKBs", "Mkbs", "Mkb"),
                )
            ),
            association_ids=association_ids_from_payload(payload, obj),
            payload=payload,
        ),
        [],
    )


def existing_manifest_matches(path: Path, manifest: dict[str, Any]) -> bool:
    if not path.exists() or not manifest:
        return False
    content = path.read_bytes()
    return (
        manifest.get("sha256") == sha256_bytes(content)
        and manifest.get("size") == len(content)
        and manifest.get("validation") == "valid"
    )


def write_atomic_bytes(path: Path, content: bytes) -> Path:
    part_path = path.with_suffix(path.suffix + ".part")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(content)
    part_path.replace(path)
    return path


def manifest_for_raw_json(
    *,
    code_version: str,
    code: int,
    version: int,
    status: int | None,
    source: str,
    http_status: int,
    content_type: str,
    raw_content: bytes,
    validation: str,
    catalog_source_record_id: int | None = None,
    document_db_id: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code_version": code_version,
        "code": code,
        "version": version,
        "status": status,
        "source": source,
        "http_status": http_status,
        "content_type": content_type,
        "size": len(raw_content),
        "sha256": sha256_bytes(raw_content),
        "downloaded_at": utc_now(),
        "validation": validation,
        "catalog_source_record_id": catalog_source_record_id,
        "document_db_id": document_db_id,
        "db_id_match": ids_match(catalog_source_record_id, document_db_id),
    }
    if error:
        payload["error"] = error
    return payload


def source_record_id_from_catalog(catalog_record: dict[str, Any]) -> int | None:
    return to_int(
        first_non_empty(
            first_present(catalog_record, "source_record_id", "SourceRecordId"),
            first_present(catalog_record, "Id", "ID", "id"),
        )
    )


def catalog_record_for_bank(catalog_record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(catalog_record)
    source_record_id = source_record_id_from_catalog(catalog_record)
    if source_record_id is not None:
        normalized["source_record_id"] = source_record_id
    for legacy_key in ("Id", "ID"):
        normalized.pop(legacy_key, None)
    return normalized


def ids_match(left: int | None, right: int | None) -> bool | None:
    if left is None or right is None:
        return None
    return left == right


def refresh_bank_manifest(
    settings: Settings,
    code_version: str,
    *,
    current_status: str | None = None,
    previous_status: str | None = None,
) -> None:
    path = bank_document_root(settings, code_version) / "bank-manifest.json"
    existing = read_json_file(path)
    code, version = parse_code_version_or_raise(code_version)
    payload = {
        "code_version": code_version,
        "code": code,
        "version": version,
        "current_status": current_status or existing.get("current_status") or "missing",
        "previous_status": previous_status or existing.get("previous_status") or "not_checked",
        "pdf_status": "not_requested",
        "updated_at": utc_now(),
    }
    write_json(path, payload)


def string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip():
            return value
    return None


def mapping_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def association_ids_from_payload(payload: dict[str, Any], obj: dict[str, Any]) -> list[Any]:
    source = first_non_empty(
        first_present(payload, "proff_associations", "ProffAssociations"),
        first_present(obj, "proff_associations", "ProffAssociations"),
    )
    values: list[Any] = []
    seen: set[str] = set()
    for item in list_value(source):
        if isinstance(item, dict):
            candidate = first_non_empty(
                first_present(item, "id", "Id", "ID", "nko_id", "NkoId", "value", "Value"),
                first_present(item, "organization_id", "OrganizationId"),
            )
        else:
            candidate = item
        if candidate is None or str(candidate).strip() == "":
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        values.append(candidate)
    return values
