from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
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
)
from clinrec.parsed.html import is_javascript_url
from clinrec.parsed.models import CANONICAL_PARSER_VERSION, CANONICAL_SCHEMA_VERSION
from clinrec.parsed.pipeline import (
    ParseConfig,
    ParsedShowcaseOptions,
    git_commit_or_unknown,
    resolve_showcase_input,
    validate_parsed_bundle,
)
from clinrec.parsed.pipeline import (
    parse_document as parse_canonical_document,
)
from clinrec.parsed.source_inventory import build_raw_source_inventory
from clinrec.research.reports import write_json, write_jsonl
from clinrec.research.sections import raw_sections, section_html

PARSED_SCHEMA_VERSION = CANONICAL_SCHEMA_VERSION
PARSER_VERSION = CANONICAL_PARSER_VERSION


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
    output: Path
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
    created_at: str
    repository_commit: str
    build_config_sha256: str
    documents: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    table_cells: list[dict[str, Any]]
    table_placements: list[dict[str, Any]]
    images: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    references: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    search_chunks: list[dict[str, Any]]
    rag_chunks: list[dict[str, Any]]
    citation_rows: list[dict[str, Any]]
    document_validations: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]


def build_parsed_dataset(options: ParsedBuildOptions) -> ParsedBuildSummary:
    validate_build_options(options)
    ensure_parsed_output_safe(options.output)
    output_parent = options.output.parent
    part_output = output_parent / f".{options.output.name}.part"
    failure_report = output_parent / f"{options.output.name}.failure.json"
    safe_remove_tree(part_output, output_parent)
    if options.output.exists():
        raise BankError(f"Parsed output already exists: {options.output}")
    part_output.mkdir(parents=True, exist_ok=True)
    (part_output / "reports").mkdir(parents=True, exist_ok=True)
    refs = select_raw_documents(options)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    repository_commit = git_commit_or_unknown()
    build_config_sha256 = sha256_bytes(
        json.dumps(
            {
                "input": options.input.as_posix(),
                "output": options.output.as_posix(),
                "code_versions": options.code_versions,
                "all_current": options.all_current,
                "include_previous": options.include_previous,
                "parser_version": PARSER_VERSION,
                "schema_version": PARSED_SCHEMA_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    state = BuildState(
        output=part_output,
        source_root=options.input,
        created_at=created_at,
        repository_commit=repository_commit,
        build_config_sha256=build_config_sha256,
        documents=[],
        sections=[],
        blocks=[],
        tables=[],
        table_cells=[],
        table_placements=[],
        images=[],
        assets=[],
        recommendations=[],
        references=[],
        relations=[],
        search_chunks=[],
        rag_chunks=[],
        citation_rows=[],
        document_validations=[],
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
    if state.anomalies:
        write_failure_report(failure_report, state, options, source_documents=len(refs))
        safe_remove_tree(part_output, output_parent)
        raise BankError(
            f"parsed-build failed for {len(state.anomalies)} document(s); "
            f"failure report: {failure_report}"
        )
    state.relations = build_relations(state.documents)
    write_dataset_artifacts(state, options, source_documents=len(refs))
    release_validation = validate_parsed_release(part_output)
    if not release_validation["valid"]:
        state.anomalies.append(
            {
                "stage": "release_validation",
                "path": part_output.as_posix(),
                "error": "parsed release validation failed",
                "errors": release_validation["errors"],
            }
        )
        write_failure_report(failure_report, state, options, source_documents=len(refs))
        safe_remove_tree(part_output, output_parent)
        raise BankError(f"parsed release validation failed; failure report: {failure_report}")
    summary = parsed_summary(state, source_documents=len(refs))
    summary_path = part_output / "reports" / "parsed-summary.json"
    write_json(summary_path, summary)
    write_jsonl(part_output / "reports" / "parser-anomalies.jsonl", state.anomalies)
    part_output.replace(options.output)
    final_summary_path = options.output / "reports" / "parsed-summary.json"
    return ParsedBuildSummary(
        output=options.output,
        source_documents=len(refs),
        parsed_documents=len(state.documents),
        failed_documents=len(state.anomalies),
        sections=len(state.sections),
        tables=len(state.tables),
        images=len(state.images),
        chunks=len(state.search_chunks),
        summary_path=final_summary_path,
    )


def validate_build_options(options: ParsedBuildOptions) -> None:
    if options.all_current and options.code_versions:
        raise BankError("--all-current and --code-version are mutually exclusive.")
    if not options.input.exists():
        raise BankError(f"Parsed input corpus is missing: {options.input}")
    for code_version in options.code_versions:
        parse_code_version_or_raise(code_version)


def write_failure_report(
    path: Path,
    state: BuildState,
    options: ParsedBuildOptions,
    *,
    source_documents: int,
) -> None:
    write_json(
        path,
        {
            "schema_version": PARSED_SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "valid": False,
            "output": options.output.as_posix(),
            "temporary_output": state.output.as_posix(),
            "source_corpus": options.input.as_posix(),
            "source_documents": source_documents,
            "parsed_documents": len(state.documents),
            "failed_documents": len(state.anomalies),
            "anomalies": state.anomalies,
            "document_validations": state.document_validations,
        },
    )


def safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise BankError(f"Refusing to remove path outside output parent: {path}") from exc
    shutil.rmtree(resolved)


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
    raw_relative = source_raw_path(state.source_root, ref.raw_path)
    source_root = ref.raw_path.parent
    source = resolve_showcase_input(
        ParsedShowcaseOptions(
            output=state.output,
            raw_json=ref.raw_path,
            manifest=source_root / "manifest.json",
            catalog_record=source_root / "catalog-record.json",
            catalog_candidates=source_root / "catalog-candidates.json",
            code_version=ref.code_version,
        )
    )
    bundle = parse_canonical_document(
        source,
        ParseConfig(
            root=state.output,
            dataset_id=state.output.name,
            created_at=state.created_at,
            repository_commit=state.repository_commit,
            build_config_sha256=state.build_config_sha256,
            document_kind=ref.kind,
            current_code_version=ref.current_code_version,
            source_raw_path=raw_relative,
        ),
    )
    validation = validate_parsed_bundle(bundle)
    validation["source_raw_path"] = raw_relative
    validation["document_kind"] = ref.kind
    validation["code_version"] = ref.code_version
    validation["current_code_version"] = ref.current_code_version
    state.document_validations.append(validation)
    if not validation["valid"]:
        raise BankError(
            f"canonical bundle validation failed for {raw_relative}: "
            f"{len(validation['errors'])} error(s)"
        )
    document_dir = document_output_dir(state.output, ref)
    document_dir.mkdir(parents=True, exist_ok=True)
    document = dict(bundle.document)
    document["schema_version"] = PARSED_SCHEMA_VERSION
    document["sections"] = [string_value(row["section_id"]) for row in bundle.sections]
    document["source_section_count"] = len(bundle.sections)
    document["image_count"] = len(bundle.images)
    document_sections = [layer_section_row(row, ref) for row in bundle.sections]
    document_blocks = [layer_canonical_row(row, ref) for row in bundle.blocks]
    document_tables = [layer_table_row(row, ref) for row in bundle.tables]
    document_table_cells = [layer_canonical_row(row, ref) for row in bundle.table_cells]
    document_table_placements = [
        layer_canonical_row(row, ref) for row in bundle.table_placements
    ]
    document_images = [layer_image_row(row, ref, raw_relative) for row in bundle.images]
    document_assets = [layer_canonical_row(row, ref) for row in bundle.assets]
    document_recommendations = [
        layer_canonical_row(row, ref) for row in bundle.recommendations
    ]
    document_references = [layer_canonical_row(row, ref) for row in bundle.references]
    rag_chunks = [layer_chunk_row(row, ref) for row in bundle.chunks]
    search_chunks = [search_chunk_from_rag(row) for row in rag_chunks]
    copy_bundle_assets(state.output, bundle.assets)
    write_json(document_dir / "document.json", document)
    write_jsonl(document_dir / "sections.jsonl", document_sections)
    write_jsonl(document_dir / "blocks.jsonl", document_blocks)
    write_jsonl(document_dir / "tables.jsonl", document_tables)
    write_jsonl(document_dir / "table-cells.jsonl", document_table_cells)
    write_jsonl(document_dir / "table-placements.jsonl", document_table_placements)
    write_jsonl(document_dir / "images.jsonl", document_images)
    state.documents.append(document)
    state.sections.extend(document_sections)
    state.blocks.extend(document_blocks)
    state.tables.extend(document_tables)
    state.table_cells.extend(document_table_cells)
    state.table_placements.extend(document_table_placements)
    state.images.extend(document_images)
    extend_unique(state.assets, document_assets, "asset_id")
    state.recommendations.extend(document_recommendations)
    state.references.extend(document_references)
    state.rag_chunks.extend(rag_chunks)
    state.search_chunks.extend(search_chunks)
    state.citation_rows.extend(citation_row(row) for row in rag_chunks)


def layer_canonical_row(row: dict[str, Any], ref: RawDocumentRef) -> dict[str, Any]:
    result = dict(row)
    result["schema_version"] = PARSED_SCHEMA_VERSION
    result["document_kind"] = ref.kind
    result["code_version"] = ref.code_version
    result["current_code_version"] = ref.current_code_version
    return result


def layer_section_row(row: dict[str, Any], ref: RawDocumentRef) -> dict[str, Any]:
    section = layer_canonical_row(row, ref)
    section["section_title"] = section.get("title")
    return section


def layer_table_row(row: dict[str, Any], ref: RawDocumentRef) -> dict[str, Any]:
    table = layer_canonical_row(row, ref)
    return table


def layer_image_row(
    row: dict[str, Any],
    ref: RawDocumentRef,
    raw_relative: str,
) -> dict[str, Any]:
    image = layer_canonical_row(row, ref)
    image["source_raw_path"] = raw_relative
    return image


def layer_chunk_row(row: dict[str, Any], ref: RawDocumentRef) -> dict[str, Any]:
    chunk = dict(row)
    chunk["schema_version"] = PARSED_SCHEMA_VERSION
    chunk["document_kind"] = ref.kind
    chunk["current_code_version"] = ref.current_code_version
    chunk["section_title"] = chunk.get("section_title")
    chunk["context_text"] = string_value(chunk.get("section_title"))
    table_id = string_value(chunk.get("table_id"))
    image_id = string_value(chunk.get("image_id"))
    chunk["table_ids"] = [table_id] if table_id else []
    chunk["image_ids"] = [image_id] if image_id else []
    return chunk


def copy_bundle_assets(output: Path, assets: list[dict[str, Any]]) -> None:
    for asset in assets:
        relative = string_value(asset.get("path"))
        if not relative:
            continue
        source = output / "canonical" / relative
        target = output / relative
        if not source.exists() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def extend_unique(
    target: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    key: str,
) -> None:
    by_id = {string_value(row.get(key)): row for row in target}
    for row in rows:
        stable_id = string_value(row.get(key))
        if stable_id in by_id:
            existing = by_id[stable_id]
            existing_occurrences = [
                string_value(value) for value in (existing.get("occurrence_ids") or [])
            ]
            for occurrence_id in row.get("occurrence_ids") or []:
                occurrence = string_value(occurrence_id)
                if occurrence and occurrence not in existing_occurrences:
                    existing_occurrences.append(occurrence)
            if existing_occurrences:
                existing["occurrence_ids"] = existing_occurrences
            continue
        target.append(row)
        by_id[stable_id] = row


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
        "created_at": state.created_at,
        "repository_commit": state.repository_commit,
        "build_config_sha256": state.build_config_sha256,
        "source_documents": source_documents,
        "parsed_documents": len(state.documents),
        "failed_documents": len(state.anomalies),
        "documents": len(state.documents),
        "sections": len(state.sections),
        "blocks": len(state.blocks),
        "tables": len(state.tables),
        "table_cells": len(state.table_cells),
        "logical_table_placements": len(state.table_placements),
        "images": len(state.images),
        "assets": len(state.assets),
        "recommendations": len(state.recommendations),
        "references": len(state.references),
        "search_chunks": len(state.search_chunks),
        "rag_chunks": len(state.rag_chunks),
    }
    write_json(state.output / "dataset.json", dataset)
    write_jsonl(state.output / "documents.jsonl", sorted_rows(state.documents, "document_id"))
    write_jsonl(state.output / "sections.jsonl", sorted_rows(state.sections, "section_id"))
    write_jsonl(state.output / "blocks.jsonl", sorted_rows(state.blocks, "block_id"))
    write_jsonl(state.output / "tables.jsonl", sorted_rows(state.tables, "table_id"))
    write_jsonl(
        state.output / "table-cells.jsonl",
        sorted_rows(state.table_cells, "cell_id"),
    )
    write_jsonl(
        state.output / "table-placements.jsonl",
        sorted_rows(state.table_placements, "placement_id"),
    )
    write_jsonl(state.output / "images.jsonl", sorted_rows(state.images, "image_id"))
    write_jsonl(state.output / "assets.jsonl", sorted_rows(state.assets, "asset_id"))
    write_jsonl(
        state.output / "recommendations.jsonl",
        sorted_rows(state.recommendations, "recommendation_id"),
    )
    write_jsonl(
        state.output / "references.jsonl",
        sorted_rows(state.references, "reference_id"),
    )
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
    write_jsonl(
        state.output / "reports" / "document-validation.jsonl",
        sorted_rows(state.document_validations, "document_id"),
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
        "blocks": len(state.blocks),
        "tables": len(state.tables),
        "table_cells": len(state.table_cells),
        "logical_table_placements": len(state.table_placements),
        "table_classifications": dict(sorted(table_classes.items())),
        "images": len(state.images),
        "assets": len(state.assets),
        "recommendations": len(state.recommendations),
        "references": len(state.references),
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


def validate_parsed_release(input_path: Path, *, write_report: bool = True) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    required = [
        "dataset.json",
        "documents.jsonl",
        "sections.jsonl",
        "blocks.jsonl",
        "tables.jsonl",
        "table-cells.jsonl",
        "table-placements.jsonl",
        "images.jsonl",
        "assets.jsonl",
        "recommendations.jsonl",
        "references.jsonl",
        "relations.jsonl",
        "search/chunks.jsonl",
        "rag/chunks.jsonl",
        "rag/citation-index.jsonl",
        "rag/embedding-input.jsonl",
        "reports/document-validation.jsonl",
    ]
    for relative in required:
        if not (input_path / relative).exists():
            errors.append(issue(relative, "required_file_missing", None))
    dataset = read_json_file(input_path / "dataset.json")
    if int(dataset.get("failed_documents") or 0) != 0:
        errors.append(
            issue(
                "dataset.json",
                "failed_documents_nonzero",
                dataset.get("failed_documents"),
            )
        )
    documents = read_jsonl(input_path / "documents.jsonl")
    sections = read_jsonl(input_path / "sections.jsonl")
    blocks = read_jsonl(input_path / "blocks.jsonl")
    tables = read_jsonl(input_path / "tables.jsonl")
    table_cells = read_jsonl(input_path / "table-cells.jsonl")
    table_placements = read_jsonl(input_path / "table-placements.jsonl")
    images = read_jsonl(input_path / "images.jsonl")
    assets = read_jsonl(input_path / "assets.jsonl")
    recommendations = read_jsonl(input_path / "recommendations.jsonl")
    references = read_jsonl(input_path / "references.jsonl")
    search_chunks = read_jsonl(input_path / "search" / "chunks.jsonl")
    chunks = read_jsonl(input_path / "rag" / "chunks.jsonl")
    citations = read_jsonl(input_path / "rag" / "citation-index.jsonl")
    embedding_inputs = read_jsonl(input_path / "rag" / "embedding-input.jsonl")
    validations = read_jsonl(input_path / "reports" / "document-validation.jsonl")
    for path, rows, key in (
        ("documents.jsonl", documents, "document_id"),
        ("sections.jsonl", sections, "section_id"),
        ("blocks.jsonl", blocks, "block_id"),
        ("tables.jsonl", tables, "table_id"),
        ("table-cells.jsonl", table_cells, "cell_id"),
        ("table-placements.jsonl", table_placements, "placement_id"),
        ("images.jsonl", images, "image_id"),
        ("assets.jsonl", assets, "asset_id"),
        ("recommendations.jsonl", recommendations, "recommendation_id"),
        ("references.jsonl", references, "reference_id"),
        ("rag/chunks.jsonl", chunks, "chunk_id"),
    ):
        seen_ids: set[str] = set()
        for row in rows:
            stable_id = string_value(row.get(key))
            if stable_id in seen_ids:
                errors.append(issue(path, "duplicate_stable_id", stable_id))
            seen_ids.add(stable_id)
    document_ids = {string_value(document.get("document_id")) for document in documents}
    section_ids = {string_value(section.get("section_id")) for section in sections}
    block_ids = {string_value(block.get("block_id")) for block in blocks}
    table_ids = {string_value(table.get("table_id")) for table in tables}
    cell_ids = {string_value(cell.get("cell_id")) for cell in table_cells}
    placement_ids = {string_value(row.get("placement_id")) for row in table_placements}
    image_ids = {string_value(image.get("image_id")) for image in images}
    asset_ids = {string_value(asset.get("asset_id")) for asset in assets}
    chunk_ids = {string_value(chunk.get("chunk_id")) for chunk in chunks}
    validation_document_ids = {
        string_value(validation.get("document_id")) for validation in validations
    }
    if validation_document_ids != document_ids:
        errors.append(
            issue(
                "reports/document-validation.jsonl",
                "document_validation_set_mismatch",
                {
                    "missing": sorted(document_ids - validation_document_ids),
                    "extra": sorted(validation_document_ids - document_ids),
                },
            )
        )
    for validation in validations:
        if string_value(validation.get("document_id")) not in document_ids:
            errors.append(
                issue(
                    string_value(validation.get("document_id")),
                    "document_validation_without_document",
                    None,
                )
            )
        if not validation.get("valid"):
            errors.append(
                issue(
                    string_value(validation.get("document_id")),
                    "document_validation_failed",
                    None,
                )
            )
    source_corpus = Path(string_value(dataset.get("source_corpus")))
    if not source_corpus.is_absolute():
        source_corpus = source_corpus.resolve()
    sections_by_document = group_rows(sections, "document_id")
    tables_by_document = group_rows(tables, "document_id")
    table_cells_by_document = group_rows(table_cells, "document_id")
    table_placements_by_document = group_rows(table_placements, "document_id")
    images_by_document = group_rows(images, "document_id")
    for document in documents:
        validate_document(
            input_path,
            source_corpus,
            document,
            sections_by_document,
            tables_by_document,
            table_cells_by_document,
            table_placements_by_document,
            images_by_document,
            errors,
        )
    for section in sections:
        validate_section_html(section, errors)
        if string_value(section.get("document_id")) not in document_ids:
            errors.append(
                issue(
                    string_value(section.get("section_id")),
                    "unresolved_document_id",
                    None,
                )
            )
    for block in blocks:
        if string_value(block.get("section_id")) not in section_ids:
            errors.append(issue(string_value(block.get("block_id")), "unresolved_section_id", None))
    for table in tables:
        if string_value(table.get("section_id")) not in section_ids:
            errors.append(
                issue(
                    string_value(table.get("table_id")),
                    "unresolved_table_section",
                    None,
                )
            )
    for cell in table_cells:
        if string_value(cell.get("table_id")) not in table_ids:
            errors.append(issue(string_value(cell.get("cell_id")), "unresolved_cell_table", None))
    for placement in table_placements:
        if string_value(placement.get("table_id")) not in table_ids:
            errors.append(
                issue(
                    string_value(placement.get("placement_id")),
                    "unresolved_placement_table",
                    None,
                )
            )
        if string_value(placement.get("origin_cell_id")) not in cell_ids:
            errors.append(
                issue(
                    string_value(placement.get("placement_id")),
                    "unresolved_origin_cell",
                    None,
                )
            )
    assets_by_id = {string_value(asset.get("asset_id")): asset for asset in assets}
    for asset in assets:
        asset_path = string_value(asset.get("path"))
        asset_file = input_path / asset_path
        if not asset_path or not asset_file.exists():
            errors.append(
                issue(string_value(asset.get("asset_id")), "asset_file_missing", asset_path)
            )
            continue
        actual_sha = sha256_file(asset_file)
        expected_sha = string_value(asset.get("asset_sha256") or asset.get("sha256"))
        if expected_sha and actual_sha != expected_sha:
            errors.append(
                issue(
                    string_value(asset.get("asset_id")),
                    "assembled_asset_sha_mismatch",
                    {"expected": expected_sha, "actual": actual_sha},
                )
            )
        expected_size = int(asset.get("size_bytes") or 0)
        if expected_size and asset_file.stat().st_size != expected_size:
            errors.append(
                issue(
                    string_value(asset.get("asset_id")),
                    "assembled_asset_size_mismatch",
                    {"expected": expected_size, "actual": asset_file.stat().st_size},
                )
            )
    for image in images:
        validate_image_asset(input_path, image, errors, warnings)
        asset_id = string_value(image.get("asset_id"))
        if asset_id and asset_id not in asset_ids:
            errors.append(
                issue(
                    string_value(image.get("image_id")),
                    "unresolved_image_asset",
                    asset_id,
                )
            )
        if asset_id and asset_id in assets_by_id:
            occurrence_ids = [
                string_value(value)
                for value in (assets_by_id[asset_id].get("occurrence_ids") or [])
            ]
            if string_value(image.get("image_id")) not in occurrence_ids:
                errors.append(
                    issue(
                        string_value(image.get("image_id")),
                        "asset_occurrence_missing_image",
                        asset_id,
                    )
                )
    for recommendation in recommendations:
        for block_id in recommendation.get("block_ids") or []:
            if string_value(block_id) not in block_ids:
                errors.append(
                    issue(
                        string_value(recommendation.get("recommendation_id")),
                        "unresolved_recommendation_block",
                        block_id,
                    )
                )
    for reference in references:
        if string_value(reference.get("block_id")) not in block_ids:
            errors.append(
                issue(
                    string_value(reference.get("reference_id")),
                    "unresolved_reference_block",
                    None,
                )
            )
    for chunk in chunks:
        if string_value(chunk.get("section_id")) not in section_ids:
            errors.append(
                issue(
                    string_value(chunk.get("chunk_id")),
                    "unresolved_chunk_section",
                    None,
                )
            )
        table_id = string_value(chunk.get("table_id"))
        if table_id and table_id not in table_ids:
            errors.append(
                issue(
                    string_value(chunk.get("chunk_id")),
                    "unresolved_chunk_table",
                    table_id,
                )
            )
        image_id = string_value(chunk.get("image_id"))
        if image_id and image_id not in image_ids:
            errors.append(
                issue(
                    string_value(chunk.get("chunk_id")),
                    "unresolved_chunk_image",
                    image_id,
                )
            )
        for placement_id in chunk.get("placement_ids") or []:
            if string_value(placement_id) not in placement_ids:
                errors.append(
                    issue(
                        string_value(chunk.get("chunk_id")),
                        "unresolved_chunk_placement",
                        placement_id,
                    )
                )
        if int(chunk.get("estimated_token_count") or chunk.get("token_estimate") or 0) > 1100:
            errors.append(
                issue(
                    string_value(chunk.get("chunk_id")),
                    "estimated_chunk_size_exceeded",
                    None,
                )
            )
    search_ids = {string_value(chunk.get("chunk_id")) for chunk in search_chunks}
    citation_ids = {string_value(row.get("chunk_id")) for row in citations}
    embedding_ids = {
        string_value(row.get("chunk_id") or row.get("id")) for row in embedding_inputs
    }
    parity: dict[str, Any] = {
        "valid": True,
        "checks": {},
        "errors": [],
    }
    for name, ids in (
        ("search_chunks", search_ids),
        ("citation_index", citation_ids),
        ("embedding_input", embedding_ids),
    ):
        missing = sorted(chunk_ids - ids)
        extra = sorted(ids - chunk_ids)
        check = {"missing": missing, "extra": extra, "passed": not missing and not extra}
        parity["checks"][name] = check
        if not check["passed"]:
            parity["valid"] = False
            errors.append(issue(name, "rag_package_parity_mismatch", check))
            parity["errors"].append({"path": name, "details": check})
    report = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "input": input_path.as_posix(),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "package_parity": parity,
        "summary": {
            "documents": len(documents),
            "sections": len(sections),
            "blocks": len(blocks),
            "tables": len(tables),
            "table_cells": len(table_cells),
            "logical_table_placements": len(table_placements),
            "images": len(images),
            "assets": len(assets),
            "recommendations": len(recommendations),
            "references": len(references),
            "chunks": len(chunks),
            "errors": len(errors),
            "warnings": len(warnings),
        },
    }
    if write_report:
        reports = input_path / "reports"
        write_json(reports / "release-validation.json", report)
        write_json(reports / "package-parity.json", parity)
    return report


def validate_parsed_dataset(input_path: Path) -> ParsedValidationSummary:
    report = validate_parsed_release(input_path)
    reports = input_path / "reports"
    write_json(reports / "parsed-validation.json", report)
    write_json(reports / "determinism.json", content_hash_manifest(input_path))
    markdown = render_validation_markdown(report)
    (reports / "parsed-validation.md").write_text(markdown, encoding="utf-8", newline="\n")
    return ParsedValidationSummary(
        input=input_path,
        valid=bool(report["valid"]),
        errors=len(report["errors"]),
        warnings=len(report["warnings"]),
        report_json=reports / "parsed-validation.json",
        report_markdown=reports / "parsed-validation.md",
    )


def validate_document(
    input_path: Path,
    source_root: Path,
    document: dict[str, Any],
    section_rows_by_document: dict[str, list[dict[str, Any]]],
    table_rows_by_document: dict[str, list[dict[str, Any]]],
    table_cell_rows_by_document: dict[str, list[dict[str, Any]]],
    table_placement_rows_by_document: dict[str, list[dict[str, Any]]],
    image_rows_by_document: dict[str, list[dict[str, Any]]],
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
    inventory = build_raw_source_inventory(payload)
    errors.extend(inventory.errors)
    raw_count = len(inventory.sections)
    parsed_sections = section_rows_by_document.get(document_id, [])
    parsed_count = len(parsed_sections)
    if raw_count != parsed_count:
        errors.append(
            issue(
                document_id,
                "section_count_mismatch",
                {"raw": raw_count, "parsed": parsed_count},
            )
        )
    expected_counts = {
        "tables": (len(inventory.tables), len(table_rows_by_document.get(document_id, []))),
        "table_cells": (
            len(inventory.table_cells),
            len(table_cell_rows_by_document.get(document_id, [])),
        ),
        "logical_table_placements": (
            len(inventory.table_placements),
            len(table_placement_rows_by_document.get(document_id, [])),
        ),
        "images": (len(inventory.images), len(image_rows_by_document.get(document_id, []))),
    }
    for unit, (raw_total, parsed_total) in expected_counts.items():
        if raw_total != parsed_total:
            errors.append(
                issue(
                    document_id,
                    f"{unit}_count_mismatch",
                    {"raw": raw_total, "parsed": parsed_total},
                )
            )
    raw_cell_hashes = [record.text_sha256 for record in inventory.table_cells]
    parsed_cell_hashes = [
        string_value(row.get("text_sha256"))
        for row in table_cell_rows_by_document.get(document_id, [])
    ]
    if raw_cell_hashes != parsed_cell_hashes:
        errors.append(issue(document_id, "raw_table_cell_text_mismatch", None))
    raw_placement_hashes = [record.text_sha256 for record in inventory.table_placements]
    parsed_placement_hashes = [
        string_value(row.get("text_sha256"))
        for row in table_placement_rows_by_document.get(document_id, [])
    ]
    if raw_placement_hashes != parsed_placement_hashes:
        errors.append(issue(document_id, "raw_table_placement_text_mismatch", None))
    raw_by_path = {record.raw_path: record for record in inventory.sections}
    parsed_paths = {string_value(section.get("raw_path")) for section in parsed_sections}
    raw_paths = set(raw_by_path)
    for missing_path in sorted(raw_paths - parsed_paths):
        errors.append(issue(document_id, "missing_raw_section_path", missing_path))
    for extra_path in sorted(parsed_paths - raw_paths):
        errors.append(issue(document_id, "parsed_section_without_raw_path", extra_path))
    for section in parsed_sections:
        raw_path_value = string_value(section.get("raw_path"))
        raw_record = raw_by_path.get(raw_path_value)
        if raw_record is None:
            continue
        raw_text = normalize_text(visible_text(section_html(raw_record.section)))
        parsed_text = normalize_text(visible_text(string_value(section.get("normalized_html"))))
        if raw_text != parsed_text:
            errors.append(
                issue(
                    string_value(section.get("section_id")),
                    "raw_section_text_mismatch",
                    {
                        "raw_path": raw_path_value,
                        "raw_sha256": sha256_text(raw_text),
                        "parsed_sha256": sha256_text(parsed_text),
                    },
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
            if attr.casefold() in {"href", "src"} and is_javascript_url(tag.get(attr)):
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
        errors.append(
            issue(
                string_value(image.get("image_id")),
                "image_decode_failure",
                image.get("decode_error"),
            )
        )
    asset_path = string_value(image.get("asset_path"))
    if image.get("source_type") in {"base64", "relative"} and not asset_path:
        errors.append(
            issue(
                string_value(image.get("image_id")),
                "unresolved_local_image_reference",
                None,
            )
        )
    if image.get("source_type") in {"http", "https"}:
        errors.append(
            issue(
                string_value(image.get("image_id")),
                "external_image_unresolved",
                image.get("original_src"),
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
    release_report = validate_parsed_release(input_path, write_report=False)
    if not release_report["valid"]:
        raise BankError(
            f"parsed-export refused invalid release: {len(release_report['errors'])} error(s)"
        )
    output_parent = output.parent
    part_output = output_parent / f".{output.name}.part"
    safe_remove_tree(part_output, output_parent)
    if output.exists():
        raise BankError(f"Export output already exists: {output}")
    part_output.mkdir(parents=True, exist_ok=True)
    backend = part_output / "backend"
    frontend = part_output / "frontend"
    search = part_output / "search"
    rag = part_output / "rag"
    backend_files = (
        ("documents.jsonl", backend / "documents.jsonl"),
        ("sections.jsonl", backend / "sections.jsonl"),
        ("blocks.jsonl", backend / "blocks.jsonl"),
        ("tables.jsonl", backend / "tables.jsonl"),
        ("table-cells.jsonl", backend / "table-cells.jsonl"),
        ("table-placements.jsonl", backend / "table-placements.jsonl"),
        ("images.jsonl", backend / "images.jsonl"),
        ("assets.jsonl", backend / "assets.jsonl"),
        ("recommendations.jsonl", backend / "recommendations.jsonl"),
        ("references.jsonl", backend / "references.jsonl"),
        ("relations.jsonl", backend / "relations.jsonl"),
        ("dataset.json", backend / "dataset.json"),
        ("reports/document-validation.jsonl", backend / "reports" / "document-validation.jsonl"),
        ("reports/release-validation.json", backend / "reports" / "release-validation.json"),
    )
    data_files = (
        ("search/chunks.jsonl", search / "chunks.jsonl"),
        ("rag/chunks.jsonl", rag / "chunks.jsonl"),
        ("rag/citation-index.jsonl", rag / "citation-index.jsonl"),
        ("rag/embedding-input.jsonl", rag / "embedding-input.jsonl"),
    )
    for source, target in (*backend_files, *data_files):
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
    write_json(part_output / "export-manifest.json", manifest)
    export_validation = validate_export_package(part_output, input_path, release_report)
    write_json(part_output / "reports" / "export-validation.json", export_validation)
    if not export_validation["valid"]:
        safe_remove_tree(part_output, output_parent)
        raise BankError(
            f"parsed-export package validation failed: "
            f"{len(export_validation['errors'])} error(s)"
        )
    write_checksums(part_output)
    part_output.replace(output)
    return ParsedExportSummary(
        input=input_path,
        output=output,
        backend_files=len(backend_files),
        frontend_documents=len(documents),
        assets=assets,
        search_chunks=len(read_jsonl(output / "search" / "chunks.jsonl")),
        rag_chunks=len(read_jsonl(output / "rag" / "chunks.jsonl")),
        manifest_path=output / "export-manifest.json",
    )


def build_parsed_diff(input_path: Path, output: Path | None = None) -> ParsedDiffSummary:
    if output is None:
        output = input_path.parent / f"{input_path.name}-diff"
    output_parent = output.parent
    part_output = output_parent / f".{output.name}.part"
    safe_remove_tree(part_output, output_parent)
    if output.exists():
        raise BankError(f"Parsed diff output already exists: {output}")
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
    write_jsonl(part_output / "pairs.jsonl", pair_rows)
    write_jsonl(part_output / "sections.jsonl", section_rows)
    write_jsonl(part_output / "tables.jsonl", table_rows)
    write_jsonl(part_output / "images.jsonl", image_rows)
    summary = {
        "schema_version": PARSED_SCHEMA_VERSION,
        "input": input_path.as_posix(),
        "output": output.as_posix(),
        "pairs": len(pair_rows),
        "section_changes": len(section_rows),
        "table_changes": len(table_rows),
        "image_changes": len(image_rows),
    }
    write_json(part_output / "summary.json", summary)
    manifest = content_hash_manifest(part_output)
    write_json(part_output / "manifest.json", manifest)
    write_checksums(part_output)
    part_output.replace(output)
    return ParsedDiffSummary(
        input=input_path,
        output=output,
        pairs=len(pair_rows),
        section_changes=len(section_rows),
        table_changes=len(table_rows),
        image_changes=len(image_rows),
        summary_path=output / "summary.json",
    )


def validate_export_package(
    output: Path,
    input_path: Path,
    release_report: dict[str, Any],
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    required = [
        "backend/documents.jsonl",
        "backend/sections.jsonl",
        "backend/blocks.jsonl",
        "backend/tables.jsonl",
        "backend/table-cells.jsonl",
        "backend/table-placements.jsonl",
        "backend/images.jsonl",
        "backend/assets.jsonl",
        "backend/recommendations.jsonl",
        "backend/references.jsonl",
        "backend/relations.jsonl",
        "backend/dataset.json",
        "search/chunks.jsonl",
        "rag/chunks.jsonl",
        "rag/citation-index.jsonl",
        "rag/embedding-input.jsonl",
        "frontend/manifest.json",
        "export-manifest.json",
    ]
    for relative in required:
        if not (output / relative).exists():
            errors.append(issue(relative, "export_required_file_missing", None))

    for source_relative, export_relative in (
        ("documents.jsonl", "backend/documents.jsonl"),
        ("sections.jsonl", "backend/sections.jsonl"),
        ("blocks.jsonl", "backend/blocks.jsonl"),
        ("tables.jsonl", "backend/tables.jsonl"),
        ("table-cells.jsonl", "backend/table-cells.jsonl"),
        ("table-placements.jsonl", "backend/table-placements.jsonl"),
        ("images.jsonl", "backend/images.jsonl"),
        ("assets.jsonl", "backend/assets.jsonl"),
        ("recommendations.jsonl", "backend/recommendations.jsonl"),
        ("references.jsonl", "backend/references.jsonl"),
        ("relations.jsonl", "backend/relations.jsonl"),
        ("search/chunks.jsonl", "search/chunks.jsonl"),
        ("rag/chunks.jsonl", "rag/chunks.jsonl"),
        ("rag/citation-index.jsonl", "rag/citation-index.jsonl"),
        ("rag/embedding-input.jsonl", "rag/embedding-input.jsonl"),
    ):
        if (input_path / source_relative).exists() and (output / export_relative).exists():
            source_hash = sha256_file(input_path / source_relative)
            export_hash = sha256_file(output / export_relative)
            if source_hash != export_hash:
                errors.append(
                    issue(
                        export_relative,
                        "export_file_hash_mismatch",
                        {"source": source_hash, "export": export_hash},
                    )
                )

    documents = read_jsonl(input_path / "documents.jsonl")
    for document in documents:
        code_version = string_value(document.get("code_version"))
        if document.get("document_kind") == "previous":
            frontend_path = (
                output
                / "frontend"
                / "documents"
                / "previous"
                / string_value(document.get("current_code_version"))
                / f"{code_version}.json"
            )
        else:
            frontend_path = output / "frontend" / "documents" / "current" / f"{code_version}.json"
        if not frontend_path.exists():
            errors.append(issue(frontend_path.as_posix(), "frontend_document_missing", None))
            continue
        payload = read_json_file(frontend_path)
        table_values = payload.get("tables")
        image_values = payload.get("images")
        section_values = payload.get("sections")
        frontend_tables: list[Any] = table_values if isinstance(table_values, list) else []
        frontend_images: list[Any] = image_values if isinstance(image_values, list) else []
        section_rows = [
            section for section in section_values if isinstance(section, dict)
        ] if isinstance(section_values, list) else []
        table_markers = 0
        image_markers = 0
        for section in section_rows:
            normalized_html = string_value(section.get("normalized_html"))
            if "data:image/" in normalized_html:
                errors.append(
                    issue(
                        string_value(section.get("section_id")),
                        "frontend_base64_image_src",
                        None,
                    )
                )
            soup = BeautifulSoup(normalized_html, "html.parser")
            for tag in soup.find_all(True):
                if not isinstance(tag, Tag):
                    continue
                for attr in tag.attrs:
                    attr_name = attr.casefold()
                    if attr_name.startswith("on"):
                        errors.append(
                            issue(
                                string_value(section.get("section_id")),
                                "frontend_unsafe_event_handler",
                                attr,
                            )
                        )
                    if attr_name in {"href", "src"} and is_javascript_url(tag.get(attr)):
                        errors.append(
                            issue(
                                string_value(section.get("section_id")),
                                "frontend_unsafe_javascript_url",
                                attr,
                            )
                        )
            table_markers += len(
                [
                    tag
                    for tag in soup.find_all(
                        lambda item: isinstance(item, Tag)
                        and item.has_attr("data-table-id")
                    )
                    if isinstance(tag, Tag)
                ]
            )
            image_markers += len(
                [
                    tag
                    for tag in soup.find_all(
                        lambda item: isinstance(item, Tag)
                        and item.has_attr("data-image-id")
                    )
                    if isinstance(tag, Tag)
                ]
            )
            for image_tag in soup.find_all("img"):
                if not isinstance(image_tag, Tag):
                    continue
                src = string_value(image_tag.get("src"))
                if not src:
                    errors.append(
                        issue(
                            string_value(section.get("section_id")),
                            "frontend_image_src_missing",
                            None,
                        )
                    )
                elif src.startswith("data:") or src.startswith(("http://", "https://")):
                    errors.append(
                        issue(
                            string_value(section.get("section_id")),
                            "frontend_image_src_not_local",
                            src,
                        )
                    )
                elif not (output / "frontend" / src).exists():
                    errors.append(
                        issue(
                            string_value(section.get("section_id")),
                            "frontend_image_asset_missing",
                            src,
                        )
                    )
        if table_markers != len(frontend_tables):
            errors.append(
                issue(
                    frontend_path.as_posix(),
                    "frontend_table_placement_count_mismatch",
                    {"markers": table_markers, "tables": len(frontend_tables)},
                )
            )
        if image_markers != len(frontend_images):
            errors.append(
                issue(
                    frontend_path.as_posix(),
                    "frontend_image_placement_count_mismatch",
                    {"markers": image_markers, "images": len(frontend_images)},
                )
            )

    return {
        "schema_version": PARSED_SCHEMA_VERSION,
        "input": input_path.as_posix(),
        "output": output.as_posix(),
        "valid": not errors and bool(release_report.get("valid")),
        "release_valid": bool(release_report.get("valid")),
        "errors": errors,
        "warnings": warnings,
    }


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
    if document.get("document_kind") == "previous":
        target = (
            root
            / "documents"
            / "previous"
            / string_value(document.get("current_code_version"))
            / f"{code_version}.json"
        )
    else:
        target = root / "documents" / "current" / f"{code_version}.json"
    write_json(target, payload)


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
