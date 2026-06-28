from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import atomic_write_json, read_json_file, sha256_file, utc_now
from clinrec.research.catalog import CatalogProfile, write_catalog_indexes
from clinrec.research.migration import research_layout
from clinrec.research.pairs import write_pair_reports
from clinrec.research.reports import read_jsonl, reports_root, write_csv
from clinrec.research.sections import ProfileArtifacts, profile_sections


@dataclass(frozen=True)
class OfflineProfileSummary:
    input: Path
    raw_files: int
    raw_hash_set_before: set[str]
    raw_hash_set_after: set[str]
    raw_hashes_by_path_before: dict[str, str]
    raw_hashes_by_path_after: dict[str, str]
    raw_hashes_unchanged: bool
    catalog: CatalogProfile
    documents: int
    sections: int
    pairs: int
    findings_path: Path


def profile_corpus_offline(
    corpus_root: Path,
    *,
    rebuild_reports: bool = True,
) -> OfflineProfileSummary:
    _ = rebuild_reports
    before_by_path = raw_hashes_by_path(corpus_root)
    catalog = write_catalog_indexes(corpus_root)
    artifacts = profile_sections(corpus_root)
    pair_rows = write_pair_reports(corpus_root)
    write_previous_attempts_report(corpus_root)
    update_corpus_metadata(corpus_root, catalog, artifacts, pair_count=len(pair_rows))
    findings_path = write_research_findings(
        corpus_root,
        catalog,
        artifacts,
        pair_count=len(pair_rows),
    )
    after_by_path = raw_hashes_by_path(corpus_root)
    return OfflineProfileSummary(
        input=corpus_root,
        raw_files=count_raw_files(corpus_root),
        raw_hash_set_before=set(before_by_path.values()),
        raw_hash_set_after=set(after_by_path.values()),
        raw_hashes_by_path_before=before_by_path,
        raw_hashes_by_path_after=after_by_path,
        raw_hashes_unchanged=before_by_path == after_by_path,
        catalog=catalog,
        documents=len(artifacts.documents),
        sections=len(artifacts.sections),
        pairs=len(pair_rows),
        findings_path=findings_path,
    )


def raw_hash_set(corpus_root: Path) -> set[str]:
    return {
        sha256_file(path)
        for path in sorted(corpus_root.rglob("getclinrec.json"))
        if path.is_file()
    }


def raw_hashes_by_path(corpus_root: Path) -> dict[str, str]:
    return {
        path.relative_to(corpus_root).as_posix(): sha256_file(path)
        for path in sorted(corpus_root.rglob("getclinrec.json"))
        if path.is_file()
    }


def count_raw_files(corpus_root: Path) -> int:
    return sum(1 for path in corpus_root.rglob("getclinrec.json") if path.is_file())


def update_corpus_metadata(
    corpus_root: Path,
    catalog: CatalogProfile,
    artifacts: ProfileArtifacts,
    *,
    pair_count: int,
) -> None:
    path = corpus_root / "corpus.json"
    payload: dict[str, Any] = read_json_file(path)
    selection = read_json_file(corpus_root / "selection.json")
    final_selected = selection.get("final_selected") if isinstance(selection, dict) else []
    initially_selected = selection.get("initially_selected") if isinstance(selection, dict) else []
    replacements = selection.get("replacements") if isinstance(selection, dict) else []
    forced_failures = selection.get("forced_failures") if isinstance(selection, dict) else []
    previous_docs = [row for row in artifacts.documents if row["document_kind"] == "previous"]
    current_docs = [row for row in artifacts.documents if row["document_kind"] == "current"]
    initial_selection_count = len(initially_selected) if isinstance(initially_selected, list) else 0
    final_selection_count = len(final_selected) if isinstance(final_selected, list) else 0
    replacement_count = len(replacements) if isinstance(replacements, list) else 0
    forced_failure_count = len(forced_failures) if isinstance(forced_failures, list) else 0
    payload.update(
        {
            "schema_version": payload.get("schema_version") or "1.0",
            "layout_version": "2.0" if (corpus_root / "previous").exists() else "1.0",
            "catalog_active_total": catalog.active_records,
            "all_statuses_catalog_total": catalog.all_statuses_records,
            "initial_selection_count": initial_selection_count,
            "final_selection_count": final_selection_count,
            "valid_current_count": len(current_docs),
            "valid_previous_count": len(previous_docs),
            "valid_legacy_count": len(previous_docs),
            "replacement_count": replacement_count,
            "forced_failure_count": forced_failure_count,
            "total_documents": len(artifacts.documents),
            "total_sections": len(artifacts.sections),
            "profiled_pair_count": pair_count,
            "updated_at": utc_now(),
        }
    )
    for legacy_key, previous_key in (
        ("legacy_target", "previous_target"),
        ("legacy_minimum", "previous_minimum"),
        ("legacy_attempt_limit", "previous_attempt_limit"),
    ):
        if legacy_key in payload and previous_key not in payload:
            payload[previous_key] = payload[legacy_key]
    atomic_write_json(path, payload)


def write_previous_attempts_report(corpus_root: Path) -> None:
    layout = research_layout(corpus_root)
    rows = read_jsonl(layout.previous_attempts_path)
    normalized = [
        {
            "current_code_version": row.get("current_code_version"),
            "previous_code_version": row.get("previous_code_version"),
            "result": row.get("result"),
            "http_status": row.get("http_status"),
            "attempted_at": row.get("attempted_at"),
            "error": row.get("error"),
        }
        for row in rows
    ]
    write_csv(
        reports_root(corpus_root) / "previous-attempts.csv",
        normalized,
        (
            "current_code_version",
            "previous_code_version",
            "result",
            "http_status",
            "attempted_at",
            "error",
        ),
    )


def write_research_findings(
    corpus_root: Path,
    catalog: CatalogProfile,
    artifacts: ProfileArtifacts,
    *,
    pair_count: int,
) -> Path:
    root = reports_root(corpus_root)
    table_summary = read_json_file(root / "table-summary.json")
    image_summary = read_json_file(root / "image-summary.json")
    doc_whole_summary = read_json_file(root / "doc-whole-summary.json")
    status_summary = read_json_file(root / "status-summary.json")
    current_docs = [row for row in artifacts.documents if row["document_kind"] == "current"]
    previous_docs = [row for row in artifacts.documents if row["document_kind"] == "previous"]
    lines = [
        "# Research findings",
        "",
        "## Corpus integrity",
        f"- Fact: Current documents profiled: {len(current_docs)}.",
        f"- Fact: Previous documents profiled: {len(previous_docs)}.",
        (
            "- Fact: Raw JSON files are preserved by offline profiling: "
            f"{count_raw_files(corpus_root)}."
        ),
        "",
        "## Catalog composition",
        f"- Fact: Active catalog rows: {catalog.active_records}.",
        f"- Fact: All-statuses catalog rows: {catalog.all_statuses_records}.",
        "",
        "## Catalog anomalies",
        f"- Fact: Duplicate CodeVersion groups: {catalog.duplicate_code_versions}.",
        f"- Fact: Malformed CodeVersion rows: {catalog.malformed_code_versions}.",
        "",
        "## Current document schema",
        "- Fact: Top-level key variants are listed in `top-level-keys.csv`.",
        "",
        "## Section schema",
        f"- Fact: Sections profiled: {len(artifacts.sections)}.",
        "",
        "## doc_title findings",
        f"- Fact: doc_title data items profiled: {len(artifacts.title_fields)}.",
        "",
        "## doc_whole duplication findings",
        f"- Fact: doc_whole present: {doc_whole_summary.get('doc_whole_present')}.",
        (
            "- Inference: likely duplicate doc_whole sections: "
            f"{doc_whole_summary.get('likely_duplicates')}."
        ),
        "",
        "## Raw status findings",
        f"- Fact: Current raw statuses: {status_summary.get('current')}.",
        f"- Fact: Previous raw statuses: {status_summary.get('previous')}.",
        "",
        "## db_id findings",
        "- Fact: db_id classifications are reported in `identities.csv`.",
        "",
        "## Current/previous pair findings",
        f"- Fact: Pairs profiled: {pair_count}.",
        "",
        "## Table findings",
        f"- Fact: Tables found: {table_summary.get('tables_total')}.",
        "",
        "## Image and asset findings",
        f"- Fact: Images found: {image_summary.get('images_total')}.",
        f"- Fact: Base64 images found: {image_summary.get('base64_images')}.",
        "",
        "## Observed stable fields",
        "- Inference: 31-section structure is stable only within this local 60-document corpus.",
        "",
        "## Observed unstable fields",
        (
            "- Inference: catalog status and previous-version active membership require "
            "larger-corpus confirmation."
        ),
        "",
        "## Parser implications",
        "- Inference: parse `doc_title.data` explicitly and keep unknown title items raw.",
        "",
        "## Diff implications",
        "- Inference: section ID/content/data hashes are the first safe diff foundation.",
        "",
        "## Lifecycle implications",
        "- Inference: lower version is previous, not necessarily inactive legacy.",
        "",
        "## Facts",
        "- All counts above come from generated local reports.",
        "",
        "## Inferences",
        "- Inferences are marked explicitly and should not be generalized beyond this corpus.",
        "",
        "## Open questions",
        "- Status meanings and predecessor relationships remain unconfirmed by this iteration.",
        "",
        "## Recommended next experiment",
        "- Build a content diff on top of section-level hash and table/image inventories.",
        "",
    ]
    path = root / "research-findings.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
