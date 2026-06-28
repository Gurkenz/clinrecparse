from __future__ import annotations

import hashlib
import json
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from clinrec.bank.common import (
    parse_code_version_or_raise,
    read_json_file,
    sha256_file,
    stable_json_dumps,
    string_value,
)
from clinrec.research.catalog import active_code_versions
from clinrec.research.html_profile import image_rows_for_html, table_rows_for_html
from clinrec.research.migration import research_layout
from clinrec.research.reports import reports_root, write_csv, write_json, write_jsonl
from clinrec.research.sections import first_present, raw_sections, section_html, section_id_for


def write_pair_reports(corpus_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []
    anomaly_rows: list[dict[str, Any]] = []
    active = active_code_versions(corpus_root)
    layout = research_layout(corpus_root)
    if layout.previous_root.exists():
        for current_dir in sorted(layout.previous_root.iterdir()):
            if not current_dir.is_dir():
                continue
            for previous_dir in sorted(current_dir.iterdir()):
                if not previous_dir.is_dir():
                    continue
                current_raw = layout.current_root / current_dir.name / "getclinrec.json"
                previous_raw = previous_dir / "getclinrec.json"
                if not current_raw.exists() or not previous_raw.exists():
                    anomaly_rows.append(
                        {
                            "current_code_version": current_dir.name,
                            "previous_code_version": previous_dir.name,
                            "anomaly": "missing_pair_raw_json",
                        }
                    )
                    continue
                row, sections = pair_row(
                    current_raw,
                    previous_raw,
                    active=active,
                )
                rows.append(row)
                section_rows.extend(sections)
                if not row["same_code"] or row["version_delta"] != 1:
                    anomaly_rows.append(
                        {
                            "current_code_version": row["current_code_version"],
                            "previous_code_version": row["previous_code_version"],
                            "anomaly": "unexpected_version_relation",
                            "same_code": row["same_code"],
                            "version_delta": row["version_delta"],
                        }
                    )
    root = reports_root(corpus_root)
    write_jsonl(root / "current-previous-pairs.jsonl", rows)
    write_jsonl(root / "current-legacy-pairs.jsonl", rows)
    write_csv(
        root / "current-previous-sections.csv",
        section_rows,
        (
            "current_code_version",
            "previous_code_version",
            "section_id",
            "section_occurrence_key",
            "content_changed",
            "data_changed",
        ),
    )
    write_csv(
        root / "current-previous-anomalies.csv",
        anomaly_rows,
        (
            "current_code_version",
            "previous_code_version",
            "anomaly",
            "same_code",
            "version_delta",
        ),
    )
    write_csv(
        root / "status-transitions.csv",
        [
            {"transition": transition, "count": count}
            for transition, count in sorted(
                Counter(row["status_transition_raw"] for row in rows).items()
            )
        ],
        ("transition", "count"),
    )
    write_json(
        root / "current-previous-summary.json",
        {
            "pair_count": len(rows),
            "membership_relations": dict(
                sorted(Counter(row["membership_relation"] for row in rows).items())
            ),
            "both_active_pairs": sum(
                1 for row in rows if row["membership_relation"] == "both_active"
            ),
            "changed_sections_total": sum(len(row["changed_section_ids"]) for row in rows),
        },
    )
    return rows


def pair_row(
    current_raw: Path,
    previous_raw: Path,
    *,
    active: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current = load_payload(current_raw)
    previous = load_payload(previous_raw)
    current_code_version = current_raw.parent.name
    previous_code_version = previous_raw.parent.name
    current_code, current_version = parse_code_version_or_raise(current_code_version)
    previous_code, previous_version = parse_code_version_or_raise(previous_code_version)
    current_sections = sections_by_occurrence(current)
    previous_sections = sections_by_occurrence(previous)
    section_rows: list[dict[str, Any]] = []
    changed_section_occurrences: list[str] = []
    unchanged_section_occurrences: list[str] = []
    for occurrence_key in sorted(set(current_sections) | set(previous_sections)):
        current_section = current_sections.get(occurrence_key)
        previous_section = previous_sections.get(occurrence_key)
        section_id = occurrence_key.rsplit("#", maxsplit=1)[0]
        content_changed = section_hash(current_section, "content") != section_hash(
            previous_section,
            "content",
        )
        data_changed = section_hash(current_section, "data") != section_hash(
            previous_section,
            "data",
        )
        if content_changed or data_changed:
            changed_section_occurrences.append(occurrence_key)
        else:
            unchanged_section_occurrences.append(occurrence_key)
        section_rows.append(
            {
                "current_code_version": current_code_version,
                "previous_code_version": previous_code_version,
                "section_id": section_id,
                "section_occurrence_key": occurrence_key,
                "content_changed": content_changed,
                "data_changed": data_changed,
            }
        )
    current_tables, current_images = html_counts(current, current_code_version, "current")
    previous_tables, previous_images = html_counts(previous, previous_code_version, "previous")
    current_catalog = read_json_file(current_raw.parent / "catalog-record.json")
    previous_catalog = read_json_file(previous_raw.parent / "catalog-record.json")
    current_manifest = read_json_file(current_raw.parent / "manifest.json")
    previous_manifest = read_json_file(previous_raw.parent / "manifest.json")
    current_mkb = mkb_codes(current, current_catalog)
    previous_mkb = mkb_codes(previous, previous_catalog)
    current_developers = developer_keys(current_catalog)
    previous_developers = developer_keys(previous_catalog)
    row = {
        "current_code_version": current_code_version,
        "previous_code_version": previous_code_version,
        "same_code": current_code == previous_code,
        "version_delta": current_version - previous_version,
        "current_db_id": current.get("db_id"),
        "previous_db_id": previous.get("db_id"),
        "db_id_changed": current.get("db_id") != previous.get("db_id"),
        "current_in_active_catalog": current_code_version in active,
        "previous_in_active_catalog": previous_code_version in active,
        "membership_relation": membership_relation(
            current_code_version,
            previous_code_version,
            active,
        ),
        "current_status_raw": current.get("status"),
        "previous_status_raw": previous.get("status"),
        "status_transition_raw": f"{previous.get('status')}->{current.get('status')}",
        "current_catalog_resolution_state": current_manifest.get("catalog_resolution_state"),
        "previous_catalog_resolution_state": previous_manifest.get("catalog_resolution_state"),
        "title_similarity": title_similarity(current, previous),
        "title_changed": title_similarity(current, previous) < 100,
        "adult_consistent": current.get("adult") == previous.get("adult"),
        "child_consistent": current.get("child") == previous.get("child"),
        "age_category_consistent": current_catalog.get("age_category")
        == previous_catalog.get("age_category"),
        "current_mkb_codes": current_mkb,
        "previous_mkb_codes": previous_mkb,
        "mkb_intersection": sorted(set(current_mkb) & set(previous_mkb)),
        "mkb_added": sorted(set(current_mkb) - set(previous_mkb)),
        "mkb_removed": sorted(set(previous_mkb) - set(current_mkb)),
        "mkb_jaccard": jaccard(current_mkb, previous_mkb),
        "current_developer_keys": current_developers,
        "previous_developer_keys": previous_developers,
        "developer_intersection": sorted(set(current_developers) & set(previous_developers)),
        "developers_added": sorted(set(current_developers) - set(previous_developers)),
        "developers_removed": sorted(set(previous_developers) - set(current_developers)),
        "developer_jaccard": jaccard(current_developers, previous_developers),
        "current_section_ids": sorted(
            section_id for section_id, _index, _section in section_occurrence_items(current)
        ),
        "previous_section_ids": sorted(
            section_id
            for section_id, _index, _section in section_occurrence_items(previous)
        ),
        "duplicate_section_ids_current": duplicate_section_ids(current),
        "duplicate_section_ids_previous": duplicate_section_ids(previous),
        "section_ids_added": sorted(
            {key.rsplit('#', maxsplit=1)[0] for key in current_sections}
            - {key.rsplit('#', maxsplit=1)[0] for key in previous_sections}
        ),
        "section_ids_removed": sorted(
            {key.rsplit('#', maxsplit=1)[0] for key in previous_sections}
            - {key.rsplit('#', maxsplit=1)[0] for key in current_sections}
        ),
        "section_order_changed": section_order(current) != section_order(previous),
        "changed_section_ids": sorted(
            {key.rsplit("#", maxsplit=1)[0] for key in changed_section_occurrences}
        ),
        "changed_section_occurrences": changed_section_occurrences,
        "unchanged_section_occurrences": unchanged_section_occurrences,
        "raw_size_delta": current_raw.stat().st_size - previous_raw.stat().st_size,
        "html_length_delta": html_length(current) - html_length(previous),
        "base64_size_delta": base64_size(current) - base64_size(previous),
        "table_count_delta": current_tables - previous_tables,
        "image_count_delta": current_images - previous_images,
        "current_sha256": sha256_file(current_raw),
        "previous_sha256": sha256_file(previous_raw),
        "byte_identical": current_raw.read_bytes() == previous_raw.read_bytes(),
    }
    return row, section_rows


def load_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def sections_by_occurrence(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section_id, index, section in section_occurrence_items(payload):
        result[f"{section_id}#{index}"] = section
    return result


def section_order(payload: dict[str, Any]) -> list[str]:
    return [
        f"{section_id}#{index}"
        for section_id, index, _section in section_occurrence_items(payload)
    ]


def section_occurrence_items(payload: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    seen: Counter[str] = Counter()
    rows: list[tuple[str, int, dict[str, Any]]] = []
    for section in raw_sections(payload):
        if not isinstance(section, dict):
            continue
        section_id = section_id_for(section)
        index = seen[section_id]
        seen[section_id] += 1
        rows.append((section_id, index, section))
    return rows


def duplicate_section_ids(payload: dict[str, Any]) -> list[str]:
    counts = Counter(
        section_id for section_id, _index, _section in section_occurrence_items(payload)
    )
    return sorted(section_id for section_id, count in counts.items() if count > 1)


def section_hash(section: dict[str, Any] | None, kind: str) -> str | None:
    if section is None:
        return None
    if kind == "content":
        value = section_html(section)
    else:
        value = first_present(section, "data", "Data")
    return hashlib_json(value)


def hashlib_json(value: Any) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def membership_relation(
    current_code_version: str,
    previous_code_version: str,
    active: set[str],
) -> str:
    current_active = current_code_version in active
    previous_active = previous_code_version in active
    if current_active and previous_active:
        return "both_active"
    if current_active and not previous_active:
        return "current_only_active"
    if not current_active and not previous_active:
        return "neither_active"
    return "unknown"


def title_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = string_value(left.get("name") or left.get("title"))
    right_title = string_value(right.get("name") or right.get("title"))
    return round(SequenceMatcher(a=left_title, b=right_title).ratio() * 100, 1)


def mkb_codes(payload: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    values = payload.get("mkbs") or catalog.get("mkbs") or []
    result: list[str] = []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                value = item.get("code") or item.get("MkbCode") or item.get("mkb_code")
                if value is not None:
                    result.append(string_value(value))
    return sorted(set(result))


def developer_keys(catalog: dict[str, Any]) -> list[str]:
    values = catalog.get("developers") or []
    result: list[str] = []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                value = (
                    item.get("NkoId")
                    or item.get("id")
                    or item.get("Name")
                    or item.get("NkoName")
                )
                if value is not None:
                    result.append(string_value(value))
    return sorted(set(result))


def jaccard(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set and not right_set:
        return 1.0
    return round(len(left_set & right_set) / len(left_set | right_set), 4)


def html_counts(payload: dict[str, Any], code_version: str, kind: str) -> tuple[int, int]:
    table_count = 0
    image_count = 0
    for section in raw_sections(payload):
        if not isinstance(section, dict):
            continue
        section_id = section_id_for(section)
        html = section_html(section)
        table_count += len(
            table_rows_for_html(
                code_version=code_version,
                document_kind=kind,
                section_id=section_id,
                html=html,
            )
        )
        image_count += len(
            image_rows_for_html(
                code_version=code_version,
                document_kind=kind,
                section_id=section_id,
                html=html,
            )
        )
    return table_count, image_count


def html_length(payload: dict[str, Any]) -> int:
    return sum(
        len(section_html(section))
        for section in raw_sections(payload)
        if isinstance(section, dict)
    )


def base64_size(payload: dict[str, Any]) -> int:
    total = 0
    for section in raw_sections(payload):
        if not isinstance(section, dict):
            continue
        html = section_html(section)
        for token in html.split("base64,")[1:]:
            total += int(len(token.split('"', 1)[0].split("'", 1)[0]) * 0.75)
    return total
