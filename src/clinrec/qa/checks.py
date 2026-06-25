from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import split_code_version
from clinrec.api.document_download import read_manifest, sha256_bytes
from clinrec.config import Settings
from clinrec.models.external import QaIssue


class QaError(RuntimeError):
    pass


@dataclass(frozen=True)
class QaOptions:
    code_versions: list[str] | None = None
    code: int | None = None
    from_code: int | None = None
    to_code: int | None = None
    strict_pdf: bool = False
    timestamp: str | None = None


@dataclass(frozen=True)
class QaDocumentSummary:
    code_version: str
    document_dir: Path
    issues: int
    fatal: int
    errors: int
    warnings: int
    info: int


@dataclass(frozen=True)
class QaSummary:
    planned: int
    fatal: int
    errors: int
    warnings: int
    info: int
    report_path: Path
    documents: list[QaDocumentSummary]


def run_qa(settings: Settings, options: QaOptions) -> QaSummary:
    document_dirs = select_qa_document_dirs(settings, options)
    issues_by_doc: dict[str, list[QaIssue]] = {}
    summaries: list[QaDocumentSummary] = []
    for document_dir in document_dirs:
        issues = qa_one_document(document_dir, options)
        issues_by_doc[document_dir.name] = issues
        counts = severity_counts(issues)
        summaries.append(
            QaDocumentSummary(
                code_version=document_dir.name,
                document_dir=document_dir,
                issues=len(issues),
                fatal=counts["fatal"],
                errors=counts["error"],
                warnings=counts["warning"],
                info=counts["info"],
            )
        )

    totals = severity_counts([issue for issues in issues_by_doc.values() for issue in issues])
    report_path = settings.paths.reports / "qa-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "planned": len(document_dirs),
                "totals": totals,
                "documents": {
                    code_version: [issue.model_dump(mode="json") for issue in issues]
                    for code_version, issues in issues_by_doc.items()
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return QaSummary(
        planned=len(document_dirs),
        fatal=totals["fatal"],
        errors=totals["error"],
        warnings=totals["warning"],
        info=totals["info"],
        report_path=report_path,
        documents=summaries,
    )


def select_qa_document_dirs(settings: Settings, options: QaOptions) -> list[Path]:
    root = settings.paths.documents
    if options.code_versions:
        dirs: list[Path] = []
        for code_version in options.code_versions:
            code, _version = split_code_version(code_version)
            if code is not None:
                dirs.append(root / str(code) / code_version)
        return dirs
    if not root.exists():
        return []
    candidates = [
        path
        for code_dir in sorted(root.iterdir(), key=lambda item: item.name)
        if code_dir.is_dir()
        for path in sorted(code_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir()
    ]
    return [path for path in candidates if qa_matches_filters(path.name, options)]


def qa_one_document(document_dir: Path, options: QaOptions) -> list[QaIssue]:
    issues: list[QaIssue] = []
    manifest = read_manifest(document_dir / "manifest.json")
    source_path = document_dir / "source" / "getclinrec.json"
    payload = read_json_file(source_path, issues, "source_json")
    if payload is None:
        return issues

    validate_source_payload(document_dir, payload, issues)
    validate_manifest(document_dir, manifest, source_path, issues)
    validate_parsed_outputs(document_dir, issues)
    validate_pdf_status(document_dir, manifest, options, issues)
    return issues


def read_json_file(path: Path, issues: list[QaIssue], label: str) -> Any | None:
    if not path.exists():
        issues.append(issue("fatal", f"missing_{label}", f"{path.name} is missing", path=path))
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(issue("fatal", f"invalid_{label}", str(exc), path=path))
        return None


def validate_source_payload(document_dir: Path, payload: Any, issues: list[QaIssue]) -> None:
    if not isinstance(payload, dict):
        issues.append(issue("fatal", "source_not_object", "source JSON root is not an object"))
        return
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    code_version = first_non_empty(payload.get("id"), obj.get("id"), obj.get("code_version"))
    if code_version and code_version != document_dir.name:
        issues.append(
            issue(
                "fatal",
                "code_version_path_mismatch",
                "source code_version does not match document directory",
                expected=document_dir.name,
                actual=code_version,
            )
        )
    code, version = split_code_version(document_dir.name)
    source_code = to_int(first_non_empty(payload.get("code"), obj.get("code")))
    source_version = to_int(
        first_non_empty(payload.get("version"), payload.get("ver"), obj.get("version"))
    )
    if code is not None and source_code is not None and code != source_code:
        issues.append(issue("error", "code_mismatch", "source code does not match path"))
    if version is not None and source_version is not None and version != source_version:
        issues.append(issue("error", "version_mismatch", "source version does not match path"))
    sections = obj.get("sections")
    if not isinstance(sections, list) or not sections:
        issues.append(issue("fatal", "empty_obj_sections", "source obj.sections is empty"))


def validate_manifest(
    document_dir: Path,
    manifest: dict[str, Any],
    source_path: Path,
    issues: list[QaIssue],
) -> None:
    if not manifest:
        issues.append(issue("warning", "missing_manifest", "manifest.json is missing or invalid"))
        return
    json_info = manifest.get("json") if isinstance(manifest.get("json"), dict) else {}
    if source_path.exists() and json_info:
        content = source_path.read_bytes()
        if json_info.get("sha256") and json_info.get("sha256") != sha256_bytes(content):
            issues.append(issue("error", "manifest_json_sha_mismatch", "JSON SHA does not match"))
        if json_info.get("size") and json_info.get("size") != len(content):
            issues.append(issue("error", "manifest_json_size_mismatch", "JSON size does not match"))
    if manifest.get("code_version") and manifest.get("code_version") != document_dir.name:
        issues.append(issue("error", "manifest_code_version_mismatch", "manifest path mismatch"))


def validate_parsed_outputs(document_dir: Path, issues: list[QaIssue]) -> None:
    parsed_dir = document_dir / "parsed"
    document_json = parsed_dir / "document.json"
    markdown = parsed_dir / "content.md"
    chunks_path = parsed_dir / "search_chunks.jsonl"
    parsed = read_json_file(document_json, issues, "parsed_document")
    if parsed is None:
        return
    if not markdown.exists():
        issues.append(issue("error", "missing_content_md", "parsed/content.md is missing"))
    if not chunks_path.exists():
        issues.append(issue("error", "missing_search_chunks", "search_chunks.jsonl is missing"))
    if markdown.exists():
        text = markdown.read_text(encoding="utf-8")
        if "data:" in text or "base64" in text:
            issues.append(
                issue("error", "markdown_contains_base64", "Markdown contains embedded data")
            )
        if re.search(r"[A-Za-z]:[\\/]", text):
            issues.append(
                issue(
                    "error",
                    "markdown_contains_absolute_path",
                    "Markdown has local absolute path",
                )
            )
        if re.search(r"\son\w+\s*=", text, re.IGNORECASE) or "javascript:" in text.lower():
            issues.append(
                issue("error", "markdown_contains_unsafe_html", "Markdown has unsafe HTML")
            )
    validate_parsed_payload(parsed, issues)
    validate_chunks(chunks_path, issues)


def validate_parsed_payload(parsed: Any, issues: list[QaIssue]) -> None:
    if not isinstance(parsed, dict):
        issues.append(issue("fatal", "parsed_not_object", "parsed document root is not an object"))
        return
    tables = parsed.get("tables") or []
    for table in tables:
        if isinstance(table, dict) and table.get("rows") and not table.get("grid"):
            issues.append(
                issue("error", "missing_table_grid", "table has rows but no logical grid")
            )
    recommendations = parsed.get("recommendations") or []
    section_ids = {
        str(section.get("section_id"))
        for section in parsed.get("sections", [])
        if isinstance(section, dict)
    }
    for recommendation in recommendations:
        if (
            isinstance(recommendation, dict)
            and str(recommendation.get("section_id")) not in section_ids
        ):
            issues.append(
                issue(
                    "error",
                    "recommendation_section_missing",
                    "recommendation section is missing",
                )
            )
        if (
            isinstance(recommendation, dict)
            and not recommendation.get("uur")
            and not recommendation.get("udd")
        ):
            issues.append(
                issue("warning", "recommendation_missing_grades", "recommendation has no UUR/UDD")
            )


def validate_chunks(path: Path, issues: list[QaIssue]) -> None:
    if not path.exists():
        return
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(issue("error", "invalid_chunk_json", str(exc), line=line_number))
            continue
        chunk_id = row.get("chunk_id") or row.get("id")
        if not chunk_id:
            issues.append(issue("error", "missing_chunk_id", "chunk has no id", line=line_number))
            continue
        if chunk_id in seen:
            issues.append(
                issue("error", "duplicate_chunk_id", "chunk id is duplicated", chunk_id=chunk_id)
            )
        seen.add(str(chunk_id))
        if len(str(row.get("text") or "")) > 4000:
            issues.append(
                issue("warning", "chunk_too_large", "chunk text exceeds limit", chunk_id=chunk_id)
            )


def validate_pdf_status(
    document_dir: Path,
    manifest: dict[str, Any],
    options: QaOptions,
    issues: list[QaIssue],
) -> None:
    pdf_value = manifest.get("pdf")
    pdf_info: dict[str, Any] = pdf_value if isinstance(pdf_value, dict) else {}
    status = pdf_info.get("status")
    if status == "not_requested" or not status:
        severity = "error" if options.strict_pdf else "info"
        code = "missing_pdf_control_source" if options.strict_pdf else "pdf_not_requested"
        issues.append(issue(severity, code, "PDF control source is not available"))
        return
    pdf_path = document_dir / "source" / "official.pdf"
    if status in {"downloaded", "already_valid"} and not pdf_path.exists():
        issues.append(issue("error", "manifest_pdf_missing", "manifest points to missing PDF"))


def severity_counts(issues: list[QaIssue]) -> dict[str, int]:
    counts = {"fatal": 0, "error": 0, "warning": 0, "info": 0}
    for item in issues:
        if item.severity in counts:
            counts[item.severity] += 1
    return counts


def issue(severity: str, code: str, message: str, **context: Any) -> QaIssue:
    safe_context = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in context.items()
    }
    return QaIssue(severity=severity, code=code, message=message, context=safe_context)


def qa_matches_filters(code_version: str, options: QaOptions) -> bool:
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


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip():
            return value
    return None


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
