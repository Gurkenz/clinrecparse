from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.bank.common import (
    bank_active_root,
    bank_legacy_root,
    bank_reports_root,
    catalog_index_path,
    first_present,
    read_json_file,
    read_jsonl,
    string_value,
    utc_now,
)
from clinrec.config import Settings


@dataclass(frozen=True)
class BankStatusAnalysisSummary:
    report_path: Path
    csv_path: Path
    transitions_path: Path
    active_catalog_records: int
    all_statuses_catalog_records: int
    documents: int


def analyze_statuses(settings: Settings) -> BankStatusAnalysisSummary:
    reports_root = bank_reports_root(settings)
    reports_root.mkdir(parents=True, exist_ok=True)
    report_path = reports_root / "status-analysis.json"
    csv_path = reports_root / "status-analysis.csv"
    transitions_path = reports_root / "status-transitions.csv"

    active_catalog = read_jsonl(catalog_index_path(settings, active=True))
    all_statuses_catalog = read_jsonl(catalog_index_path(settings, active=False))
    documents = document_status_rows(settings)
    transition_rows = neighboring_status_rows(active_catalog + all_statuses_catalog)

    catalog_active_status = Counter(status_key(row.get("status")) for row in active_catalog)
    catalog_all_status = Counter(status_key(row.get("status")) for row in all_statuses_catalog)
    document_status = Counter(row["document_status_raw"] for row in documents)
    status_pairs = Counter(
        (row["catalog_status_raw"], row["document_status_raw"]) for row in documents
    )
    apply_pairs = Counter(
        (
            row["catalog_status_raw"],
            row["document_status_raw"],
            row["apply_status_raw"],
            row["apply_status_calculated_raw"],
        )
        for row in documents
    )
    legacy_status = Counter(
        row["document_status_raw"] for row in documents if row["bank_area"] == "legacy"
    )

    write_json(
        report_path,
        {
            "generated_at": utc_now(),
            "status_interpretation": "unknown",
            "active_catalog_records": len(active_catalog),
            "all_statuses_catalog_records": len(all_statuses_catalog),
            "documents": len(documents),
            "catalog_status_frequency_active": counter_dict(catalog_active_status),
            "catalog_status_frequency_all_statuses": counter_dict(catalog_all_status),
            "document_status_frequency": counter_dict(document_status),
            "catalog_document_status_pairs": {
                f"{left}|{right}": count for (left, right), count in sorted(status_pairs.items())
            },
            "apply_status_combinations": {
                "|".join(values): count for values, count in sorted(apply_pairs.items())
            },
            "legacy_document_status_frequency": counter_dict(legacy_status),
            "neighboring_versions": transition_rows,
        },
    )
    write_status_csv(csv_path, active_catalog, all_statuses_catalog, documents)
    write_transitions_csv(transitions_path, transition_rows)
    return BankStatusAnalysisSummary(
        report_path=report_path,
        csv_path=csv_path,
        transitions_path=transitions_path,
        active_catalog_records=len(active_catalog),
        all_statuses_catalog_records=len(all_statuses_catalog),
        documents=len(documents),
    )


def document_status_rows(settings: Settings) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for area, root in (
        ("active", bank_active_root(settings)),
        ("legacy", bank_legacy_root(settings)),
    ):
        if not root.exists():
            continue
        for document_root in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest = read_json_file(document_root / "current" / "manifest.json")
            if not manifest:
                continue
            rows.append(
                {
                    "bank_area": area,
                    "code_version": document_root.name,
                    "catalog_status_raw": status_key(manifest.get("catalog_status_raw")),
                    "document_status_raw": status_key(manifest.get("document_status_raw")),
                    "apply_status_raw": status_key(manifest.get("apply_status_raw")),
                    "apply_status_calculated_raw": status_key(
                        manifest.get("apply_status_calculated_raw")
                    ),
                }
            )
    return rows


def neighboring_status_rows(catalog_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in catalog_rows:
        code = string_value(row.get("code"))
        if code:
            by_code[code].append(row)
    rows: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for code, records in sorted(by_code.items()):
        ordered = sorted(records, key=lambda row: int(row.get("version") or 0))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            previous_code_version = string_value(previous.get("code_version"))
            current_code_version = string_value(current.get("code_version"))
            pair_key = (previous_code_version, current_code_version)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            rows.append(
                {
                    "code": code,
                    "previous_code_version": previous_code_version,
                    "current_code_version": current_code_version,
                    "previous_catalog_status_raw": status_key(previous.get("status")),
                    "current_catalog_status_raw": status_key(current.get("status")),
                    "previous_apply_status_raw": status_key(
                        first_present(previous, "apply_status", "ApplyStatus")
                    ),
                    "current_apply_status_raw": status_key(
                        first_present(current, "apply_status", "ApplyStatus")
                    ),
                    "previous_apply_status_calculated_raw": status_key(
                        first_present(
                            previous,
                            "apply_status_calculated",
                            "ApplyStatusCalculated",
                        )
                    ),
                    "current_apply_status_calculated_raw": status_key(
                        first_present(
                            current,
                            "apply_status_calculated",
                            "ApplyStatusCalculated",
                        )
                    ),
                }
            )
    return rows


def write_status_csv(
    path: Path,
    active_catalog: list[dict[str, Any]],
    all_statuses_catalog: list[dict[str, Any]],
    documents: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["scope", "status_field", "status_value", "count"]
    rows: list[dict[str, Any]] = []
    for scope, catalog_rows in (
        ("active_catalog", active_catalog),
        ("all_statuses_catalog", all_statuses_catalog),
    ):
        for status, count in Counter(status_key(row.get("status")) for row in catalog_rows).items():
            rows.append(
                {
                    "scope": scope,
                    "status_field": "catalog.Status",
                    "status_value": status,
                    "count": count,
                }
            )
    for status, count in Counter(row["document_status_raw"] for row in documents).items():
        rows.append(
            {
                "scope": "documents",
                "status_field": "GetClinrec2.status",
                "status_value": status,
                "count": count,
            }
        )
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            sorted(
                rows,
                key=lambda row: (
                    row["scope"],
                    row["status_field"],
                    row["status_value"],
                ),
            )
        )


def write_transitions_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "code",
        "previous_code_version",
        "current_code_version",
        "previous_catalog_status_raw",
        "current_catalog_status_raw",
        "previous_apply_status_raw",
        "current_apply_status_raw",
        "previous_apply_status_calculated_raw",
        "current_apply_status_calculated_raw",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def status_key(value: Any) -> str:
    return "<null>" if value is None else string_value(value)
