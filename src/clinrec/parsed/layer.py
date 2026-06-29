from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

from clinrec.bank.common import (
    BankError,
    parse_code_version_or_raise,
    read_json_file,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    string_value,
    write_atomic_bytes,
)
from clinrec.research.reports import write_json, write_jsonl
from clinrec.research.sections import raw_sections, section_html, section_id_for

PARSED_SCHEMA_VERSION = "1.0"
PARSER_VERSION = "parsed-layer-1"


@dataclass(frozen=True)
class ParsedBuildOptions:
    input: Path
    output: Path
    code_versions: tuple[str, ...] = ()
    all_current: bool = False
    include_previous: bool = False


@dataclass(frozen=True)
class ParsedBuildSummary:
    output: Path
    source_documents: int
    parsed_documents: int
    failed_documents: int
    sections: int
    tables: int
    images: int
    chunks: int
    summary_path: Path


@dataclass(frozen=True)
class ParsedValidationSummary:
    input: Path
    valid: bool
    errors: int
    warnings: int
    report_json: Path
    report_markdown: Path


@dataclass(frozen=True)
class ParsedExportSummary:
    input: Path
    output: Path
    backend_files: int
    frontend_documents: int
    assets: int
    search_chunks: int
    rag_chunks: int
    manifest_path: Path


@dataclass(frozen=True)
class ParsedDiffSummary:
    input: Path
    pairs: int
    section_changes: int
    table_changes: int
    image_changes: int
    summary_path: Path


@dataclass(frozen=True)
class RawDocumentRef:
    kind: str
    raw_path: Path
    code_version: str
    current_code_version: str | None


@dataclass
class BuildState:
    output: Path
    source_root: Path
    documents: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    images: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    search_chunks: list[dict[str, Any]]
    rag_chunks: list[dict[str, Any]]
    citation_rows: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]


def build_parsed_dataset(options: ParsedBuildOptions) -> ParsedBuildSummary:
    validate_build_options(options)
    ensure_parsed_output_safe(options.output)
    options.output.mkdir(parents=True, exist_ok=True)
    (options.output / "reports").mkdir(parents=True, exist_ok=True)
    refs = select_raw_documents(options)
    state = BuildState(
        output=options.output,
        source_root=options.input,
        documents=[],
        sections=[],
        tables=[],
        images=[],
        relations=[],
        search_chunks=[],
        rag_chunks=[],
        citation_rows=[],
        anomalies=[],
    )
    for ref in refs:
        try:
            parse_raw_document(state, ref)
        except (OSError, ValueError, json.JSONDecodeError, BankError) as exc:
            state.anomalies.append(
                {
                    "stage": "parse",
                    "path": source_raw_path(options.input, ref.raw_path),
                    "code_version": ref.code_version,
                    "document_kind": ref.kind,
                    "error": str(exc),
                }
            )
    state.relations = build_relations(state.documents)
    write_dataset_artifacts(state, options, source_documents=len(refs))
    summary = parsed_summary(state, source_documents=len(refs))
    summary_path = options.output / "reports" / "parsed-summary.json"
    write_json(summary_path, summary)
    write_jsonl(options.output / "reports" / "parser-anomalies.jsonl", state.anomalies)
    return ParsedBuildSummary(
        output=options.output,
        source_documents=len(refs),
        parsed_documents=len(state.documents),
        failed_documents=len(state.anomalies),
        sections=len(state.sections),
        tables=len(state.tables),
        images=len(state.images),
        chunks=len(state.search_chunks),
        summary_path=summary_path,
    )


def validate_build_options(options: ParsedBuildOptions) -> None:
    if options.all_current and options.code_versions:
        raise BankError("--all-current and --code-version are mutually exclusive.")
    if not options.input.exists():
        raise BankError(f"Parsed input corpus is missing: {options.input}")
    for code_version in options.code_versions:
        parse_code_version_or_raise(code_version)


def ensure_parsed_output_safe(output: Path) -> None:
    parts = {part.casefold() for part in output.resolve().parts}
    if "bank" in parts:
        raise BankError("Parsed output must not be written inside data/bank.")


def select_raw_documents(options: ParsedBuildOptions) -> list[RawDocumentRef]:
    current_root = options.input / "current"
    selected = set(options.code_versions)
    refs: list[RawDocumentRef] = []
    if current_root.exists():
        for raw_path in sorted(current_root.glob("*/getclinrec.json")):
            code_version = raw_path.parent.name
            if selected and code_version not in selected:
                continue
            refs.append(
                RawDocumentRef(
                    kind="current",
                    raw_path=raw_path,
                    code_version=code_version,
                    current_code_version=None,
                )
            )
    if options.include_previous:
        previous_root = options.input / "previous"
        selected_current = {ref.code_version for ref in refs}
        if previous_root.exists():
            for raw_path in sorted(previous_root.glob("*/*/getclinrec.json")):
                current_code_version = raw_path.parent.parent.name
                if selected_current and current_code_version not in selected_current:
                    continue
                refs.append(
                    RawDocumentRef(
                        kind="previous",
                        raw_path=raw_path,
                        code_version=raw_path.parent.name,
                        current_code_version=current_code_version,
                    )
                )
    return refs


def parse_raw_document(state: BuildState, ref: RawDocumentRef) -> None:
    raw_bytes = ref.raw_path.read_bytes()
    payload_value = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload_value, dict):
        raise BankError("raw JSON root is not an object")
    payload: dict[str, Any] = payload_value
    raw_sha = sha256_bytes(raw_bytes)
    raw_relative = source_raw_path(state.source_root, ref.raw_path)
    sections = [section for section in raw_sections(payload) if isinstance(section, dict)]
    document_uid = document_uid_for(ref.kind, ref.code_version, ref.current_code_version)
    document_dir = document_output_dir(state.output, ref)
    document_dir.mkdir(parents=True, exist_ok=True)
    document_section_ids: list[str] = []
    document: dict[str, Any] = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "document_id": document_uid,
        "document_kind": ref.kind,
        "code_version": ref.code_version,
        "current_code_version": ref.current_code_version,
        "code": first_int(payload, "code", "Code"),
        "version": first_int(payload, "version", "Version", "ver", "Ver"),
        "db_id": first_int(payload, "db_id", "dbId", "DbId", "DB_ID"),
        "title": document_title(payload),
        "source_raw_path": raw_relative,
        "source_raw_sha256": raw_sha,
        "source_section_count": len(sections),
        "sections": document_section_ids,
        "warnings": [],
    }
    document_sections: list[dict[str, Any]] = []
    document_tables: list[dict[str, Any]] = []
    document_images: list[dict[str, Any]] = []
    section_occurrences: Counter[str] = Counter()
    for source_order, section in enumerate(sections, start=1):
        source_section_id = section_id_for(section) or f"section_{source_order:04d}"
        occurrence_index = section_occurrences[source_section_id]
        section_occurrences[source_section_id] += 1
        parsed_section, table_rows, image_rows = parse_section(
            state,
            ref,
            section,
            source_order=source_order,
            source_section_id=source_section_id,
            occurrence_index=occurrence_index,
            raw_relative=raw_relative,
            raw_sha=raw_sha,
        )
        document_sections.append(parsed_section)
        document_tables.extend(table_rows)
        document_images.extend(image_rows)
        document_section_ids.append(string_value(parsed_section["section_id"]))
    document["section_count"] = len(document_sections)
    document["table_count"] = len(document_tables)
    document["image_count"] = len(document_images)
    write_json(document_dir / "document.json", document)
    write_jsonl(document_dir / "sections.jsonl", document_sections)
    write_jsonl(document_dir / "tables.jsonl", document_tables)
    write_jsonl(document_dir / "images.jsonl", document_images)
    state.documents.append(document)
    state.sections.extend(document_sections)
    state.tables.extend(document_tables)
    state.images.extend(document_images)


def parse_section(
    state: BuildState,
    ref: RawDocumentRef,
    section: dict[str, Any],
    *,
    source_order: int,
    source_section_id: str,
    occurrence_index: int,
    raw_relative: str,
    raw_sha: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    section_key = f"{safe_id(source_section_id)}#{occurrence_index}"
    document_uid = document_uid_for(ref.kind, ref.code_version, ref.current_code_version)
    section_uid = f"{document_uid}:{section_key}"
    raw_html = section_html(section)
    normalized_html, image_rows = normalize_html(
        state,
        raw_html,
        ref=ref,
        section_uid=section_uid,
        section_key=section_key,
        source_order=source_order,
        raw_relative=raw_relative,
        raw_sha=raw_sha,
    )
    plain_text = visible_text(normalized_html)
    table_rows = extract_tables(
        normalized_html,
        ref=ref,
        section_uid=section_uid,
        section_key=section_key,
        source_order=source_order,
        raw_relative=raw_relative,
        raw_sha=raw_sha,
    )
    section_row = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "section_id": section_uid,
        "document_id": document_uid,
        "document_kind": ref.kind,
        "code_version": ref.code_version,
        "current_code_version": ref.current_code_version,
        "source_order": source_order,
        "source_section_id": source_section_id,
        "occurrence_index": occurrence_index,
        "section_key": section_key,
        "section_title": section_title(section),
        "raw_html_sha256": sha256_text(raw_html),
        "normalized_html": normalized_html,
        "normalized_html_sha256": sha256_text(normalized_html),
        "plain_text": plain_text,
        "plain_text_sha256": sha256_text(plain_text),
        "source_raw_path": raw_relative,
        "source_raw_sha256": raw_sha,
        "table_ids": [row["table_id"] for row in table_rows],
        "image_ids": [row["image_id"] for row in image_rows],
    }
    append_chunks_for_section(state, section_row, table_rows)
    return section_row, table_rows, image_rows


def normalize_html(
    state: BuildState,
    raw_html: str,
    *,
    ref: RawDocumentRef,
    section_uid: str,
    section_key: str,
    source_order: int,
    raw_relative: str,
    raw_sha: str,
) -> tuple[str, list[dict[str, Any]]]:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    image_rows: list[dict[str, Any]] = []
    for image_index, image in enumerate(soup.find_all("img")):
        if not isinstance(image, Tag):
            continue
        image_rows.append(
            normalize_image(
                state,
                image,
                ref=ref,
                section_uid=section_uid,
                section_key=section_key,
                source_order=source_order,
                image_index=image_index,
                raw_relative=raw_relative,
                raw_sha=raw_sha,
            )
        )
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        sanitize_attributes(tag)
    return stable_html_fragment(soup), image_rows


def normalize_image(
    state: BuildState,
    image: Tag,
    *,
    ref: RawDocumentRef,
    section_uid: str,
    section_key: str,
    source_order: int,
    image_index: int,
    raw_relative: str,
    raw_sha: str,
) -> dict[str, Any]:
    src_present = image.has_attr("src")
    src = string_value(image.get("src")) if src_present else ""
    source_type = classify_image_src(src, src_present=src_present)
    mime_type: str | None = None
    asset_sha: str | None = None
    asset_path: str | None = None
    decoded_size: int | None = None
    decode_error: str | None = None
    if source_type == "base64":
        mime_type, token = split_data_uri(src)
        try:
            decoded = base64.b64decode(token, validate=True)
            decoded_size = len(decoded)
            asset_sha = sha256_bytes(decoded)
            asset_path = write_asset(state.output, decoded, mime_type)
            image["src"] = asset_path
            image["data-clinrec-asset-sha256"] = asset_sha
        except (binascii.Error, ValueError) as exc:
            decode_error = str(exc)
            image["src"] = ""
            image["data-clinrec-image-status"] = "decode_failed"
    image_id = f"{section_uid}:image#{image_index:04d}"
    image["data-clinrec-image-id"] = image_id
    return {
        "schema_version": PARSED_SCHEMA_VERSION,
        "image_id": image_id,
        "occurrence_id": image_id,
        "section_id": section_uid,
        "document_id": document_uid_for(ref.kind, ref.code_version, ref.current_code_version),
        "document_kind": ref.kind,
        "code_version": ref.code_version,
        "current_code_version": ref.current_code_version,
        "section_key": section_key,
        "source_order": source_order,
        "image_index": image_index,
        "source_type": source_type,
        "mime_type": mime_type,
        "asset_id": f"sha256:{asset_sha}" if asset_sha is not None else None,
        "asset_sha256": asset_sha,
        "asset_path": asset_path,
        "decoded_size_bytes": decoded_size,
        "decode_error": decode_error,
        "alt": string_value(image.get("alt")),
        "source_raw_path": raw_relative,
        "source_raw_sha256": raw_sha,
    }


def sanitize_attributes(tag: Tag) -> None:
    for attr in list(tag.attrs):
        lowered = attr.casefold()
        value = tag.get(attr)
        if lowered.startswith("on"):
            del tag.attrs[attr]
            continue
        if lowered in {"href", "src"} and javascript_url(value):
            del tag.attrs[attr]


def javascript_url(value: Any) -> bool:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    return text.strip().casefold().startswith("javascript:")


def stable_html_fragment(soup: BeautifulSoup) -> str:
    root = soup.body if soup.body is not None else soup
    return "".join(str(child) for child in root.children)


def write_asset(output: Path, content: bytes, mime_type: str | None) -> str:
    asset_sha = sha256_bytes(content)
    extension = extension_for_mime(mime_type)
    relative = f"assets/by-sha256/{asset_sha}.{extension}"
    write_atomic_bytes(output / relative, content)
    return relative


def extract_tables(
    html: str,
    *,
    ref: RawDocumentRef,
    section_uid: str,
    section_key: str,
    source_order: int,
    raw_relative: str,
    raw_sha: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for table_index, table in enumerate(soup.find_all("table")):
        if not isinstance(table, Tag):
            continue
        table_id = f"{section_uid}:table#{table_index + 1:04d}"
        table_rows = table_grid(table)
        classification = table_classification(table, table_rows)
        row = {
            "schema_version": PARSED_SCHEMA_VERSION,
            "table_id": table_id,
            "section_id": section_uid,
            "document_id": document_uid_for(ref.kind, ref.code_version, ref.current_code_version),
            "document_kind": ref.kind,
            "code_version": ref.code_version,
            "current_code_version": ref.current_code_version,
            "section_key": section_key,
            "source_order": source_order,
            "table_index": table_index,
            "classification": classification,
            "rows": table_rows,
            "row_count": len(table_rows),
            "column_count": max((len(item) for item in table_rows), default=0),
            "html_sha256": sha256_text(str(table)),
            "plain_text_sha256": sha256_text(table.get_text(" ", strip=True)),
            "source_raw_path": raw_relative,
            "source_raw_sha256": raw_sha,
        }
        rows.append(row)
    return rows


def table_grid(table: Tag) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    tr_items = [
        item
        for item in table.find_all("tr")
        if isinstance(item, Tag) and nearest_table(item) == table
    ]
    for row_index, tr in enumerate(tr_items):
        if not isinstance(tr, Tag):
            continue
        cells: list[dict[str, Any]] = []
        cell_items = [
            item
            for item in tr.find_all(["td", "th"])
            if isinstance(item, Tag) and nearest_table(item) == table
        ]
        for column_index, cell in enumerate(cell_items):
            if not isinstance(cell, Tag):
                continue
            cells.append(
                {
                    "row_index": row_index,
                    "column_index": column_index,
                    "tag": cell.name,
                    "text": visible_text(str(cell)),
                    "rowspan": positive_int(cell.get("rowspan")),
                    "colspan": positive_int(cell.get("colspan")),
                }
            )
        rows.append(cells)
    return rows


def nearest_table(tag: Tag) -> Tag | None:
    parent = tag.find_parent("table")
    return parent if isinstance(parent, Tag) else None


def table_classification(table: Tag, rows: list[list[dict[str, Any]]]) -> str:
    if not rows or not any(rows):
        return "malformed"
    if table.find("table") is not None:
        return "nested"
    widths = {sum(int(cell.get("colspan") or 1) for cell in row) for row in rows}
    has_spans = any(
        int(cell.get("rowspan") or 1) > 1 or int(cell.get("colspan") or 1) > 1
        for row in rows
        for cell in row
    )
    if len(widths) == 1 and not has_spans:
        return "simple_rectangular"
    return "complex"


def append_chunks_for_section(
    state: BuildState,
    section: dict[str, Any],
    tables: list[dict[str, Any]],
) -> None:
    text = string_value(section.get("plain_text"))
    if text:
        chunk_index = next_chunk_index(state.rag_chunks, string_value(section["section_id"]))
        chunk = chunk_for_text(section, text, chunk_index=chunk_index, table_ids=[])
        state.search_chunks.append(search_chunk_from_rag(chunk))
        state.rag_chunks.append(chunk)
        state.citation_rows.append(citation_row(chunk))
    for table in tables:
        table_text = table_text_for_chunk(table)
        if not table_text:
            continue
        chunk_index = next_chunk_index(state.rag_chunks, string_value(section["section_id"]))
        chunk = chunk_for_text(
            section,
            table_text,
            chunk_index=chunk_index,
            table_ids=[string_value(table.get("table_id"))],
        )
        state.search_chunks.append(search_chunk_from_rag(chunk))
        state.rag_chunks.append(chunk)
        state.citation_rows.append(citation_row(chunk))


def chunk_for_text(
    section: dict[str, Any],
    text: str,
    *,
    chunk_index: int,
    table_ids: list[str],
) -> dict[str, Any]:
    code_version = string_value(section.get("code_version"))
    section_key = string_value(section.get("section_key"))
    chunk_id = f"{code_version}:{section_key}:chunk#{chunk_index:04d}"
    return {
        "schema_version": PARSED_SCHEMA_VERSION,
        "chunk_id": chunk_id,
        "code_version": code_version,
        "document_kind": section.get("document_kind"),
        "current_code_version": section.get("current_code_version"),
        "document_title": None,
        "section_id": section.get("section_id"),
        "section_key": section_key,
        "section_title": section.get("section_title"),
        "chunk_index": chunk_index,
        "text": text,
        "context_text": string_value(section.get("section_title")),
        "token_estimate": estimate_tokens(text),
        "source_raw_path": section.get("source_raw_path"),
        "source_raw_sha256": section.get("source_raw_sha256"),
        "section_html_sha256": section.get("normalized_html_sha256"),
        "plain_text_sha256": section.get("plain_text_sha256"),
        "table_ids": table_ids,
        "image_ids": section.get("image_ids") or [],
        "citation": {
            "code_version": code_version,
            "section_key": section_key,
            "section_title": section.get("section_title"),
            "source_order": section.get("source_order"),
            "source_raw_sha256": section.get("source_raw_sha256"),
        },
    }


def search_chunk_from_rag(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PARSED_SCHEMA_VERSION,
        "chunk_id": chunk["chunk_id"],
        "code_version": chunk["code_version"],
        "document_kind": chunk["document_kind"],
        "section_id": chunk["section_id"],
        "section_key": chunk["section_key"],
        "section_title": chunk["section_title"],
        "text": chunk["text"],
        "token_estimate": chunk["token_estimate"],
        "table_ids": chunk["table_ids"],
        "image_ids": chunk["image_ids"],
    }


def citation_row(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "citation": chunk["citation"],
    }


def next_chunk_index(chunks: list[dict[str, Any]], section_id: str) -> int:
    return 1 + sum(1 for chunk in chunks if chunk.get("section_id") == section_id)


def table_text_for_chunk(table: dict[str, Any]) -> str:
    lines: list[str] = []
    for row in table.get("rows", []):
        if isinstance(row, list):
            text = " | ".join(string_value(cell.get("text")) for cell in row if cell.get("text"))
            if text:
                lines.append(text)
    return "\n".join(lines)


def build_relations(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_by_code_version = {
        string_value(row.get("code_version")): row
        for row in documents
        if row.get("document_kind") == "current"
    }
    previous_rows = [row for row in documents if row.get("document_kind") == "previous"]
    relations: list[dict[str, Any]] = []
    for current_code_version, current in sorted(current_by_code_version.items()):
        previous = next(
            (
                row
                for row in previous_rows
                if row.get("current_code_version") == current_code_version
            ),
            None,
        )
        previous_code_version = (
            string_value(previous.get("code_version")) if previous is not None else None
        )
        relation_kind = relation_kind_for(current_code_version, previous_code_version)
        relations.append(
            {
                "schema_version": PARSED_SCHEMA_VERSION,
                "current_code_version": current_code_version,
                "previous_code_version": previous_code_version,
                "same_code": same_code(current_code_version, previous_code_version),
                "version_delta": version_delta(current_code_version, previous_code_version),
                "relation_kind": relation_kind,
                "current_raw_sha256": current.get("source_raw_sha256"),
                "previous_raw_sha256": previous.get("source_raw_sha256")
                if previous is not None
                else None,
                "warnings": [],
            }
        )
    return relations


def write_dataset_artifacts(
    state: BuildState,
    options: ParsedBuildOptions,
    *,
    source_documents: int,
) -> None:
    dataset = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "dataset_id": options.output.name,
        "source_corpus": options.input.as_posix(),
        "source_documents": source_documents,
        "parsed_documents": len(state.documents),
        "failed_documents": len(state.anomalies),
        "documents": len(state.documents),
        "sections": len(state.sections),
        "tables": len(state.tables),
        "images": len(state.images),
        "search_chunks": len(state.search_chunks),
        "rag_chunks": len(state.rag_chunks),
    }
    write_json(state.output / "dataset.json", dataset)
    write_jsonl(state.output / "documents.jsonl", sorted_rows(state.documents, "document_id"))
    write_jsonl(state.output / "sections.jsonl", sorted_rows(state.sections, "section_id"))
    write_jsonl(state.output / "tables.jsonl", sorted_rows(state.tables, "table_id"))
    write_jsonl(state.output / "images.jsonl", sorted_rows(state.images, "image_id"))
    write_jsonl(
        state.output / "relations.jsonl",
        sorted_rows(state.relations, "current_code_version"),
    )
    write_jsonl(
        state.output / "search" / "chunks.jsonl",
        sorted_rows(state.search_chunks, "chunk_id"),
    )
    write_jsonl(state.output / "rag" / "chunks.jsonl", sorted_rows(state.rag_chunks, "chunk_id"))
    write_jsonl(
        state.output / "rag" / "citation-index.jsonl",
        sorted_rows(state.citation_rows, "chunk_id"),
    )
    write_jsonl(
        state.output / "rag" / "embedding-input.jsonl",
        embedding_rows(state.rag_chunks),
    )


def parsed_summary(state: BuildState, *, source_documents: int) -> dict[str, Any]:
    table_classes = Counter(string_value(row.get("classification")) for row in state.tables)
    image_sources = Counter(string_value(row.get("source_type")) for row in state.images)
    return {
        "schema_version": PARSED_SCHEMA_VERSION,
        "source_documents": source_documents,
        "parsed_documents": len(state.documents),
        "failed_documents": len(state.anomalies),
        "sections": len(state.sections),
        "tables": len(state.tables),
        "table_classifications": dict(sorted(table_classes.items())),
        "images": len(state.images),
        "images_by_source_type": dict(sorted(image_sources.items())),
        "base64_decoded": sum(1 for row in state.images if row.get("asset_sha256")),
        "external_images": sum(
            1 for row in state.images if row.get("source_type") in {"http", "https"}
        ),
        "image_decode_failures": sum(1 for row in state.images if row.get("decode_error")),
        "search_chunks": len(state.search_chunks),
        "rag_chunks": len(state.rag_chunks),
        "diff_pairs": 0,
        "warnings": 0,
        "errors": len(state.anomalies),
    }


def validate_parsed_dataset(input_path: Path) -> ParsedValidationSummary:
    dataset = read_json_file(input_path / "dataset.json")
    source_root = Path(string_value(dataset.get("source_corpus")))
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    text_checks: list[dict[str, Any]] = []
    documents = read_jsonl(input_path / "documents.jsonl")
    sections = read_jsonl(input_path / "sections.jsonl")
    images = read_jsonl(input_path / "images.jsonl")
    seen_ids: set[str] = set()
    for path, rows, key in (
        ("documents.jsonl", documents, "document_id"),
        ("sections.jsonl", sections, "section_id"),
        ("images.jsonl", images, "image_id"),
        ("tables.jsonl", read_jsonl(input_path / "tables.jsonl"), "table_id"),
    ):
        for row in rows:
            stable_id = string_value(row.get(key))
            if stable_id in seen_ids:
                errors.append(issue(path, "duplicate_stable_id", stable_id))
            seen_ids.add(stable_id)
    section_rows_by_document = group_rows(sections, "document_id")
    for document in documents:
        validate_document(input_path, source_root, document, section_rows_by_document, errors)
    for section in sections:
        validate_section_html(section, errors)
        text_checks.append(validate_text_preservation(source_root, section, errors))
    for image in images:
        validate_image_asset(input_path, image, errors, warnings)
    report = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "input": input_path.as_posix(),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "documents": len(documents),
            "sections": len(sections),
            "images": len(images),
            "errors": len(errors),
            "warnings": len(warnings),
        },
    }
    reports = input_path / "reports"
    write_json(reports / "parsed-validation.json", report)
    write_json(reports / "text-preservation.json", {"checks": text_checks})
    write_json(reports / "determinism.json", content_hash_manifest(input_path))
    markdown = render_validation_markdown(report)
    (reports / "parsed-validation.md").write_text(markdown, encoding="utf-8", newline="\n")
    return ParsedValidationSummary(
        input=input_path,
        valid=not errors,
        errors=len(errors),
        warnings=len(warnings),
        report_json=reports / "parsed-validation.json",
        report_markdown=reports / "parsed-validation.md",
    )


def validate_document(
    input_path: Path,
    source_root: Path,
    document: dict[str, Any],
    section_rows_by_document: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, Any]],
) -> None:
    document_id = string_value(document.get("document_id"))
    raw_path = source_root / string_value(document.get("source_raw_path"))
    if not raw_path.exists():
        errors.append(issue(document_id, "source_raw_missing", raw_path.as_posix()))
        return
    if sha256_file(raw_path) != document.get("source_raw_sha256"):
        errors.append(issue(document_id, "source_raw_sha_mismatch", raw_path.as_posix()))
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_count = len([section for section in raw_sections(payload) if isinstance(section, dict)])
    parsed_count = len(section_rows_by_document.get(document_id, []))
    if raw_count != parsed_count:
        errors.append(
            issue(
                document_id,
                "section_count_mismatch",
                {"raw": raw_count, "parsed": parsed_count},
            )
        )
    document_json = document_json_path(input_path, document)
    if not document_json.exists():
        errors.append(issue(document_id, "parsed_document_missing_document_json", None))


def validate_section_html(section: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    html = string_value(section.get("normalized_html"))
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(["script", "style"]) is not None:
        errors.append(issue(string_value(section.get("section_id")), "unsafe_html_tag", None))
    if "data:image/" in html:
        errors.append(
            issue(string_value(section.get("section_id")), "base64_in_normalized_html", None)
        )
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in tag.attrs:
            if attr.casefold().startswith("on"):
                errors.append(
                    issue(string_value(section.get("section_id")), "unsafe_event_handler", attr)
                )
            if attr.casefold() in {"href", "src"} and javascript_url(tag.get(attr)):
                errors.append(
                    issue(string_value(section.get("section_id")), "unsafe_javascript_url", attr)
                )


def validate_text_preservation(
    source_root: Path,
    section: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_path = source_root / string_value(section.get("source_raw_path"))
    source_order = int(section.get("source_order") or 0)
    raw_text = ""
    if raw_path.exists() and source_order > 0:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_items = [item for item in raw_sections(payload) if isinstance(item, dict)]
        if source_order <= len(raw_items):
            raw_text = normalize_text(visible_text(section_html(raw_items[source_order - 1])))
    normalized_text = normalize_text(visible_text(string_value(section.get("normalized_html"))))
    passed = raw_text == normalized_text
    if not passed:
        errors.append(issue(string_value(section.get("section_id")), "text_loss", None))
    return {
        "section_id": section.get("section_id"),
        "passed": passed,
        "raw_text_sha256": sha256_text(raw_text),
        "normalized_text_sha256": sha256_text(normalized_text),
    }


def validate_image_asset(
    input_path: Path,
    image: dict[str, Any],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    if image.get("decode_error"):
        warnings.append(issue(string_value(image.get("image_id")), "image_decode_failure", None))
        return
    asset_path = string_value(image.get("asset_path"))
    if image.get("source_type") == "base64" and not asset_path:
        errors.append(
            issue(
                string_value(image.get("image_id")),
                "unresolved_local_image_reference",
                None,
            )
        )
    if asset_path and not (input_path / asset_path).exists():
        errors.append(
            issue(
                string_value(image.get("image_id")),
                "unresolved_local_image_reference",
                asset_path,
            )
        )


def export_parsed_dataset(input_path: Path, output: Path) -> ParsedExportSummary:
    output.mkdir(parents=True, exist_ok=True)
    backend = output / "backend"
    frontend = output / "frontend"
    search = output / "search"
    rag = output / "rag"
    for source, target in (
        ("documents.jsonl", backend / "documents.jsonl"),
        ("sections.jsonl", backend / "sections.jsonl"),
        ("tables.jsonl", backend / "tables.jsonl"),
        ("images.jsonl", backend / "images.jsonl"),
        ("relations.jsonl", backend / "relations.jsonl"),
        ("dataset.json", backend / "dataset.json"),
        ("search/chunks.jsonl", search / "chunks.jsonl"),
        ("rag/chunks.jsonl", rag / "chunks.jsonl"),
        ("rag/citation-index.jsonl", rag / "citation-index.jsonl"),
        ("rag/embedding-input.jsonl", rag / "embedding-input.jsonl"),
    ):
        copy_file(input_path / source, target)
    documents = read_jsonl(input_path / "documents.jsonl")
    sections = group_rows(read_jsonl(input_path / "sections.jsonl"), "document_id")
    tables = group_rows(read_jsonl(input_path / "tables.jsonl"), "document_id")
    images = group_rows(read_jsonl(input_path / "images.jsonl"), "document_id")
    for document in documents:
        write_frontend_document(frontend, document, sections, tables, images)
    assets = copy_assets(input_path / "assets" / "by-sha256", frontend / "assets" / "by-sha256")
    manifest = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "dataset": read_json_file(input_path / "dataset.json"),
        "frontend_documents": len(documents),
        "assets": assets,
    }
    write_json(frontend / "manifest.json", manifest)
    write_checksums(output)
    write_json(output / "export-manifest.json", manifest)
    return ParsedExportSummary(
        input=input_path,
        output=output,
        backend_files=6,
        frontend_documents=len(documents),
        assets=assets,
        search_chunks=len(read_jsonl(search / "chunks.jsonl")),
        rag_chunks=len(read_jsonl(rag / "chunks.jsonl")),
        manifest_path=output / "export-manifest.json",
    )


def build_parsed_diff(input_path: Path) -> ParsedDiffSummary:
    relations = read_jsonl(input_path / "relations.jsonl")
    sections = read_jsonl(input_path / "sections.jsonl")
    tables = read_jsonl(input_path / "tables.jsonl")
    images = read_jsonl(input_path / "images.jsonl")
    sections_by_doc = group_rows(sections, "document_id")
    tables_by_doc = group_rows(tables, "document_id")
    images_by_doc = group_rows(images, "document_id")
    pair_rows: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []
    table_rows: list[dict[str, Any]] = []
    image_rows: list[dict[str, Any]] = []
    for relation in relations:
        previous_code_version = relation.get("previous_code_version")
        if not previous_code_version:
            continue
        current_doc = f"current:{relation['current_code_version']}"
        previous_doc = f"previous:{relation['current_code_version']}:{previous_code_version}"
        pair_rows.append(
            {
                "current_code_version": relation["current_code_version"],
                "previous_code_version": previous_code_version,
                "raw_byte_identical": relation.get("current_raw_sha256")
                == relation.get("previous_raw_sha256"),
                "relation_kind": relation.get("relation_kind"),
            }
        )
        section_rows.extend(diff_sections(current_doc, previous_doc, sections_by_doc))
        table_rows.extend(diff_asset_rows(current_doc, previous_doc, tables_by_doc, "table_id"))
        image_rows.extend(diff_asset_rows(current_doc, previous_doc, images_by_doc, "image_id"))
    diff_root = input_path / "diff"
    write_jsonl(diff_root / "pairs.jsonl", pair_rows)
    write_jsonl(diff_root / "sections.jsonl", section_rows)
    write_jsonl(diff_root / "tables.jsonl", table_rows)
    write_jsonl(diff_root / "images.jsonl", image_rows)
    summary = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "pairs": len(pair_rows),
        "section_changes": len(section_rows),
        "table_changes": len(table_rows),
        "image_changes": len(image_rows),
    }
    write_json(diff_root / "summary.json", summary)
    parsed_summary_path = input_path / "reports" / "parsed-summary.json"
    parsed = read_json_file(parsed_summary_path)
    parsed["diff_pairs"] = len(pair_rows)
    write_json(parsed_summary_path, parsed)
    return ParsedDiffSummary(
        input=input_path,
        pairs=len(pair_rows),
        section_changes=len(section_rows),
        table_changes=len(table_rows),
        image_changes=len(image_rows),
        summary_path=diff_root / "summary.json",
    )


def diff_sections(
    current_doc: str,
    previous_doc: str,
    sections_by_doc: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    current = {string_value(row.get("section_key")): row for row in sections_by_doc[current_doc]}
    previous = {string_value(row.get("section_key")): row for row in sections_by_doc[previous_doc]}
    rows: list[dict[str, Any]] = []
    for section_key in sorted(set(current) | set(previous)):
        left = current.get(section_key)
        right = previous.get(section_key)
        if left is None or right is None:
            rows.append({"section_key": section_key, "change": "section_added_or_removed"})
            continue
        if left.get("plain_text_sha256") != right.get("plain_text_sha256"):
            rows.append(
                {
                    "section_key": section_key,
                    "change": "visible_text_changed",
                    "text_similarity": text_similarity(
                        string_value(left.get("plain_text")),
                        string_value(right.get("plain_text")),
                    ),
                }
            )
        elif left.get("normalized_html_sha256") != right.get("normalized_html_sha256"):
            rows.append({"section_key": section_key, "change": "markup_only_changed"})
    return rows


def diff_asset_rows(
    current_doc: str,
    previous_doc: str,
    rows_by_doc: dict[str, list[dict[str, Any]]],
    key: str,
) -> list[dict[str, Any]]:
    current = {string_value(row.get(key)): row for row in rows_by_doc.get(current_doc, [])}
    previous = {string_value(row.get(key)): row for row in rows_by_doc.get(previous_doc, [])}
    rows: list[dict[str, Any]] = []
    for item_id in sorted(set(current) | set(previous)):
        left = current.get(item_id)
        right = previous.get(item_id)
        if left is None or right is None:
            rows.append({key: item_id, "change": "added_or_removed"})
    return rows


def document_output_dir(output: Path, ref: RawDocumentRef) -> Path:
    if ref.kind == "previous":
        return (
            output
            / "documents"
            / "previous"
            / string_value(ref.current_code_version)
            / ref.code_version
        )
    return output / "documents" / "current" / ref.code_version


def document_json_path(input_path: Path, document: dict[str, Any]) -> Path:
    if document.get("document_kind") == "previous":
        return (
            input_path
            / "documents"
            / "previous"
            / string_value(document.get("current_code_version"))
            / string_value(document.get("code_version"))
            / "document.json"
        )
    return (
        input_path
        / "documents"
        / "current"
        / string_value(document.get("code_version"))
        / "document.json"
    )


def document_uid_for(kind: str, code_version: str, current_code_version: str | None) -> str:
    if kind == "previous":
        return f"previous:{current_code_version}:{code_version}"
    return f"current:{code_version}"


def source_raw_path(root: Path, raw_path: Path) -> str:
    return raw_path.relative_to(root).as_posix()


def document_title(payload: dict[str, Any]) -> str:
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    return string_value(
        first_present(payload, "name", "Name", "title", "Title")
        or first_present(obj, "name", "Name", "title", "Title")
    )


def section_title(section: dict[str, Any]) -> str:
    return string_value(first_present(section, "name", "Name", "title", "Title"))


def first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def visible_text(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip()).strip("_")
    return cleaned or "section"


def positive_int(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def classify_image_src(src: str, *, src_present: bool) -> str:
    if not src_present:
        return "missing"
    if not src:
        return "empty"
    lowered = src.casefold()
    if lowered.startswith("data:") and ";base64," in lowered:
        return "base64"
    if lowered.startswith("http://"):
        return "http"
    if lowered.startswith("https://"):
        return "https"
    return "relative"


def split_data_uri(src: str) -> tuple[str | None, str]:
    prefix, token = src.split(",", maxsplit=1)
    mime_type = prefix[5:].split(";", maxsplit=1)[0] if prefix.startswith("data:") else None
    return mime_type, token


def extension_for_mime(mime_type: str | None) -> str:
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/svg+xml": "svg",
    }.get(string_value(mime_type).casefold(), "bin")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def relation_kind_for(current: str, previous: str | None) -> str:
    if previous is None:
        return "current_only_active"
    delta = version_delta(current, previous)
    return "version_minus_one_candidate" if delta == 1 else "unknown"


def same_code(current: str, previous: str | None) -> bool:
    if previous is None:
        return False
    try:
        current_code, _ = parse_code_version_or_raise(current)
        previous_code, _ = parse_code_version_or_raise(previous)
    except BankError:
        return False
    return current_code == previous_code


def version_delta(current: str, previous: str | None) -> int | None:
    if previous is None:
        return None
    try:
        current_code, current_version = parse_code_version_or_raise(current)
        previous_code, previous_version = parse_code_version_or_raise(previous)
    except BankError:
        return None
    if current_code != previous_code:
        return None
    return current_version - previous_version


def sorted_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: string_value(row.get(key)))


def embedding_rows(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": chunk["chunk_id"],
            "text": chunk["text"],
            "metadata": {
                "code_version": chunk.get("code_version"),
                "document_kind": chunk.get("document_kind"),
                "section_key": chunk.get("section_key"),
                "citation": chunk.get("citation"),
            },
        }
        for chunk in sorted_rows(chunks, "chunk_id")
    ]


def issue(path: str, code: str, details: Any) -> dict[str, Any]:
    return {"path": path, "code": code, "details": details}


def group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(string_value(row.get(key)), []).append(row)
    return grouped


def content_hash_manifest(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("reports/"):
            continue
        rows.append({"path": relative, "sha256": sha256_file(path)})
    digest = sha256_bytes(json.dumps(rows, sort_keys=True).encode("utf-8"))
    return {"schema_version": PARSED_SCHEMA_VERSION, "content_sha256": digest, "files": rows}


def render_validation_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Parsed validation",
        "",
        f"- valid: {report['valid']}",
        f"- documents: {summary['documents']}",
        f"- sections: {summary['sections']}",
        f"- errors: {summary['errors']}",
        f"- warnings: {summary['warnings']}",
        "",
        "## Error codes",
    ]
    for code, count in sorted(Counter(row["code"] for row in report["errors"]).items()):
        lines.append(f"- {code}: {count}")
    return "\n".join(lines) + "\n"


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def write_frontend_document(
    root: Path,
    document: dict[str, Any],
    sections: dict[str, list[dict[str, Any]]],
    tables: dict[str, list[dict[str, Any]]],
    images: dict[str, list[dict[str, Any]]],
) -> None:
    document_id = string_value(document.get("document_id"))
    code_version = string_value(document.get("code_version"))
    payload = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "metadata": document,
        "toc": [
            {
                "section_id": section.get("section_id"),
                "section_key": section.get("section_key"),
                "title": section.get("section_title"),
                "source_order": section.get("source_order"),
            }
            for section in sections.get(document_id, [])
        ],
        "sections": sections.get(document_id, []),
        "tables": tables.get(document_id, []),
        "images": images.get(document_id, []),
        "warnings": document.get("warnings") or [],
    }
    write_json(root / "documents" / f"{code_version}.json", payload)


def copy_assets(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    count = 0
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        copy_file(path, target / path.name)
        count += 1
    return count


def write_checksums(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            rows.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "checksums.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")


def text_similarity(left: str, right: str) -> float:
    return round(SequenceMatcher(a=left, b=right).ratio(), 4)
