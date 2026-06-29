from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import split_code_version
from clinrec.bank.common import string_value
from clinrec.config import Settings
from clinrec.models.external import QaIssue
from clinrec.parsed.models import CANONICAL_PARSER_VERSION, CANONICAL_SCHEMA_VERSION
from clinrec.parsed.pipeline import (
    ParseConfig,
    ParsedDocumentBundle,
    ParsedShowcaseOptions,
    parse_document,
    resolve_showcase_input,
    validate_parsed_bundle,
)
from clinrec.research.reports import write_json, write_jsonl

PARSER_VERSION = CANONICAL_PARSER_VERSION


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


def parse_documents(settings: Settings, options: ParseOptions) -> ParseSummary:
    timestamp = options.timestamp or utc_timestamp()
    candidates = select_document_dirs(settings, options)
    if not candidates:
        raise ParseError("No matching document directories found for parsing.")

    documents: list[ParsedDocumentSummary] = []
    for document_dir in candidates:
        source_path = document_dir / "source" / "getclinrec.json"
        if not source_path.exists():
            documents.append(write_missing_source_report(document_dir, timestamp))
            continue
        try:
            documents.append(parse_one_document(settings, document_dir, timestamp=timestamp))
        except ParseError as exc:
            documents.append(write_parse_error_report(document_dir, timestamp, exc))

    parsed = sum(1 for document in documents if document.status == "parsed")
    return ParseSummary(
        timestamp=timestamp,
        planned=len(candidates),
        parsed=parsed,
        failed=len(documents) - parsed,
        documents=documents,
    )


def parse_one_document(
    settings: Settings,
    document_dir: Path,
    *,
    timestamp: str | None = None,
) -> ParsedDocumentSummary:
    _ = settings
    current_timestamp = timestamp or utc_timestamp()
    source_path = document_dir / "source" / "getclinrec.json"
    if not source_path.exists():
        raise ParseError(f"Source JSON is missing: {source_path}")

    code_version = document_dir.name
    work_root = document_dir / ".canonical-parse.part"
    safe_remove_tree(work_root, document_dir)
    source_dir = document_dir / "source"
    source = resolve_showcase_input(
        ParsedShowcaseOptions(
            output=work_root,
            raw_json=source_path,
            manifest=source_dir / "manifest.json",
            catalog_record=source_dir / "catalog-record.json",
            catalog_candidates=source_dir / "catalog-candidates.json",
            code_version=code_version,
        )
    )
    bundle = parse_document(
        source,
        ParseConfig(
            root=work_root,
            dataset_id=f"legacy:{code_version}",
            created_at=current_timestamp,
            repository_commit="",
            build_config_sha256="",
            source_raw_path="source/getclinrec.json",
        ),
    )
    validation = validate_parsed_bundle(bundle)
    if not validation["valid"]:
        write_json(document_dir / "qa" / "parse-bundle-validation.json", validation)
        safe_remove_tree(work_root, document_dir)
        raise ParseError(
            f"canonical bundle validation failed with {len(validation['errors'])} error(s)"
        )

    parsed_dir = document_dir / "parsed"
    qa_dir = document_dir / "qa"
    document_json_path = parsed_dir / "document.json"
    markdown_path = parsed_dir / "content.md"
    search_chunks_path = parsed_dir / "search_chunks.jsonl"
    qa_report_path = qa_dir / "parse-report.json"

    copy_bundle_assets(document_dir, bundle)
    legacy_payload = legacy_document_payload(bundle, validation)
    write_json(document_json_path, legacy_payload)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(bundle), encoding="utf-8", newline="\n")
    write_jsonl(search_chunks_path, legacy_search_chunks(bundle))
    write_qa_report(qa_report_path, bundle, validation=validation, timestamp=current_timestamp)
    safe_remove_tree(work_root, document_dir)

    return ParsedDocumentSummary(
        code_version=code_version,
        document_dir=document_dir,
        document_json_path=document_json_path,
        markdown_path=markdown_path,
        search_chunks_path=search_chunks_path,
        qa_report_path=qa_report_path,
        sections=len(bundle.sections),
        blocks=len(bundle.blocks),
        tables=len(bundle.tables),
        images=len(bundle.images),
        recommendations=len(bundle.recommendations),
        references=len(bundle.references),
        issues=len(validation["warnings"]),
        status="parsed",
    )


def legacy_document_payload(
    bundle: ParsedDocumentBundle,
    validation: dict[str, Any],
) -> dict[str, Any]:
    document = dict(bundle.document)
    if document.get("age") is not None and not isinstance(document.get("age"), dict):
        document["age"] = {"category": document.get("age")}
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "parser_version": CANONICAL_PARSER_VERSION,
        "source": {
            "json_sha256": bundle.state.source.raw_sha256,
            "pdf_status": "not_requested",
        },
        "document": document,
        "sections": bundle.sections,
        "blocks": bundle.blocks,
        "tables": [legacy_table_row(table) for table in bundle.tables],
        "images": [legacy_image_row(image) for image in bundle.images],
        "recommendations": bundle.recommendations,
        "references": bundle.references,
        "validation": validation,
    }


def legacy_table_row(table: dict[str, Any]) -> dict[str, Any]:
    row = dict(table)
    row["id"] = table.get("table_id")
    row["grid"] = table.get("logical_grid") or []
    return row


def legacy_image_row(image: dict[str, Any]) -> dict[str, Any]:
    row = dict(image)
    row["id"] = image.get("image_id")
    row["path"] = image.get("asset_path")
    row["mime"] = image.get("detected_mime_type") or image.get("mime_type")
    row["sha256"] = image.get("asset_sha256")
    return row


def legacy_search_chunks(bundle: ParsedDocumentBundle) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in bundle.chunks:
        chunk_id = string_value(chunk.get("chunk_id"))
        rows.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "document_id": chunk.get("document_id"),
                "type": chunk.get("chunk_type"),
                "section_path": [
                    {"id": chunk.get("section_id"), "title": chunk.get("section_title")}
                ],
                "text": chunk.get("text"),
                "uur": first_value_for_chunk(bundle.recommendations, chunk, "uur"),
                "udd": first_value_for_chunk(bundle.recommendations, chunk, "udd"),
                "references": chunk.get("citation"),
                "source_block_ids": chunk.get("primary_block_ids") or [],
            }
        )
    return rows


def first_value_for_chunk(
    recommendations: list[dict[str, Any]],
    chunk: dict[str, Any],
    key: str,
) -> Any:
    block_ids = {string_value(value) for value in (chunk.get("primary_block_ids") or [])}
    for recommendation in recommendations:
        if block_ids.intersection(
            string_value(value) for value in (recommendation.get("block_ids") or [])
        ):
            return recommendation.get(key)
    return None


def render_markdown(bundle: ParsedDocumentBundle) -> str:
    lines = [f"# {string_value(bundle.document.get('title'))}", ""]
    for section in sorted(bundle.sections, key=lambda row: int(row.get("source_order") or 0)):
        title = string_value(section.get("title") or section.get("source_section_id"))
        if title:
            lines.extend([f"## {title}", ""])
        html = string_value(section.get("normalized_html"))
        if html:
            lines.extend([html, ""])
    return "\n".join(lines).strip() + "\n"


def write_qa_report(
    path: Path,
    bundle: ParsedDocumentBundle,
    *,
    validation: dict[str, Any],
    timestamp: str,
) -> None:
    issues = [
        QaIssue(
            severity="warning",
            code=string_value(warning.get("code")),
            message="Canonical parser warning.",
            context={"path": warning.get("path"), "details": warning.get("details")},
        )
        for warning in validation["warnings"]
    ]
    write_json(
        path,
        {
            "timestamp": timestamp,
            "code_version": bundle.state.source.code_version,
            "status": "parsed",
            "counts": {
                "sections": len(bundle.sections),
                "blocks": len(bundle.blocks),
                "tables": len(bundle.tables),
                "images": len(bundle.images),
                "recommendations": len(bundle.recommendations),
                "references": len(bundle.references),
            },
            "issues": [issue.model_dump(mode="json") for issue in issues],
            "validation": validation,
        },
    )


def copy_bundle_assets(document_dir: Path, bundle: ParsedDocumentBundle) -> None:
    for asset in bundle.assets:
        relative = string_value(asset.get("path"))
        if not relative:
            continue
        source = bundle.state.root / "canonical" / relative
        target = document_dir / relative
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


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
            "counts": empty_counts(),
            "issues": [issue.model_dump(mode="json")],
        },
    )
    return failed_summary(document_dir, qa_report_path, issues=1)


def write_parse_error_report(
    document_dir: Path,
    timestamp: str,
    error: ParseError,
) -> ParsedDocumentSummary:
    code_version = document_dir.name
    qa_report_path = document_dir / "qa" / "parse-report.json"
    issue = QaIssue(
        severity="fatal",
        code="parse_fatal",
        message=str(error),
        context={"document_dir": str(document_dir)},
    )
    write_json(
        qa_report_path,
        {
            "timestamp": timestamp,
            "code_version": code_version,
            "status": "failed",
            "counts": empty_counts(),
            "issues": [issue.model_dump(mode="json")],
        },
    )
    return failed_summary(document_dir, qa_report_path, issues=1)


def failed_summary(
    document_dir: Path,
    qa_report_path: Path,
    *,
    issues: int,
) -> ParsedDocumentSummary:
    return ParsedDocumentSummary(
        code_version=document_dir.name,
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
        issues=issues,
        status="failed",
    )


def empty_counts() -> dict[str, int]:
    return {
        "sections": 0,
        "blocks": 0,
        "tables": 0,
        "images": 0,
        "recommendations": 0,
        "references": 0,
    }


def safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise ParseError(f"Refusing to remove path outside document directory: {path}") from exc
    shutil.rmtree(resolved)


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


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
