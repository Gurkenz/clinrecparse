from __future__ import annotations

import base64
import binascii
import csv
import html
import json
import re
import shutil
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from clinrec.bank.common import (
    BankError,
    first_non_empty,
    first_present,
    parse_code_version_or_raise,
    read_json_file,
    sha256_bytes,
    sha256_file,
    stable_json_dumps,
    string_value,
)
from clinrec.parsed.layer import estimate_tokens, extension_for_mime, safe_id, sha256_text
from clinrec.research.reports import write_json, write_jsonl
from clinrec.research.sections import raw_sections, section_html, section_id_for

SHOWCASE_SCHEMA_VERSION = "0.2-pilot"
SHOWCASE_PARSER_VERSION = "parsed-showcase-0.2"
DEFAULT_SHOWCASE_CODE_VERSION = "843_1"
CHUNK_TEXT_LIMIT = 4000
UNSAFE_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "meta",
    "link",
}
SAFE_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "caption",
    "col",
    "colgroup",
    "dd",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "section",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
HEADING_TAGS = {f"h{level}" for level in range(1, 7)}
DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[-\w.+]+/[-\w.+]+)(?:;[-\w.+]+=[^;,]+)*;base64,(?P<data>.*)$",
    re.IGNORECASE | re.DOTALL,
)
REFERENCE_RE = re.compile(
    r"\[(?P<body>\d+(?:\s*[-\u2013]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-\u2013]\s*\d+)?)*)\]"
)
RECOMMENDATION_RE = re.compile(
    r"\b("
    r"\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u0435\u0442\u0441\u044f|"
    r"\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u044e\u0442\u0441\u044f|"
    r"\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u043e\u0432\u0430\u043d\u043e|"
    r"\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u043e\u0432\u0430\u043d\u044b|"
    r"\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u0435\u043c"
    r")\b",
    re.IGNORECASE,
)
UUR_RE = re.compile(
    r"\b\u0423\u0423\u0420\s*[:\-\u2013]?\s*([ABC\u0410\u0412\u0421])\b",
    re.IGNORECASE,
)
UDD_RE = re.compile(
    r"\b\u0423\u0414\u0414\s*[:\-\u2013]?\s*([1-5][ABC\u0410\u0412\u0421]?)\b",
    re.IGNORECASE,
)
IMAGE_SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/jpg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}


class ShowcaseError(RuntimeError):
    pass


class ShowcaseInputError(ShowcaseError):
    pass


class ShowcaseValidationError(ShowcaseError):
    def __init__(self, message: str, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path


@dataclass(frozen=True)
class ParsedShowcaseOptions:
    output: Path
    input_corpus: Path | None = None
    raw_json: Path | None = None
    manifest: Path | None = None
    catalog_record: Path | None = None
    catalog_candidates: Path | None = None
    code_version: str = DEFAULT_SHOWCASE_CODE_VERSION
    overwrite: bool = False
    keep_builds: bool = False
    created_at: str | None = None


@dataclass(frozen=True)
class ParsedShowcaseSummary:
    output: Path
    archive: Path
    archive_sha256: str
    archive_size: int
    raw_path: Path
    raw_sha256: str
    manifest_valid: bool
    document_title: str
    code_version: str
    sections: int
    blocks: int
    table_classifications: dict[str, int]
    tables: int
    table_cells: int
    image_occurrences: int
    unique_assets: int
    image_decode_failures: int
    recommendations: int
    references: int
    text_chunks: int
    table_chunks: int
    image_chunks: int
    hard_errors: int
    warnings: int
    determinism_passed: bool
    validation_report: Path
    zip_verified: bool


@dataclass(frozen=True)
class ShowcaseInput:
    source_kind: str
    source_root: Path | None
    raw_json: Path
    manifest_path: Path | None
    catalog_record_path: Path | None
    catalog_candidates_path: Path | None
    raw_bytes: bytes
    payload: dict[str, Any]
    manifest: dict[str, Any]
    catalog_record: dict[str, Any]
    catalog_candidates: dict[str, Any]
    code_version: str
    code: int
    version: int
    raw_sha256: str
    raw_size: int
    manifest_valid: bool


@dataclass
class ShowcaseState:
    root: Path
    source: ShowcaseInput
    document_id: str
    dataset_id: str
    created_at: str
    repository_commit: str
    build_config_sha256: str
    document: dict[str, Any]
    sections: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    table_cells: list[dict[str, Any]]
    images: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    references: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    citation_rows: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]


def build_parsed_showcase(options: ParsedShowcaseOptions) -> ParsedShowcaseSummary:
    source = resolve_showcase_input(options)
    ensure_output_configuration(options)
    created_at = options.created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    repository_commit = git_commit_or_unknown()
    build_config = {
        "code_version": source.code_version,
        "parser_version": SHOWCASE_PARSER_VERSION,
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "source_raw_sha256": source.raw_sha256,
    }
    build_config_sha256 = sha256_bytes(stable_json_dumps(build_config).encode("utf-8"))
    showcase_parent = options.output.parent
    build_a = showcase_parent / f".{source.code_version}.build-a.part"
    build_b = showcase_parent / f".{source.code_version}.build-b.part"
    for part in (build_a, build_b):
        safe_remove_tree(part, showcase_parent)

    state_a = build_showcase_directory(
        build_a,
        source,
        created_at=created_at,
        repository_commit=repository_commit,
        build_config_sha256=build_config_sha256,
    )
    report_a = validate_showcase_directory(build_a)
    if not report_a["valid"]:
        raise ShowcaseValidationError(
            "showcase validation failed for build A",
            build_a / "reports" / "showcase-validation.json",
        )
    build_showcase_directory(
        build_b,
        source,
        created_at=created_at,
        repository_commit=repository_commit,
        build_config_sha256=build_config_sha256,
    )
    report_b = validate_showcase_directory(build_b)
    if not report_b["valid"]:
        raise ShowcaseValidationError(
            "showcase validation failed for build B",
            build_b / "reports" / "showcase-validation.json",
        )

    determinism = compare_deterministic_trees(build_a, build_b)
    write_json(build_a / "reports" / "determinism-comparison.json", determinism)
    if not determinism["passed"]:
        raise ShowcaseValidationError(
            "showcase deterministic rebuild failed",
            build_a / "reports" / "determinism-comparison.json",
        )

    raw_after_sha = sha256_file(source.raw_json)
    if raw_after_sha != source.raw_sha256:
        raise ShowcaseValidationError("source raw changed during build")

    if options.output.exists():
        if not options.overwrite:
            raise ShowcaseInputError(f"Output already exists: {options.output}")
        safe_remove_tree(options.output, showcase_parent)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    build_a.replace(options.output)
    if not options.keep_builds:
        safe_remove_tree(build_b, showcase_parent)

    finalize_showcase_directory(options.output, state_a, raw_after_sha=raw_after_sha)
    archive = options.output.parent / f"clinrec-showcase-{source.code_version}.zip"
    if archive.exists():
        archive.unlink()
    create_showcase_zip(options.output, archive)
    zip_report = verify_showcase_zip(options.output, archive)
    archive_sha = sha256_file(archive)
    archive_size = archive.stat().st_size
    write_json(
        options.output / "reports" / "archive-verification.json",
        {
            **zip_report,
            "archive_path": archive.as_posix(),
            "archive_sha256": archive_sha,
            "archive_size": archive_size,
        },
    )
    write_checksums(options.output)
    validation = validate_showcase_directory(options.output)
    if not validation["valid"]:
        raise ShowcaseValidationError(
            "showcase validation failed after finalization",
            options.output / "reports" / "showcase-validation.json",
        )

    return summary_from_state(
        state_a,
        output=options.output,
        archive=archive,
        archive_sha256=archive_sha,
        archive_size=archive_size,
        validation=validation,
        zip_verified=bool(zip_report["valid"]),
    )


def resolve_showcase_input(options: ParsedShowcaseOptions) -> ShowcaseInput:
    if options.input_corpus is not None and options.raw_json is not None:
        raise ShowcaseInputError("--input-corpus and --raw-json are mutually exclusive")
    parse_code_version_or_raise(options.code_version)
    source_kind = "corpus_document" if options.raw_json is None else "standalone_raw_json"
    source_root: Path | None = None
    manifest_path: Path | None
    catalog_record_path: Path | None
    catalog_candidates_path: Path | None
    if options.raw_json is None:
        corpus_root = options.input_corpus or Path("data/research/corpora/live-json-250")
        source_root = corpus_root
        raw_json = corpus_root / "current" / options.code_version / "getclinrec.json"
        manifest_path = corpus_root / "current" / options.code_version / "manifest.json"
        catalog_record_path = (
            corpus_root / "current" / options.code_version / "catalog-record.json"
        )
        catalog_candidates_path = (
            corpus_root / "current" / options.code_version / "catalog-candidates.json"
        )
    else:
        raw_json = options.raw_json
        manifest_path = options.manifest
        catalog_record_path = options.catalog_record
        catalog_candidates_path = options.catalog_candidates

    if not raw_json.exists():
        raise ShowcaseInputError(f"Raw JSON is missing: {raw_json}")
    raw_bytes = raw_json.read_bytes()
    try:
        payload_value = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowcaseInputError(f"Raw JSON cannot be read as UTF-8 object: {exc}") from exc
    if not isinstance(payload_value, dict):
        raise ShowcaseInputError("Raw JSON root is not an object")
    payload: dict[str, Any] = payload_value
    raw_sha = sha256_bytes(raw_bytes)
    raw_size = len(raw_bytes)
    code, version = parse_code_version_or_raise(options.code_version)
    payload_code_version = document_code_version(payload)
    if payload_code_version != options.code_version:
        raise ShowcaseInputError(
            f"CodeVersion mismatch: expected {options.code_version}, got {payload_code_version}"
        )
    payload_code = int_value(first_present(payload, "code", "Code"))
    payload_version = int_value(first_present(payload, "version", "Version", "ver", "Ver"))
    if payload_code != code or payload_version != version:
        raise ShowcaseInputError(
            "code/version mismatch: "
            f"expected {code}_{version}, got {payload_code}_{payload_version}"
        )

    manifest = read_json_file(manifest_path) if manifest_path is not None else {}
    manifest_valid = False
    if manifest_path is not None and manifest_path.exists():
        manifest_valid = manifest.get("sha256") == raw_sha and manifest.get("size") == raw_size
        if not manifest_valid:
            raise ShowcaseInputError("Raw manifest SHA or size mismatch")
    catalog_record = read_json_file(catalog_record_path) if catalog_record_path is not None else {}
    catalog_candidates = (
        read_json_file(catalog_candidates_path) if catalog_candidates_path is not None else {}
    )
    return ShowcaseInput(
        source_kind=source_kind,
        source_root=source_root,
        raw_json=raw_json,
        manifest_path=(
            manifest_path
            if manifest_path is not None and manifest_path.exists()
            else None
        ),
        catalog_record_path=(
            catalog_record_path
            if catalog_record_path is not None and catalog_record_path.exists()
            else None
        ),
        catalog_candidates_path=(
            catalog_candidates_path
            if catalog_candidates_path is not None and catalog_candidates_path.exists()
            else None
        ),
        raw_bytes=raw_bytes,
        payload=payload,
        manifest=manifest,
        catalog_record=catalog_record,
        catalog_candidates=catalog_candidates,
        code_version=options.code_version,
        code=code,
        version=version,
        raw_sha256=raw_sha,
        raw_size=raw_size,
        manifest_valid=manifest_valid,
    )


def ensure_output_configuration(options: ParsedShowcaseOptions) -> None:
    if not options.output:
        raise ShowcaseInputError("--output is required")
    if "bank" in {part.casefold() for part in options.output.resolve().parts}:
        raise ShowcaseInputError("Showcase output must not be written inside data/bank")
    if options.output.exists() and not options.overwrite:
        raise ShowcaseInputError(f"Output already exists: {options.output}")


def build_showcase_directory(
    root: Path,
    source: ShowcaseInput,
    *,
    created_at: str,
    repository_commit: str,
    build_config_sha256: str,
) -> ShowcaseState:
    root.mkdir(parents=True, exist_ok=True)
    copy_source_files(root, source)
    document_id = f"current:{source.code_version}"
    state = ShowcaseState(
        root=root,
        source=source,
        document_id=document_id,
        dataset_id=f"showcase:{source.code_version}",
        created_at=created_at,
        repository_commit=repository_commit,
        build_config_sha256=build_config_sha256,
        document={},
        sections=[],
        blocks=[],
        tables=[],
        table_cells=[],
        images=[],
        assets=[],
        recommendations=[],
        references=[],
        chunks=[],
        citation_rows=[],
        warnings=[],
        errors=[],
    )
    parse_showcase_document(state)
    write_showcase_packages(state)
    return state


def copy_source_files(root: Path, source: ShowcaseInput) -> None:
    source_root = root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "getclinrec.json").write_bytes(source.raw_bytes)
    for path, name in (
        (source.manifest_path, "manifest.json"),
        (source.catalog_record_path, "catalog-record.json"),
        (source.catalog_candidates_path, "catalog-candidates.json"),
    ):
        if path is not None:
            shutil.copyfile(path, source_root / name)


def parse_showcase_document(state: ShowcaseState) -> None:
    payload = state.source.payload
    sections = [section for section in raw_sections(payload) if isinstance(section, dict)]
    occurrence_counts: Counter[str] = Counter()
    parent_stack: list[tuple[int, str]] = []
    for source_order, raw_section in enumerate(sections):
        source_section_id = section_id_for(raw_section) or f"section_{source_order:04d}"
        occurrence_index = occurrence_counts[source_section_id]
        occurrence_counts[source_section_id] += 1
        depth = section_depth(raw_section)
        while parent_stack and parent_stack[-1][0] >= depth:
            parent_stack.pop()
        parent_section_id = parent_stack[-1][1] if parent_stack and depth > 0 else None
        section = parse_showcase_section(
            state,
            raw_section,
            source_order=source_order,
            source_section_id=source_section_id,
            occurrence_index=occurrence_index,
            parent_section_id=parent_section_id,
            depth=depth,
        )
        state.sections.append(section)
        parent_stack.append((depth, string_value(section["section_id"])))

    extract_recommendations(state)
    build_chunks(state)
    refresh_document_record(state)


def parse_showcase_section(
    state: ShowcaseState,
    raw_section: dict[str, Any],
    *,
    source_order: int,
    source_section_id: str,
    occurrence_index: int,
    parent_section_id: str | None,
    depth: int,
) -> dict[str, Any]:
    section_key = f"{safe_id(source_section_id)}#{occurrence_index}"
    section_id = f"{state.document_id}:{section_key}"
    raw_html = section_html(raw_section)
    raw_table_htmls = raw_table_fragments(raw_html)
    raw_img_count = len(BeautifulSoup(raw_html, "lxml").find_all("img")) if raw_html else 0
    soup = BeautifulSoup(raw_html, "lxml")
    root = soup.body if soup.body is not None else soup
    warnings: list[str] = []
    sanitize_html_tree(root, warnings)
    image_ids = process_section_images(
        state,
        root,
        section_id=section_id,
        section_key=section_key,
        source_order=source_order,
    )
    table_ids = process_section_tables(
        state,
        root,
        raw_table_htmls=raw_table_htmls,
        section_id=section_id,
        section_key=section_key,
        source_order=source_order,
    )
    add_section_attributes(root, section_id=section_id)
    normalized_html = fragment_html(root)
    plain_text = visible_text(normalized_html)
    block_ids = process_section_blocks(
        state,
        root,
        section_id=section_id,
        section_key=section_key,
    )
    if not raw_html and not raw_section.get("data"):
        warnings.append("empty_section")
    if raw_img_count and len(image_ids) != raw_img_count:
        warnings.append("some_images_not_extracted")
    for warning in warnings:
        state.warnings.append({"path": section_id, "code": warning, "details": None})
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "section_id": section_id,
        "source_section_id": source_section_id,
        "occurrence_index": occurrence_index,
        "section_key": section_key,
        "source_order": source_order,
        "parent_section_id": parent_section_id,
        "depth": depth,
        "title": section_title(raw_section),
        "raw_html": raw_html,
        "raw_html_sha256": sha256_text(raw_html),
        "normalized_html": normalized_html,
        "normalized_html_sha256": sha256_text(normalized_html),
        "plain_text": plain_text,
        "plain_text_sha256": sha256_text(plain_text),
        "block_ids": block_ids,
        "table_ids": table_ids,
        "image_ids": image_ids,
        "recommendation_ids": [],
        "reference_ids": [
            row["reference_id"] for row in state.references if row.get("section_id") == section_id
        ],
        "raw_data": raw_section,
        "warnings": warnings,
        "anchor": stable_anchor("section", source_order, section_key),
        "source_raw_path": "source/getclinrec.json",
        "source_raw_sha256": state.source.raw_sha256,
        "parser_version": SHOWCASE_PARSER_VERSION,
    }


def process_section_images(
    state: ShowcaseState,
    root: Tag | BeautifulSoup,
    *,
    section_id: str,
    section_key: str,
    source_order: int,
) -> list[str]:
    image_ids: list[str] = []
    images = [image for image in root.find_all("img") if isinstance(image, Tag)]
    for image_index, image in enumerate(images):
        image_id = f"{section_id}:image#{image_index}"
        src = string_value(image.get("src"))
        source_type = classify_image_src(src, src_present=image.has_attr("src"))
        mime_type: str | None = None
        asset_sha: str | None = None
        asset_id: str | None = None
        asset_path: str | None = None
        decoded_size: int | None = None
        decode_error: str | None = None
        signature_matches: bool | None = None
        if source_type == "base64":
            mime_type, token = split_data_uri(src)
            try:
                content = base64.b64decode(re.sub(r"\s+", "", token), validate=True)
                decoded_size = len(content)
                signature_matches = image_signature_matches(mime_type, content)
                asset_sha = sha256_bytes(content)
                asset_id = f"sha256:{asset_sha}"
                asset_path = write_asset_once(state, content, mime_type)
                image["src"] = asset_path
                image["data-asset-id"] = asset_id
                if signature_matches is False:
                    state.warnings.append(
                        {
                            "path": image_id,
                            "code": "image_mime_declaration_mismatch",
                            "details": mime_type,
                        }
                    )
            except (binascii.Error, ValueError) as exc:
                decode_error = str(exc)
                image["src"] = ""
                image["data-image-status"] = "decode_failed"
        elif source_type in {"http", "https"}:
            state.warnings.append(
                {"path": image_id, "code": "external_image_not_fetched", "details": src}
            )
        image["data-image-id"] = image_id
        image_record = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "section_id": section_id,
            "section_key": section_key,
            "source_order": source_order,
            "image_id": image_id,
            "occurrence_id": image_id,
            "image_index": image_index,
            "asset_id": asset_id,
            "asset_sha256": asset_sha,
            "asset_path": asset_path,
            "source_type": source_type,
            "mime_type": mime_type,
            "decoded_size_bytes": decoded_size,
            "signature_matches_declared_mime": signature_matches,
            "decode_error": decode_error,
            "alt": string_value(image.get("alt")),
            "raw_src_sha256": sha256_text(src) if src else None,
            "warnings": [],
            "source_raw_sha256": state.source.raw_sha256,
        }
        state.images.append(image_record)
        image_ids.append(image_id)
    return image_ids


def process_section_tables(
    state: ShowcaseState,
    root: Tag | BeautifulSoup,
    *,
    raw_table_htmls: list[str],
    section_id: str,
    section_key: str,
    source_order: int,
) -> list[str]:
    table_ids: list[str] = []
    tables = [table for table in root.find_all("table") if isinstance(table, Tag)]
    for table_index, table in enumerate(tables):
        table_id = f"{section_id}:table#{table_index}"
        table["data-table-id"] = table_id
        cell_rows, logical_grid = table_cells_and_grid(table, table_id=table_id)
        for cell in cell_rows:
            cell.update(
                {
                    "schema_version": SHOWCASE_SCHEMA_VERSION,
                    "dataset_id": state.dataset_id,
                    "document_id": state.document_id,
                    "section_id": section_id,
                }
            )
            state.table_cells.append(cell)
        nested_table_ids = [
            f"{section_id}:table#{nested_index}"
            for nested_index, nested in enumerate(tables)
            if nested is not table and nested.find_parent("table") is table
        ]
        source_html = raw_table_htmls[table_index] if table_index < len(raw_table_htmls) else ""
        normalized_html = str(table)
        row_count = len({int(cell["row_index"]) for cell in cell_rows})
        column_count = max((int(cell["column_index"]) + 1 for cell in cell_rows), default=0)
        logical_row_count = len(logical_grid)
        logical_column_count = max((len(row) for row in logical_grid), default=0)
        table_record = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "section_id": section_id,
            "section_key": section_key,
            "table_id": table_id,
            "table_index": table_index,
            "classification": table_classification(table, cell_rows),
            "source_html": source_html,
            "normalized_html": normalized_html,
            "row_count": row_count,
            "column_count": column_count,
            "logical_row_count": logical_row_count,
            "logical_column_count": logical_column_count,
            "has_rowspan": any(int(cell["rowspan"]) > 1 for cell in cell_rows),
            "has_colspan": any(int(cell["colspan"]) > 1 for cell in cell_rows),
            "nested_table_ids": nested_table_ids,
            "cell_ids": [string_value(cell["cell_id"]) for cell in cell_rows],
            "caption": table_caption(table),
            "plain_text": visible_text(normalized_html),
            "plain_text_sha256": sha256_text(visible_text(normalized_html)),
            "source_order": source_order,
            "safe_id": safe_file_id(table_id),
            "warnings": [],
            "source_raw_sha256": state.source.raw_sha256,
        }
        state.tables.append(table_record)
        table_ids.append(table_id)
    return table_ids


def process_section_blocks(
    state: ShowcaseState,
    root: Tag | BeautifulSoup,
    *,
    section_id: str,
    section_key: str,
) -> list[str]:
    block_ids: list[str] = []
    for block_index, child in enumerate(meaningful_children(root)):
        block = build_block_record(
            state,
            child,
            section_id=section_id,
            section_key=section_key,
            block_index=block_index,
        )
        if block is None:
            continue
        state.blocks.append(block)
        block_ids.append(string_value(block["block_id"]))
    return block_ids


def build_block_record(
    state: ShowcaseState,
    element: PageElement,
    *,
    section_id: str,
    section_key: str,
    block_index: int,
) -> dict[str, Any] | None:
    if isinstance(element, NavigableString):
        text = normalize_text(str(element))
        if not text:
            return None
        tag_name = None
        raw_html = html.escape(str(element))
        normalized_html = raw_html
        table_ids: list[str] = []
        image_ids: list[str] = []
        block_type = "paragraph"
    elif isinstance(element, Tag):
        tag_name = element.name.lower()
        raw_html = str(element)
        normalized_html = str(element)
        text = normalize_text(element.get_text(" ", strip=True))
        table_ids = [
            string_value(table.get("data-table-id"))
            for table in element.find_all("table")
            if isinstance(table, Tag) and table.get("data-table-id")
        ]
        if tag_name == "table" and element.get("data-table-id"):
            table_ids = [string_value(element.get("data-table-id"))]
        image_ids = [
            string_value(image.get("data-image-id"))
            for image in element.find_all("img")
            if isinstance(image, Tag) and image.get("data-image-id")
        ]
        if tag_name == "img" and element.get("data-image-id"):
            image_ids = [string_value(element.get("data-image-id"))]
        block_type = block_type_for_tag(tag_name)
        if block_type == "table_placeholder":
            text = table_caption(element) or ""
    else:
        return None
    block_id = f"{section_id}:block#{block_index}"
    reference_ids = register_references(state, text, section_id=section_id, block_id=block_id)
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "section_id": section_id,
        "section_key": section_key,
        "block_id": block_id,
        "block_index": block_index,
        "block_type": block_type,
        "tag": tag_name,
        "raw_html": raw_html,
        "normalized_html": normalized_html,
        "text": text,
        "text_sha256": sha256_text(text),
        "table_ids": table_ids,
        "image_ids": image_ids,
        "recommendation_ids": [],
        "reference_ids": reference_ids,
        "uur": extract_uur(text),
        "udd": extract_udd(text),
        "warnings": [],
    }


def register_references(
    state: ShowcaseState,
    text: str,
    *,
    section_id: str,
    block_id: str,
) -> list[str]:
    reference_ids: list[str] = []
    for match in REFERENCE_RE.finditer(text):
        reference_id = f"{section_id}:reference#{len(state.references)}"
        reference = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "section_id": section_id,
            "block_id": block_id,
            "reference_id": reference_id,
            "reference_index": len(state.references),
            "source_text": match.group(0),
            "numbers": normalize_reference_numbers(match.group("body")),
            "source_raw_sha256": state.source.raw_sha256,
        }
        state.references.append(reference)
        reference_ids.append(reference_id)
    return reference_ids


def extract_recommendations(state: ShowcaseState) -> None:
    by_section: dict[str, list[dict[str, Any]]] = {}
    for block in state.blocks:
        by_section.setdefault(string_value(block["section_id"]), []).append(block)
    section_recommendations: dict[str, list[str]] = {}
    for section_id, blocks in by_section.items():
        section_index = 0
        for block in blocks:
            text = string_value(block.get("text"))
            if not text or RECOMMENDATION_RE.search(text) is None:
                continue
            recommendation_id = f"{section_id}:recommendation#{section_index}"
            section_index += 1
            recommendation = {
                "schema_version": SHOWCASE_SCHEMA_VERSION,
                "dataset_id": state.dataset_id,
                "document_id": state.document_id,
                "section_id": section_id,
                "recommendation_id": recommendation_id,
                "recommendation_index": section_index - 1,
                "text": text,
                "text_sha256": sha256_text(text),
                "block_ids": [block["block_id"]],
                "reference_ids": block.get("reference_ids") or [],
                "uur": block.get("uur"),
                "udd": block.get("udd"),
                "source_raw_sha256": state.source.raw_sha256,
            }
            state.recommendations.append(recommendation)
            block["recommendation_ids"] = [recommendation_id]
            if block.get("block_type") == "paragraph":
                block["block_type"] = "recommendation"
            section_recommendations.setdefault(section_id, []).append(recommendation_id)
    for section in state.sections:
        section["recommendation_ids"] = section_recommendations.get(
            string_value(section["section_id"]),
            [],
        )


def build_chunks(state: ShowcaseState) -> None:
    chunk_index_by_section: Counter[str] = Counter()
    for section in state.sections:
        text = section_text_without_tables_and_images(string_value(section["normalized_html"]))
        if text:
            section_id = string_value(section["section_id"])
            chunk_index = chunk_index_by_section[section_id]
            chunk_index_by_section[section_id] += 1
            append_chunk(
                state,
                chunk_id=f"{state.document_id}:{section['section_key']}:chunk#{chunk_index}",
                chunk_type="text",
                text=text,
                section=section,
                table_id=None,
                image_id=None,
                asset_path=None,
                row_start=None,
                row_end=None,
            )
    for table in state.tables:
        text = table_text_for_chunk(table, state.table_cells)
        if not text:
            state.warnings.append(
                {"path": table["table_id"], "code": "layout_table_not_indexed", "details": None}
            )
            continue
        section = section_by_id(state, string_value(table["section_id"]))
        rows = max(int(table.get("logical_row_count") or 0) - 1, 0)
        append_chunk(
            state,
            chunk_id=f"{table['table_id']}:rows#0-{rows}",
            chunk_type="table",
            text=text,
            section=section,
            table_id=string_value(table["table_id"]),
            image_id=None,
            asset_path=None,
            row_start=0,
            row_end=rows,
        )
    for image in state.images:
        image_id = string_value(image["image_id"])
        section = section_by_id(state, string_value(image["section_id"]))
        context = image_context_text(section, image)
        if not string_value(image.get("alt")):
            state.warnings.append(
                {"path": image_id, "code": "image_without_textual_context", "details": None}
            )
        append_chunk(
            state,
            chunk_id=f"{image_id}:context",
            chunk_type="image",
            text=context,
            section=section,
            table_id=None,
            image_id=image_id,
            asset_path=string_value(image.get("asset_path")) or None,
            row_start=None,
            row_end=None,
        )


def append_chunk(
    state: ShowcaseState,
    *,
    chunk_id: str,
    chunk_type: str,
    text: str,
    section: dict[str, Any],
    table_id: str | None,
    image_id: str | None,
    asset_path: str | None,
    row_start: int | None,
    row_end: int | None,
) -> None:
    normalized = normalize_text(text)
    if len(normalized) > CHUNK_TEXT_LIMIT:
        normalized = normalized[:CHUNK_TEXT_LIMIT].rstrip()
    chunk = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "chunk_id": chunk_id,
        "chunk_type": chunk_type,
        "text": normalized,
        "text_sha256": sha256_text(normalized),
        "token_estimate": estimate_tokens(normalized),
        "section_id": section.get("section_id"),
        "section_key": section.get("section_key"),
        "section_title": section.get("title"),
        "table_id": table_id,
        "image_id": image_id,
        "asset_path": asset_path,
        "row_start": row_start,
        "row_end": row_end,
        "source_raw_sha256": state.source.raw_sha256,
        "citation": citation_for_chunk(state, section, table_id=table_id, image_id=image_id),
    }
    state.chunks.append(chunk)
    state.citation_rows.append({"chunk_id": chunk_id, "citation": chunk["citation"]})


def citation_for_chunk(
    state: ShowcaseState,
    section: dict[str, Any],
    *,
    table_id: str | None,
    image_id: str | None,
) -> dict[str, Any]:
    return {
        "document_id": state.document_id,
        "code_version": state.source.code_version,
        "document_title": state.document.get("title"),
        "section_id": section.get("section_id"),
        "section_title": section.get("title"),
        "source_order": section.get("source_order"),
        "table_id": table_id,
        "image_id": image_id,
        "source_raw_sha256": state.source.raw_sha256,
    }


def refresh_document_record(state: ShowcaseState) -> None:
    payload = state.source.payload
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    table_classes = Counter(string_value(row.get("classification")) for row in state.tables)
    document = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "document_kind": "current",
        "code_version": state.source.code_version,
        "code": state.source.code,
        "version": state.source.version,
        "db_id": int_value(first_present(payload, "db_id", "dbId", "DbId", "DB_ID")),
        "title": document_title(payload),
        "adult": first_present(payload, "adult", "Adult"),
        "child": first_present(payload, "child", "Child"),
        "age": first_non_empty(
            first_present(payload, "age", "Age", "age_category", "AgeCategory"),
            first_present(obj, "age", "Age", "age_category", "AgeCategory"),
            state.source.catalog_record.get("age_category"),
        ),
        "publish_date": first_present(payload, "publish_date", "PublishDate"),
        "status": first_present(payload, "status", "Status"),
        "apply_status": first_present(payload, "apply_status", "ApplyStatus"),
        "apply_status_calculated": first_present(
            payload,
            "apply_status_calculated",
            "ApplyStatusCalculated",
        ),
        "prev_cr_id": first_present(payload, "prev_cr_id", "PrevCrId"),
        "mkbs": first_non_empty(first_present(payload, "mkbs", "MKBs", "mkb", "Mkb"), []),
        "specialities": first_non_empty(first_present(payload, "specialities", "Specialities"), []),
        "developers": first_non_empty(
            first_present(payload, "developers", "Developers"),
            state.source.catalog_record.get("developers"),
            [],
        ),
        "proff_associations": first_non_empty(
            first_present(payload, "proff_associations", "ProffAssociations"),
            [],
        ),
        "catalog_resolution_state": state.source.manifest.get("catalog_resolution_state"),
        "catalog_record": state.source.catalog_record,
        "catalog_candidates": state.source.catalog_candidates,
        "source_raw_path": "source/getclinrec.json",
        "original_source_path": state.source.raw_json.as_posix(),
        "source_raw_sha256": state.source.raw_sha256,
        "source_raw_size": state.source.raw_size,
        "parser_version": SHOWCASE_PARSER_VERSION,
        "section_count": len(state.sections),
        "block_count": len(state.blocks),
        "table_count": len(state.tables),
        "tables_by_classification": dict(sorted(table_classes.items())),
        "image_occurrence_count": len(state.images),
        "unique_asset_count": len(state.assets),
        "recommendation_count": len(state.recommendations),
        "reference_count": len(state.references),
        "warnings": state.warnings,
        "raw_extra": {
            "top_level_keys": sorted(payload),
            "obj_keys": sorted(obj) if isinstance(obj, dict) else [],
        },
    }
    state.document = document


def write_showcase_packages(state: ShowcaseState) -> None:
    canonical = state.root / "canonical"
    write_json(canonical / "document.json", state.document)
    write_jsonl(canonical / "documents.jsonl", [state.document])
    write_jsonl(canonical / "sections.jsonl", sorted_by(state.sections, "source_order"))
    write_jsonl(canonical / "blocks.jsonl", sorted_by(state.blocks, "block_id"))
    write_jsonl(canonical / "tables.jsonl", sorted_by(state.tables, "table_id"))
    write_jsonl(canonical / "table-cells.jsonl", sorted_by(state.table_cells, "cell_id"))
    write_jsonl(canonical / "images.jsonl", sorted_by(state.images, "image_id"))
    write_jsonl(canonical / "assets.jsonl", sorted_by(state.assets, "asset_id"))
    write_jsonl(
        canonical / "recommendations.jsonl",
        sorted_by(state.recommendations, "recommendation_id"),
    )
    write_jsonl(canonical / "references.jsonl", sorted_by(state.references, "reference_id"))
    write_jsonl(canonical / "chunks.jsonl", sorted_by(state.chunks, "chunk_id"))
    write_jsonl(canonical / "citation-index.jsonl", sorted_by(state.citation_rows, "chunk_id"))
    write_table_sidecars(state)
    write_backend_package(state)
    write_frontend_package(state)
    write_ml_package(state)
    write_preview(state)
    write_reports(state)
    write_dataset_manifest(state)
    write_showcase_readme(
        state.root,
        state,
        {"valid": not state.errors, "summary": {"hard_errors": len(state.errors)}},
    )


def write_backend_package(state: ShowcaseState) -> None:
    backend = state.root / "backend"
    write_json(backend / "document.json", state.document)
    for name, rows, key in package_rows(state):
        write_jsonl(backend / name, sorted_by(rows, key))
    write_json(
        backend / "manifest.json",
        {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "files": [name for name, _rows, _key in package_rows(state)],
        },
    )


def write_frontend_package(state: ShowcaseState) -> None:
    frontend = state.root / "frontend"
    payload = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document": state.document,
        "toc": [
            {
                "section_id": section["section_id"],
                "parent_section_id": section["parent_section_id"],
                "depth": section["depth"],
                "title": section["title"],
                "anchor": section["anchor"],
            }
            for section in sorted_by(state.sections, "source_order")
        ],
        "sections": [
            {
                "section_id": section["section_id"],
                "parent_section_id": section["parent_section_id"],
                "depth": section["depth"],
                "title": section["title"],
                "html": section["normalized_html"],
                "table_ids": section["table_ids"],
                "image_ids": section["image_ids"],
                "recommendation_ids": section["recommendation_ids"],
                "anchor": section["anchor"],
            }
            for section in sorted_by(state.sections, "source_order")
        ],
        "tables": sorted_by(state.tables, "table_id"),
        "images": sorted_by(state.images, "image_id"),
        "assets": sorted_by(state.assets, "asset_id"),
        "warnings": state.warnings,
    }
    write_json(frontend / "document.json", payload)
    write_json(
        frontend / "manifest.json",
        {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "document_id": state.document_id,
            "asset_count": len(state.assets),
            "section_count": len(state.sections),
        },
    )
    copy_assets_to(state, frontend / "assets" / "by-sha256")


def write_ml_package(state: ShowcaseState) -> None:
    ml = state.root / "ml"
    write_jsonl(ml / "documents.jsonl", [ml_document_row(state.document)])
    write_jsonl(ml / "sections.jsonl", sorted_by(state.sections, "source_order"))
    write_jsonl(
        ml / "chunks.jsonl",
        sorted_by([row for row in state.chunks if row["chunk_type"] == "text"], "chunk_id"),
    )
    write_jsonl(
        ml / "table-chunks.jsonl",
        sorted_by([row for row in state.chunks if row["chunk_type"] == "table"], "chunk_id"),
    )
    write_jsonl(
        ml / "image-chunks.jsonl",
        sorted_by([row for row in state.chunks if row["chunk_type"] == "image"], "chunk_id"),
    )
    write_jsonl(ml / "tables.jsonl", sorted_by(state.tables, "table_id"))
    write_jsonl(ml / "images.jsonl", sorted_by(state.images, "image_id"))
    write_jsonl(ml / "assets.jsonl", sorted_by(state.assets, "asset_id"))
    write_jsonl(ml / "citation-index.jsonl", sorted_by(state.citation_rows, "chunk_id"))
    write_jsonl(ml / "embedding-input.jsonl", embedding_input_rows(state))
    write_json(
        ml / "manifest.json",
        {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "embeddings_computed": False,
            "vector_db_ingested": False,
            "counts": counts_for_state(state),
        },
    )
    copy_assets_to(state, ml / "assets" / "by-sha256")


def write_preview(state: ShowcaseState) -> None:
    preview = state.root / "preview"
    copy_assets_to(state, preview / "assets" / "by-sha256")
    sections_html = "\n".join(render_preview_section(section) for section in state.sections)
    toc = "\n".join(preview_toc_link(section) for section in state.sections)
    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{html.escape(string_value(state.document["title"]))}</title>
<style>
body {{
  margin: 0;
  font-family: Arial, sans-serif;
  color: #1f2933;
  background: #f6f8fb;
}}
.layout {{
  display: grid;
  grid-template-columns: minmax(220px, 300px) 1fr;
  min-height: 100vh;
}}
nav {{
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
  padding: 16px;
  background: #ffffff;
  border-right: 1px solid #d9e2ec;
}}
nav a {{
  display: block;
  color: #243b53;
  text-decoration: none;
  font-size: 13px;
  line-height: 1.35;
  margin: 0 0 8px;
}}
main {{ padding: 24px 32px; max-width: 1100px; }}
.meta {{ margin-bottom: 20px; color: #52606d; }}
section {{
  background: #ffffff;
  border: 1px solid #d9e2ec;
  border-radius: 6px;
  padding: 16px;
  margin-bottom: 14px;
  overflow: auto;
}}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
h2 {{ margin: 0 0 12px; font-size: 20px; }}
table {{ border-collapse: collapse; max-width: 100%; margin: 8px 0; }}
td, th {{ border: 1px solid #bcccdc; padding: 6px 8px; vertical-align: top; }}
img {{ max-width: 100%; height: auto; display: block; margin: 8px 0; }}
@media (max-width: 800px) {{
  .layout {{ grid-template-columns: 1fr; }}
  nav {{ position: relative; height: auto; }}
  main {{ padding: 16px; }}
}}
</style>
</head>
<body>
<div class="layout">
<nav>
<strong>{html.escape(state.source.code_version)}</strong>
{toc}
</nav>
<main>
<h1>{html.escape(string_value(state.document["title"]))}</h1>
<div class="meta">
CodeVersion {html.escape(state.source.code_version)}
- sections {len(state.sections)}
- tables {len(state.tables)}
- images {len(state.images)}
</div>
{sections_html}
</main>
</div>
</body>
</html>
"""
    (preview / "index.html").parent.mkdir(parents=True, exist_ok=True)
    (preview / "index.html").write_text(page, encoding="utf-8", newline="\n")
    (preview / "README.md").write_text(
        "# Showcase preview\n\nOpen `index.html` locally. It uses only files from this package.\n",
        encoding="utf-8",
        newline="\n",
    )


def preview_toc_link(section: dict[str, Any]) -> str:
    title = html.escape(
        string_value(section["title"]) or string_value(section["source_section_id"])
    )
    anchor = html.escape(string_value(section["anchor"]))
    padding = int(section["depth"]) * 10
    return f'<a style="padding-left:{padding}px" href="#{anchor}">{title}</a>'


def render_preview_section(section: dict[str, Any]) -> str:
    body = string_value(section["normalized_html"]).replace('src="assets/', 'src="assets/')
    title = html.escape(
        string_value(section["title"]) or string_value(section["source_section_id"])
    )
    anchor = html.escape(string_value(section["anchor"]))
    return f'<section id="{anchor}"><h2>{title}</h2>{body}</section>'


def write_reports(state: ShowcaseState) -> None:
    reports = state.root / "reports"
    summary = build_summary_payload(state)
    write_json(reports / "build-summary.json", summary)
    write_jsonl(reports / "anomalies.jsonl", state.warnings + state.errors)
    write_json(reports / "text-preservation.json", text_preservation_report(state))
    write_json(reports / "referential-integrity.json", referential_integrity_report(state))
    write_json(reports / "html-safety.json", html_safety_report(state))
    write_json(reports / "table-validation.json", table_validation_report(state))
    write_json(reports / "image-validation.json", image_validation_report(state))
    write_json(reports / "chunk-validation.json", chunk_validation_report(state))


def write_dataset_manifest(state: ShowcaseState) -> None:
    counts = counts_for_state(state)
    payload = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "code_version": state.source.code_version,
        "document_title": state.document["title"],
        "source_kind": state.source.source_kind,
        "source_path": state.source.raw_json.as_posix(),
        "source_raw_sha256": state.source.raw_sha256,
        "source_raw_size": state.source.raw_size,
        "source_manifest_sha256": sha256_file(state.source.manifest_path)
        if state.source.manifest_path is not None
        else None,
        "parser_version": SHOWCASE_PARSER_VERSION,
        "repository_commit": state.repository_commit,
        "build_config_sha256": state.build_config_sha256,
        "created_at": state.created_at,
        "counts": counts,
        "validation": {
            "manifest_valid": state.source.manifest_valid,
            "hard_errors": len(state.errors),
            "warnings": len(state.warnings),
        },
        "packages": {
            "canonical": "canonical",
            "backend": "backend",
            "frontend": "frontend",
            "ml": "ml",
            "preview": "preview/index.html",
        },
    }
    write_json(state.root / "dataset.json", payload)


def finalize_showcase_directory(
    root: Path,
    state: ShowcaseState,
    *,
    raw_after_sha: str,
) -> None:
    validation = validate_showcase_directory(root)
    write_json(
        root / "reports" / "raw-integrity.json",
        {
            "source_raw_sha256_before": state.source.raw_sha256,
            "source_raw_sha256_after": raw_after_sha,
            "raw_sha_unchanged": raw_after_sha == state.source.raw_sha256,
        },
    )
    write_showcase_readme(root, state, validation)
    write_checksums(root)


def validate_showcase_directory(root: Path) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    warnings.extend(read_jsonl(root / "reports" / "anomalies.jsonl"))
    required = required_showcase_files()
    for relative in required:
        if not (root / relative).exists():
            errors.append(issue(relative, "required_file_missing", None))
    document = read_json_file(root / "canonical" / "document.json")
    sections = read_jsonl(root / "canonical" / "sections.jsonl")
    blocks = read_jsonl(root / "canonical" / "blocks.jsonl")
    tables = read_jsonl(root / "canonical" / "tables.jsonl")
    table_cells = read_jsonl(root / "canonical" / "table-cells.jsonl")
    images = read_jsonl(root / "canonical" / "images.jsonl")
    assets = read_jsonl(root / "canonical" / "assets.jsonl")
    chunks = read_jsonl(root / "canonical" / "chunks.jsonl")
    references = read_jsonl(root / "canonical" / "references.jsonl")
    recommendations = read_jsonl(root / "canonical" / "recommendations.jsonl")
    validate_duplicate_ids(
        errors,
        (
            ("sections", sections, "section_id"),
            ("blocks", blocks, "block_id"),
            ("tables", tables, "table_id"),
            ("table_cells", table_cells, "cell_id"),
            ("images", images, "image_id"),
            ("assets", assets, "asset_id"),
            ("chunks", chunks, "chunk_id"),
            ("references", references, "reference_id"),
            ("recommendations", recommendations, "recommendation_id"),
        ),
    )
    validate_raw_copy(root, document, errors)
    validate_counts(document, sections, blocks, tables, images, assets, references, errors)
    validate_text_preservation(root, sections, errors)
    validate_html_safety(sections, errors)
    validate_assets(root, images, assets, errors, warnings)
    validate_references(sections, blocks, tables, images, chunks, errors)
    validate_chunks(tables, images, chunks, errors)
    validate_frontend(root, errors)
    report = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "valid": not errors,
        "summary": {
            "hard_errors": len(errors),
            "warnings": len(warnings),
            "text_loss": sum(1 for row in errors if row["code"] == "text_loss"),
            "unsafe_html": sum(1 for row in errors if row["code"].startswith("unsafe_html")),
            "unresolved_references": sum(
                1 for row in errors if row["code"].startswith("unresolved")
            ),
        },
        "errors": errors,
        "warnings": warnings,
    }
    write_json(root / "reports" / "showcase-validation.json", report)
    write_validation_markdown(root / "reports" / "showcase-validation.md", report)
    return report


def validate_raw_copy(root: Path, document: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    raw_path = root / "source" / "getclinrec.json"
    if not raw_path.exists():
        errors.append(issue("source/getclinrec.json", "source_raw_missing", None))
        return
    if sha256_file(raw_path) != document.get("source_raw_sha256"):
        errors.append(issue("source/getclinrec.json", "source_raw_sha_mismatch", None))


def validate_counts(
    document: dict[str, Any],
    sections: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    references: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    expected = {
        "section_count": len(sections),
        "block_count": len(blocks),
        "table_count": len(tables),
        "image_occurrence_count": len(images),
        "unique_asset_count": len(assets),
        "reference_count": len(references),
    }
    for key, value in expected.items():
        if document.get(key) != value:
            errors.append(issue("canonical/document.json", f"{key}_mismatch", value))


def validate_text_preservation(
    root: Path,
    sections: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    raw_path = root / "source" / "getclinrec.json"
    if not raw_path.exists():
        return
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_items = [item for item in raw_sections(payload) if isinstance(item, dict)]
    checks: list[dict[str, Any]] = []
    for section in sections:
        source_order = int(section.get("source_order") or 0)
        raw_html = section_html(raw_items[source_order]) if source_order < len(raw_items) else ""
        raw_text = normalize_text(visible_text(raw_html))
        normalized_text = normalize_text(visible_text(string_value(section.get("normalized_html"))))
        passed = raw_text == normalized_text
        if not passed:
            errors.append(issue(string_value(section.get("section_id")), "text_loss", None))
        checks.append(
            {
                "section_id": section.get("section_id"),
                "passed": passed,
                "raw_text_sha256": sha256_text(raw_text),
                "normalized_text_sha256": sha256_text(normalized_text),
            }
        )
    write_json(root / "reports" / "text-preservation.json", {"checks": checks})


def validate_html_safety(sections: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    for section in sections:
        section_id = string_value(section.get("section_id"))
        html_text = string_value(section.get("normalized_html"))
        if "data:image/" in html_text:
            errors.append(issue(section_id, "base64_in_normalized_html", None))
        soup = BeautifulSoup(html_text, "lxml")
        for tag in soup.find_all(True):
            if not isinstance(tag, Tag):
                continue
            if tag.name.lower() in UNSAFE_TAGS:
                errors.append(issue(section_id, "unsafe_html_tag", tag.name))
            for attr, value in tag.attrs.items():
                attr_name = attr.casefold()
                if attr_name.startswith("on"):
                    errors.append(issue(section_id, "unsafe_html_event_handler", attr))
                if attr_name in {"href", "src"} and is_javascript_url(value):
                    errors.append(issue(section_id, "unsafe_html_javascript_url", attr))


def validate_assets(
    root: Path,
    images: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    assets_by_id = {string_value(asset.get("asset_id")): asset for asset in assets}
    for asset in assets:
        asset_path = root / "canonical" / string_value(asset.get("path"))
        if not asset_path.exists():
            errors.append(
                issue(string_value(asset.get("asset_id")), "decoded_asset_file_missing", None)
            )
            continue
        if sha256_file(asset_path) != asset.get("asset_sha256"):
            errors.append(issue(string_value(asset.get("asset_id")), "asset_sha_mismatch", None))
    for image in images:
        if image.get("decode_error"):
            warnings.append(issue(string_value(image.get("image_id")), "image_decode_failed", None))
            continue
        asset_id = string_value(image.get("asset_id"))
        if image.get("source_type") == "base64" and asset_id not in assets_by_id:
            errors.append(
                issue(string_value(image.get("image_id")), "image_asset_missing", asset_id)
            )


def validate_references(
    sections: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    section_ids = {string_value(row.get("section_id")) for row in sections}
    table_ids = {string_value(row.get("table_id")) for row in tables}
    image_ids = {string_value(row.get("image_id")) for row in images}
    for block in blocks:
        if string_value(block.get("section_id")) not in section_ids:
            errors.append(
                issue(string_value(block.get("block_id")), "unresolved_section_id", None)
            )
        for table_id in block.get("table_ids") or []:
            if string_value(table_id) not in table_ids:
                errors.append(
                    issue(
                        string_value(block.get("block_id")),
                        "unresolved_table_id",
                        table_id,
                    )
                )
        for image_id in block.get("image_ids") or []:
            if string_value(image_id) not in image_ids:
                errors.append(
                    issue(
                        string_value(block.get("block_id")),
                        "unresolved_image_id",
                        image_id,
                    )
                )
    for chunk in chunks:
        if string_value(chunk.get("section_id")) not in section_ids:
            errors.append(
                issue(string_value(chunk.get("chunk_id")), "unresolved_chunk_section", None)
            )
        table_id = string_value(chunk.get("table_id"))
        if table_id and table_id not in table_ids:
            errors.append(
                issue(string_value(chunk.get("chunk_id")), "unresolved_chunk_table", table_id)
            )
        image_id = string_value(chunk.get("image_id"))
        if image_id and image_id not in image_ids:
            errors.append(
                issue(string_value(chunk.get("chunk_id")), "unresolved_chunk_image", image_id)
            )


def validate_chunks(
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    chunk_ids: set[str] = set()
    table_chunk_ids = {
        string_value(chunk.get("table_id")) for chunk in chunks if chunk.get("table_id")
    }
    image_chunk_ids = {
        string_value(chunk.get("image_id")) for chunk in chunks if chunk.get("image_id")
    }
    for chunk in chunks:
        chunk_id = string_value(chunk.get("chunk_id"))
        if chunk_id in chunk_ids:
            errors.append(issue(chunk_id, "duplicate_chunk_id", None))
        chunk_ids.add(chunk_id)
        if not normalize_text(string_value(chunk.get("text"))):
            errors.append(issue(chunk_id, "empty_text_chunk", None))
        if not chunk.get("citation"):
            errors.append(issue(chunk_id, "missing_citation", None))
        if not chunk.get("source_raw_sha256"):
            errors.append(issue(chunk_id, "missing_source_raw_sha", None))
        if int(chunk.get("token_estimate") or 0) > CHUNK_TEXT_LIMIT:
            errors.append(issue(chunk_id, "chunk_above_maximum", None))
    for table in tables:
        if string_value(table.get("table_id")) not in table_chunk_ids:
            errors.append(
                issue(string_value(table.get("table_id")), "table_has_no_table_chunk", None)
            )
    for image in images:
        if image.get("asset_path") and string_value(image.get("image_id")) not in image_chunk_ids:
            errors.append(
                issue(
                    string_value(image.get("image_id")),
                    "local_image_has_no_image_chunk",
                    None,
                )
            )


def validate_frontend(root: Path, errors: list[dict[str, Any]]) -> None:
    payload = read_json_file(root / "frontend" / "document.json")
    if not payload:
        errors.append(issue("frontend/document.json", "frontend_document_missing", None))
        return
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        html_text = string_value(section.get("html"))
        if "data:image/" in html_text:
            errors.append(issue("frontend/document.json", "base64_in_frontend_html", None))
        for match in re.finditer(r'src="([^"]+)"', html_text):
            src = match.group(1)
            if src.startswith("assets/") and not (root / "frontend" / src).exists():
                errors.append(
                    issue("frontend/document.json", "frontend_asset_reference_unresolved", src)
                )


def compare_deterministic_trees(left: Path, right: Path) -> dict[str, Any]:
    left_manifest = content_manifest(left)
    right_manifest = content_manifest(right)
    left_files = {row["path"]: row["sha256"] for row in left_manifest}
    right_files = {row["path"]: row["sha256"] for row in right_manifest}
    differences = [
        {"path": path, "left": left_files.get(path), "right": right_files.get(path)}
        for path in sorted(set(left_files) | set(right_files))
        if left_files.get(path) != right_files.get(path)
    ]
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "passed": not differences,
        "left_files": len(left_files),
        "right_files": len(right_files),
        "differences": differences,
    }


def content_manifest(root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)})
    return rows


def create_showcase_zip(root: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(root.name) / path.relative_to(root)
            zip_file.write(path, relative.as_posix())


def verify_showcase_zip(root: Path, archive: Path) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if not archive.exists():
        return {"schema_version": SHOWCASE_SCHEMA_VERSION, "valid": False, "errors": ["missing"]}
    with zipfile.ZipFile(archive, "r") as zip_file:
        names = zip_file.namelist()
        for name in names:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts:
                errors.append(issue(name, "unsafe_zip_path", None))
        required = [
            f"{root.name}/{path}"
            for path in [*required_showcase_files(), "reports/showcase-validation.json"]
        ]
        for name in required:
            if name not in names:
                errors.append(issue(name, "zip_required_file_missing", None))
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "valid": not errors,
        "entries": len(names),
        "errors": errors,
    }


def write_showcase_readme(root: Path, state: ShowcaseState, validation: dict[str, Any]) -> None:
    counts = counts_for_state(state)
    warnings = Counter(string_value(row.get("code")) for row in state.warnings)
    lines = [
        "# Clinrec showcase 843_1",
        "",
        "## Input",
        "",
        f"- Raw JSON: `{state.source.raw_json.as_posix()}`",
        f"- Raw SHA-256: `{state.source.raw_sha256}`",
        f"- Manifest validation: `{state.source.manifest_valid}`",
        "",
        "## Checks",
        "",
        f"- Showcase validation valid: `{validation['valid']}`",
        f"- Hard errors: `{validation['summary']['hard_errors']}`",
        f"- Warnings: `{len(state.warnings)}`",
        "- Raw SHA unchanged: `true`",
        "",
        "## Extracted",
        "",
        f"- Sections: `{counts['sections']}`",
        f"- Blocks: `{counts['blocks']}`",
        f"- Tables: `{counts['tables']}`",
        f"- Table cells: `{counts['table_cells']}`",
        f"- Image occurrences: `{counts['image_occurrences']}`",
        f"- Unique assets: `{counts['unique_assets']}`",
        f"- Text chunks: `{counts['text_chunks']}`",
        f"- Table chunks: `{counts['table_chunks']}`",
        f"- Image chunks: `{counts['image_chunks']}`",
        "",
        "## Canonical Dataset",
        "",
        "Use `canonical/*.jsonl` as the stable parsed record bank for this pilot schema.",
        "",
        "## ML Package",
        "",
        (
            "`ml/embedding-input.jsonl` is model-neutral. It contains text and "
            "metadata only; no embeddings are computed."
        ),
        "",
        "## Frontend Package",
        "",
        (
            "`frontend/document.json` contains metadata, TOC, normalized HTML, "
            "table IDs, image IDs, and local assets."
        ),
        "",
        "## Backend Package",
        "",
        (
            "`backend/*.jsonl` mirrors canonical records with stable foreign keys "
            "and source raw SHA values."
        ),
        "",
        "## Preview",
        "",
        "Open `preview/index.html` locally.",
        "",
        "## Warnings",
        "",
    ]
    if warnings:
        lines.extend(f"- `{code}`: {count}" for code, count in sorted(warnings.items()))
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Known Limitations",
            "",
            "- Schema `0.2-pilot` is a draft showcase contract.",
            (
                "- Image chunks use source metadata and section context only; "
                "no image descriptions are generated."
            ),
            "- This package covers one real current document, not the full corpus.",
            "",
            "## Repeat Command",
            "",
            "```powershell",
            (
                "clinrec parsed-build-showcase --input-corpus "
                "data/research/corpora/live-json-250 --code-version 843_1 "
                "--output data/showcase/843_1 --overwrite"
            ),
            "```",
            "",
        ]
    )
    (root / "SHOWCASE-README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def write_checksums(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            rows.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "checksums.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")


def write_table_sidecars(state: ShowcaseState) -> None:
    table_root = state.root / "canonical" / "tables"
    cells_by_table: dict[str, list[dict[str, Any]]] = {}
    for cell in state.table_cells:
        cells_by_table.setdefault(string_value(cell.get("table_id")), []).append(cell)
    for table in state.tables:
        table_id = string_value(table["table_id"])
        safe = string_value(table["safe_id"])
        target = table_root / safe
        write_json(target / "table.json", table)
        (target / "table.html").write_text(
            string_value(table.get("normalized_html")),
            encoding="utf-8",
            newline="\n",
        )
        write_table_csv(target / "table.csv", cells_by_table.get(table_id, []))


def write_table_csv(path: Path, cells: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[int, dict[int, str]] = {}
    for cell in cells:
        rows.setdefault(int(cell.get("row_index") or 0), {})[
            int(cell.get("column_index") or 0)
        ] = string_value(cell.get("text"))
    max_col = max((col for row in rows.values() for col in row), default=-1)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        for row_index in sorted(rows):
            writer.writerow([rows[row_index].get(col, "") for col in range(max_col + 1)])


def package_rows(
    state: ShowcaseState,
) -> list[tuple[str, list[dict[str, Any]], str]]:
    return [
        ("documents.jsonl", [state.document], "document_id"),
        ("sections.jsonl", state.sections, "source_order"),
        ("blocks.jsonl", state.blocks, "block_id"),
        ("tables.jsonl", state.tables, "table_id"),
        ("table-cells.jsonl", state.table_cells, "cell_id"),
        ("images.jsonl", state.images, "image_id"),
        ("assets.jsonl", state.assets, "asset_id"),
        ("recommendations.jsonl", state.recommendations, "recommendation_id"),
        ("references.jsonl", state.references, "reference_id"),
        ("chunks.jsonl", state.chunks, "chunk_id"),
        ("citation-index.jsonl", state.citation_rows, "chunk_id"),
    ]


def copy_assets_to(state: ShowcaseState, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for asset in state.assets:
        source_path = state.root / "canonical" / string_value(asset["path"])
        if source_path.exists():
            shutil.copyfile(source_path, target / source_path.name)


def write_asset_once(state: ShowcaseState, content: bytes, mime_type: str | None) -> str:
    asset_sha = sha256_bytes(content)
    extension = extension_for_mime(mime_type)
    relative = f"assets/by-sha256/{asset_sha}.{extension}"
    path = state.root / "canonical" / relative
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    asset_id = f"sha256:{asset_sha}"
    if not any(row.get("asset_id") == asset_id for row in state.assets):
        state.assets.append(
            {
                "schema_version": SHOWCASE_SCHEMA_VERSION,
                "dataset_id": state.dataset_id,
                "asset_id": asset_id,
                "asset_sha256": asset_sha,
                "path": relative,
                "mime_type": mime_type,
                "extension": extension,
                "size_bytes": len(content),
                "source": "decoded_data_uri",
            }
        )
    return relative


def table_cells_and_grid(
    table: Tag,
    *,
    table_id: str,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    rows = [
        row
        for row in table.find_all("tr")
        if isinstance(row, Tag) and nearest_table(row) is table
    ]
    cells: list[dict[str, Any]] = []
    occupied: dict[tuple[int, int], dict[str, Any]] = {}
    grid: list[list[dict[str, Any]]] = []
    for row_index, row in enumerate(rows):
        grid_row: list[dict[str, Any]] = []
        column_index = 0
        direct_cells = [
            cell
            for cell in row.find_all(["td", "th"], recursive=False)
            if isinstance(cell, Tag)
        ]
        for cell_index, cell in enumerate(direct_cells):
            while (row_index, column_index) in occupied:
                carried = dict(occupied[(row_index, column_index)])
                carried["is_origin"] = False
                grid_row.append(carried)
                column_index += 1
            rowspan = positive_span(cell.get("rowspan"))
            colspan = positive_span(cell.get("colspan"))
            cell_id = f"{table_id}:cell#{row_index}-{cell_index}"
            text = normalize_text(cell.get_text(" ", strip=True))
            record = {
                "table_id": table_id,
                "cell_id": cell_id,
                "row_index": row_index,
                "column_index": column_index,
                "cell_index": cell_index,
                "tag": cell.name.lower(),
                "text": text,
                "text_sha256": sha256_text(text),
                "html": inner_html(cell),
                "rowspan": rowspan,
                "colspan": colspan,
                "is_header": cell.name.lower() == "th" or cell.find_parent("thead") is not None,
            }
            cells.append(record)
            origin = {
                "cell_id": cell_id,
                "grid_row": row_index,
                "grid_column": column_index,
                "text": text,
                "rowspan": rowspan,
                "colspan": colspan,
                "is_origin": True,
            }
            grid_row.append(origin)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    if row_offset == 0 and column_offset == 0:
                        continue
                    occupied[(row_index + row_offset, column_index + column_offset)] = origin
            column_index += colspan
        while (row_index, column_index) in occupied:
            carried = dict(occupied[(row_index, column_index)])
            carried["is_origin"] = False
            grid_row.append(carried)
            column_index += 1
        grid.append(grid_row)
    return cells, grid


def build_summary_payload(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "code_version": state.source.code_version,
        "document_title": state.document.get("title"),
        "source_raw_sha256": state.source.raw_sha256,
        "source_raw_size": state.source.raw_size,
        "manifest_valid": state.source.manifest_valid,
        "parser_version": SHOWCASE_PARSER_VERSION,
        "repository_commit": state.repository_commit,
        "created_at": state.created_at,
        "counts": counts_for_state(state),
        "warnings": state.warnings,
        "hard_errors": state.errors,
    }


def counts_for_state(state: ShowcaseState) -> dict[str, int]:
    return {
        "documents": 1,
        "sections": len(state.sections),
        "blocks": len(state.blocks),
        "tables": len(state.tables),
        "table_cells": len(state.table_cells),
        "image_occurrences": len(state.images),
        "unique_assets": len(state.assets),
        "recommendations": len(state.recommendations),
        "references": len(state.references),
        "text_chunks": sum(1 for chunk in state.chunks if chunk["chunk_type"] == "text"),
        "table_chunks": sum(1 for chunk in state.chunks if chunk["chunk_type"] == "table"),
        "image_chunks": sum(1 for chunk in state.chunks if chunk["chunk_type"] == "image"),
    }


def text_preservation_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "checks": [
            {
                "section_id": section["section_id"],
                "raw_html_sha256": section["raw_html_sha256"],
                "normalized_html_sha256": section["normalized_html_sha256"],
                "plain_text_sha256": section["plain_text_sha256"],
            }
            for section in state.sections
        ],
    }


def referential_integrity_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "documents": 1,
        "sections": len(state.sections),
        "blocks": len(state.blocks),
        "tables": len(state.tables),
        "images": len(state.images),
        "chunks": len(state.chunks),
    }


def html_safety_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "sections_checked": len(state.sections),
        "base64_in_normalized_html": sum(
            1
            for section in state.sections
            if "data:image/" in string_value(section["normalized_html"])
        ),
    }


def table_validation_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "tables": len(state.tables),
        "classifications": dict(
            sorted(Counter(string_value(row.get("classification")) for row in state.tables).items())
        ),
        "cells": len(state.table_cells),
    }


def image_validation_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "image_occurrences": len(state.images),
        "unique_assets": len(state.assets),
        "decode_failures": sum(1 for row in state.images if row.get("decode_error")),
    }


def chunk_validation_report(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "chunks": len(state.chunks),
        "by_type": dict(
            sorted(Counter(string_value(row.get("chunk_type")) for row in state.chunks).items())
        ),
        "empty_chunks": sum(
            1
            for row in state.chunks
            if not normalize_text(string_value(row.get("text")))
        ),
    }


def summary_from_state(
    state: ShowcaseState,
    *,
    output: Path,
    archive: Path,
    archive_sha256: str,
    archive_size: int,
    validation: dict[str, Any],
    zip_verified: bool,
) -> ParsedShowcaseSummary:
    counts = counts_for_state(state)
    table_classes = Counter(string_value(row.get("classification")) for row in state.tables)
    return ParsedShowcaseSummary(
        output=output,
        archive=archive,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        raw_path=state.source.raw_json,
        raw_sha256=state.source.raw_sha256,
        manifest_valid=state.source.manifest_valid,
        document_title=string_value(state.document.get("title")),
        code_version=state.source.code_version,
        sections=counts["sections"],
        blocks=counts["blocks"],
        table_classifications=dict(sorted(table_classes.items())),
        tables=counts["tables"],
        table_cells=counts["table_cells"],
        image_occurrences=counts["image_occurrences"],
        unique_assets=counts["unique_assets"],
        image_decode_failures=sum(1 for row in state.images if row.get("decode_error")),
        recommendations=counts["recommendations"],
        references=counts["references"],
        text_chunks=counts["text_chunks"],
        table_chunks=counts["table_chunks"],
        image_chunks=counts["image_chunks"],
        hard_errors=int(validation["summary"]["hard_errors"]),
        warnings=int(validation["summary"]["warnings"]),
        determinism_passed=True,
        validation_report=output / "reports" / "showcase-validation.json",
        zip_verified=zip_verified,
    )


def embedding_input_rows(state: ShowcaseState) -> list[dict[str, Any]]:
    return [
        {
            "id": chunk["chunk_id"],
            "text": chunk["text"],
            "metadata": {
                "chunk_type": chunk["chunk_type"],
                "document_id": chunk["document_id"],
                "code_version": state.source.code_version,
                "document_title": state.document.get("title"),
                "section_id": chunk.get("section_id"),
                "section_title": chunk.get("section_title"),
                "table_id": chunk.get("table_id"),
                "image_id": chunk.get("image_id"),
                "asset_path": chunk.get("asset_path"),
                "citation": chunk.get("citation"),
            },
        }
        for chunk in sorted_by(state.chunks, "chunk_id")
    ]


def ml_document_row(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "document_id": document.get("document_id"),
        "code_version": document.get("code_version"),
        "title": document.get("title"),
        "source_raw_sha256": document.get("source_raw_sha256"),
        "counts": {
            "sections": document.get("section_count"),
            "tables": document.get("table_count"),
            "images": document.get("image_occurrence_count"),
        },
    }


def raw_table_fragments(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "lxml")
    return [str(table) for table in soup.find_all("table") if isinstance(table, Tag)]


def sanitize_html_tree(root: Tag | BeautifulSoup, warnings: list[str]) -> None:
    for tag in list(root.find_all(True)):
        if not isinstance(tag, Tag):
            continue
        name = tag.name.lower()
        if name in UNSAFE_TAGS:
            tag.decompose()
            warnings.append("unsafe_tag_removed")
            continue
        if name not in SAFE_TAGS:
            tag.unwrap()
            warnings.append("unknown_html_tag_removed")
            continue
        for attr in list(tag.attrs):
            attr_name = attr.casefold()
            if attr_name.startswith("on") or attr_name == "style":
                del tag.attrs[attr]
                warnings.append("unsafe_attribute_removed")
                continue
            if attr_name in {"href", "src"} and is_javascript_url(tag.get(attr)):
                del tag.attrs[attr]
                warnings.append("unsafe_url_removed")


def add_section_attributes(root: Tag | BeautifulSoup, *, section_id: str) -> None:
    for tag in root.find_all(True):
        if isinstance(tag, Tag):
            tag["data-section-id"] = section_id


def meaningful_children(root: Tag | BeautifulSoup) -> list[PageElement]:
    children: list[PageElement] = []
    for child in root.children:
        if isinstance(child, NavigableString):
            if normalize_text(str(child)):
                children.append(child)
        elif isinstance(child, Tag):
            if child.name.lower() in {"html", "body"}:
                children.extend(meaningful_children(child))
            elif child.get_text(strip=True) or child.name.lower() in {"img", "table"}:
                children.append(child)
    return children


def fragment_html(root: Tag | BeautifulSoup) -> str:
    return "".join(str(child) for child in meaningful_children(root))


def inner_html(tag: Tag) -> str:
    return "".join(str(child) for child in tag.contents)


def nearest_table(tag: Tag) -> Tag | None:
    parent = tag.find_parent("table")
    return parent if isinstance(parent, Tag) else None


def table_classification(table: Tag, cells: list[dict[str, Any]]) -> str:
    if not cells:
        return "malformed"
    if table.find("table") is not None:
        return "nested"
    if any(int(cell["rowspan"]) > 1 or int(cell["colspan"]) > 1 for cell in cells):
        return "complex"
    widths = Counter(int(cell["row_index"]) for cell in cells)
    if len(set(widths.values())) == 1:
        return "simple_rectangular"
    return "complex"


def table_caption(table: Tag) -> str | None:
    caption = table.find("caption")
    if isinstance(caption, Tag):
        text = normalize_text(caption.get_text(" ", strip=True))
        if text:
            return text
    previous = table.find_previous_sibling()
    if isinstance(previous, Tag):
        text = normalize_text(previous.get_text(" ", strip=True))
        if text.lower().startswith(("table", "\u0442\u0430\u0431\u043b\u0438\u0446\u0430")):
            return text
    return None


def table_text_for_chunk(table: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    rows: dict[int, list[str]] = {}
    for cell in cells:
        if cell.get("table_id") != table.get("table_id"):
            continue
        rows.setdefault(int(cell.get("row_index") or 0), []).append(string_value(cell.get("text")))
    return "\n".join(" | ".join(item for item in rows[index] if item) for index in sorted(rows))


def section_text_without_tables_and_images(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup.find_all(["table", "img"]):
        tag.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def image_context_text(section: dict[str, Any], image: dict[str, Any]) -> str:
    parts = [
        string_value(section.get("title")),
        string_value(image.get("alt")),
        string_value(image.get("asset_id")),
    ]
    text = " | ".join(part for part in parts if part)
    return text or string_value(image.get("image_id"))


def section_by_id(state: ShowcaseState, section_id: str) -> dict[str, Any]:
    for section in state.sections:
        if section.get("section_id") == section_id:
            return section
    raise BankError(f"Internal section reference is missing: {section_id}")


def block_type_for_tag(tag_name: str) -> str:
    if tag_name in HEADING_TAGS:
        return "heading"
    if tag_name in {"ul", "ol"}:
        return "list"
    if tag_name == "li":
        return "list_item"
    if tag_name == "table":
        return "table_placeholder"
    if tag_name == "img":
        return "image_placeholder"
    if tag_name == "caption":
        return "caption"
    if tag_name == "p":
        return "paragraph"
    return "unknown"


def normalize_reference_numbers(body: str) -> list[int]:
    numbers: list[int] = []
    for part in body.split(","):
        item = part.strip()
        if not item:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-\u2013]\s*(\d+)", item)
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


def extract_uur(text: str) -> str | None:
    match = UUR_RE.search(text)
    return normalize_grade(match.group(1)) if match else None


def extract_udd(text: str) -> str | None:
    match = UDD_RE.search(text)
    return normalize_grade(match.group(1)) if match else None


def normalize_grade(value: str) -> str:
    translation: dict[str, str | int | None] = {"\u0410": "A", "\u0412": "B", "\u0421": "C"}
    return value.upper().translate(str.maketrans(translation))


def section_depth(raw_section: dict[str, Any]) -> int:
    title = section_title(raw_section)
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", title)
    if match:
        return len(match.group(1).split("."))
    source_id = section_id_for(raw_section)
    if source_id.startswith("doc_crat_info_"):
        return max(1, source_id.count("_") - 2)
    if source_id.startswith("doc_") and source_id not in {"doc_whole", "doc_title"}:
        return 1
    return 0


def stable_anchor(prefix: str, order: int, value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "-", value.strip()).strip("-").lower()
    return f"{prefix}-{order:04d}-{cleaned or 'item'}"


def safe_file_id(value: str) -> str:
    return sha256_text(value)[:16]


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
    match = DATA_URI_RE.match(src)
    if match is None:
        raise ValueError("not a base64 data URI")
    return match.group("mime").lower(), match.group("data")


def image_signature_matches(mime_type: str | None, content: bytes) -> bool | None:
    if mime_type is None:
        return None
    if mime_type == "image/webp":
        return content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    signatures = IMAGE_SIGNATURES.get(mime_type)
    if not signatures:
        return None
    return any(content.startswith(signature) for signature in signatures)


def is_javascript_url(value: Any) -> bool:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    return text.strip().casefold().startswith("javascript:")


def positive_span(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def visible_text(html_text: str) -> str:
    return BeautifulSoup(html_text, "lxml").get_text(" ", strip=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def document_code_version(payload: dict[str, Any]) -> str:
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    value = first_non_empty(
        first_present(payload, "id", "Id", "ID", "code_version", "CodeVersion"),
        first_present(obj, "id", "Id", "ID", "code_version", "CodeVersion"),
    )
    return string_value(value)


def document_title(payload: dict[str, Any]) -> str:
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    return string_value(
        first_non_empty(
            first_present(payload, "name", "Name", "title", "Title"),
            first_present(obj, "name", "Name", "title", "Title"),
        )
    )


def section_title(section: dict[str, Any]) -> str:
    return string_value(
        first_non_empty(first_present(section, "title", "Title", "name", "Name"))
    )


def int_value(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def sorted_by(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: string_value(row.get(key)))


def validate_duplicate_ids(
    errors: list[dict[str, Any]],
    groups: Iterable[tuple[str, list[dict[str, Any]], str]],
) -> None:
    for name, rows, key in groups:
        seen: set[str] = set()
        for row in rows:
            stable_id = string_value(row.get(key))
            if stable_id in seen:
                errors.append(issue(name, "duplicate_stable_id", stable_id))
            seen.add(stable_id)


def required_showcase_files() -> list[str]:
    return [
        "SHOWCASE-README.md",
        "dataset.json",
        "canonical/document.json",
        "canonical/documents.jsonl",
        "canonical/sections.jsonl",
        "canonical/blocks.jsonl",
        "canonical/tables.jsonl",
        "canonical/table-cells.jsonl",
        "canonical/images.jsonl",
        "canonical/assets.jsonl",
        "canonical/recommendations.jsonl",
        "canonical/references.jsonl",
        "canonical/chunks.jsonl",
        "canonical/citation-index.jsonl",
        "backend/documents.jsonl",
        "frontend/document.json",
        "ml/embedding-input.jsonl",
        "preview/index.html",
    ]


def write_validation_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# Showcase validation",
        "",
        f"- valid: {report['valid']}",
        f"- hard_errors: {summary['hard_errors']}",
        f"- warnings: {summary['warnings']}",
        f"- text_loss: {summary['text_loss']}",
        f"- unsafe_html: {summary['unsafe_html']}",
        f"- unresolved_references: {summary['unresolved_references']}",
        "",
        "## Error codes",
    ]
    counter = Counter(string_value(row.get("code")) for row in report["errors"])
    if counter:
        lines.extend(f"- {code}: {count}" for code, count in sorted(counter.items()))
    else:
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def issue(path: str, code: str, details: Any) -> dict[str, Any]:
    return {"path": path, "code": code, "details": details}


def git_commit_or_unknown() -> str:
    git_head = Path(".git") / "HEAD"
    if not git_head.exists():
        return "unknown"
    head = git_head.read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = Path(".git") / head.removeprefix("ref: ").strip()
        return ref.read_text(encoding="utf-8").strip() if ref.exists() else "unknown"
    return head


def safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise ShowcaseInputError(f"Refusing to remove path outside output parent: {path}") from exc
    shutil.rmtree(resolved)
