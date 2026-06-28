from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import read_jsonl, source_record_id_from_catalog, string_value
from clinrec.research.reports import reports_root, write_csv, write_json, write_jsonl


@dataclass(frozen=True)
class CatalogProfile:
    active_records: int
    all_statuses_records: int
    unique_source_record_ids: int
    duplicate_source_record_ids: int
    unique_code_versions: int
    duplicate_code_versions: int
    malformed_code_versions: int


def catalog_root(corpus_root: Path) -> Path:
    return corpus_root / "catalog"


def active_catalog_path(corpus_root: Path) -> Path:
    return catalog_root(corpus_root) / "catalog-active.jsonl"


def all_statuses_catalog_path(corpus_root: Path) -> Path:
    return catalog_root(corpus_root) / "catalog-all-statuses.jsonl"


def read_active_catalog(corpus_root: Path) -> list[dict[str, Any]]:
    return read_jsonl(active_catalog_path(corpus_root))


def read_all_statuses_catalog(corpus_root: Path) -> list[dict[str, Any]]:
    return read_jsonl(all_statuses_catalog_path(corpus_root))


def write_catalog_indexes(corpus_root: Path) -> CatalogProfile:
    active_rows = read_active_catalog(corpus_root)
    all_rows = read_all_statuses_catalog(corpus_root)
    indexed_rows: list[dict[str, Any]] = []
    malformed_rows: list[dict[str, Any]] = []
    by_source_id: dict[int, list[int]] = defaultdict(list)
    by_code_version: dict[str, list[int]] = defaultdict(list)

    for index, row in enumerate(all_rows, start=1):
        source_record_id = source_record_id_from_catalog(row)
        code_version = string_value(row.get("code_version"))
        if source_record_id is not None:
            by_source_id[source_record_id].append(index)
        if code_version:
            by_code_version[code_version].append(index)
        malformed_kind = classify_code_version(row)
        if malformed_kind is not None:
            malformed_rows.append(
                {
                    "row_index": index,
                    "source_record_id": source_record_id,
                    "code_version": code_version,
                    "malformed_kind": malformed_kind,
                    "code": row.get("code"),
                    "version": row.get("version"),
                }
            )
        indexed_rows.append(
            {
                "row_index": index,
                "source_record_id": source_record_id,
                "source_record_id_valid": source_record_id is not None,
                "duplicate_source_record_id": bool(
                    source_record_id is not None and len(by_source_id[source_record_id]) > 1
                ),
                "code_version": code_version,
                "record": row,
            }
        )

    source_counts = Counter(source_record_id_from_catalog(row) for row in all_rows)
    source_counts.pop(None, None)
    code_version_index = [
        {
            "code_version": code_version,
            "source_record_ids": source_ids_for_rows(all_rows, row_indexes),
            "row_indexes": row_indexes,
            "records_count": len(row_indexes),
        }
        for code_version, row_indexes in sorted(by_code_version.items())
    ]
    collision_rows = [
        row
        for row in code_version_index
        if records_count(row) > 1
    ]
    duplicate_source_rows = [
        {
            "source_record_id": source_id,
            "count": count,
        }
        for source_id, count in sorted(source_counts.items())
        if count > 1
    ]

    root = catalog_root(corpus_root)
    report_root = reports_root(corpus_root)
    write_jsonl(root / "all-statuses-by-source-id.jsonl", indexed_rows)
    write_jsonl(root / "code-version-index.jsonl", code_version_index)
    write_json(
        report_root / "catalog-anomalies.json",
        {
            "active_records": len(active_rows),
            "all_statuses_records": len(all_rows),
            "unique_source_record_ids": len(source_counts),
            "duplicate_source_record_ids": len(duplicate_source_rows),
            "unique_code_versions": len(by_code_version),
            "duplicate_code_versions": len(collision_rows),
            "malformed_code_versions": len(malformed_rows),
            "malformed_kinds": dict(
                sorted(Counter(row["malformed_kind"] for row in malformed_rows).items())
            ),
        },
    )
    write_csv(
        report_root / "catalog-code-version-collisions.csv",
        collision_rows,
        ("code_version", "records_count", "source_record_ids", "row_indexes"),
    )
    write_csv(
        report_root / "catalog-malformed-records.csv",
        malformed_rows,
        ("row_index", "source_record_id", "code_version", "malformed_kind", "code", "version"),
    )
    write_csv(
        report_root / "catalog-duplicate-source-records.csv",
        duplicate_source_rows,
        ("source_record_id", "count"),
    )
    return CatalogProfile(
        active_records=len(active_rows),
        all_statuses_records=len(all_rows),
        unique_source_record_ids=len(source_counts),
        duplicate_source_record_ids=len(duplicate_source_rows),
        unique_code_versions=len(by_code_version),
        duplicate_code_versions=len(collision_rows),
        malformed_code_versions=len(malformed_rows),
    )


def source_ids_for_rows(rows: list[dict[str, Any]], row_indexes: list[int]) -> list[int | None]:
    result: list[int | None] = []
    for row_index in row_indexes:
        result.append(source_record_id_from_catalog(rows[row_index - 1]))
    return result


def records_count(row: dict[str, Any]) -> int:
    value = row.get("records_count")
    return value if isinstance(value, int) else 0


def classify_code_version(row: dict[str, Any]) -> str | None:
    raw = row.get("code_version")
    code_version = string_value(raw).strip()
    if not code_version:
        return "empty"
    if code_version == "_":
        return "_"
    if "_" not in code_version:
        return "missing version"
    code_text, version_text = code_version.split("_", maxsplit=1)
    if not code_text:
        return "missing code"
    if not version_text:
        return "missing version"
    if not code_text.isdigit():
        return "non-numeric code"
    if not version_text.isdigit():
        return "non-numeric version"
    code_value = row.get("code")
    version_value = row.get("version")
    if code_value is not None and string_value(code_value) != code_text:
        return "inconsistent code/version fields"
    if version_value is not None and string_value(version_value) != version_text:
        return "inconsistent code/version fields"
    return None


def active_code_versions(corpus_root: Path) -> set[str]:
    return {
        string_value(row.get("code_version"))
        for row in read_active_catalog(corpus_root)
        if string_value(row.get("code_version"))
    }


def all_status_records_by_code_version(corpus_root: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_all_statuses_catalog(corpus_root):
        code_version = string_value(row.get("code_version"))
        if code_version:
            result[code_version].append(row)
    return dict(result)
