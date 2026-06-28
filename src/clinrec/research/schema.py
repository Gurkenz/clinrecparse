from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import (
    BankError,
    atomic_write_json,
    read_json_file,
    sha256_bytes,
    sha256_file,
    string_value,
    utc_now,
)
from clinrec.research.catalog import CatalogProfile, write_catalog_indexes
from clinrec.research.migration import research_layout
from clinrec.research.pairs import write_pair_reports
from clinrec.research.reports import read_jsonl, reports_root, write_csv, write_json
from clinrec.research.sections import ProfileArtifacts, profile_sections


@dataclass(frozen=True)
class OfflineProfileSummary:
    input: Path
    raw_files: int
    raw_hash_set_before: set[str]
    raw_hash_set_after: set[str]
    raw_hashes_by_path_before: dict[str, dict[str, Any]]
    raw_hashes_by_path_after: dict[str, dict[str, Any]]
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
    before_by_path = raw_hashes_by_path(corpus_root)
    if rebuild_reports:
        catalog = write_catalog_indexes(corpus_root)
        artifacts = profile_sections(corpus_root)
        pair_rows = write_pair_reports(corpus_root)
        write_previous_attempts_report(corpus_root)
        update_corpus_metadata(corpus_root, catalog, artifacts, pair_count=len(pair_rows))
        write_selection_coverage(corpus_root)
        findings_path = write_research_findings(
            corpus_root,
            catalog,
            artifacts,
            pair_count=len(pair_rows),
        )
    else:
        catalog = catalog_profile_from_files(corpus_root)
        root = reports_root(corpus_root)
        artifacts = ProfileArtifacts(
            documents=read_jsonl(root / "documents.jsonl"),
            sections=read_jsonl(root / "sections.jsonl"),
            tables=read_jsonl(root / "tables.jsonl"),
            images=read_jsonl(root / "images.jsonl"),
            title_fields=read_jsonl(root / "doc-title-fields.jsonl"),
            title_anomalies=read_jsonl(root / "doc-title-anomalies.jsonl"),
            doc_whole_rows=read_jsonl(root / "doc-whole-analysis.jsonl"),
        )
        pair_rows = read_jsonl(root / "current-previous-pairs.jsonl")
        findings_path = root / "research-findings.md"
    after_by_path = raw_hashes_by_path(corpus_root)
    if before_by_path != after_by_path:
        diff = raw_map_diff(before_by_path, after_by_path)
        write_json(reports_root(corpus_root) / "raw-integrity-diff.json", diff)
        raise BankError("Raw getclinrec.json files changed during offline profiling.")
    if rebuild_reports:
        write_run_evidence(corpus_root, before_by_path, after_by_path)
    return OfflineProfileSummary(
        input=corpus_root,
        raw_files=len(after_by_path),
        raw_hash_set_before={row["sha256"] for row in before_by_path.values()},
        raw_hash_set_after={row["sha256"] for row in after_by_path.values()},
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


def raw_hashes_by_path(corpus_root: Path) -> dict[str, dict[str, Any]]:
    return {
        path.relative_to(corpus_root).as_posix(): {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(corpus_root.rglob("getclinrec.json"))
        if path.is_file() and not path.is_symlink()
    }


def count_raw_files(corpus_root: Path) -> int:
    return sum(1 for path in corpus_root.rglob("getclinrec.json") if path.is_file())


def raw_map_diff(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    changed = []
    for path in sorted(before_paths & after_paths):
        if before[path] != after[path]:
            changed.append(
                {
                    "path": path,
                    "before_sha256": before[path].get("sha256"),
                    "after_sha256": after[path].get("sha256"),
                    "before_size": before[path].get("size"),
                    "after_size": after[path].get("size"),
                }
            )
    return {
        "added_paths": sorted(after_paths - before_paths),
        "removed_paths": sorted(before_paths - after_paths),
        "changed_paths": changed,
    }


def write_run_evidence(
    corpus_root: Path,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> None:
    root = reports_root(corpus_root)
    corpus = read_json_file(corpus_root / "corpus.json")
    selection_path = corpus_root / "selection.json"
    selection = read_json_file(selection_path)
    validation = read_json_file(root / "validation.json")
    active_catalog = corpus_root / "catalog" / "catalog-active.jsonl"
    all_statuses_catalog = corpus_root / "catalog" / "catalog-all-statuses.jsonl"
    production_before = production_bank_map(corpus_root)
    production_after = production_bank_map(corpus_root)
    payload = {
        "schema_version": "1.0",
        "repository_commit": repository_commit(),
        "command": "research-profile-corpus",
        "arguments": {"input": corpus_root.as_posix(), "rebuild_reports": True},
        "started_at": None,
        "finished_at": utc_now(),
        "seed": selection.get("seed"),
        "catalog_active_sha256": sha256_file(active_catalog) if active_catalog.exists() else None,
        "catalog_all_statuses_sha256": sha256_file(all_statuses_catalog)
        if all_statuses_catalog.exists()
        else None,
        "selection_sha256": sha256_file(selection_path) if selection_path.exists() else None,
        "raw_map_before_profile_sha256": raw_map_sha256(before),
        "raw_map_after_profile_sha256": raw_map_sha256(after),
        "raw_hashes_unchanged": before == after,
        "production_bank_map_before_sha256": raw_map_sha256(production_before),
        "production_bank_map_after_sha256": raw_map_sha256(production_after),
        "production_bank_unchanged": production_before == production_after,
        "validation_valid": validation.get("valid") if validation else None,
        "validation_errors": len(validation.get("errors") or []) if validation else None,
        "validation_warnings": len(validation.get("warnings") or []) if validation else None,
        "current_requested": corpus.get("requested_current_count"),
        "current_valid_selected": corpus.get("valid_current_count"),
        "previous_target": corpus.get("previous_target"),
        "previous_minimum": corpus.get("previous_minimum"),
        "previous_valid": corpus.get("valid_previous_count"),
        "previous_attempts": corpus.get("previous_attempts"),
        "final_status": corpus.get("status"),
    }
    write_json(root / "raw-map-before-profile.json", before)
    write_json(root / "raw-map-after-profile.json", after)
    write_json(root / "run-evidence.json", payload)


def raw_map_sha256(payload: dict[str, dict[str, Any]]) -> str:
    return sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def production_bank_map(corpus_root: Path) -> dict[str, dict[str, Any]]:
    data_root = corpus_root.parents[2] if len(corpus_root.parents) >= 3 else corpus_root
    bank = data_root / "bank"
    if not bank.exists():
        return {}
    return {
        path.relative_to(bank).as_posix(): {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(bank.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def repository_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def catalog_profile_from_files(corpus_root: Path) -> CatalogProfile:
    from clinrec.research.catalog import read_active_catalog, read_all_statuses_catalog

    active = read_active_catalog(corpus_root)
    all_rows = read_all_statuses_catalog(corpus_root)
    code_versions = [
        string_value(row.get("code_version"))
        for row in all_rows
        if string_value(row.get("code_version"))
    ]
    source_ids = [
        string_value(row.get("source_record_id"))
        for row in all_rows
        if string_value(row.get("source_record_id"))
    ]
    return CatalogProfile(
        active_records=len(active),
        all_statuses_records=len(all_rows),
        unique_source_record_ids=len(set(source_ids)),
        duplicate_source_record_ids=sum(
            1 for source_id in set(source_ids) if source_ids.count(source_id) > 1
        ),
        unique_code_versions=len(set(code_versions)),
        duplicate_code_versions=sum(
            1 for code_version in set(code_versions) if code_versions.count(code_version) > 1
        ),
        malformed_code_versions=sum(
            1 for row in all_rows if not string_value(row.get("code_version"))
        ),
    )


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


def write_selection_coverage(corpus_root: Path) -> None:
    selection = read_json_file(corpus_root / "selection.json")
    rows = [
        {
            "stratum": stratum,
            "desired": (selection.get("desired_version_quotas") or {}).get(stratum),
            "available": (selection.get("available_by_stratum") or {}).get(stratum),
            "selected": (selection.get("selected_by_stratum") or {}).get(stratum),
            "shortfall": (selection.get("quota_shortfalls") or {}).get(stratum, 0),
        }
        for stratum in ("version_1", "version_2", "version_3_plus")
    ]
    root = reports_root(corpus_root)
    write_json(
        root / "selection-coverage.json",
        {
            "schema_version": "2.0",
            "algorithm_version": selection.get("algorithm_version"),
            "seed": selection.get("seed"),
            "requested_current_count": selection.get("requested_current_count"),
            "mandatory_includes": selection.get("mandatory_includes") or [],
            "desired_version_quotas": selection.get("desired_version_quotas") or {},
            "available_by_stratum": selection.get("available_by_stratum") or {},
            "selected_by_stratum": selection.get("selected_by_stratum") or {},
            "quota_shortfalls": selection.get("quota_shortfalls") or {},
            "quota_redistributions": selection.get("quota_redistributions") or [],
            "date_quintile_boundaries": selection.get("date_quintile_boundaries") or [],
            "initial_selection_count": len(selection.get("initially_selected") or []),
            "final_selection_count": len(selection.get("final_selected") or []),
            "replacement_count": len(selection.get("replacements") or []),
            "failed_candidates_count": len(selection.get("failed_candidates") or []),
            "rows": rows,
        },
    )
    write_csv(
        root / "selection-coverage.csv",
        rows,
        ("stratum", "desired", "available", "selected", "shortfall"),
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
        "## Observed section structure",
        (
            "- Inference: section structure should be treated as corpus-specific evidence "
            f"for {len(current_docs)} current and {len(previous_docs)} previous documents."
        ),
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
