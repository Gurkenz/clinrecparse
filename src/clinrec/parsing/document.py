from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, cast

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag
from pydantic import ValidationError

from clinrec.api.catalog_sync import split_code_version, to_int, write_json, write_jsonl
from clinrec.config import Settings
from clinrec.models.external import ClinrecResponse, QaIssue


class ParseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParseOptions:
    code_versions: list[str] | None = None
    code: int | None = None
    from_code: int | None = None
    to_code: int | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class ParsedDocumentSummary:
    code_version: str
    document_dir: Path
    document_json_path: Path
    markdown_path: Path
    search_chunks_path: Path
    qa_report_path: Path
    sections: int
    blocks: int
    tables: int
    images: int
    recommendations: int
    references: int
    issues: int
    status: str


@dataclass(frozen=True)
class ParseSummary:
    timestamp: str
    planned: int
    parsed: int
    failed: int
    documents: list[ParsedDocumentSummary]


@dataclass
class ParseState:
    document_dir: Path
    timestamp: str
    issues: list[QaIssue]
    sections: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    images: list[dict[str, Any]]
    references: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]

    block_order: int = 0
    table_order: int = 0
    image_order: int = 0
    reference_order: int = 0
    recommendation_order: int = 0


SUPPORTED_TAGS = {
    "a",
    "br",
    "caption",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "img",
    "li",
    "ol",
    "p",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
HEADING_TAGS = {f"h{level}" for level in range(1, 7)}
DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[-\w.+]+/[-\w.+]+)(?:;[-\w.+]+=[^;,]+)*;base64,(?P<data>.*)$",
    re.IGNORECASE | re.DOTALL,
)
REFERENCE_RE = re.compile(
    r"\[(?P<body>\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)\]"
)
NUMBER_RE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)*)(?:[.)]\s+|\s+|$)")
RECOMMENDATION_RE = re.compile(
    r"(^|[\n\r•\-\u2013]\s*|[.!?]\s+)"
    r"(Рекомендуется|Рекомендуются|Рекомендовано|Рекомендованы|Рекомендуем)\b",
    re.IGNORECASE,
)
COMMENT_RE = re.compile(r"^\s*(Комментар(?:ий|ии)|Примечание)\b", re.IGNORECASE)
UUR_PATTERNS = [
    re.compile(r"\bУУР\s*[:\-–]?\s*([A-CАВС])\b", re.IGNORECASE),
    re.compile(
        r"Уровень\s+убедительности\s+рекомендац\w*\s*[:\-–]?\s*([A-CАВС])\b",
        re.IGNORECASE,
    ),
]
UDD_PATTERNS = [
    re.compile(r"\bУДД\s*[:\-–]?\s*([1-5][A-CАВС]?)\b", re.IGNORECASE),
    re.compile(
        r"Уровень\s+достоверности\s+доказательств\s*[:\-–]?\s*([1-5][A-CАВС]?)\b",
        re.IGNORECASE,
    ),
]
EXPECTED_843_1 = {"sections": 31, "tables": 14, "images": 30}
IMAGE_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}


def parse_documents(settings: Settings, options: ParseOptions) -> ParseSummary:
    timestamp = options.timestamp or utc_timestamp()
    candidates = select_document_dirs(settings, options)
    if not candidates:
        raise ParseError("No matching document directories found for parsing.")

    documents: list[ParsedDocumentSummary] = []
    for document_dir in candidates:
        source_path = document_dir / "source" / "getclinrec.json"
        if not source_path.exists():
            summary = write_missing_source_report(document_dir, timestamp)
            documents.append(summary)
            continue
        documents.append(parse_one_document(settings, document_dir, timestamp=timestamp))

    parsed = sum(1 for document in documents if document.status == "parsed")
    failed = len(documents) - parsed
    return ParseSummary(
        timestamp=timestamp,
        planned=len(candidates),
        parsed=parsed,
        failed=failed,
        documents=documents,
    )


def parse_one_document(
    settings: Settings,
    document_dir: Path,
    *,
    timestamp: str | None = None,
) -> ParsedDocumentSummary:
    current_timestamp = timestamp or utc_timestamp()
    source_path = document_dir / "source" / "getclinrec.json"
    if not source_path.exists():
        raise ParseError(f"Source JSON is missing: {source_path}")

    try:
        raw_payload = json.loads(source_path.read_text(encoding="utf-8"))
        response = ClinrecResponse.model_validate(raw_payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ParseError(f"Cannot parse source JSON {source_path}: {exc}") from exc

    raw_obj = as_mapping(first_present(raw_payload, "obj", "Obj", "data", "Data"))
    raw_sections = as_list(first_present(raw_obj, "sections", "Sections"))
    catalog_record = load_catalog_record(settings, document_dir, response.obj.code_version)
    document = build_document_metadata(response, raw_obj, catalog_record, document_dir)

    state = ParseState(
        document_dir=document_dir,
        timestamp=current_timestamp,
        issues=[],
        sections=[],
        blocks=[],
        tables=[],
        images=[],
        references=[],
        recommendations=[],
    )
    if not (document_dir / "source" / "official.pdf").exists():
        state.issues.append(
            QaIssue(
                severity="warning",
                code="missing_pdf",
                message="Official PDF is not available for visual/control validation.",
                context={"path": "source/official.pdf"},
            )
        )

    for source_order, section in enumerate(raw_sections, start=1):
        if isinstance(section, dict):
            parse_section(state, section, source_order=source_order, parent_id=None, depth=1)
        else:
            state.issues.append(
                QaIssue(
                    severity="error",
                    code="invalid_section",
                    message="Section entry is not an object.",
                    context={"source_order": source_order, "type": type(section).__name__},
                )
            )

    state.recommendations.extend(extract_recommendations(state))
    payload = {
        "document": document,
        "sections": state.sections,
        "blocks": state.blocks,
        "tables": state.tables,
        "images": state.images,
        "recommendations": state.recommendations,
        "references": state.references,
    }

    parsed_dir = document_dir / "parsed"
    qa_dir = document_dir / "qa"
    document_json_path = parsed_dir / "document.json"
    markdown_path = parsed_dir / "content.md"
    search_chunks_path = parsed_dir / "search_chunks.jsonl"
    qa_report_path = qa_dir / "parse-report.json"

    write_json(document_json_path, payload)
    markdown = render_markdown(document, state.sections, table_lookup(state.tables))
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    write_jsonl(search_chunks_path, build_search_chunks(document, state))
    write_qa_report(
        qa_report_path,
        state,
        document=document,
        source_path=source_path,
    )

    return ParsedDocumentSummary(
        code_version=str(document["code_version"]),
        document_dir=document_dir,
        document_json_path=document_json_path,
        markdown_path=markdown_path,
        search_chunks_path=search_chunks_path,
        qa_report_path=qa_report_path,
        sections=len(state.sections),
        blocks=len(state.blocks),
        tables=len(state.tables),
        images=len(state.images),
        recommendations=len(state.recommendations),
        references=len(state.references),
        issues=len(state.issues),
        status="parsed",
    )


def select_document_dirs(settings: Settings, options: ParseOptions) -> list[Path]:
    documents_root = settings.paths.documents
    explicit_code_versions = options.code_versions or []
    if explicit_code_versions:
        return [
            documents_root / str(code) / code_version
            for code_version in explicit_code_versions
            for code, _version in [split_code_version(code_version)]
            if code is not None
        ]

    if not documents_root.exists():
        return []

    candidates = [
        path
        for code_dir in sorted(documents_root.iterdir(), key=lambda item: item.name)
        if code_dir.is_dir()
        for path in sorted(code_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir()
    ]
    filtered = [path for path in candidates if matches_filters(path.name, options)]
    if has_filter(options):
        return filtered
    return [path for path in filtered if (path / "source" / "getclinrec.json").exists()]


def write_missing_source_report(document_dir: Path, timestamp: str) -> ParsedDocumentSummary:
    code_version = document_dir.name
    qa_report_path = document_dir / "qa" / "parse-report.json"
    issue = QaIssue(
        severity="error",
        code="missing_source_json",
        message="source/getclinrec.json is required before parsing.",
        context={"path": "source/getclinrec.json"},
    )
    write_json(
        qa_report_path,
        {
            "timestamp": timestamp,
            "code_version": code_version,
            "status": "failed",
            "counts": {
                "sections": 0,
                "blocks": 0,
                "tables": 0,
                "images": 0,
                "recommendations": 0,
                "references": 0,
            },
            "issues": [issue.model_dump(mode="json")],
        },
    )
    return ParsedDocumentSummary(
        code_version=code_version,
        document_dir=document_dir,
        document_json_path=document_dir / "parsed" / "document.json",
        markdown_path=document_dir / "parsed" / "content.md",
        search_chunks_path=document_dir / "parsed" / "search_chunks.jsonl",
        qa_report_path=qa_report_path,
        sections=0,
        blocks=0,
        tables=0,
        images=0,
        recommendations=0,
        references=0,
        issues=1,
        status="failed",
    )


def build_document_metadata(
    response: ClinrecResponse,
    raw_obj: dict[str, Any],
    catalog_record: dict[str, Any],
    document_dir: Path,
) -> dict[str, Any]:
    raw_code_version = first_non_empty(
        response.obj.code_version,
        first_present(raw_obj, "code_version", "CodeVersion", "id", "Id", "ID"),
        catalog_record.get("code_version"),
        document_dir.name,
    )
    code_version = str(raw_code_version)
    parsed_code, parsed_version = split_code_version(code_version)
    code = first_int(
        response.obj.code,
        raw_obj.get("code"),
        raw_obj.get("Code"),
        catalog_record.get("code"),
    )
    version = first_int(
        response.obj.version,
        raw_obj.get("version"),
        raw_obj.get("Version"),
        catalog_record.get("version"),
    )
    title = first_non_empty(
        response.obj.title,
        first_present(raw_obj, "title", "Title", "name", "Name"),
        catalog_record.get("name"),
        catalog_record.get("title"),
    )
    age_category = first_present(
        raw_obj,
        "age",
        "Age",
        "age_category",
        "AgeCategory",
    )
    age: dict[str, Any]
    if isinstance(age_category, dict):
        age = age_category
    else:
        age = {}
        catalog_age = catalog_record.get("age_category")
        catalog_age_name = catalog_record.get("age_category_name")
        if catalog_age is not None:
            age["category"] = catalog_age
        if catalog_age_name is not None:
            age["category_name"] = catalog_age_name

    return {
        "code": code if code is not None else parsed_code,
        "version": version if version is not None else parsed_version,
        "code_version": code_version,
        "title": str(title or ""),
        "age": age,
        "mkbs": as_list(first_present(raw_obj, "mkbs", "MKBs", "Mkbs", "Mkb"))
        or as_list(catalog_record.get("mkbs")),
        "developers": as_list(
            first_present(raw_obj, "developers", "Developers", "developer", "Developer")
        )
        or as_list(catalog_record.get("developers")),
    }


def parse_section(
    state: ParseState,
    raw_section: dict[str, Any],
    *,
    source_order: int,
    parent_id: str | None,
    depth: int,
) -> None:
    source_id = first_present(raw_section, "id", "Id", "ID")
    section_id = str(source_id if source_id is not None else f"section-{len(state.sections) + 1}")
    section_title = str(first_present(raw_section, "title", "Title", "name", "Name") or "")
    source_html = str(
        first_present(raw_section, "html", "Html", "HTML", "text", "Text", "content", "Content")
        or ""
    )
    soup = BeautifulSoup(source_html, "lxml")
    root = soup.body or soup

    section_blocks = parse_section_html(
        state,
        root,
        section_id=section_id,
        section_title=section_title,
    )
    section = {
        "id": source_id,
        "section_id": section_id,
        "order": len(state.sections) + 1,
        "source_order": source_order,
        "parent_id": parent_id,
        "depth": depth,
        "title": section_title,
        "source_html": source_html,
        "html": render_fragment_html(root),
        "found": raw_section.get("found"),
        "donotsearch": raw_section.get("donotsearch"),
        "required": raw_section.get("required"),
        "rules": raw_section.get("rules"),
        "blocks": section_blocks,
    }
    state.sections.append(section)

    nested_sections = as_list(
        first_present(raw_section, "sections", "Sections", "children", "Children")
    )
    for nested_order, nested in enumerate(nested_sections, start=1):
        if isinstance(nested, dict):
            parse_section(
                state,
                nested,
                source_order=nested_order,
                parent_id=section_id,
                depth=depth + 1,
            )
        else:
            state.issues.append(
                QaIssue(
                    severity="error",
                    code="invalid_nested_section",
                    message="Nested section entry is not an object.",
                    context={
                        "parent_section_id": section_id,
                        "source_order": nested_order,
                        "type": type(nested).__name__,
                    },
                )
            )


def parse_section_html(
    state: ParseState,
    root: Tag,
    *,
    section_id: str,
    section_title: str,
) -> list[dict[str, Any]]:
    register_unknown_tags(state, root, section_id)
    tables_by_tag = process_tables(state, root, section_id=section_id)
    process_images_outside_tables(state, root, section_id=section_id)

    children = meaningful_children(root)
    if not children and root.get_text(strip=True):
        children = [NavigableString(root.get_text())]

    blocks: list[dict[str, Any]] = []
    for child in children:
        block = build_block(
            state,
            child,
            section_id=section_id,
            section_title=section_title,
            tables_by_tag=tables_by_tag,
        )
        if block is not None:
            state.blocks.append(block)
            blocks.append(block)
    return blocks


def build_block(
    state: ParseState,
    element: PageElement,
    *,
    section_id: str,
    section_title: str,
    tables_by_tag: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if isinstance(element, NavigableString):
        text = normalize_text(str(element))
        if not text:
            return None
        source_html = escape(str(element))
        tag_name = None
        block_type = "paragraph"
        html = source_html
    elif isinstance(element, Tag):
        tag_name = element.name.lower()
        text = normalize_text(element.get_text(" ", strip=True))
        source_html = str(element)
        html = str(element)
        block_type = block_type_for_tag(tag_name)
    else:
        return None

    state.block_order += 1
    block_id = f"block-{state.block_order:04d}"
    references = register_references(state, text, section_id=section_id, block_id=block_id)
    block: dict[str, Any] = {
        "id": block_id,
        "order": state.block_order,
        "section_id": section_id,
        "type": block_type,
        "tag": tag_name,
        "source_html": source_html,
        "html": html,
        "text": text,
        "references": references,
    }
    if isinstance(element, Tag) and tag_name in HEADING_TAGS:
        block["heading"] = parse_heading(element, section_id, section_title, state)
    if isinstance(element, Tag) and tag_name == "table":
        table = tables_by_tag.get(id(element))
        if table is not None:
            block["table_id"] = table["id"]
    if isinstance(element, Tag):
        image_ids = [
            str(image.get("id"))
            for image in state.images
            if image.get("section_id") == section_id and source_html_contains_image(element, image)
        ]
        if image_ids:
            block["image_ids"] = image_ids
    return block


def process_tables(state: ParseState, root: Tag, *, section_id: str) -> dict[int, dict[str, Any]]:
    tables_by_tag: dict[int, dict[str, Any]] = {}
    for table_tag in root.find_all("table"):
        table = table_tag
        state.table_order += 1
        table_id = f"table-{state.table_order:04d}"
        table_source_html = str(table)
        rows: list[dict[str, Any]] = []
        header_rows: list[int] = []
        for row_index, row_tag in enumerate(table.find_all("tr"), start=1):
            row = row_tag
            cells: list[dict[str, Any]] = []
            is_header_row = row.find_parent("thead") is not None
            for column_index, cell_tag in enumerate(
                row.find_all(["td", "th"], recursive=False),
                start=1,
            ):
                cell = cell_tag
                if cell.name == "th":
                    is_header_row = True
                images = [
                    process_image(
                        state,
                        img_tag,
                        section_id=section_id,
                        table_id=table_id,
                        row=row_index,
                        column=column_index,
                    )
                    for img_tag in cell.find_all("img")
                ]
                cells.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "kind": cell.name,
                        "text": normalize_text(cell.get_text(" ", strip=True)),
                        "html": inner_html(cell),
                        "rowspan": parse_span(cell.get("rowspan")),
                        "colspan": parse_span(cell.get("colspan")),
                        "images": [image["id"] for image in images if image is not None],
                    }
                )
            if is_header_row:
                header_rows.append(row_index)
            rows.append({"index": row_index, "cells": cells})

        caption = find_table_caption(table)
        table_record: dict[str, Any] = {
            "id": table_id,
            "order": state.table_order,
            "section_id": section_id,
            "position_in_section": element_position(root, table),
            "caption": caption,
            "source_html": table_source_html,
            "html": str(table),
            "rows": rows,
            "header_rows": header_rows,
            "is_complex": is_complex_table(rows),
        }
        state.tables.append(table_record)
        tables_by_tag[id(table)] = table_record
    return tables_by_tag


def process_images_outside_tables(state: ParseState, root: Tag, *, section_id: str) -> None:
    for image_tag in root.find_all("img"):
        image = image_tag
        if image.find_parent("table") is None:
            process_image(
                state,
                image,
                section_id=section_id,
                table_id=None,
                row=None,
                column=None,
            )


def process_image(
    state: ParseState,
    image_tag: Tag,
    *,
    section_id: str,
    table_id: str | None,
    row: int | None,
    column: int | None,
) -> dict[str, Any] | None:
    existing_id = image_tag.get("data-clinrec-image-id")
    if isinstance(existing_id, str):
        for image in state.images:
            if image.get("id") == existing_id:
                return image

    state.image_order += 1
    image_id = f"image-{state.image_order:04d}"
    src = str(image_tag.get("src") or "")
    alt = str(image_tag.get("alt") or "")
    data_match = DATA_URI_RE.match(src)
    if data_match:
        mime = data_match.group("mime").lower()
        try:
            encoded = re.sub(r"\s+", "", data_match.group("data"))
            content = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            state.issues.append(
                QaIssue(
                    severity="error",
                    code="invalid_image_data_uri",
                    message="Image data URI contains invalid base64.",
                    context={"section_id": section_id, "image_id": image_id},
                )
            )
            content = b""
        extension = IMAGE_EXTENSIONS.get(mime, "bin")
        asset_relative = f"assets/{section_id}/{image_id}.{extension}"
        asset_path = state.document_dir / asset_relative
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        size = len(content)
        derived_src = f"../{asset_relative}"
        image_tag["src"] = derived_src
        embedded = True
    else:
        mime = None
        sha256 = None
        size = 0
        asset_relative = src
        derived_src = src
        embedded = False
        state.issues.append(
            QaIssue(
                severity="warning",
                code="non_data_uri_image",
                message="Image source is not a data URI and was not decoded.",
                context={"section_id": section_id, "image_id": image_id, "src": src},
            )
        )

    image_tag["data-clinrec-image-id"] = image_id
    image_record: dict[str, Any] = {
        "id": image_id,
        "order": state.image_order,
        "section_id": section_id,
        "table_id": table_id,
        "row": row,
        "column": column,
        "mime": mime,
        "sha256": sha256,
        "size": size,
        "alt": alt,
        "source_src": "data-uri" if data_match else src,
        "path": asset_relative,
        "derived_src": derived_src,
        "embedded": embedded,
    }
    state.images.append(image_record)
    return image_record


def register_unknown_tags(state: ParseState, root: Tag, section_id: str) -> None:
    for tag in root.find_all(True):
        current = tag
        name = current.name.lower()
        if name not in SUPPORTED_TAGS:
            state.issues.append(
                QaIssue(
                    severity="warning",
                    code="unknown_html_tag",
                    message="HTML tag is not in the supported tag list.",
                    context={
                        "section_id": section_id,
                        "tag": name,
                        "text": normalize_text(current.get_text(" ", strip=True)),
                        "source_html": str(current),
                    },
                )
            )


def parse_heading(
    tag: Tag,
    section_id: str,
    section_title: str,
    state: ParseState,
) -> dict[str, Any]:
    source_text = normalize_text(tag.get_text(" ", strip=True))
    source_number = extract_number(source_text)
    section_number = extract_number(section_title) or section_number_from_id(section_id)
    normalized_number = source_number
    normalization_status = "unchanged" if source_number else "missing_number"
    if source_number and section_number:
        corrected = correct_number_by_parent(source_number, section_number)
        if corrected != source_number:
            normalized_number = corrected
            normalization_status = "corrected_by_parent_context"
            state.issues.append(
                QaIssue(
                    severity="warning",
                    code="heading_number_corrected",
                    message="Heading number was corrected from parent section context.",
                    context={
                        "section_id": section_id,
                        "source_text": source_text,
                        "source_number": source_number,
                        "normalized_number": normalized_number,
                        "parent_number": section_number,
                    },
                )
            )
    tag_level = int(tag.name[1]) if tag.name in HEADING_TAGS else 1
    return {
        "source_text": source_text,
        "source_number": source_number,
        "normalized_number": normalized_number,
        "level": len(source_number.split(".")) if source_number else tag_level,
        "parent_section_id": section_id,
        "normalization_status": normalization_status,
    }


def extract_recommendations(state: ParseState) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    blocks_by_section: dict[str, list[dict[str, Any]]] = {}
    for block in state.blocks:
        blocks_by_section.setdefault(str(block["section_id"]), []).append(block)

    for section_id, blocks in blocks_by_section.items():
        index = 0
        while index < len(blocks):
            block = blocks[index]
            text = str(block.get("text") or "")
            if not is_recommendation_start(block, text):
                index += 1
                continue

            group = [block]
            lookahead = index + 1
            while lookahead < len(blocks) and is_recommendation_neighbor(blocks[lookahead]):
                group.append(blocks[lookahead])
                lookahead += 1

            state.recommendation_order += 1
            combined_text = "\n".join(str(item.get("text") or "") for item in group).strip()
            combined_html = "\n".join(str(item.get("source_html") or "") for item in group)
            comments = [
                str(item.get("text") or "")
                for item in group[1:]
                if COMMENT_RE.search(str(item.get("text") or ""))
            ]
            reference_occurrences = merge_reference_occurrences(group)
            recommendation = {
                "id": f"recommendation-{state.recommendation_order:04d}",
                "order": state.recommendation_order,
                "section_id": section_id,
                "text": str(block.get("text") or ""),
                "uur": extract_uur(combined_text),
                "udd": extract_udd(combined_text),
                "comments": comments,
                "literature_references": reference_occurrences,
                "source_html": combined_html,
                "block_ids": [item["id"] for item in group],
            }
            recommendations.append(recommendation)
            index = lookahead
    return recommendations


def register_references(
    state: ParseState,
    text: str,
    *,
    section_id: str,
    block_id: str,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for match in REFERENCE_RE.finditer(text):
        state.reference_order += 1
        source_text = match.group(0)
        numbers = normalize_reference_numbers(match.group("body"))
        reference = {
            "id": f"reference-{state.reference_order:04d}",
            "order": state.reference_order,
            "section_id": section_id,
            "block_id": block_id,
            "source_text": source_text,
            "numbers": numbers,
        }
        state.references.append(reference)
        references.append(reference)
    return references


def render_markdown(
    document: dict[str, Any],
    sections: list[dict[str, Any]],
    tables: dict[str, dict[str, Any]],
) -> str:
    lines: list[str] = [f"# {document['title']}", "", f"`{document['code_version']}`", ""]
    for section in sections:
        title = str(section.get("title") or section.get("section_id"))
        anchor = stable_anchor("section", int(section["order"]), str(section.get("section_id")))
        lines.extend([f"## {title} {{#{anchor}}}", ""])
        for block in section.get("blocks", []):
            rendered = render_block_markdown(cast(dict[str, Any], block), tables)
            if rendered:
                lines.extend([rendered, ""])
    return "\n".join(lines).rstrip() + "\n"


def render_block_markdown(block: dict[str, Any], tables: dict[str, dict[str, Any]]) -> str:
    block_type = block.get("type")
    if block_type == "heading":
        heading = cast(dict[str, Any], block.get("heading") or {})
        level = max(1, min(6, int(heading.get("level") or 2)))
        anchor = stable_anchor(
            "heading",
            int(block["order"]),
            str(heading.get("normalized_number") or ""),
        )
        return f"{'#' * level} {heading.get('source_text') or block.get('text')} {{#{anchor}}}"
    if block_type == "list":
        soup = BeautifulSoup(str(block.get("html") or ""), "lxml")
        root = soup.find(["ul", "ol"]) or soup
        return render_list_markdown(root)
    if block_type == "table":
        table_id = block.get("table_id")
        table = tables.get(str(table_id))
        if table is None:
            return str(block.get("html") or "")
        if not table.get("is_complex"):
            return render_table_markdown(table)
        return str(table.get("html") or block.get("html") or "")
    if block_type == "image":
        soup = BeautifulSoup(str(block.get("html") or ""), "lxml")
        image = soup.find("img")
        if isinstance(image, Tag):
            alt = str(image.get("alt") or "")
            src = str(image.get("src") or "")
            return f"![{escape_markdown(alt)}]({src})"
    if block_type == "paragraph":
        return inline_html_to_markdown(str(block.get("html") or block.get("text") or ""))
    return str(block.get("text") or "")


def render_list_markdown(root: Tag, *, ordered: bool | None = None, depth: int = 0) -> str:
    if ordered is None:
        ordered = root.name == "ol"
    lines: list[str] = []
    for index, li_tag in enumerate(root.find_all("li", recursive=False), start=1):
        li = li_tag
        nested = list(li.find_all(["ul", "ol"], recursive=False))
        for nested_list in nested:
            nested_list.extract()
        prefix = f"{index}. " if ordered else "- "
        lines.append(f"{'  ' * depth}{prefix}{inline_html_to_markdown(inner_html(li)).strip()}")
        for nested_list in nested:
            lines.append(
                render_list_markdown(
                    nested_list,
                    ordered=nested_list.name == "ol",
                    depth=depth + 1,
                )
            )
    return "\n".join(line for line in lines if line)


def render_table_markdown(table: dict[str, Any]) -> str:
    rows = cast(list[dict[str, Any]], table["rows"])
    if not rows:
        return ""
    row_values = [
        [escape_table_cell(str(cell.get("text") or "")) for cell in row.get("cells", [])]
        for row in rows
    ]
    max_width = max((len(row) for row in row_values), default=0)
    padded = [row + [""] * (max_width - len(row)) for row in row_values]
    if not padded or max_width == 0:
        return ""
    header = padded[0]
    separator = ["---"] * max_width
    body = padded[1:]
    markdown_rows = [header, separator, *body]
    return "\n".join("| " + " | ".join(row) + " |" for row in markdown_rows)


def inline_html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup
    return "".join(markdown_node(child) for child in root.children).strip()


def markdown_node(node: PageElement) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    content = "".join(markdown_node(child) for child in node.children)
    if name == "strong":
        return f"**{content}**"
    if name == "em":
        return f"*{content}*"
    if name == "a":
        href = str(node.get("href") or "")
        return f"[{content}]({href})" if href else content
    if name == "br":
        return "  \n"
    if name in {"sub", "sup"}:
        return f"<{name}>{content}</{name}>"
    if name == "img":
        alt = str(node.get("alt") or "")
        src = str(node.get("src") or "")
        return f"![{escape_markdown(alt)}]({src})"
    if name == "p":
        return content
    return content


def build_search_chunks(document: dict[str, Any], state: ParseState) -> list[dict[str, Any]]:
    section_lookup = {str(section["section_id"]): section for section in state.sections}
    chunks: list[dict[str, Any]] = []
    for recommendation in state.recommendations:
        section = section_lookup.get(str(recommendation["section_id"]), {})
        chunks.append(
            {
                "id": f"chunk-{len(chunks) + 1:04d}",
                "type": "recommendation",
                "document": {
                    "code": document.get("code"),
                    "version": document.get("version"),
                    "code_version": document.get("code_version"),
                    "title": document.get("title"),
                },
                "section": {
                    "id": section.get("section_id"),
                    "title": section.get("title"),
                },
                "text": recommendation.get("text"),
                "uur": recommendation.get("uur"),
                "udd": recommendation.get("udd"),
                "comments": recommendation.get("comments"),
                "references": recommendation.get("literature_references"),
                "source_block_ids": recommendation.get("block_ids"),
            }
        )

    recommendation_block_ids = {
        str(block_id)
        for recommendation in state.recommendations
        for block_id in cast(list[Any], recommendation.get("block_ids") or [])
    }
    for section in state.sections:
        text = "\n".join(
            str(block.get("text") or "")
            for block in cast(list[dict[str, Any]], section.get("blocks") or [])
            if str(block.get("id")) not in recommendation_block_ids and block.get("text")
        ).strip()
        if text:
            chunks.append(
                {
                    "id": f"chunk-{len(chunks) + 1:04d}",
                    "type": "section",
                    "document": {
                        "code": document.get("code"),
                        "version": document.get("version"),
                        "code_version": document.get("code_version"),
                        "title": document.get("title"),
                    },
                    "section": {
                        "id": section.get("section_id"),
                        "title": section.get("title"),
                    },
                    "text": text,
                }
            )
    return chunks


def write_qa_report(
    path: Path,
    state: ParseState,
    *,
    document: dict[str, Any],
    source_path: Path,
) -> None:
    counts = {
        "sections": len(state.sections),
        "blocks": len(state.blocks),
        "tables": len(state.tables),
        "images": len(state.images),
        "recommendations": len(state.recommendations),
        "references": len(state.references),
    }
    report: dict[str, Any] = {
        "timestamp": state.timestamp,
        "status": "parsed",
        "document": {
            "code": document.get("code"),
            "version": document.get("version"),
            "code_version": document.get("code_version"),
            "title": document.get("title"),
        },
        "source": {"json": source_path.relative_to(state.document_dir).as_posix()},
        "counts": counts,
        "issues": [issue.model_dump(mode="json") for issue in state.issues],
    }
    if document.get("code_version") == "843_1":
        report["expected_counts"] = EXPECTED_843_1
        report["count_checks"] = count_checks(counts)
    write_json(path, report)


def count_checks(counts: dict[str, int]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for metric, expected in EXPECTED_843_1.items():
        actual = counts.get(metric, 0)
        tolerance = 2 if metric == "sections" else 0
        matched = abs(actual - expected) <= tolerance
        checks.append(
            {
                "metric": metric,
                "expected": expected,
                "actual": actual,
                "status": "match" if matched else "mismatch",
                "explanation": None
                if matched
                else (
                    f"Expected about {expected}, parsed {actual}; "
                    "inspect source HTML/PDF alignment."
                ),
            }
        )
    return checks


def load_catalog_record(
    settings: Settings,
    document_dir: Path,
    code_version: str | None,
) -> dict[str, Any]:
    source_catalog_path = document_dir / "source" / "catalog-record.json"
    if source_catalog_path.exists():
        try:
            payload = json.loads(source_catalog_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            return {}

    if not code_version:
        code_version = document_dir.name
    index_path = settings.paths.indexes / "catalog.jsonl"
    if not index_path.exists():
        return {}
    with index_path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("code_version") == code_version:
                return row
    return {}


def table_lookup(tables: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(table["id"]): table for table in tables}


def has_filter(options: ParseOptions) -> bool:
    return bool(options.code_versions) or any(
        value is not None for value in (options.code, options.from_code, options.to_code)
    )


def matches_filters(code_version: str, options: ParseOptions) -> bool:
    code, _version = split_code_version(code_version)
    if code is None:
        return False
    if options.code is not None and code != options.code:
        return False
    if options.from_code is not None and code < options.from_code:
        return False
    if options.to_code is not None and code > options.to_code:
        return False
    return True


def block_type_for_tag(tag_name: str) -> str:
    if tag_name in HEADING_TAGS:
        return "heading"
    if tag_name in {"ul", "ol"}:
        return "list"
    if tag_name == "table":
        return "table"
    if tag_name == "img":
        return "image"
    if tag_name == "p":
        return "paragraph"
    if tag_name in SUPPORTED_TAGS:
        return "block"
    return "unknown"


def meaningful_children(root: Tag) -> list[PageElement]:
    children: list[PageElement] = []
    for child in root.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                children.append(child)
        elif isinstance(child, Tag):
            if child.name.lower() in {"html", "body"}:
                children.extend(meaningful_children(child))
            elif child.get_text(strip=True) or child.name.lower() in {"img", "table"}:
                children.append(child)
    return children


def render_fragment_html(root: Tag) -> str:
    return "".join(str(child) for child in meaningful_children(root))


def inner_html(tag: Tag) -> str:
    return "".join(str(child) for child in tag.contents)


def parse_span(value: Any) -> int:
    parsed = to_int(value)
    return parsed if parsed is not None and parsed > 0 else 1


def element_position(root: Tag, element: Tag) -> int:
    position = 0
    for child in meaningful_children(root):
        if isinstance(child, Tag):
            position += 1
            if child is element:
                return position
    return position


def find_table_caption(table: Tag) -> str | None:
    caption = table.find("caption")
    if isinstance(caption, Tag):
        text = normalize_text(caption.get_text(" ", strip=True))
        if text:
            return text
    previous = table.find_previous_sibling()
    if isinstance(previous, Tag):
        text = normalize_text(previous.get_text(" ", strip=True))
        if re.match(r"^(Таблица|Table)\b", text, re.IGNORECASE):
            return text
    return None


def is_complex_table(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    widths = {len(cast(list[Any], row.get("cells", []))) for row in rows}
    if len(widths) > 1:
        return True
    for row in rows:
        for cell in cast(list[dict[str, Any]], row.get("cells", [])):
            if int(cell.get("rowspan") or 1) > 1 or int(cell.get("colspan") or 1) > 1:
                return True
            if cell.get("images"):
                return True
    return False


def source_html_contains_image(element: Tag, image: dict[str, Any]) -> bool:
    image_id = str(image.get("id") or "")
    return element.find("img", attrs={"data-clinrec-image-id": image_id}) is not None


def is_recommendation_start(block: dict[str, Any], text: str) -> bool:
    if block.get("type") not in {"paragraph", "list", "block"}:
        return False
    return RECOMMENDATION_RE.search(text) is not None


def is_recommendation_neighbor(block: dict[str, Any]) -> bool:
    if block.get("type") not in {"paragraph", "list", "block"}:
        return False
    text = str(block.get("text") or "")
    return (
        COMMENT_RE.search(text) is not None
        or extract_uur(text) is not None
        or extract_udd(text) is not None
        or bool(REFERENCE_RE.search(text))
    )


def extract_uur(text: str) -> str | None:
    for pattern in UUR_PATTERNS:
        match = pattern.search(text)
        if match:
            return normalize_grade(match.group(1))
    return None


def extract_udd(text: str) -> str | None:
    for pattern in UDD_PATTERNS:
        match = pattern.search(text)
        if match:
            return normalize_grade(match.group(1))
    return None


def normalize_grade(value: str) -> str:
    translation: dict[str, str | int | None] = {"А": "A", "В": "B", "С": "C"}
    return value.upper().translate(str.maketrans(translation))


def merge_reference_occurrences(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for block in blocks:
        references.extend(cast(list[dict[str, Any]], block.get("references") or []))
    return references


def normalize_reference_numbers(body: str) -> list[int]:
    numbers: list[int] = []
    for part in body.split(","):
        item = part.strip()
        if not item:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", item)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                numbers.extend(range(start, end + 1))
            else:
                numbers.extend([start, end])
        else:
            numbers.append(int(item))
    return numbers


def extract_number(text: str) -> str | None:
    match = NUMBER_RE.match(text)
    return match.group("number") if match else None


def section_number_from_id(section_id: str) -> str | None:
    match = re.search(r"_(\d+(?:_\d+)*)$", section_id)
    if not match:
        return None
    return match.group(1).replace("_", ".")


def correct_number_by_parent(source_number: str, parent_number: str) -> str:
    source_parts = source_number.split(".")
    parent_parts = parent_number.split(".")
    if (
        len(source_parts) == len(parent_parts) + 1
        and source_parts[0] == parent_parts[0]
        and source_parts[: len(parent_parts)] != parent_parts
    ):
        return ".".join([*parent_parts, source_parts[-1]])
    return source_number


def stable_anchor(prefix: str, order: int, value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "-", value.strip()).strip("-").lower()
    if not cleaned:
        cleaned = "item"
    return f"{prefix}-{order:04d}-{cleaned}"


def escape_markdown(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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


def first_int(*values: Any) -> int | None:
    for value in values:
        parsed = to_int(value)
        if parsed is not None:
            return parsed
    return None


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
