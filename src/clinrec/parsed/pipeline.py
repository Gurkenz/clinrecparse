from __future__ import annotations

import base64
import binascii
import csv
import html
import json
import re
import shutil
import tempfile
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
from clinrec.parsed.html import (
    GENERATED_SAFE_ATTRS,
    SAFE_ATTRS_BY_TAG,
    SAFE_TAGS,
    UNSAFE_TAGS,
    add_section_attributes,
    fragment_html,
    inner_html,
    is_safe_url,
    meaningful_children,
    nearest_table,
    normalize_text,
    positive_span,
    sanitize_html_tree,
    table_cell_text,
    visible_text,
)
from clinrec.parsed.models import (
    CANONICAL_PARSER_VERSION,
    CANONICAL_SCHEMA_VERSION,
    estimate_tokens,
    extension_for_mime,
    safe_id,
    sha256_text,
)
from clinrec.parsed.source_inventory import (
    RawSectionRecord,
    build_raw_source_inventory,
)
from clinrec.research.reports import write_json, write_jsonl
from clinrec.research.sections import section_html, section_id_for

SHOWCASE_SCHEMA_VERSION = CANONICAL_SCHEMA_VERSION
SHOWCASE_PARSER_VERSION = CANONICAL_PARSER_VERSION
DEFAULT_SHOWCASE_CODE_VERSION = "843_1"
CHUNK_TARGET_TOKENS = 700
CHUNK_MAXIMUM_TOKENS = 1100
TOKEN_CHAR_BUDGET = CHUNK_MAXIMUM_TOKENS * 4
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
COMMENT_RE = re.compile(r"^\s*(Комментар(?:ий|ии)|Примечание)\b", re.IGNORECASE)
UUR_PATTERNS = [
    re.compile(r"\bУУР\s*[:\-\u2013\u2014]?\s*([ABCАВС])\b", re.IGNORECASE),
    re.compile(
        r"Уровень\s+убедительности\s+рекомендац\w*\s*[:\-\u2013\u2014]?\s*([ABCАВС])\b",
        re.IGNORECASE,
    ),
]
UDD_PATTERNS = [
    re.compile(r"\bУДД\s*[:\-\u2013\u2014]?\s*([1-5][ABCАВС]?)\b", re.IGNORECASE),
    re.compile(
        r"уровень\s+достоверности\s+доказательств\s*[:\-\u2013\u2014]?\s*([1-5][ABCАВС]?)\b",
        re.IGNORECASE,
    ),
]
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
    document_kind: str
    current_code_version: str | None
    source_raw_path: str
    dataset_id: str
    created_at: str
    repository_commit: str
    build_config_sha256: str
    document: dict[str, Any]
    sections: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    table_cells: list[dict[str, Any]]
    table_placements: list[dict[str, Any]]
    images: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    references: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    citation_rows: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]


type RawDocumentSource = ShowcaseInput


@dataclass(frozen=True)
class ParseConfig:
    root: Path
    dataset_id: str
    created_at: str
    repository_commit: str
    build_config_sha256: str
    document_kind: str = "current"
    current_code_version: str | None = None
    source_raw_path: str = "source/getclinrec.json"


@dataclass(frozen=True)
class ParsedDocumentBundle:
    state: ShowcaseState
    document: dict[str, Any]
    sections: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    table_cells: list[dict[str, Any]]
    table_placements: list[dict[str, Any]]
    images: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    references: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    citation_index: list[dict[str, Any]]
    coverage_map: dict[str, Any]
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
    validation = validate_showcase_directory(options.output)
    if not validation["valid"]:
        raise ShowcaseValidationError(
            "showcase validation failed after finalization",
            options.output / "reports" / "showcase-validation.json",
        )
    write_checksums(options.output)
    archive = options.output.parent / f"clinrec-showcase-{source.code_version}.zip"
    if archive.exists():
        archive.unlink()
    create_showcase_zip(options.output, archive)
    zip_report = verify_showcase_zip(options.output, archive)
    archive_sha = sha256_file(archive)
    archive_size = archive.stat().st_size
    archive_report = archive.with_suffix(".archive-verification.json")
    write_json(
        archive_report,
        {
            **zip_report,
            "archive_path": archive.as_posix(),
            "archive_sha256": archive_sha,
            "archive_size": archive_size,
        },
    )
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{archive_sha}  {archive.name}\n",
        encoding="utf-8",
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
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    payload_code = int_value(first_present(payload, "code", "Code"))
    if payload_code is None:
        payload_code = int_value(first_present(obj, "code", "Code"))
    payload_version = int_value(first_present(payload, "version", "Version", "ver", "Ver"))
    if payload_version is None:
        payload_version = int_value(first_present(obj, "version", "Version", "ver", "Ver"))
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
    bundle = parse_document(
        source,
        ParseConfig(
            root=root,
            dataset_id=f"showcase:{source.code_version}",
            created_at=created_at,
            repository_commit=repository_commit,
            build_config_sha256=build_config_sha256,
        ),
    )
    bundle_validation = validate_parsed_bundle(bundle)
    write_json(root / "reports" / "bundle-validation.json", bundle_validation)
    if not bundle_validation["valid"]:
        raise ShowcaseValidationError(
            "canonical bundle validation failed",
            root / "reports" / "bundle-validation.json",
        )
    write_showcase_packages(bundle.state)
    return bundle.state


def parse_document(
    source: RawDocumentSource,
    config: ParseConfig,
) -> ParsedDocumentBundle:
    document_id = document_id_for_parse(source, config)
    state = ShowcaseState(
        root=config.root,
        source=source,
        document_id=document_id,
        document_kind=config.document_kind,
        current_code_version=config.current_code_version,
        source_raw_path=config.source_raw_path,
        dataset_id=config.dataset_id,
        created_at=config.created_at,
        repository_commit=config.repository_commit,
        build_config_sha256=config.build_config_sha256,
        document={},
        sections=[],
        blocks=[],
        tables=[],
        table_cells=[],
        table_placements=[],
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
    return ParsedDocumentBundle(
        state=state,
        document=state.document,
        sections=state.sections,
        blocks=state.blocks,
        tables=state.tables,
        table_cells=state.table_cells,
        table_placements=state.table_placements,
        images=state.images,
        assets=state.assets,
        recommendations=state.recommendations,
        references=state.references,
        chunks=state.chunks,
        citation_index=state.citation_rows,
        coverage_map=coverage_map_for_state(state),
        warnings=state.warnings,
        errors=state.errors,
    )


def validate_parsed_bundle(bundle: ParsedDocumentBundle) -> dict[str, Any]:
    errors: list[dict[str, Any]] = list(bundle.errors)
    warnings: list[dict[str, Any]] = list(bundle.warnings)
    inventory = build_raw_source_inventory(bundle.state.source.payload)
    errors.extend(inventory.errors)
    raw_sections_count = len(inventory.sections)
    raw_paths = {record.raw_path for record in inventory.sections}
    parsed_paths = {string_value(section.get("raw_path")) for section in bundle.sections}
    for missing_path in sorted(raw_paths - parsed_paths):
        errors.append(issue(missing_path, "missing_raw_section", None))
    for extra_path in sorted(parsed_paths - raw_paths):
        errors.append(issue(extra_path, "parsed_section_without_raw_path", None))
    parsed_counts = {
        "sections": len(bundle.sections),
        "tables": len(bundle.tables),
        "images": len(bundle.images),
        "blocks": len(bundle.blocks),
        "table_cells": len(bundle.table_cells),
        "logical_table_placements": len(bundle.table_placements),
        "assets": len(bundle.assets),
        "recommendations": len(bundle.recommendations),
        "references": len(bundle.references),
        "chunks": len(bundle.chunks),
    }
    source_counts = {
        "sections": raw_sections_count,
        "text_units": len(inventory.text_units),
        "tables": len(inventory.tables),
        "table_cells": len(inventory.table_cells),
        "logical_table_placements": len(inventory.table_placements),
        "images": len(inventory.images),
    }
    for key in ("sections", "tables", "table_cells", "logical_table_placements", "images"):
        if source_counts[key] != parsed_counts[key]:
            errors.append(
                issue(
                    bundle.document.get("document_id", "document"),
                    f"raw_{key}_count_mismatch",
                    {"raw": source_counts[key], "parsed": parsed_counts[key]},
                )
            )
    raw_cell_hashes = [record.text_sha256 for record in inventory.table_cells]
    parsed_cell_hashes = [string_value(row.get("text_sha256")) for row in bundle.table_cells]
    if raw_cell_hashes != parsed_cell_hashes:
        errors.append(
            issue(
                bundle.document.get("document_id", "document"),
                "raw_table_cell_text_mismatch",
                {
                    "raw_cells": len(raw_cell_hashes),
                    "parsed_cells": len(parsed_cell_hashes),
                },
            )
        )
    raw_placement_hashes = [record.text_sha256 for record in inventory.table_placements]
    parsed_placement_hashes = [
        string_value(row.get("text_sha256")) for row in bundle.table_placements
    ]
    if raw_placement_hashes != parsed_placement_hashes:
        errors.append(
            issue(
                bundle.document.get("document_id", "document"),
                "raw_table_placement_text_mismatch",
                {
                    "raw_placements": len(raw_placement_hashes),
                    "parsed_placements": len(parsed_placement_hashes),
                },
            )
        )
    validate_duplicate_ids(
        errors,
        (
            ("sections", bundle.sections, "section_id"),
            ("blocks", bundle.blocks, "block_id"),
            ("tables", bundle.tables, "table_id"),
            ("table_cells", bundle.table_cells, "cell_id"),
            ("table_placements", bundle.table_placements, "placement_id"),
            ("images", bundle.images, "image_id"),
            ("assets", bundle.assets, "asset_id"),
            ("chunks", bundle.chunks, "chunk_id"),
            ("references", bundle.references, "reference_id"),
            ("recommendations", bundle.recommendations, "recommendation_id"),
        ),
    )
    validate_counts(
        bundle.document,
        bundle.sections,
        bundle.blocks,
        bundle.tables,
        bundle.images,
        bundle.assets,
        bundle.references,
        errors,
    )
    validate_assets(bundle.state.root, bundle.images, bundle.assets, errors, warnings)
    validate_references(
        bundle.sections,
        bundle.blocks,
        bundle.tables,
        bundle.images,
        bundle.chunks,
        errors,
    )
    validate_table_placements(
        bundle.tables,
        bundle.table_cells,
        bundle.table_placements,
        bundle.chunks,
        errors,
    )
    validate_chunks(bundle.tables, bundle.images, bundle.chunks, errors)
    validate_recommendation_chunks(bundle.blocks, bundle.recommendations, bundle.chunks, errors)
    validate_citation_titles(bundle.chunks, errors)
    coverage = coverage_map_for_state(bundle.state)
    for report in (coverage["text"], coverage["tables"], coverage["images"]):
        if report.get("coverage_percent") != 100.0:
            errors.append(issue("bundle", "coverage_incomplete", report))
    maximum_estimated_tokens = max(
        (
            int(chunk.get("estimated_token_count") or chunk.get("token_estimate") or 0)
            for chunk in bundle.chunks
        ),
        default=0,
    )
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "parser_version": SHOWCASE_PARSER_VERSION,
        "document_id": bundle.document.get("document_id"),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_counts": source_counts,
        "parsed_counts": parsed_counts,
        "coverage": coverage,
        "maximum_estimated_tokens": maximum_estimated_tokens,
    }


def document_id_for_parse(source: RawDocumentSource, config: ParseConfig) -> str:
    if config.document_kind == "previous":
        current = config.current_code_version or source.code_version
        return f"previous:{current}:{source.code_version}"
    return f"current:{source.code_version}"


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


def raw_section_id(record: RawSectionRecord) -> str:
    source_section_id = section_id_for(record.section)
    if source_section_id:
        return source_section_id
    return f"raw-path-{sha256_text(record.raw_path)[:12]}"


def collision_safe_section_components(records: list[RawSectionRecord]) -> dict[str, str]:
    by_slug: dict[str, set[str]] = {}
    for record in records:
        source_section_id = raw_section_id(record)
        by_slug.setdefault(safe_id(source_section_id), set()).add(source_section_id)
    components: dict[str, str] = {}
    for record in records:
        source_section_id = raw_section_id(record)
        slug = safe_id(source_section_id)
        if len(by_slug.get(slug, set())) > 1 or source_section_id.startswith("raw-path-"):
            slug = f"{slug}-{sha256_text(record.raw_path)[:8]}"
        components[record.raw_path] = slug
    return components


def parse_showcase_document(state: ShowcaseState) -> None:
    payload = state.source.payload
    inventory = build_raw_source_inventory(payload)
    state.errors.extend(inventory.errors)
    sections = inventory.sections
    section_components = collision_safe_section_components(sections)
    occurrence_counts: Counter[str] = Counter()
    parsed_by_raw_path: dict[str, str] = {}
    for record in sections:
        raw_section = record.section
        source_section_id = raw_section_id(record)
        section_component = section_components[record.raw_path]
        occurrence_index = occurrence_counts[section_component]
        occurrence_counts[section_component] += 1
        depth = record.depth or section_depth(raw_section)
        parent_section_id = (
            parsed_by_raw_path.get(record.parent_raw_path)
            if record.parent_raw_path is not None
            else None
        )
        section = parse_showcase_section(
            state,
            raw_section,
            source_order=record.source_order,
            source_section_id=source_section_id,
            section_component=section_component,
            occurrence_index=occurrence_index,
            parent_section_id=parent_section_id,
            depth=depth,
            raw_path=record.raw_path,
            parent_raw_path=record.parent_raw_path,
        )
        state.sections.append(section)
        parsed_by_raw_path[record.raw_path] = string_value(section["section_id"])

    refresh_document_record(state)
    extract_recommendations(state)
    refresh_document_record(state)
    populate_image_contexts(state)
    build_chunks(state)
    refresh_document_record(state)


def parse_showcase_section(
    state: ShowcaseState,
    raw_section: dict[str, Any],
    *,
    source_order: int,
    source_section_id: str,
    section_component: str,
    occurrence_index: int,
    parent_section_id: str | None,
    depth: int,
    raw_path: str,
    parent_raw_path: str | None,
) -> dict[str, Any]:
    section_key = f"{section_component}#{occurrence_index}"
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
        "raw_path": raw_path,
        "parent_raw_path": parent_raw_path,
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
        "source_raw_path": state.source_raw_path,
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
        declared_mime_type: str | None = None
        detected_mime_type: str | None = None
        asset_sha: str | None = None
        asset_id: str | None = None
        asset_path: str | None = None
        decoded_size: int | None = None
        decode_error: str | None = None
        resolution_status = "not_resolved"
        signature_matches: bool | None = None
        width: int | None = None
        height: int | None = None
        content: bytes | None = None
        if source_type == "base64":
            declared_mime_type, token = split_data_uri(src)
            try:
                content = base64.b64decode(re.sub(r"\s+", "", token), validate=True)
                decoded_size = len(content)
            except (binascii.Error, ValueError) as exc:
                decode_error = str(exc)
                image["src"] = ""
                image["data-image-status"] = "decode_failed"
        elif source_type == "relative":
            resolved = resolve_relative_image(state.source, src)
            if resolved is None:
                decode_error = "relative image could not be resolved inside allowed source roots"
                image["src"] = ""
                image["data-image-status"] = "unresolved"
            else:
                content = resolved.read_bytes()
                decoded_size = len(content)
                resolution_status = "resolved"
        elif source_type in {"http", "https"}:
            decode_error = "external image references are not downloaded for release"
            image["src"] = ""
            image["data-image-status"] = "external_unresolved"
        if content is not None:
            detected_mime_type = detect_image_mime(content)
            signature_matches = image_signature_matches(declared_mime_type, content)
            if detected_mime_type != declared_mime_type and declared_mime_type is not None:
                state.warnings.append(
                    {
                        "path": image_id,
                        "code": "image_mime_declaration_mismatch",
                        "details": {
                            "declared": declared_mime_type,
                            "detected": detected_mime_type,
                        },
                    }
                )
            if detected_mime_type == "image/svg+xml":
                decode_error = "unsupported unsanitized SVG image"
                image["src"] = ""
                image["data-image-status"] = "unsupported_svg"
            elif not is_supported_raster_mime(detected_mime_type):
                decode_error = "unsupported or undetected image format"
                image["src"] = ""
                image["data-image-status"] = "unsupported_image"
            else:
                if detected_mime_type is None:
                    raise ValueError("detected raster MIME is missing")
                width, height = image_dimensions(content, detected_mime_type)
                asset_sha = sha256_bytes(content)
                asset_id = f"sha256:{asset_sha}"
                asset_path = write_asset_once(
                    state,
                    content,
                    declared_mime_type=declared_mime_type,
                    detected_mime_type=detected_mime_type,
                    width=width,
                    height=height,
                    occurrence_id=image_id,
                )
                resolution_status = "resolved"
                image["src"] = asset_path
                image["data-asset-id"] = asset_id
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
            "declared_mime_type": declared_mime_type,
            "detected_mime_type": detected_mime_type,
            "mime_type": detected_mime_type or declared_mime_type,
            "extension": extension_for_mime(detected_mime_type),
            "size_bytes": decoded_size,
            "decoded_size_bytes": decoded_size,
            "signature_matches_declared_mime": signature_matches,
            "decode_status": (
                "failed" if decode_error else ("decoded" if asset_id else "not_decoded")
            ),
            "resolution_status": resolution_status,
            "decode_error": decode_error,
            "original_src": src,
            "alt": string_value(image.get("alt")),
            "title": string_value(image.get("title")),
            "width": width,
            "height": height,
            "caption": None,
            "preceding_block_id": None,
            "following_block_id": None,
            "preceding_text": "",
            "following_text": "",
            "section_title": None,
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
        cell_rows, logical_grid, placement_rows = table_cells_and_grid(table, table_id=table_id)
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
        for placement in placement_rows:
            placement.update(
                {
                    "schema_version": SHOWCASE_SCHEMA_VERSION,
                    "dataset_id": state.dataset_id,
                    "document_id": state.document_id,
                    "section_id": section_id,
                }
            )
            state.table_placements.append(placement)
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
        classification = table_classification(table, cell_rows)
        table_record = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "section_id": section_id,
            "section_key": section_key,
            "table_id": table_id,
            "table_index": table_index,
            "classification": classification,
            "csv_available": classification == "simple_rectangular",
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
            "placement_ids": [
                string_value(placement["placement_id"]) for placement in placement_rows
            ],
            "logical_grid": logical_grid,
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
    for block_index, child in enumerate(logical_block_children(root)):
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


def logical_block_children(root: Tag | BeautifulSoup) -> list[PageElement]:
    children: list[PageElement] = []
    for child in meaningful_children(root):
        children.extend(logical_block_units(child))
    return children


def logical_block_units(element: PageElement) -> list[PageElement]:
    if isinstance(element, NavigableString):
        return [element] if normalize_text(str(element)) else []
    if not isinstance(element, Tag):
        return []
    tag_name = element.name.lower()
    if tag_name in {"html", "body"}:
        return logical_block_children(element)
    if tag_name in {"ul", "ol"}:
        return [
            child
            for child in element.find_all("li", recursive=False)
            if isinstance(child, Tag) and normalize_text(child.get_text(" ", strip=True))
        ]
    if tag_name in {"table", "img"}:
        return [element]
    has_embedded_object = element.find(["table", "img"]) is not None
    if has_embedded_object:
        flattened: list[PageElement] = []
        for child in element.children:
            if isinstance(child, NavigableString):
                if normalize_text(str(child)):
                    flattened.append(child)
            elif isinstance(child, Tag):
                flattened.extend(logical_block_units(child))
        return flattened
    return [element] if normalize_text(element.get_text(" ", strip=True)) else []


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
        "list_id": list_id_for_element(element) if isinstance(element, Tag) else None,
        "list_type": list_type_for_element(element) if isinstance(element, Tag) else None,
        "item_index": list_item_index(element) if isinstance(element, Tag) else None,
        "warnings": [],
        "source_raw_sha256": state.source.raw_sha256,
    }


def register_references(
    state: ShowcaseState,
    text: str,
    *,
    section_id: str,
    block_id: str,
) -> list[str]:
    reference_ids: list[str] = []
    section_reference_index = sum(
        1 for reference in state.references if reference.get("section_id") == section_id
    )
    for match in REFERENCE_RE.finditer(text):
        reference_id = f"{section_id}:reference#{section_reference_index}"
        reference = {
            "schema_version": SHOWCASE_SCHEMA_VERSION,
            "dataset_id": state.dataset_id,
            "document_id": state.document_id,
            "section_id": section_id,
            "block_id": block_id,
            "reference_id": reference_id,
            "reference_index": section_reference_index,
            "section_reference_index": section_reference_index,
            "source_text": match.group(0),
            "numbers": normalize_reference_numbers(match.group("body")),
            "source_raw_sha256": state.source.raw_sha256,
        }
        state.references.append(reference)
        reference_ids.append(reference_id)
        section_reference_index += 1
    return reference_ids


def extract_recommendations(state: ShowcaseState) -> None:
    by_section: dict[str, list[dict[str, Any]]] = {}
    for block in state.blocks:
        by_section.setdefault(string_value(block["section_id"]), []).append(block)
    section_recommendations: dict[str, list[str]] = {}
    for section_id, blocks in by_section.items():
        blocks = sorted(blocks, key=lambda row: int(row.get("block_index") or 0))
        section_index = 0
        index = 0
        while index < len(blocks):
            block = blocks[index]
            text = string_value(block.get("text"))
            if not is_recommendation_start_block(block, text):
                index += 1
                continue
            group = [block]
            next_index = index + 1
            while next_index < len(blocks):
                candidate = blocks[next_index]
                candidate_text = string_value(candidate.get("text"))
                if is_recommendation_start_block(candidate, candidate_text):
                    break
                if is_recommendation_boundary_block(candidate):
                    break
                if COMMENT_RE.search(candidate_text):
                    group.append(candidate)
                    next_index += 1
                    continue
                if (
                    extract_uur(candidate_text) is not None
                    or extract_udd(candidate_text) is not None
                    or bool(REFERENCE_RE.search(candidate_text))
                ):
                    group.append(candidate)
                    next_index += 1
                    continue
                break
            recommendation_id = f"{section_id}:recommendation#{section_index}"
            section_index += 1
            group_texts = [string_value(row.get("text")) for row in group if row.get("text")]
            group_text = "\n\n".join(group_texts)
            recommendation_block_ids: list[str] = []
            comment_block_ids: list[str] = []
            grade_block_ids: list[str] = []
            reference_block_ids: list[str] = []
            reference_ids: list[str] = []
            for grouped_block in group:
                grouped_text = string_value(grouped_block.get("text"))
                grouped_block_id = string_value(grouped_block.get("block_id"))
                if grouped_block is block:
                    recommendation_block_ids.append(grouped_block_id)
                if COMMENT_RE.search(grouped_text):
                    comment_block_ids.append(grouped_block_id)
                if extract_uur(grouped_text) or extract_udd(grouped_text):
                    grade_block_ids.append(grouped_block_id)
                if REFERENCE_RE.search(grouped_text):
                    reference_block_ids.append(grouped_block_id)
                reference_ids.extend(
                    string_value(reference_id)
                    for reference_id in (grouped_block.get("reference_ids") or [])
                )
            uur = next(
                (value for value in (extract_uur(text) for text in group_texts) if value),
                None,
            )
            udd = next(
                (value for value in (extract_udd(text) for text in group_texts) if value),
                None,
            )
            recommendation = {
                "schema_version": SHOWCASE_SCHEMA_VERSION,
                "dataset_id": state.dataset_id,
                "document_id": state.document_id,
                "section_id": section_id,
                "recommendation_id": recommendation_id,
                "recommendation_index": section_index - 1,
                "text": text,
                "text_sha256": sha256_text(text),
                "group_text": group_text,
                "group_text_sha256": sha256_text(group_text),
                "block_ids": [row["block_id"] for row in group],
                "recommendation_block_ids": recommendation_block_ids,
                "comment_block_ids": comment_block_ids,
                "grade_block_ids": grade_block_ids,
                "reference_block_ids": reference_block_ids,
                "reference_ids": sorted(set(reference_ids)),
                "uur": uur,
                "udd": udd,
                "raw_grade_text": next(
                    (value for value in group_texts if extract_uur(value) or extract_udd(value)),
                    None,
                ),
                "raw_grade_texts": [
                    value for value in group_texts if extract_uur(value) or extract_udd(value)
                ],
                "comments": [
                    string_value(grouped_block.get("text"))
                    for grouped_block in group
                    if string_value(grouped_block.get("block_id")) in set(comment_block_ids)
                ],
                "source_raw_sha256": state.source.raw_sha256,
            }
            state.recommendations.append(recommendation)
            for grouped_block in group:
                grouped_block["recommendation_ids"] = [recommendation_id]
                grouped_text = string_value(grouped_block.get("text"))
                if grouped_block is block and grouped_block.get("block_type") in {
                    "paragraph",
                    "list",
                    "list_item",
                }:
                    grouped_block["block_type"] = "recommendation"
                elif extract_uur(grouped_text) or extract_udd(grouped_text):
                    grouped_block["block_type"] = "grade"
                elif COMMENT_RE.search(grouped_text):
                    grouped_block["block_type"] = "recommendation_comment"
                elif REFERENCE_RE.search(grouped_text):
                    grouped_block["block_type"] = "reference"
            section_recommendations.setdefault(section_id, []).append(recommendation_id)
            index = next_index
    for section in state.sections:
        section["recommendation_ids"] = section_recommendations.get(
            string_value(section["section_id"]),
            [],
        )


def is_recommendation_start_block(block: dict[str, Any], text: str) -> bool:
    if not text or COMMENT_RE.search(text):
        return False
    if block.get("block_type") in {"heading", "table_placeholder", "image_placeholder", "caption"}:
        return False
    return RECOMMENDATION_RE.search(text) is not None


def is_recommendation_boundary_block(block: dict[str, Any]) -> bool:
    return block.get("block_type") in {"heading", "table_placeholder"}


def build_chunks(state: ShowcaseState) -> None:
    for section in state.sections:
        append_text_chunks_for_section(state, section)
    for table in state.tables:
        append_table_chunks_for_table(state, table)
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
            primary_block_ids=[],
            overlap_block_ids=[],
            source_fragments=[
                {
                    "kind": "image_context",
                    "image_id": image_id,
                    "source_fields": [
                        "section_title",
                        "alt",
                        "title",
                        "caption",
                        "preceding_text",
                        "following_text",
                    ],
                }
            ],
            extra={
                "asset_id": image.get("asset_id"),
                "mime_type": image.get("mime_type"),
                "width": image.get("width"),
                "height": image.get("height"),
            },
        )


def append_text_chunks_for_section(state: ShowcaseState, section: dict[str, Any]) -> None:
    section_id = string_value(section["section_id"])
    blocks = sorted(
        [block for block in state.blocks if block.get("section_id") == section_id],
        key=lambda row: int(row.get("block_index") or 0),
    )
    units = text_units_from_blocks(blocks)
    chunk_units: list[dict[str, Any]] = []
    chunk_index = 0
    current_tokens = 0
    for unit in units:
        for prepared in split_unit_if_needed(unit, state):
            unit_tokens = int(prepared["token_estimate"])
            if chunk_units and current_tokens + unit_tokens > CHUNK_MAXIMUM_TOKENS:
                append_text_chunk(state, section, chunk_units, chunk_index)
                chunk_index += 1
                chunk_units = []
                current_tokens = 0
            chunk_units.append(prepared)
            current_tokens += unit_tokens
            if current_tokens >= CHUNK_TARGET_TOKENS:
                append_text_chunk(state, section, chunk_units, chunk_index)
                chunk_index += 1
                chunk_units = []
                current_tokens = 0
    if chunk_units:
        append_text_chunk(state, section, chunk_units, chunk_index)


def text_units_from_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        text = normalize_text(string_value(block.get("text")))
        if not is_indexable_text_block(block, text):
            index += 1
            continue
        recommendation_ids = [
            string_value(value) for value in (block.get("recommendation_ids") or [])
        ]
        if recommendation_ids:
            recommendation_id = recommendation_ids[0]
            group: list[dict[str, Any]] = []
            while index < len(blocks):
                candidate = blocks[index]
                candidate_ids = [
                    string_value(value) for value in (candidate.get("recommendation_ids") or [])
                ]
                candidate_text = normalize_text(string_value(candidate.get("text")))
                if recommendation_id not in candidate_ids or not is_indexable_text_block(
                    candidate,
                    candidate_text,
                ):
                    break
                group.append(candidate)
                index += 1
            units.append(unit_from_blocks(group, unit_type="recommendation_group"))
            continue
        units.append(unit_from_blocks([block], unit_type="block"))
        index += 1
    return units


def is_indexable_text_block(block: dict[str, Any], text: str) -> bool:
    if not text:
        return False
    if block.get("table_ids") or block.get("image_ids"):
        return False
    return block.get("block_type") not in {"table_placeholder", "image_placeholder"}


def unit_from_blocks(blocks: list[dict[str, Any]], *, unit_type: str) -> dict[str, Any]:
    text = "\n\n".join(normalize_text(string_value(block.get("text"))) for block in blocks)
    fragments = [
        {
            "kind": "block",
            "block_id": block.get("block_id"),
            "source_unit_id": block.get("block_id"),
            "block_type": block.get("block_type"),
            "source_char_start": 0,
            "source_char_end": len(normalize_text(string_value(block.get("text")))),
            "fragment_index": 0,
            "text": normalize_text(string_value(block.get("text"))),
            "text_sha256": sha256_text(normalize_text(string_value(block.get("text")))),
        }
        for block in blocks
    ]
    return {
        "unit_type": unit_type,
        "text": text,
        "primary_block_ids": [string_value(block.get("block_id")) for block in blocks],
        "source_fragments": fragments,
        "token_estimate": estimate_tokens(text),
    }


def split_unit_if_needed(unit: dict[str, Any], state: ShowcaseState) -> list[dict[str, Any]]:
    if int(unit["token_estimate"]) <= CHUNK_MAXIMUM_TOKENS:
        return [unit]
    if len(unit["source_fragments"]) > 1:
        split_units: list[dict[str, Any]] = []
        for fragment in unit["source_fragments"]:
            text = string_value(fragment.get("text"))
            block_unit = {
                "unit_type": "block_fragment",
                "text": text,
                "primary_block_ids": [string_value(fragment.get("block_id"))],
                "source_fragments": [fragment],
                "token_estimate": estimate_tokens(text),
            }
            split_units.extend(split_unit_if_needed(block_unit, state))
        return split_units
    fragment = unit["source_fragments"][0]
    block_id = string_value(fragment.get("block_id"))
    pieces = split_text_losslessly(string_value(unit.get("text")))
    state.warnings.append(
        {
            "path": block_id,
            "code": "oversized_sentence_split" if len(pieces) > 1 else "oversized_block",
            "details": {"pieces": len(pieces), "token_estimate": unit["token_estimate"]},
        }
    )
    return [
        {
            "unit_type": "block_fragment",
            "text": piece["text"],
            "primary_block_ids": [block_id],
            "source_fragments": [
                {
                    "kind": "block",
                    "block_id": block_id,
                    "source_unit_id": block_id,
                    "block_type": fragment.get("block_type"),
                    "source_char_start": piece["start"],
                    "source_char_end": piece["end"],
                    "fragment_index": piece_index,
                    "text": piece["text"],
                    "text_sha256": sha256_text(piece["text"]),
                }
            ],
            "token_estimate": estimate_tokens(piece["text"]),
        }
        for piece_index, piece in enumerate(pieces)
        if piece["text"]
    ]


def split_text_losslessly(text: str) -> list[dict[str, Any]]:
    if estimate_tokens(text) <= CHUNK_MAXIMUM_TOKENS:
        return [{"text": text, "start": 0, "end": len(text)}]
    spans = sentence_spans(text)
    pieces: list[dict[str, Any]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_text = ""
    for start, end in spans:
        sentence = text[start:end]
        if estimate_tokens(sentence) > CHUNK_MAXIMUM_TOKENS:
            if current_text and current_start is not None and current_end is not None:
                pieces.append({"text": current_text, "start": current_start, "end": current_end})
                current_text = ""
                current_start = None
                current_end = None
            pieces.extend(whitespace_spans(text, start, end))
            continue
        candidate = sentence if not current_text else text[current_start:start] + sentence
        if current_text and estimate_tokens(candidate) > CHUNK_MAXIMUM_TOKENS:
            if current_start is not None and current_end is not None:
                pieces.append({"text": current_text, "start": current_start, "end": current_end})
            current_start = start
            current_end = end
            current_text = sentence
        else:
            if current_start is None:
                current_start = start
            current_end = end
            current_text = text[current_start:current_end]
    if current_text and current_start is not None and current_end is not None:
        pieces.append({"text": current_text, "start": current_start, "end": current_end})
    return pieces


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+", text):
        end = match.end()
        spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans or [(0, len(text))]


def whitespace_spans(text: str, start: int, end: int) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    piece_start = start
    last_space = start
    index = start
    while index < end:
        if text[index].isspace():
            last_space = index + 1
        if index - piece_start >= TOKEN_CHAR_BUDGET:
            split_at = last_space if last_space > piece_start else index
            pieces.append(
                {"text": text[piece_start:split_at], "start": piece_start, "end": split_at}
            )
            piece_start = split_at
            last_space = split_at
        index += 1
    if piece_start < end:
        pieces.append({"text": text[piece_start:end], "start": piece_start, "end": end})
    return pieces


def append_text_chunk(
    state: ShowcaseState,
    section: dict[str, Any],
    units: list[dict[str, Any]],
    chunk_index: int,
) -> None:
    text = "\n\n".join(string_value(unit["text"]) for unit in units if unit.get("text"))
    primary_block_ids = [
        block_id
        for unit in units
        for block_id in (unit.get("primary_block_ids") or [])
        if block_id
    ]
    source_fragments = [
        fragment
        for unit in units
        for fragment in (unit.get("source_fragments") or [])
    ]
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
        primary_block_ids=primary_block_ids,
        overlap_block_ids=[],
        source_fragments=source_fragments,
        extra=recommendation_metadata_for_blocks(state, primary_block_ids),
    )


def recommendation_metadata_for_blocks(
    state: ShowcaseState,
    block_ids: list[str],
) -> dict[str, Any]:
    block_id_set = {string_value(block_id) for block_id in block_ids}
    matched = [
        recommendation
        for recommendation in state.recommendations
        if block_id_set.intersection(
            {string_value(value) for value in (recommendation.get("block_ids") or [])}
        )
    ]
    if not matched:
        return {}
    recommendation_ids = [
        string_value(recommendation.get("recommendation_id")) for recommendation in matched
    ]
    return {
        "recommendation_ids": recommendation_ids,
        "recommendation_block_ids": sorted(
            {
                string_value(value)
                for recommendation in matched
                for value in (recommendation.get("recommendation_block_ids") or [])
                if string_value(value) in block_id_set
            }
        ),
        "comment_block_ids": sorted(
            {
                string_value(value)
                for recommendation in matched
                for value in (recommendation.get("comment_block_ids") or [])
                if string_value(value) in block_id_set
            }
        ),
        "grade_block_ids": sorted(
            {
                string_value(value)
                for recommendation in matched
                for value in (recommendation.get("grade_block_ids") or [])
                if string_value(value) in block_id_set
            }
        ),
        "reference_block_ids": sorted(
            {
                string_value(value)
                for recommendation in matched
                for value in (recommendation.get("reference_block_ids") or [])
                if string_value(value) in block_id_set
            }
        ),
        "recommendation_reference_ids": sorted(
            {
                string_value(value)
                for recommendation in matched
                for value in (recommendation.get("reference_ids") or [])
            }
        ),
        "uur": next(
            (recommendation.get("uur") for recommendation in matched if recommendation.get("uur")),
            None,
        ),
        "udd": next(
            (recommendation.get("udd") for recommendation in matched if recommendation.get("udd")),
            None,
        ),
    }


def append_table_chunks_for_table(state: ShowcaseState, table: dict[str, Any]) -> None:
    table_id = string_value(table["table_id"])
    section = section_by_id(state, string_value(table["section_id"]))
    rows = table_rows_for_chunks(table_id, state.table_placements, state.table_cells)
    non_empty_rows = [row for row in rows if row["text"]]
    if not non_empty_rows:
        state.warnings.append(
            {"path": table_id, "code": "layout_table_not_indexed", "details": None}
        )
        return
    header_indices = [
        row["row_index"]
        for row in non_empty_rows
        if any(cell.get("is_header") for cell in row["cells"])
    ]
    if not header_indices and non_empty_rows:
        header_indices = [non_empty_rows[0]["row_index"]]
    header_rows = [row for row in non_empty_rows if row["row_index"] in set(header_indices)]
    data_rows = [row for row in non_empty_rows if row["row_index"] not in set(header_indices)]
    if not data_rows:
        data_rows = non_empty_rows
        header_rows = []
    group: list[dict[str, Any]] = []
    group_index = 0
    for row in data_rows:
        candidate_rows = [*header_rows, *group, row]
        candidate_text = table_text_from_rows(candidate_rows, table)
        if group and estimate_tokens(candidate_text) > CHUNK_MAXIMUM_TOKENS:
            append_table_chunk(state, section, table, header_rows, group, group_index)
            group_index += 1
            group = []
        if estimate_tokens(table_text_from_rows([*header_rows, row], table)) > CHUNK_MAXIMUM_TOKENS:
            split_rows = split_oversized_table_row(row, table_id, state)
            for split_row in split_rows:
                append_table_chunk(state, section, table, header_rows, [split_row], group_index)
                group_index += 1
            continue
        group.append(row)
        group_tokens = estimate_tokens(table_text_from_rows([*header_rows, *group], table))
        if group_tokens >= CHUNK_TARGET_TOKENS:
            append_table_chunk(state, section, table, header_rows, group, group_index)
            group_index += 1
            group = []
    if group:
        append_table_chunk(state, section, table, header_rows, group, group_index)


def table_rows_for_chunks(
    table_id: str,
    placements: list[dict[str, Any]],
    cells: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: dict[int, list[dict[str, Any]]] = {}
    cells_by_id = {string_value(cell.get("cell_id")): cell for cell in cells}
    for placement in placements:
        if placement.get("table_id") != table_id:
            continue
        logical_row = int(placement.get("logical_row") or 0)
        origin = cells_by_id.get(string_value(placement.get("origin_cell_id")), {})
        row_item = dict(placement)
        row_item["is_header"] = bool(origin.get("is_header"))
        row_item["source_char_start"] = 0
        row_item["source_char_end"] = len(string_value(row_item.get("text")))
        row_item["fragment_index"] = 0
        row_item["fragment_count"] = 1
        rows.setdefault(logical_row, []).append(row_item)
    result: list[dict[str, Any]] = []
    for row_index in sorted(rows):
        row_cells = sorted(
            rows[row_index],
            key=lambda cell: int(cell.get("logical_column") or 0),
        )
        text = " | ".join(
            string_value(cell.get("text")) for cell in row_cells if cell.get("text")
        )
        result.append({"row_index": row_index, "cells": row_cells, "text": text})
    return result


def table_text_from_rows(rows: list[dict[str, Any]], table: dict[str, Any]) -> str:
    lines = []
    caption = string_value(table.get("caption"))
    if caption:
        lines.append(caption)
    seen: set[int] = set()
    for row in rows:
        row_index = int(row["row_index"])
        if row_index in seen:
            continue
        seen.add(row_index)
        if row["text"]:
            lines.append(f"row {row_index}: {row['text']}")
    return "\n".join(lines)


def split_oversized_table_row(
    row: dict[str, Any],
    table_id: str,
    state: ShowcaseState,
) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    for cell in row["cells"]:
        text = string_value(cell.get("text"))
        if not text:
            continue
        split_pieces = split_text_losslessly(text)
        for piece_index, piece in enumerate(split_pieces):
            cell_piece = dict(cell)
            cell_piece["text"] = piece["text"]
            cell_piece["source_char_start"] = piece["start"]
            cell_piece["source_char_end"] = piece["end"]
            cell_piece["fragment_index"] = piece_index
            cell_piece["fragment_count"] = len(split_pieces)
            pieces.append(
                {
                    "row_index": row["row_index"],
                    "cells": [cell_piece],
                    "text": (
                        f"cell {cell.get('physical_row')}:{cell.get('logical_column')}: "
                        f"{piece['text']}"
                    ),
                }
            )
    state.warnings.append(
        {
            "path": table_id,
            "code": "oversized_table_cell_split",
            "details": {"row_index": row["row_index"], "pieces": len(pieces)},
        }
    )
    return pieces


def append_table_chunk(
    state: ShowcaseState,
    section: dict[str, Any],
    table: dict[str, Any],
    header_rows: list[dict[str, Any]],
    data_rows: list[dict[str, Any]],
    group_index: int,
) -> None:
    table_id = string_value(table["table_id"])
    included_rows = [*header_rows, *data_rows]
    text = table_text_from_rows(included_rows, table)
    row_indices = [int(row["row_index"]) for row in data_rows]
    row_start = min(row_indices) if row_indices else 0
    row_end = max(row_indices) if row_indices else 0
    cell_ids = [
        string_value(cell.get("origin_cell_id") or cell.get("cell_id"))
        for row in included_rows
        for cell in row["cells"]
        if string_value(cell.get("text"))
    ]
    placement_ids = [
        string_value(cell.get("placement_id"))
        for row in included_rows
        for cell in row["cells"]
        if string_value(cell.get("text"))
    ]
    append_chunk(
        state,
        chunk_id=f"{table_id}:rows#{row_start}-{row_end}"
        if group_index == 0
        else f"{table_id}:rows#{row_start}-{row_end}#{group_index}",
        chunk_type="table",
        text=text,
        section=section,
        table_id=table_id,
        image_id=None,
        asset_path=None,
        row_start=row_start,
        row_end=row_end,
        primary_block_ids=[],
        overlap_block_ids=[],
        source_fragments=[
            {
                "kind": "table_rows",
                "table_id": table_id,
                "header_row_indices": [row["row_index"] for row in header_rows],
                "row_start": row_start,
                "row_end": row_end,
                "cell_ids": cell_ids,
                "placement_ids": placement_ids,
            }
        ]
        + table_source_fragments(included_rows, table_id),
        extra={
            "header_row_indices": [row["row_index"] for row in header_rows],
            "cell_ids": cell_ids,
            "placement_ids": placement_ids,
        },
    )


def table_source_fragments(
    rows: list[dict[str, Any]],
    table_id: str,
) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for row in rows:
        for cell in row["cells"]:
            text = string_value(cell.get("text"))
            if not text:
                continue
            placement_id = string_value(cell.get("placement_id"))
            fragments.append(
                {
                    "kind": "table_placement",
                    "table_id": table_id,
                    "placement_id": placement_id,
                    "source_unit_id": placement_id,
                    "origin_cell_id": string_value(
                        cell.get("origin_cell_id") or cell.get("cell_id")
                    ),
                    "source_char_start": int(cell.get("source_char_start") or 0),
                    "source_char_end": int(cell.get("source_char_end") or len(text)),
                    "fragment_index": int(cell.get("fragment_index") or 0),
                    "fragment_count": int(cell.get("fragment_count") or 1),
                    "text": text,
                    "text_sha256": sha256_text(text),
                }
            )
    return fragments


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
    primary_block_ids: list[str],
    overlap_block_ids: list[str],
    source_fragments: list[dict[str, Any]],
    extra: dict[str, Any],
) -> None:
    serialized = text
    estimated_token_count = estimate_tokens(serialized)
    chunk = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "dataset_id": state.dataset_id,
        "document_id": state.document_id,
        "document_title": state.document.get("title"),
        "code_version": state.source.code_version,
        "chunk_id": chunk_id,
        "chunk_type": chunk_type,
        "text": serialized,
        "text_sha256": sha256_text(serialized),
        "estimated_token_count": estimated_token_count,
        "estimator_name": "chars_div_4",
        "token_estimate": estimated_token_count,
        "section_id": section.get("section_id"),
        "section_key": section.get("section_key"),
        "section_title": section.get("title"),
        "table_id": table_id,
        "image_id": image_id,
        "asset_path": asset_path,
        "row_start": row_start,
        "row_end": row_end,
        "primary_block_ids": primary_block_ids,
        "overlap_block_ids": overlap_block_ids,
        "source_fragments": source_fragments,
        "frontend_anchor": section.get("anchor"),
        "source_raw_sha256": state.source.raw_sha256,
        "citation": citation_for_chunk(state, section, table_id=table_id, image_id=image_id),
    }
    chunk.update(extra)
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
        "document_kind": state.document_kind,
        "code_version": state.source.code_version,
        "current_code_version": state.current_code_version,
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
        "source_raw_path": state.source_raw_path,
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
    write_jsonl(
        canonical / "table-placements.jsonl",
        sorted_by(state.table_placements, "placement_id"),
    )
    write_jsonl(canonical / "images.jsonl", sorted_by(state.images, "image_id"))
    write_jsonl(canonical / "assets.jsonl", sorted_by(state.assets, "asset_id"))
    write_jsonl(
        canonical / "recommendations.jsonl",
        sorted_by(state.recommendations, "recommendation_id"),
    )
    write_jsonl(canonical / "references.jsonl", sorted_by(state.references, "reference_id"))
    write_jsonl(canonical / "chunks.jsonl", sorted_by(state.chunks, "chunk_id"))
    write_jsonl(canonical / "citation-index.jsonl", sorted_by(state.citation_rows, "chunk_id"))
    write_json(canonical / "coverage-map.json", coverage_map_for_state(state))
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
        "table_placements": sorted_by(state.table_placements, "placement_id"),
        "images": [frontend_image_row(row) for row in sorted_by(state.images, "image_id")],
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


def frontend_image_row(image: dict[str, Any]) -> dict[str, Any]:
    row = dict(image)
    row.pop("original_src", None)
    row.pop("raw_src_sha256", None)
    return row


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
    write_jsonl(
        ml / "table-placements.jsonl",
        sorted_by(state.table_placements, "placement_id"),
    )
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
    write_json(reports / "text-index-coverage.json", text_index_coverage_report(state))
    write_json(reports / "table-index-coverage.json", table_index_coverage_report(state))
    write_json(reports / "image-occurrence-coverage.json", image_occurrence_coverage_report(state))


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
    table_placements = read_jsonl(root / "canonical" / "table-placements.jsonl")
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
            ("table_placements", table_placements, "placement_id"),
            ("images", images, "image_id"),
            ("assets", assets, "asset_id"),
            ("chunks", chunks, "chunk_id"),
            ("references", references, "reference_id"),
            ("recommendations", recommendations, "recommendation_id"),
        ),
    )
    validate_raw_copy(root, document, errors)
    validate_raw_occurrence_counts(
        root,
        sections,
        tables,
        table_cells,
        table_placements,
        images,
        errors,
    )
    validate_counts(document, sections, blocks, tables, images, assets, references, errors)
    validate_text_preservation(root, sections, errors)
    validate_html_safety(sections, errors)
    validate_assets(root, images, assets, errors, warnings)
    validate_references(sections, blocks, tables, images, chunks, errors)
    validate_table_placements(tables, table_cells, table_placements, chunks, errors)
    validate_chunks(tables, images, chunks, errors)
    validate_recommendation_chunks(blocks, recommendations, chunks, errors)
    validate_citation_titles(chunks, errors)
    validate_coverage_reports(root, errors)
    validate_frontend(root, tables, images, errors)
    text_coverage_path = root / "reports" / "text-index-coverage.json"
    table_coverage_path = root / "reports" / "table-index-coverage.json"
    image_coverage_path = root / "reports" / "image-occurrence-coverage.json"
    text_coverage = read_json_file(text_coverage_path) if text_coverage_path.exists() else {}
    table_coverage = read_json_file(table_coverage_path) if table_coverage_path.exists() else {}
    image_coverage = read_json_file(image_coverage_path) if image_coverage_path.exists() else {}
    report = {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "valid": not errors,
        "summary": {
            "hard_errors": len(errors),
            "warnings": len(warnings),
            "text_loss": sum(1 for row in errors if row["code"] == "text_loss"),
            "visible_text_coverage_percent": 100
            if not any(row["code"] == "text_loss" for row in errors)
            else 0,
            "text_index_coverage_percent": text_coverage.get("coverage_percent"),
            "table_index_coverage_percent": table_coverage.get("coverage_percent"),
            "image_occurrence_coverage_percent": image_coverage.get("coverage_percent"),
            "unsafe_html": sum(1 for row in errors if row["code"].startswith("unsafe_html")),
            "unresolved_references": sum(
                1 for row in errors if row["code"].startswith("unresolved")
            ),
            "unresolved_foreign_keys": sum(
                1 for row in errors if row["code"].startswith("unresolved")
            ),
            "chunks_over_maximum": sum(1 for row in errors if row["code"] == "chunk_above_maximum"),
            "silent_truncations": sum(1 for row in errors if row["code"] == "chunk_text_truncated"),
            "citation_titles_missing": sum(
                1 for row in errors if row["code"] == "citation_document_title_missing"
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


def validate_raw_occurrence_counts(
    root: Path,
    sections: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    table_cells: list[dict[str, Any]],
    table_placements: list[dict[str, Any]],
    images: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    raw_path = root / "source" / "getclinrec.json"
    if not raw_path.exists():
        return
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    inventory = build_raw_source_inventory(payload)
    for inventory_error in inventory.errors:
        errors.append(inventory_error)
    expected = {
        "sections": (len(inventory.sections), len(sections)),
        "tables": (len(inventory.tables), len(tables)),
        "table_cells": (len(inventory.table_cells), len(table_cells)),
        "logical_table_placements": (len(inventory.table_placements), len(table_placements)),
        "images": (len(inventory.images), len(images)),
    }
    for unit, (raw_count, parsed_count) in expected.items():
        if raw_count != parsed_count:
            errors.append(
                issue(
                    f"canonical/{unit}.jsonl",
                    f"raw_{unit}_count_mismatch",
                    {"raw": raw_count, "parsed": parsed_count},
                )
            )
    raw_paths = {record.raw_path for record in inventory.sections}
    parsed_paths = {string_value(section.get("raw_path")) for section in sections}
    for missing_path in sorted(raw_paths - parsed_paths):
        errors.append(issue(missing_path, "missing_raw_section", None))
    for extra_path in sorted(parsed_paths - raw_paths):
        errors.append(issue(extra_path, "parsed_section_without_raw_path", None))


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
    inventory = build_raw_source_inventory(payload)
    raw_items = [record.section for record in inventory.sections]
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
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup.find_all(True):
            if not isinstance(tag, Tag):
                continue
            tag_name = tag.name.lower()
            if tag_name in {"html", "body"}:
                continue
            if tag_name in UNSAFE_TAGS or tag_name not in SAFE_TAGS:
                errors.append(issue(section_id, "unsafe_html_tag", tag.name))
                continue
            allowed_attrs = (
                SAFE_ATTRS_BY_TAG.get("*", set())
                | SAFE_ATTRS_BY_TAG.get(tag_name, set())
                | GENERATED_SAFE_ATTRS
            )
            for attr, value in tag.attrs.items():
                attr_name = attr.casefold()
                if attr_name.startswith("on"):
                    errors.append(issue(section_id, "unsafe_html_event_handler", attr))
                if attr_name not in allowed_attrs:
                    errors.append(issue(section_id, "unsafe_html_attribute", attr))
                if attr_name in {"href", "src"} and not is_safe_url(value, tag_name=tag_name):
                    errors.append(issue(section_id, "unsafe_html_url", attr))


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
            errors.append(
                issue(
                    string_value(image.get("image_id")),
                    "image_asset_unresolved",
                    image.get("decode_error"),
                )
            )
        asset_id = string_value(image.get("asset_id"))
        if image.get("detected_mime_type") == "image/svg+xml":
            errors.append(issue(string_value(image.get("image_id")), "unsafe_svg_exposed", None))
        if image.get("source_type") in {"base64", "relative"} and asset_id not in assets_by_id:
            errors.append(
                issue(string_value(image.get("image_id")), "image_asset_missing", asset_id)
            )
        if image.get("source_type") in {"http", "https"}:
            errors.append(
                issue(
                    string_value(image.get("image_id")),
                    "external_image_unresolved",
                    image.get("original_src"),
                )
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


def validate_table_placements(
    tables: list[dict[str, Any]],
    table_cells: list[dict[str, Any]],
    table_placements: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    table_ids = {string_value(table.get("table_id")) for table in tables}
    cell_ids = {string_value(cell.get("cell_id")) for cell in table_cells}
    expected_placements = {
        string_value(placement.get("placement_id"))
        for placement in table_placements
        if normalize_text(string_value(placement.get("text")))
    }
    covered_placements = {
        string_value(placement_id)
        for chunk in chunks
        if chunk.get("chunk_type") == "table"
        for placement_id in (chunk.get("placement_ids") or [])
    }
    fragment_placements = {
        string_value(fragment.get("placement_id"))
        for chunk in chunks
        if chunk.get("chunk_type") == "table"
        for fragment in (chunk.get("source_fragments") or [])
        if isinstance(fragment, dict) and fragment.get("kind") == "table_placement"
    }
    placements_by_id = {
        string_value(placement.get("placement_id")): placement
        for placement in table_placements
    }
    for placement in table_placements:
        placement_id = string_value(placement.get("placement_id"))
        table_id = string_value(placement.get("table_id"))
        origin_cell_id = string_value(placement.get("origin_cell_id"))
        if table_id not in table_ids:
            errors.append(issue(placement_id, "unresolved_placement_table", table_id))
        if origin_cell_id not in cell_ids:
            errors.append(issue(placement_id, "unresolved_origin_cell", origin_cell_id))
    missing = sorted(expected_placements - covered_placements)
    if missing:
        errors.append(
            issue(
                "canonical/table-placements.jsonl",
                "logical_table_placement_missing",
                missing,
            )
        )
    missing_fragments = sorted(expected_placements - fragment_placements)
    if missing_fragments:
        errors.append(
            issue(
                "canonical/chunks.jsonl",
                "logical_table_placement_fragment_missing",
                missing_fragments,
            )
        )
    for chunk in chunks:
        if chunk.get("chunk_type") != "table":
            continue
        for fragment in chunk.get("source_fragments") or []:
            if not isinstance(fragment, dict) or fragment.get("kind") != "table_placement":
                continue
            placement_id = string_value(fragment.get("placement_id"))
            matched_placement = placements_by_id.get(placement_id)
            if matched_placement is None:
                errors.append(
                    issue(
                        string_value(chunk.get("chunk_id")),
                        "unknown_table_fragment_placement",
                        placement_id,
                    )
                )
                continue
            fragment_text = string_value(fragment.get("text"))
            if string_value(fragment.get("text_sha256")) != sha256_text(fragment_text):
                errors.append(
                    issue(
                        string_value(chunk.get("chunk_id")),
                        "table_fragment_hash_mismatch",
                        placement_id,
                    )
                )
            start = int(fragment.get("source_char_start") or 0)
            end = int(fragment.get("source_char_end") or 0)
            source_text = string_value(matched_placement.get("text"))
            if start < 0 or end < start or end > len(source_text):
                errors.append(
                    issue(
                        string_value(chunk.get("chunk_id")),
                        "table_fragment_range_invalid",
                        {"placement_id": placement_id, "start": start, "end": end},
                    )
                )
            elif source_text[start:end] != fragment_text:
                errors.append(
                    issue(
                        string_value(chunk.get("chunk_id")),
                        "table_fragment_text_mismatch",
                        placement_id,
                    )
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
        estimated_tokens = int(
            chunk.get("estimated_token_count") or chunk.get("token_estimate") or 0
        )
        if estimated_tokens > CHUNK_MAXIMUM_TOKENS:
            errors.append(issue(chunk_id, "chunk_above_maximum", None))
        if chunk.get("chunk_type") == "text":
            fragments = [
                fragment
                for fragment in (chunk.get("source_fragments") or [])
                if isinstance(fragment, dict) and fragment.get("kind") == "block"
            ]
            reconstructed = "\n\n".join(
                string_value(fragment.get("text")) for fragment in fragments
            )
            if reconstructed != string_value(chunk.get("text")):
                errors.append(issue(chunk_id, "source_fragment_text_mismatch", None))
            for fragment in fragments:
                fragment_text = string_value(fragment.get("text"))
                expected_hash = string_value(fragment.get("text_sha256"))
                if expected_hash and expected_hash != sha256_text(fragment_text):
                    errors.append(
                        issue(
                            chunk_id,
                            "source_fragment_hash_mismatch",
                            fragment.get("block_id"),
                        )
                    )
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


def validate_recommendation_chunks(
    blocks: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    recommendation_ids = {
        string_value(recommendation.get("recommendation_id"))
        for recommendation in recommendations
    }
    block_to_recommendations: dict[str, set[str]] = {}
    for block in blocks:
        block_id = string_value(block.get("block_id"))
        for recommendation_id in block.get("recommendation_ids") or []:
            block_to_recommendations.setdefault(block_id, set()).add(
                string_value(recommendation_id)
            )
    chunked_recommendations: set[str] = set()
    for chunk in chunks:
        chunk_id = string_value(chunk.get("chunk_id"))
        expected_ids: set[str] = set()
        for block_id in chunk.get("primary_block_ids") or []:
            expected_ids.update(block_to_recommendations.get(string_value(block_id), set()))
        if not expected_ids:
            continue
        actual_ids = {
            string_value(value) for value in (chunk.get("recommendation_ids") or [])
        }
        if not actual_ids:
            errors.append(issue(chunk_id, "recommendation_chunk_metadata_missing", None))
            continue
        if not expected_ids.issubset(actual_ids):
            errors.append(
                issue(
                    chunk_id,
                    "recommendation_chunk_metadata_incomplete",
                    {"expected": sorted(expected_ids), "actual": sorted(actual_ids)},
                )
            )
        chunked_recommendations.update(actual_ids)
    missing = sorted(recommendation_ids - chunked_recommendations)
    if missing:
        errors.append(issue("recommendations", "recommendations_not_chunked", missing))


def validate_citation_titles(chunks: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    for chunk in chunks:
        citation_value = chunk.get("citation")
        citation: dict[str, Any] = citation_value if isinstance(citation_value, dict) else {}
        if not string_value(citation.get("document_title")):
            errors.append(
                issue(
                    string_value(chunk.get("chunk_id")),
                    "citation_document_title_missing",
                    None,
                )
            )


def validate_coverage_reports(root: Path, errors: list[dict[str, Any]]) -> None:
    blocks = read_jsonl(root / "canonical" / "blocks.jsonl")
    table_placements = read_jsonl(root / "canonical" / "table-placements.jsonl")
    images = read_jsonl(root / "canonical" / "images.jsonl")
    chunks = read_jsonl(root / "canonical" / "chunks.jsonl")
    indexable_blocks = [
        block
        for block in blocks
        if is_indexable_text_block(block, normalize_text(string_value(block.get("text"))))
    ]
    covered_blocks = {
        string_value(block_id)
        for chunk in chunks
        if chunk.get("chunk_type") == "text"
        for block_id in (chunk.get("primary_block_ids") or [])
    }
    missing_blocks = [
        string_value(block.get("block_id"))
        for block in indexable_blocks
        if string_value(block.get("block_id")) not in covered_blocks
    ]
    fragment_gaps = block_fragment_gaps(indexable_blocks, chunks)
    if missing_blocks or fragment_gaps:
        errors.append(
            issue(
                "canonical/chunks.jsonl",
                "text_index_coverage_incomplete",
                {"missing": missing_blocks, "fragment_gaps": fragment_gaps},
            )
        )
    expected_placements = {
        string_value(placement.get("placement_id"))
        for placement in table_placements
        if normalize_text(string_value(placement.get("text")))
    }
    covered_placements = {
        string_value(placement_id)
        for chunk in chunks
        if chunk.get("chunk_type") == "table"
        for placement_id in (chunk.get("placement_ids") or [])
    }
    missing_placements = sorted(expected_placements - covered_placements)
    if missing_placements:
        errors.append(
            issue(
                "canonical/table-placements.jsonl",
                "table_index_coverage_incomplete",
                missing_placements,
            )
        )
    image_chunk_ids = {
        string_value(chunk.get("image_id"))
        for chunk in chunks
        if chunk.get("chunk_type") == "image"
    }
    missing_image_chunks = [
        string_value(image.get("image_id"))
        for image in images
        if string_value(image.get("image_id")) not in image_chunk_ids
    ]
    if missing_image_chunks:
        errors.append(
            issue(
                "canonical/images.jsonl",
                "image_occurrence_coverage_incomplete",
                missing_image_chunks,
            )
        )
    for relative, code_prefix in (
        ("reports/text-index-coverage.json", "text_index"),
        ("reports/table-index-coverage.json", "table_index"),
        ("reports/image-occurrence-coverage.json", "image_occurrence"),
    ):
        path = root / relative
        if not path.exists():
            errors.append(issue(relative, f"{code_prefix}_coverage_report_missing", None))
            continue
        report = read_json_file(path)
        if report.get("coverage_percent") != 100.0:
            errors.append(issue(relative, f"{code_prefix}_coverage_incomplete", report))
        if report.get("missing") or report.get("missing_chunks") or report.get("fragment_gaps"):
            errors.append(issue(relative, f"{code_prefix}_coverage_missing_units", report))


def validate_frontend(
    root: Path,
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    payload = read_json_file(root / "frontend" / "document.json")
    if not payload:
        errors.append(issue("frontend/document.json", "frontend_document_missing", None))
        return
    frontend_html = ""
    frontend_soup = BeautifulSoup("", "lxml")
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        html_text = string_value(section.get("html"))
        frontend_html += html_text
        if "data:image/" in html_text:
            errors.append(issue("frontend/document.json", "base64_in_frontend_html", None))
        for src_match in re.finditer(r'src="([^"]+)"', html_text):
            src = src_match.group(1)
            if src.startswith("assets/") and not (root / "frontend" / src).exists():
                errors.append(
                    issue("frontend/document.json", "frontend_asset_reference_unresolved", src)
                )
    frontend_soup = BeautifulSoup(frontend_html, "lxml")
    preview_html = (root / "preview" / "index.html").read_text(encoding="utf-8")
    preview_soup = BeautifulSoup(preview_html, "lxml")
    for table in tables:
        table_id = string_value(table.get("table_id"))
        if not table_id:
            continue
        frontend_matches = frontend_soup.find_all(
            lambda tag, expected=table_id: isinstance(tag, Tag)
            and tag.get("data-table-id") == expected
        )
        preview_matches = preview_soup.find_all(
            lambda tag, expected=table_id: isinstance(tag, Tag)
            and tag.get("data-table-id") == expected
        )
        if len(frontend_matches) != 1:
            errors.append(
                issue(table_id, "frontend_table_placement_count_mismatch", len(frontend_matches))
            )
        if len(preview_matches) != 1:
            errors.append(
                issue(table_id, "preview_table_placement_count_mismatch", len(preview_matches))
            )
    for image in images:
        if not image.get("asset_path"):
            continue
        image_id = string_value(image.get("image_id"))
        expected_asset = string_value(image.get("asset_path"))
        image_attrs: dict[str, Any] = {"data-image-id": image_id}
        frontend_matches = frontend_soup.find_all("img", attrs=image_attrs)
        preview_matches = preview_soup.find_all("img", attrs=image_attrs)
        if len(frontend_matches) != 1:
            errors.append(
                issue(
                    image_id,
                    "frontend_image_placement_count_mismatch",
                    len(frontend_matches),
                )
            )
        if len(preview_matches) != 1:
            errors.append(
                issue(
                    image_id,
                    "preview_image_placement_count_mismatch",
                    len(preview_matches),
                )
            )
        for image_tag in [*frontend_matches, *preview_matches]:
            src = string_value(image_tag.get("src"))
            if src != expected_asset:
                errors.append(
                    issue(
                        image_id,
                        "frontend_image_asset_mismatch",
                        {"expected": expected_asset, "actual": src},
                    )
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
    names: list[str] = []
    with zipfile.ZipFile(archive, "r") as zip_file:
        names = zip_file.namelist()
        for name in names:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts:
                errors.append(issue(name, "unsafe_zip_path", None))
        expected_names = {
            (Path(root.name) / path.relative_to(root)).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        actual_names = set(names)
        missing_names = sorted(expected_names - actual_names)
        extra_names = sorted(actual_names - expected_names)
        for name in missing_names:
            errors.append(issue(name, "zip_file_missing", None))
        for name in extra_names:
            errors.append(issue(name, "zip_extra_file", None))
        if not errors:
            with tempfile.TemporaryDirectory(prefix="clinrec-showcase-zip-") as temp_name:
                extract_root = Path(temp_name)
                zip_file.extractall(extract_root)
                extracted_showcase = extract_root / root.name
                checksum_file = extracted_showcase / "checksums.sha256"
                if not checksum_file.exists():
                    errors.append(issue("checksums.sha256", "zip_checksums_missing", None))
                else:
                    listed_files: set[str] = set()
                    for line in checksum_file.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        digest, relative = line.split(maxsplit=1)
                        relative = relative.strip()
                        listed_files.add(relative)
                        extracted_file = extracted_showcase / relative
                        if not extracted_file.exists():
                            errors.append(issue(relative, "zip_checksum_file_missing", None))
                            continue
                        actual_digest = sha256_file(extracted_file)
                        if actual_digest != digest:
                            errors.append(
                                issue(
                                    relative,
                                    "zip_checksum_mismatch",
                                    {"expected": digest, "actual": actual_digest},
                                )
                            )
                    extracted_files = {
                        path.relative_to(extracted_showcase).as_posix()
                        for path in extracted_showcase.rglob("*")
                        if path.is_file() and path.name != "checksums.sha256"
                    }
                    extra_unlisted = sorted(extracted_files - listed_files)
                    missing_listed = sorted(listed_files - extracted_files)
                    for relative in extra_unlisted:
                        errors.append(issue(relative, "zip_checksum_unlisted_file", None))
                    for relative in missing_listed:
                        errors.append(issue(relative, "zip_checksum_listed_file_missing", None))
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
        f"# Clinrec showcase {state.source.code_version}",
        "",
        string_value(state.document.get("title")),
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
            f"- Schema `{SHOWCASE_SCHEMA_VERSION}` is a draft showcase contract.",
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
        if table.get("csv_available"):
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
        ("table-placements.jsonl", state.table_placements, "placement_id"),
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


def write_asset_once(
    state: ShowcaseState,
    content: bytes,
    *,
    declared_mime_type: str | None,
    detected_mime_type: str,
    width: int | None,
    height: int | None,
    occurrence_id: str,
) -> str:
    asset_sha = sha256_bytes(content)
    extension = extension_for_mime(detected_mime_type)
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
                "sha256": asset_sha,
                "path": relative,
                "declared_mime_type": declared_mime_type,
                "detected_mime_type": detected_mime_type,
                "mime_type": detected_mime_type,
                "extension": extension,
                "size_bytes": len(content),
                "width": width,
                "height": height,
                "occurrence_ids": [occurrence_id],
                "source": "decoded_data_uri",
            }
        )
    else:
        for asset in state.assets:
            if asset.get("asset_id") == asset_id:
                occurrence_ids = [
                    string_value(value) for value in (asset.get("occurrence_ids") or [])
                ]
                if occurrence_id not in occurrence_ids:
                    asset["occurrence_ids"] = [*occurrence_ids, occurrence_id]
                if asset.get("width") is None:
                    asset["width"] = width
                if asset.get("height") is None:
                    asset["height"] = height
                break
    return relative


def table_cells_and_grid(
    table: Tag,
    *,
    table_id: str,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]], list[dict[str, Any]]]:
    rows = [
        row
        for row in table.find_all("tr")
        if isinstance(row, Tag) and nearest_table(row) is table
    ]
    cells: list[dict[str, Any]] = []
    occupied: dict[tuple[int, int], dict[str, Any]] = {}
    grid: list[list[dict[str, Any]]] = []
    placements: list[dict[str, Any]] = []
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
            cell_id = f"{table_id}:cell#{row_index}:{column_index}:{cell_index}"
            text = table_cell_text(cell)
            record = {
                "table_id": table_id,
                "cell_id": cell_id,
                "origin_cell_id": cell_id,
                "row_index": row_index,
                "column_index": column_index,
                "cell_index": cell_index,
                "tag": cell.name.lower(),
                "text": text,
                "text_sha256": sha256_text(text),
                "source_char_start": 0,
                "source_char_end": len(text),
                "fragment_index": 0,
                "html": inner_html(cell),
                "rowspan": rowspan,
                "colspan": colspan,
                "is_header": cell.name.lower() == "th" or cell.find_parent("thead") is not None,
            }
            cells.append(record)
            origin = {
                "cell_id": cell_id,
                "origin_cell_id": cell_id,
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
    for logical_row, grid_row_items in enumerate(grid):
        for logical_column, logical_placement in enumerate(grid_row_items):
            origin_cell_id = string_value(
                logical_placement.get("origin_cell_id") or logical_placement.get("cell_id")
            )
            placement_id = f"{table_id}:placement#{logical_row}:{logical_column}"
            placements.append(
                {
                    "table_id": table_id,
                    "placement_id": placement_id,
                    "origin_cell_id": origin_cell_id,
                    "cell_id": origin_cell_id,
                    "logical_row": logical_row,
                    "logical_column": logical_column,
                    "physical_row": logical_placement.get("grid_row"),
                    "physical_column": logical_placement.get("grid_column"),
                    "is_origin": bool(logical_placement.get("is_origin")),
                    "text": string_value(logical_placement.get("text")),
                    "text_sha256": sha256_text(string_value(logical_placement.get("text"))),
                    "rowspan": logical_placement.get("rowspan"),
                    "colspan": logical_placement.get("colspan"),
                }
            )
            logical_placement["placement_id"] = placement_id
            logical_placement["origin_cell_id"] = origin_cell_id
    return cells, grid, placements


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
        "logical_table_placements": len(state.table_placements),
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
        "logical_placements": len(state.table_placements),
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


def coverage_map_for_state(state: ShowcaseState) -> dict[str, Any]:
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "source_raw_sha256": state.source.raw_sha256,
        "text": text_index_coverage_report(state),
        "tables": table_index_coverage_report(state),
        "images": image_occurrence_coverage_report(state),
    }


def text_index_coverage_report(state: ShowcaseState) -> dict[str, Any]:
    indexable_blocks = [
        block
        for block in state.blocks
        if is_indexable_text_block(block, normalize_text(string_value(block.get("text"))))
    ]
    covered = {
        string_value(block_id)
        for chunk in state.chunks
        if chunk.get("chunk_type") == "text"
        for block_id in (chunk.get("primary_block_ids") or [])
    }
    missing = [
        string_value(block.get("block_id"))
        for block in indexable_blocks
        if string_value(block.get("block_id")) not in covered
    ]
    fragment_gaps = block_fragment_gaps(indexable_blocks, state.chunks)
    expected = len(indexable_blocks)
    covered_count = expected - len(missing)
    percent = 100.0 if expected == 0 else round((covered_count / expected) * 100, 6)
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "unit": "indexable_primary_block",
        "expected": expected,
        "covered": covered_count,
        "missing": missing,
        "fragment_gaps": fragment_gaps,
        "coverage_percent": percent if not fragment_gaps else min(percent, 99.999999),
    }


def block_fragment_gaps(
    indexable_blocks: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ranges_by_block: dict[str, list[tuple[int, int]]] = {}
    for chunk in chunks:
        if chunk.get("chunk_type") != "text":
            continue
        for fragment in chunk.get("source_fragments") or []:
            if fragment.get("kind") != "block":
                continue
            block_id = string_value(fragment.get("block_id"))
            ranges_by_block.setdefault(block_id, []).append(
                (
                    int(fragment.get("source_char_start") or 0),
                    int(fragment.get("source_char_end") or 0),
                )
            )
    gaps: list[dict[str, Any]] = []
    for block in indexable_blocks:
        block_id = string_value(block.get("block_id"))
        text_length = len(normalize_text(string_value(block.get("text"))))
        ranges = sorted(ranges_by_block.get(block_id, []))
        cursor = 0
        for start, end in ranges:
            if start > cursor:
                gaps.append({"block_id": block_id, "start": cursor, "end": start})
            cursor = max(cursor, end)
        if cursor < text_length:
            gaps.append({"block_id": block_id, "start": cursor, "end": text_length})
    return gaps


def table_index_coverage_report(state: ShowcaseState) -> dict[str, Any]:
    expected_placements = {
        string_value(placement.get("placement_id"))
        for placement in state.table_placements
        if normalize_text(string_value(placement.get("text")))
    }
    covered_placements = {
        string_value(placement_id)
        for chunk in state.chunks
        if chunk.get("chunk_type") == "table"
        for placement_id in (chunk.get("placement_ids") or [])
    }
    missing = sorted(expected_placements - covered_placements)
    expected = len(expected_placements)
    covered = expected - len(missing)
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "unit": "non_empty_logical_placement",
        "expected": expected,
        "covered": covered,
        "missing": missing,
        "coverage_percent": 100.0 if expected == 0 else round((covered / expected) * 100, 6),
    }


def image_occurrence_coverage_report(state: ShowcaseState) -> dict[str, Any]:
    image_chunk_ids = {
        string_value(chunk.get("image_id"))
        for chunk in state.chunks
        if chunk.get("chunk_type") == "image"
    }
    missing_chunks = [
        string_value(image.get("image_id"))
        for image in state.images
        if string_value(image.get("image_id")) not in image_chunk_ids
    ]
    expected = len(state.images)
    covered = expected - len(missing_chunks)
    return {
        "schema_version": SHOWCASE_SCHEMA_VERSION,
        "unit": "image_occurrence",
        "expected": expected,
        "covered": covered,
        "missing_chunks": missing_chunks,
        "coverage_percent": 100.0 if expected == 0 else round((covered / expected) * 100, 6),
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
        string_value(image.get("title")),
        string_value(image.get("caption")),
        string_value(image.get("preceding_text")),
        string_value(image.get("following_text")),
    ]
    text = " | ".join(part for part in parts if part)
    return text or string_value(image.get("image_id"))


def section_by_id(state: ShowcaseState, section_id: str) -> dict[str, Any]:
    for section in state.sections:
        if section.get("section_id") == section_id:
            return section
    raise BankError(f"Internal section reference is missing: {section_id}")


def populate_image_contexts(state: ShowcaseState) -> None:
    blocks_by_section: dict[str, list[dict[str, Any]]] = {}
    for block in state.blocks:
        blocks_by_section.setdefault(string_value(block.get("section_id")), []).append(block)
    for blocks in blocks_by_section.values():
        blocks.sort(key=lambda row: int(row.get("block_index") or 0))
    for image in state.images:
        image_id = string_value(image.get("image_id"))
        section = section_by_id(state, string_value(image.get("section_id")))
        section_blocks = blocks_by_section.get(string_value(section.get("section_id")), [])
        image_block_index = next(
            (
                index
                for index, block in enumerate(section_blocks)
                if image_id in {string_value(value) for value in (block.get("image_ids") or [])}
            ),
            None,
        )
        preceding = (
            nearest_text_block(section_blocks[:image_block_index], reverse=True)
            if image_block_index is not None
            else None
        )
        following = (
            nearest_text_block(section_blocks[image_block_index + 1 :], reverse=False)
            if image_block_index is not None
            else None
        )
        image["section_title"] = section.get("title")
        image["preceding_block_id"] = preceding.get("block_id") if preceding else None
        image["preceding_text"] = string_value(preceding.get("text")) if preceding else ""
        image["following_block_id"] = following.get("block_id") if following else None
        image["following_text"] = string_value(following.get("text")) if following else ""
        image["caption"] = image_caption(section_blocks, image_block_index)


def nearest_text_block(
    blocks: list[dict[str, Any]],
    *,
    reverse: bool,
) -> dict[str, Any] | None:
    iterable = reversed(blocks) if reverse else iter(blocks)
    for block in iterable:
        if normalize_text(string_value(block.get("text"))):
            return block
    return None


def image_caption(blocks: list[dict[str, Any]], image_block_index: int | None) -> str | None:
    if image_block_index is None:
        return None
    candidates = []
    if image_block_index > 0:
        candidates.append(blocks[image_block_index - 1])
    if image_block_index + 1 < len(blocks):
        candidates.append(blocks[image_block_index + 1])
    for block in candidates:
        text = normalize_text(string_value(block.get("text")))
        if text.casefold().startswith(("рис", "рисунок", "figure")):
            return text
    return None


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


def list_type_for_element(element: Tag) -> str | None:
    if element.name.lower() != "li":
        return None
    parent = element.parent
    if isinstance(parent, Tag) and parent.name.lower() in {"ul", "ol"}:
        return "ordered" if parent.name.lower() == "ol" else "unordered"
    return None


def list_id_for_element(element: Tag) -> str | None:
    if element.name.lower() != "li":
        return None
    parent = element.parent
    if not isinstance(parent, Tag) or parent.name.lower() not in {"ul", "ol"}:
        return None
    section_id = string_value(element.get("data-section-id"))
    marker = sha256_text(str(parent))[:12]
    return f"{section_id}:list#{marker}" if section_id else f"list#{marker}"


def list_item_index(element: Tag) -> int | None:
    if element.name.lower() != "li":
        return None
    parent = element.parent
    if not isinstance(parent, Tag):
        return None
    siblings = [
        child
        for child in parent.find_all("li", recursive=False)
        if isinstance(child, Tag)
    ]
    for index, sibling in enumerate(siblings):
        if sibling is element:
            return index
    return None


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


def resolve_relative_image(source: RawDocumentSource, src: str) -> Path | None:
    stripped = src.strip()
    if not stripped:
        return None
    path = Path(stripped)
    if path.is_absolute():
        return None
    candidates: list[tuple[Path, Path]] = [(source.raw_json.parent, source.raw_json.parent / path)]
    if source.source_root is not None:
        candidates.append((source.source_root, source.source_root / path))
    for root, candidate in candidates:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError:
            continue
        if resolved_candidate.is_file():
            return resolved_candidate
    return None


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


def detect_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if re.match(rb"^\s*(?:<\?xml[^>]*>\s*)?<svg[\s>]", content[:512], re.IGNORECASE):
        return "image/svg+xml"
    return None


def is_supported_raster_mime(mime_type: str | None) -> bool:
    return mime_type in {"image/png", "image/jpeg", "image/gif", "image/webp"}


def image_dimensions(content: bytes, mime_type: str | None) -> tuple[int | None, int | None]:
    if mime_type == "image/png" and len(content) >= 24 and content.startswith(b"\x89PNG\r\n\x1a\n"):
        width = int.from_bytes(content[16:20], "big")
        height = int.from_bytes(content[20:24], "big")
        return width, height
    if mime_type in {"image/jpeg", "image/jpg"} and content.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height = int.from_bytes(content[index + 5 : index + 7], "big")
                width = int.from_bytes(content[index + 7 : index + 9], "big")
                return width, height
            segment_length = int.from_bytes(content[index + 2 : index + 4], "big")
            if segment_length < 2:
                break
            index += 2 + segment_length
    return None, None


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
        "canonical/table-placements.jsonl",
        "canonical/images.jsonl",
        "canonical/assets.jsonl",
        "canonical/recommendations.jsonl",
        "canonical/references.jsonl",
        "canonical/chunks.jsonl",
        "canonical/citation-index.jsonl",
        "canonical/coverage-map.json",
        "backend/documents.jsonl",
        "frontend/document.json",
        "ml/embedding-input.jsonl",
        "reports/text-index-coverage.json",
        "reports/table-index-coverage.json",
        "reports/image-occurrence-coverage.json",
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
