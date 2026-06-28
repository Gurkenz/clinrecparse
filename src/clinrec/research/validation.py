from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import (
    minimal_validate_raw_document,
    read_json_file,
    sha256_bytes,
    string_value,
)
from clinrec.research.catalog import read_all_statuses_catalog
from clinrec.research.migration import research_layout
from clinrec.research.reports import reports_root, write_json
from clinrec.research.sections import (
    iter_document_paths,
    load_payload,
    raw_sections,
    section_id_for,
)


@dataclass(frozen=True)
class ValidationSummary:
    input: Path
    valid: bool
    errors: int
    warnings: int
    report_json: Path
    report_markdown: Path


def validate_corpus(corpus_root: Path) -> ValidationSummary:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    current_count = 0
    previous_count = 0
    raw_hashes: dict[str, list[str]] = {}

    for kind, raw_path, current_code_version in iter_document_paths(corpus_root):
        if kind == "current":
            current_count += 1
        else:
            previous_count += 1
        relative = raw_path.relative_to(corpus_root).as_posix()
        code_version = raw_path.parent.name
        raw_bytes = raw_path.read_bytes()
        raw_sha = sha256_bytes(raw_bytes)
        raw_hashes.setdefault(raw_sha, []).append(relative)
        manifest_path = raw_path.parent / "manifest.json"
        catalog_path = raw_path.parent / "catalog-record.json"
        if not manifest_path.exists():
            errors.append(issue(relative, "missing_manifest"))
            continue
        if not catalog_path.exists():
            errors.append(issue(relative, "missing_catalog_record"))
        manifest = read_json_file(manifest_path)
        if manifest.get("sha256") != raw_sha:
            errors.append(issue(relative, "manifest_sha_mismatch"))
        if int(manifest.get("size") or -1) != len(raw_bytes):
            errors.append(issue(relative, "manifest_size_mismatch"))
        if manifest.get("validation") != "valid":
            errors.append(issue(relative, "manifest_not_valid"))
        info, validation_errors = minimal_validate_raw_document(
            raw_bytes,
            expected_code_version=code_version,
        )
        if info is None:
            errors.append(issue(relative, "invalid_raw_json", details=validation_errors))
            continue
        payload = load_payload(raw_path)
        if raw_path.parent.name != string_value(payload.get("id")):
            errors.append(issue(relative, "folder_name_not_json_id"))
        if string_value(payload.get("id")) != code_version:
            errors.append(issue(relative, "json_id_not_code_version"))
        if f"{info.code}_{info.version}" != code_version:
            errors.append(issue(relative, "code_version_fields_mismatch"))
        if kind == "previous" and current_code_version is not None:
            validate_previous_relation(
                current_code_version,
                code_version,
                relative,
                errors,
                warnings,
            )
        if manifest.get("db_id_state") == "mismatch":
            warnings.append(issue(relative, "catalog_db_id_mismatch"))
        unknown_keys = sorted(set(payload) - known_top_level_keys())
        if unknown_keys:
            warnings.append(issue(relative, "unknown_top_level_keys", details=unknown_keys))
        unknown_sections = [
            section_id_for(section)
            for section in raw_sections(payload)
            if isinstance(section, dict) and not known_section_id(section_id_for(section))
        ]
        if unknown_sections:
            warnings.append(
                issue(
                    relative,
                    "unknown_section_ids",
                    details=sorted(set(unknown_sections)),
                )
            )

    duplicate_raw = [
        {"sha256": sha, "paths": paths}
        for sha, paths in sorted(raw_hashes.items())
        if len(paths) > 1
    ]
    for duplicate in duplicate_raw:
        warnings.append(issue("corpus", "duplicate_raw_bytes", details=duplicate))

    catalog_rows = read_all_statuses_catalog(corpus_root)
    malformed = sum(1 for row in catalog_rows if not string_value(row.get("code_version")))
    layout = research_layout(corpus_root)
    report = {
        "input": corpus_root.as_posix(),
        "layout": "legacy-compatible" if layout.used_legacy_compat else "previous",
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "valid_current": current_count,
            "valid_previous": previous_count,
            "raw_json_files": current_count + previous_count,
            "raw_hash_mismatches": sum(
                1 for row in errors if row["code"] == "manifest_sha_mismatch"
            ),
            "identity_mismatches": sum(
                1 for row in warnings if row["code"] == "catalog_db_id_mismatch"
            ),
            "all_statuses_records": len(catalog_rows),
            "blank_code_versions": malformed,
            "duplicate_raw_files": len(duplicate_raw),
            "section_counts": dict(section_count_distribution(corpus_root)),
        },
    }
    root = reports_root(corpus_root)
    report_json = root / "validation.json"
    report_markdown = root / "validation.md"
    write_json(report_json, report)
    report_markdown.write_text(render_validation_markdown(report), encoding="utf-8")
    return ValidationSummary(
        input=corpus_root,
        valid=not errors,
        errors=len(errors),
        warnings=len(warnings),
        report_json=report_json,
        report_markdown=report_markdown,
    )


def validate_previous_relation(
    current_code_version: str,
    previous_code_version: str,
    relative: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    try:
        current_code, current_version = split_code_version(current_code_version)
        previous_code, previous_version = split_code_version(previous_code_version)
    except ValueError:
        errors.append(issue(relative, "invalid_previous_code_version"))
        return
    if current_code != previous_code:
        errors.append(issue(relative, "previous_code_mismatch"))
    if current_version - previous_version != 1:
        warnings.append(issue(relative, "previous_version_not_version_minus_one"))


def split_code_version(code_version: str) -> tuple[int, int]:
    code_text, version_text = code_version.split("_", maxsplit=1)
    return int(code_text), int(version_text)


def issue(path: str, code: str, *, details: Any = None) -> dict[str, Any]:
    return {"path": path, "code": code, "details": details}


def known_top_level_keys() -> set[str]:
    return {
        "id",
        "db_id",
        "code",
        "version",
        "name",
        "title",
        "status",
        "adult",
        "child",
        "mkbs",
        "proff_associations",
        "age_category",
        "obj",
    }


def known_section_id(section_id: str) -> bool:
    return bool(
        section_id in {"doc_title", "doc_whole"}
        or section_id.startswith("doc_")
        or section_id.startswith("section_")
    )


def section_count_distribution(corpus_root: Path) -> Counter[str]:
    counter: Counter[str] = Counter()
    for _, raw_path, _ in iter_document_paths(corpus_root):
        payload = load_payload(raw_path)
        section_count = len(
            [section for section in raw_sections(payload) if isinstance(section, dict)]
        )
        counter[str(section_count)] += 1
    return counter


def render_validation_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Corpus validation",
        "",
        f"- valid: {report['valid']}",
        f"- current documents: {summary['valid_current']}",
        f"- previous documents: {summary['valid_previous']}",
        f"- raw hash mismatches: {summary['raw_hash_mismatches']}",
        f"- warnings: {len(report['warnings'])}",
        f"- errors: {len(report['errors'])}",
        "",
        "## Warning codes",
    ]
    for code, count in sorted(Counter(row["code"] for row in report["warnings"]).items()):
        lines.append(f"- {code}: {count}")
    lines.append("")
    lines.append("## Error codes")
    for code, count in sorted(Counter(row["code"] for row in report["errors"]).items()):
        lines.append(f"- {code}: {count}")
    return "\n".join(lines) + "\n"
