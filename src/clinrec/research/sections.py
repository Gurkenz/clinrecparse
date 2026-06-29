from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from clinrec.bank.common import (
    read_json_file,
    sha256_file,
    stable_json_dumps,
    string_value,
)
from clinrec.research.html_profile import image_rows_for_html, table_rows_for_html
from clinrec.research.migration import research_layout
from clinrec.research.reports import reports_root, write_csv, write_json, write_jsonl

KNOWN_SPECIAL_SECTIONS = {
    "doc_title": {"special_parser": "title_data_parser", "index_by_default": True},
    "doc_whole": {"special_parser": "whole_document_analyzer", "index_by_default": False},
}


@dataclass(frozen=True)
class ProfileArtifacts:
    documents: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    images: list[dict[str, Any]]
    title_fields: list[dict[str, Any]]
    title_anomalies: list[dict[str, Any]]
    doc_whole_rows: list[dict[str, Any]]


def profile_sections(corpus_root: Path) -> ProfileArtifacts:
    documents: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    title_fields: list[dict[str, Any]] = []
    title_anomalies: list[dict[str, Any]] = []
    doc_whole_rows: list[dict[str, Any]] = []

    for kind, raw_path, current_code_version in iter_document_paths(corpus_root):
        code_version = raw_path.parent.name
        payload = load_payload(raw_path)
        manifest = read_json_file(raw_path.parent / "manifest.json")
        catalog = read_json_file(raw_path.parent / "catalog-record.json")
        candidates_payload = read_json_file(raw_path.parent / "catalog-candidates.json")
        raw_candidates = candidates_payload.get("candidates")
        catalog_candidates = raw_candidates if isinstance(raw_candidates, list) else []
        document_row, section_rows = profile_document(
            kind=kind,
            code_version=code_version,
            current_code_version=current_code_version,
            raw_path=raw_path,
            payload=payload,
            manifest=manifest,
            catalog=catalog,
            catalog_candidates_count=len(catalog_candidates),
        )
        documents.append(document_row)
        sections.extend(section_rows)
        for section in raw_sections(payload):
            if not isinstance(section, dict):
                continue
            section_id = section_id_for(section)
            html = section_html(section)
            tables.extend(
                table_rows_for_html(
                    code_version=code_version,
                    document_kind=kind,
                    current_code_version=current_code_version,
                    section_id=section_id,
                    html=html,
                )
            )
            images.extend(
                image_rows_for_html(
                    code_version=code_version,
                    document_kind=kind,
                    current_code_version=current_code_version,
                    section_id=section_id,
                    html=html,
                )
            )
            if section_id == "doc_title":
                fields, anomalies = parse_title_data(code_version, kind, section)
                title_fields.extend(fields)
                title_anomalies.extend(anomalies)
        doc_whole_rows.append(analyze_doc_whole(kind, code_version, payload))

    write_section_reports(
        corpus_root,
        ProfileArtifacts(
            documents=documents,
            sections=sections,
            tables=tables,
            images=images,
            title_fields=title_fields,
            title_anomalies=title_anomalies,
            doc_whole_rows=doc_whole_rows,
        ),
    )
    return ProfileArtifacts(
        documents=documents,
        sections=sections,
        tables=tables,
        images=images,
        title_fields=title_fields,
        title_anomalies=title_anomalies,
        doc_whole_rows=doc_whole_rows,
    )


def iter_document_paths(corpus_root: Path) -> Iterable[tuple[str, Path, str | None]]:
    layout = research_layout(corpus_root)
    if layout.current_root.exists():
        for raw_path in sorted(layout.current_root.glob("*/getclinrec.json")):
            yield "current", raw_path, None
    if layout.previous_root.exists():
        for raw_path in sorted(layout.previous_root.glob("*/*/getclinrec.json")):
            yield "previous", raw_path, raw_path.parent.parent.name


def load_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def raw_sections(payload: dict[str, Any]) -> list[Any]:
    obj_value = payload.get("obj")
    obj = obj_value if isinstance(obj_value, dict) else {}
    sections_value = obj.get("sections")
    return sections_value if isinstance(sections_value, list) else []


def profile_document(
    *,
    kind: str,
    code_version: str,
    current_code_version: str | None,
    raw_path: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
    catalog: dict[str, Any],
    catalog_candidates_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    sections = [section for section in raw_sections(payload) if isinstance(section, dict)]
    document_row = {
        "document_kind": kind,
        "code_version": code_version,
        "current_code_version": current_code_version,
        "db_id": manifest.get("document_db_id"),
        "catalog_source_record_id": manifest.get("catalog_source_record_id"),
        "db_id_state": manifest.get("db_id_state"),
        "code": manifest.get("code"),
        "version": manifest.get("version"),
        "raw_status": manifest.get("document_status_raw"),
        "name": string_value(payload.get("name") or obj.get("name") or catalog.get("name")),
        "adult": payload.get("adult") if "adult" in payload else obj.get("adult"),
        "child": payload.get("child") if "child" in payload else obj.get("child"),
        "age_category": payload.get("age_category")
        or obj.get("age_category")
        or catalog.get("age_category"),
        "publish_date": catalog.get("publish_date"),
        "file_size_bytes": raw_path.stat().st_size,
        "sha256": sha256_file(raw_path),
        "top_level_keys": sorted(payload),
        "obj_keys": sorted(obj.keys()),
        "sections_count": len(sections),
        "mkb_count": len(payload.get("mkbs") or obj.get("mkbs") or catalog.get("mkbs") or []),
        "professional_association_count": len(payload.get("proff_associations") or []),
        "catalog_developer_count": len(catalog.get("developers") or []),
        "catalog_candidates_count": catalog_candidates_count,
        "catalog_candidate_source_record_ids": manifest.get("catalog_candidate_source_record_ids"),
        "catalog_resolution_state": manifest.get("catalog_resolution_state"),
        "catalog_resolved_source_record_id": manifest.get("catalog_resolved_source_record_id"),
        "catalog_metadata_ambiguous": bool(manifest.get("catalog_metadata_ambiguous")),
    }
    section_rows = [
        profile_section(code_version, kind, current_code_version, index, section)
        for index, section in enumerate(sections)
    ]
    return document_row, section_rows


def profile_section(
    code_version: str,
    document_kind: str,
    current_code_version: str | None,
    index: int,
    section: dict[str, Any],
) -> dict[str, Any]:
    content = first_present(section, "content", "Content", "text")
    data = first_present(section, "data", "Data")
    html = content if isinstance(content, str) else ""
    soup = BeautifulSoup(html, "html.parser") if html else None
    section_id = section_id_for(section)
    return {
        "document_code_version": code_version,
        "document_kind": document_kind,
        "current_code_version": current_code_version,
        "document_identity": document_identity(document_kind, code_version, current_code_version),
        "section_index": index,
        "section_id": section_id,
        "section_occurrence_key": section_occurrence_key(section_id, index),
        "section_name": first_present(section, "name", "Name", "title"),
        "section_keys": sorted(section),
        "content_present": content is not None,
        "content_type": type(content).__name__ if content is not None else None,
        "content_length_chars": len(html),
        "content_sha256": sha256_text(html) if html else None,
        "data_present": data is not None,
        "data_type": type(data).__name__ if data is not None else None,
        "data_item_count": len(data) if isinstance(data, list) else None,
        "data_sha256": sha256_text(stable_json_dumps(data)) if data is not None else None,
        "html_table_count": len(soup.find_all("table")) if soup else 0,
        "html_img_count": len(soup.find_all("img")) if soup else 0,
        "base64_image_count": html.count("data:image/"),
        "estimated_base64_bytes": estimated_base64_bytes(html),
        "link_count": len(soup.find_all("a")) if soup else 0,
        "ul_count": len(soup.find_all("ul")) if soup else 0,
        "ol_count": len(soup.find_all("ol")) if soup else 0,
        "li_count": len(soup.find_all("li")) if soup else 0,
        "index_by_default": index_by_default(section_id),
        "special_parser": KNOWN_SPECIAL_SECTIONS.get(section_id, {}).get("special_parser"),
    }


def parse_title_data(
    code_version: str,
    document_kind: str,
    section: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = first_present(section, "data", "Data")
    fields: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    if data is None:
        anomalies.append(
            {
                "code_version": code_version,
                "document_kind": document_kind,
                "anomaly": "missing_data",
                "item_index": None,
            }
        )
        return fields, anomalies
    if not isinstance(data, list):
        anomalies.append(
            {
                "code_version": code_version,
                "document_kind": document_kind,
                "anomaly": "data_not_list",
                "item_index": None,
                "data_type": type(data).__name__,
            }
        )
        return fields, anomalies
    seen: Counter[str] = Counter()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            anomalies.append(
                {
                    "code_version": code_version,
                    "document_kind": document_kind,
                    "anomaly": "item_not_object",
                    "item_index": index,
                    "item_type": type(item).__name__,
                }
            )
            continue
        key = stable_title_key(item, index)
        seen[key] += 1
        value = title_value(item)
        fields.append(
            {
                "code_version": code_version,
                "document_kind": document_kind,
                "item_index": index,
                "field_key": key,
                "display_label": title_label(item),
                "value_type": type(value).__name__ if value is not None else None,
                "value": value,
                "raw_item": item,
                "repeated": seen[key] > 1,
                "known": key != f"item_{index}",
            }
        )
        if seen[key] > 1:
            anomalies.append(
                {
                    "code_version": code_version,
                    "document_kind": document_kind,
                    "anomaly": "repeated_title_item",
                    "item_index": index,
                    "field_key": key,
                }
            )
    return fields, anomalies


def analyze_doc_whole(
    document_kind: str,
    code_version: str,
    payload: dict[str, Any],
    *,
    max_shingles: int = 5000,
) -> dict[str, Any]:
    sections = [section for section in raw_sections(payload) if isinstance(section, dict)]
    doc_whole_section = next(
        (section for section in sections if section_id_for(section) == "doc_whole"),
        None,
    )
    whole_html = section_html(doc_whole_section) if doc_whole_section else ""
    whole_text = normalize_text(html_text(whole_html))
    structured_parts = [
        normalize_text(html_text(section_html(section)))
        for section in sections
        if index_by_default(section_id_for(section)) and section_id_for(section) != "doc_title"
    ]
    structured_text = normalize_text(" ".join(part for part in structured_parts if part))
    exact_match = bool(whole_text and structured_text and whole_text == structured_text)
    length_ratio = ratio(len(whole_text), len(structured_text))
    overlap = shingle_overlap(whole_text, structured_text, max_shingles=max_shingles)
    estimated_duplicate = exact_match or (
        bool(whole_text)
        and bool(structured_text)
        and 0.75 <= length_ratio <= 1.25
        and overlap >= 0.8
    )
    recommendation = "ignore"
    if whole_text:
        recommendation = "exclude_from_index" if estimated_duplicate else "manual_review"
    return {
        "code_version": code_version,
        "document_kind": document_kind,
        "doc_whole_present": doc_whole_section is not None,
        "doc_whole_length": len(whole_text),
        "structured_length": len(structured_text),
        "doc_whole_text_sha256": sha256_text(whole_text) if whole_text else None,
        "structured_text_sha256": sha256_text(structured_text) if structured_text else None,
        "raw_content_sha256": sha256_text(whole_html) if whole_html else None,
        "exact_match": exact_match,
        "length_ratio": length_ratio,
        "shingle_overlap": overlap,
        "estimated_duplicate": estimated_duplicate,
        "recommendation": recommendation,
    }


def write_section_reports(corpus_root: Path, artifacts: ProfileArtifacts) -> None:
    root = reports_root(corpus_root)
    write_jsonl(root / "documents.jsonl", artifacts.documents)
    write_jsonl(root / "sections.jsonl", artifacts.sections)
    write_jsonl(root / "tables.jsonl", artifacts.tables)
    write_jsonl(root / "images.jsonl", artifacts.images)
    write_jsonl(root / "doc-whole-analysis.jsonl", artifacts.doc_whole_rows)
    write_json(
        root / "schema-profile.json",
        schema_profile(artifacts.documents, artifacts.sections),
    )
    write_json(root / "document-schema.json", document_schema(artifacts.documents))
    write_section_registry(root, artifacts.sections)
    write_title_reports(root, artifacts.title_fields, artifacts.title_anomalies)
    write_doc_whole_reports(root, artifacts.doc_whole_rows)
    write_html_reports(root, artifacts.tables, artifacts.images)
    write_status_reports(root, artifacts.documents)
    write_key_reports(root, artifacts.documents, artifacts.sections)


def schema_profile(
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_count": len(documents),
        "section_count": len(sections),
        "top_level_keys": count_list_values(documents, "top_level_keys"),
        "obj_keys": count_list_values(documents, "obj_keys"),
        "section_keys": count_list_values(sections, "section_keys"),
        "status_values": dict(
            sorted(Counter(str(row.get("raw_status")) for row in documents).items())
        ),
        "db_id_states": dict(
            sorted(Counter(str(row.get("db_id_state")) for row in documents).items())
        ),
        "documents_containing_tables": count_documents_with(sections, "html_table_count"),
        "documents_containing_base64_images": count_documents_with(sections, "base64_image_count"),
        "largest_documents": sorted(
            documents,
            key=lambda row: int(row.get("file_size_bytes") or 0),
            reverse=True,
        )[:10],
        "largest_sections": sorted(
            sections,
            key=lambda row: int(row.get("content_length_chars") or 0),
            reverse=True,
        )[:10],
    }


def document_schema(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "documents": len(documents),
        "document_kinds": dict(sorted(Counter(row["document_kind"] for row in documents).items())),
        "top_level_keys": count_list_values(documents, "top_level_keys"),
        "obj_keys": count_list_values(documents, "obj_keys"),
        "section_count_values": dict(
            sorted(Counter(str(row.get("sections_count")) for row in documents).items())
        ),
        "catalog_metadata_ambiguous": sum(
            1 for row in documents if row.get("catalog_metadata_ambiguous")
        ),
    }


def write_section_registry(root: Path, sections: list[dict[str, Any]]) -> None:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sections:
        by_id[string_value(row.get("section_id"))].append(row)
    registry_rows = []
    unknown_rows = []
    for section_id, rows in sorted(by_id.items()):
        names = sorted(
            {
                string_value(row.get("section_name"))
                for row in rows
                if row.get("section_name")
            }
        )
        content_types = sorted(
            {
                string_value(row.get("content_type"))
                for row in rows
                if row.get("content_type")
            }
        )
        data_types = sorted(
            {string_value(row.get("data_type")) for row in rows if row.get("data_type")}
        )
        profile = {
            "section_id": section_id,
            "observed_names": names,
            "document_frequency": len({row["document_code_version"] for row in rows}),
            "content_type_variants": content_types,
            "data_type_variants": data_types,
            "has_html": any(int(row.get("content_length_chars") or 0) > 0 for row in rows),
            "special_parser": KNOWN_SPECIAL_SECTIONS.get(section_id, {}).get("special_parser"),
            "index_by_default": index_by_default(section_id),
        }
        registry_rows.append(profile)
        if section_id not in KNOWN_SPECIAL_SECTIONS and not known_empirical_section(section_id):
            unknown_rows.append(profile)
    write_csv(
        root / "section-registry.csv",
        registry_rows,
        (
            "section_id",
            "observed_names",
            "document_frequency",
            "content_type_variants",
            "data_type_variants",
            "has_html",
            "special_parser",
            "index_by_default",
        ),
    )
    write_csv(
        root / "unknown-sections.csv",
        unknown_rows,
        ("section_id", "observed_names", "document_frequency", "content_type_variants"),
    )
    write_csv(
        root / "section-profile.csv",
        sections,
        (
            "document_code_version",
            "document_kind",
            "current_code_version",
            "section_index",
            "section_id",
            "section_occurrence_key",
            "section_name",
            "content_length_chars",
            "data_type",
            "html_table_count",
            "html_img_count",
            "index_by_default",
        ),
    )


def write_title_reports(
    root: Path,
    fields: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
) -> None:
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fields:
        by_key[string_value(row.get("field_key"))].append(row)
    write_json(
        root / "doc-title-schema.json",
        {
            "field_count": len(by_key),
            "item_count": len(fields),
            "anomaly_count": len(anomalies),
            "fields": {
                key: {
                    "frequency": len(rows),
                    "value_types": sorted({string_value(row.get("value_type")) for row in rows}),
                    "labels": sorted(
                        {
                            string_value(row.get("display_label"))
                            for row in rows
                            if row.get("display_label")
                        }
                    ),
                }
                for key, rows in sorted(by_key.items())
            },
        },
    )
    write_csv(
        root / "doc-title-fields.csv",
        fields,
        (
            "code_version",
            "document_kind",
            "item_index",
            "field_key",
            "display_label",
            "value_type",
            "value",
            "repeated",
            "known",
            "raw_item",
        ),
    )
    write_csv(
        root / "doc-title-anomalies.csv",
        anomalies,
        ("code_version", "document_kind", "anomaly", "item_index", "field_key", "data_type"),
    )


def write_doc_whole_reports(root: Path, rows: list[dict[str, Any]]) -> None:
    write_json(
        root / "doc-whole-summary.json",
        {
            "documents": len(rows),
            "doc_whole_present": sum(1 for row in rows if row.get("doc_whole_present")),
            "likely_duplicates": sum(1 for row in rows if row.get("estimated_duplicate")),
            "recommendations": dict(
                sorted(Counter(string_value(row.get("recommendation")) for row in rows).items())
            ),
        },
    )
    write_csv(
        root / "doc-whole-review.csv",
        rows,
        (
            "code_version",
            "document_kind",
            "doc_whole_present",
            "doc_whole_length",
            "structured_length",
            "exact_match",
            "length_ratio",
            "shingle_overlap",
            "estimated_duplicate",
            "recommendation",
        ),
    )


def write_html_reports(
    root: Path,
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> None:
    write_json(
        root / "table-summary.json",
        {
            "tables_total": len(tables),
            "documents_with_tables": len(
                {
                    (row["document_kind"], row["code_version"])
                    for row in tables
                    if row.get("rows") is not None
                }
            ),
            "nested_tables": sum(int(row.get("nested_table_count") or 0) for row in tables),
            "malformed_tables": sum(1 for row in tables if row.get("malformed")),
        },
    )
    write_csv(
        root / "table-complexity.csv",
        tables,
        (
            "code_version",
            "document_kind",
            "current_code_version",
            "section_id",
            "table_index",
            "rows",
            "cells",
            "rowspan_count",
            "colspan_count",
            "nested_table_count",
            "invalid_span_count",
            "text_length",
            "html_sha256",
        ),
    )
    write_csv(
        root / "table-anomalies.csv",
        [row for row in tables if row.get("malformed") or row.get("nested_table_count")],
        (
            "code_version",
            "document_kind",
            "current_code_version",
            "section_id",
            "table_index",
            "malformed",
            "nested_table_count",
            "invalid_span_count",
            "invalid_spans",
        ),
    )
    write_json(
        root / "image-summary.json",
        {
            "images_total": len(images),
            "src_classes": dict(sorted(Counter(row.get("src_class") for row in images).items())),
            "base64_images": sum(1 for row in images if row.get("src_class") == "base64"),
            "unique_base64_assets": len({row.get("sha256") for row in images if row.get("sha256")}),
            "decode_errors": sum(1 for row in images if row.get("decode_error")),
        },
    )
    write_csv(
        root / "image-mime-types.csv",
        [
            {"mime_type": mime_type, "count": count}
            for mime_type, count in sorted(
                Counter(string_value(row.get("mime_type")) for row in images).items()
            )
        ],
        ("mime_type", "count"),
    )
    write_csv(
        root / "image-duplicates.csv",
        duplicate_image_rows(images),
        ("sha256", "duplicate_count", "code_versions"),
    )
    write_csv(
        root / "image-anomalies.csv",
        [
            row
            for row in images
            if row.get("decode_error")
            or row.get("src_class") in {"file", "http", "https", "empty", "missing"}
        ],
        (
            "code_version",
            "document_kind",
            "current_code_version",
            "section_id",
            "image_index",
            "src_class",
            "mime_type",
            "decode_error",
        ),
    )


def write_status_reports(root: Path, documents: list[dict[str, Any]]) -> None:
    current = [row for row in documents if row["document_kind"] == "current"]
    previous = [row for row in documents if row["document_kind"] == "previous"]
    write_csv(root / "statuses-current.csv", status_rows(current), ("raw_status", "count"))
    write_csv(root / "statuses-previous.csv", status_rows(previous), ("raw_status", "count"))
    write_csv(root / "statuses.csv", status_rows(documents), ("raw_status", "count"))
    write_json(
        root / "status-summary.json",
        {
            "current": dict(Counter(str(row.get("raw_status")) for row in current)),
            "previous": dict(Counter(str(row.get("raw_status")) for row in previous)),
            "all_documents": dict(Counter(str(row.get("raw_status")) for row in documents)),
        },
    )
    write_csv(
        root / "identities.csv",
        documents,
        (
            "document_kind",
            "code_version",
            "current_code_version",
            "db_id",
            "catalog_source_record_id",
            "db_id_state",
            "catalog_candidates_count",
            "catalog_candidate_source_record_ids",
            "catalog_resolution_state",
            "catalog_resolved_source_record_id",
            "catalog_metadata_ambiguous",
        ),
    )


def write_key_reports(
    root: Path,
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> None:
    write_csv(
        root / "top-level-keys.csv",
        [
            {"key": key, "count": count}
            for key, count in count_list_values(documents, "top_level_keys").items()
        ],
        ("key", "count"),
    )
    write_csv(
        root / "object-keys.csv",
        [
            {"key": key, "count": count}
            for key, count in count_list_values(documents, "obj_keys").items()
        ],
        ("key", "count"),
    )
    write_csv(
        root / "section-keys.csv",
        [
            {"key": key, "count": count}
            for key, count in count_list_values(sections, "section_keys").items()
        ],
        ("key", "count"),
    )


def duplicate_image_rows(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in images:
        sha = string_value(row.get("sha256"))
        if sha:
            by_sha[sha].append(row)
    return [
        {
            "sha256": sha,
            "duplicate_count": len(rows),
            "code_versions": sorted({string_value(row.get("code_version")) for row in rows}),
        }
        for sha, rows in sorted(by_sha.items())
        if len(rows) > 1
    ]


def status_rows(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"raw_status": status, "count": count}
        for status, count in sorted(
            Counter(str(row.get("raw_status")) for row in documents).items()
        )
    ]


def count_list_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            counter.update(string_value(item) for item in value)
    return dict(sorted(counter.items()))


def count_documents_with(sections: list[dict[str, Any]], key: str) -> int:
    grouped: dict[str, int] = defaultdict(int)
    for row in sections:
        grouped[
            document_identity(
                string_value(row.get("document_kind")),
                string_value(row.get("document_code_version")),
                string_value(row.get("current_code_version")) or None,
            )
        ] += int(row.get(key) or 0)
    return sum(1 for value in grouped.values() if value > 0)


def estimated_base64_bytes(html: str) -> int:
    total = 0
    marker = "base64,"
    for item in html.split(marker)[1:]:
        token = item.split('"', 1)[0].split("'", 1)[0]
        total += int(len(token) * 0.75)
    return total


def first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def section_id_for(section: dict[str, Any]) -> str:
    return string_value(first_present(section, "id", "Id"))


def section_occurrence_key(section_id: str, index: int) -> str:
    return f"{section_id}#{index}"


def document_identity(
    document_kind: str,
    code_version: str,
    current_code_version: str | None,
) -> str:
    if document_kind == "previous":
        return f"previous:{current_code_version}:{code_version}"
    return f"current:{code_version}"


def section_html(section: dict[str, Any] | None) -> str:
    if section is None:
        return ""
    value = first_present(
        section,
        "content",
        "Content",
        "html",
        "Html",
        "HTML",
        "text",
        "Text",
    )
    return value if isinstance(value, str) else ""


def index_by_default(section_id: str) -> bool:
    return section_id != "doc_whole"


def known_empirical_section(section_id: str) -> bool:
    return section_id in KNOWN_SPECIAL_SECTIONS


def title_label(item: dict[str, Any]) -> str:
    return string_value(
        first_present(item, "label", "Label", "name", "Name", "title", "Title", "caption")
    )


def stable_title_key(item: dict[str, Any], index: int) -> str:
    for key in ("key", "Key", "id", "Id", "field", "Field", "code", "Code"):
        value = item.get(key)
        if value not in {None, ""}:
            return string_value(value)
    label = title_label(item)
    if label:
        return normalize_key(label)
    return f"item_{index}"


def title_value(item: dict[str, Any]) -> Any:
    for key in ("value", "Value", "text", "Text", "data", "Data", "content", "Content"):
        if key in item:
            return item[key]
    return None


def normalize_key(value: str) -> str:
    normalized = re.sub(r"\W+", "_", value.strip().lower(), flags=re.UNICODE).strip("_")
    return normalized or "unknown"


def html_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def shingle_overlap(left: str, right: str, *, max_shingles: int) -> float:
    left_shingles = bounded_shingles(left, max_shingles=max_shingles)
    right_shingles = bounded_shingles(right, max_shingles=max_shingles)
    if not left_shingles or not right_shingles:
        return 0.0
    return round(len(left_shingles & right_shingles) / len(left_shingles | right_shingles), 4)


def bounded_shingles(value: str, *, max_shingles: int, width: int = 5) -> set[str]:
    words = value.split()
    if len(words) < width:
        return set(words)
    step = max(1, (len(words) - width + 1) // max_shingles)
    shingles: set[str] = set()
    for index in range(0, len(words) - width + 1, step):
        shingles.add(" ".join(words[index : index + width]))
        if len(shingles) >= max_shingles:
            break
    return shingles


def ratio(left: int, right: int) -> float:
    if left == 0 and right == 0:
        return 1.0
    if left == 0 or right == 0:
        return 0.0
    return round(left / right, 4)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def title_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = string_value(left.get("name") or left.get("title"))
    right_title = string_value(right.get("name") or right.get("title"))
    return round(SequenceMatcher(a=left_title, b=right_title).ratio() * 100, 1)
