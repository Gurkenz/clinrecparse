from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import sync_catalog, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    BankError,
    BankRecordFilter,
    accepted_catalog_path,
    bank_active_root,
    bank_legacy_root,
    bank_plans_root,
    bank_staging_root,
    bank_state_root,
    catalog_record_for_bank,
    compact_timestamp,
    load_catalog_records,
    normalize_title,
    read_json_file,
    read_jsonl,
    relative_to_data_root,
    source_record_id_from_catalog,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.bank.current import download_current_documents
from clinrec.bank.qa import run_bank_qa
from clinrec.bank.references import enrich_developers, update_references
from clinrec.config import Settings

METADATA_FIELDS = ("name", "mkbs", "developers", "age_category", "publish_date")


@dataclass(frozen=True)
class BankPlanSummary:
    plan_path: Path
    markdown_path: Path
    total_actions: int
    requires_manual_review: bool


@dataclass(frozen=True)
class BankApplySummary:
    applied: int
    moved_to_legacy: int
    reactivated: int
    plan_path: Path


def accepted_catalog_records_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "accepted-catalog-records.jsonl"


def read_accepted_catalog_records(settings: Settings) -> list[dict[str, Any]]:
    return read_jsonl(accepted_catalog_records_path(settings))


def accept_current_catalog(
    settings: Settings,
    *,
    timestamp: str | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    records = [catalog_record_for_bank(row) for row in load_catalog_records(settings, active=True)]
    code_versions = [string_value(row.get("code_version")) for row in records]
    if not records:
        raise BankError("Refusing to accept an empty active catalog.")
    if len(code_versions) != len(set(code_versions)):
        raise BankError("Refusing to accept active catalog with duplicate CodeVersion.")
    identity_conflicts = identity_conflicts_in_catalog(records)
    if identity_conflicts:
        raise BankError("Refusing to accept active catalog with identity conflicts.")

    current_timestamp = timestamp or compact_timestamp()
    records_path = accepted_catalog_records_path(settings)
    write_jsonl(records_path, records)
    sha256 = sha256_rows(records)
    accepted = {
        "timestamp": current_timestamp,
        "snapshot_path": relative_to_data_root(settings, snapshot_path)
        if snapshot_path is not None
        else None,
        "index_path": relative_to_data_root(
            settings,
            settings.paths.indexes / "catalog-active.jsonl",
        ),
        "records_path": relative_to_data_root(settings, records_path),
        "total_records": len(records),
        "unique_code_versions": len(set(code_versions)),
        "sha256": sha256,
        "accepted_at": utc_now(),
    }
    write_json(accepted_catalog_path(settings), accepted)
    return accepted


def build_update_plan(
    settings: Settings,
    *,
    timestamp: str | None = None,
    allow_large_delta: bool = False,
) -> BankPlanSummary:
    current_records = [
        catalog_record_for_bank(row) for row in load_catalog_records(settings, active=True)
    ]
    previous_records = read_accepted_catalog_records(settings)
    plan_timestamp = timestamp or compact_timestamp()
    plan_dir = bank_plans_root(settings) / plan_timestamp
    plan_path = plan_dir / "plan.json"
    markdown_path = plan_dir / "plan.md"
    plan = reconcile_catalogs(settings, previous_records, current_records)
    plan["timestamp"] = plan_timestamp
    plan["requires_manual_review"] = requires_manual_review(settings, plan, allow_large_delta)
    plan["warnings"] = warnings_for_plan(settings, plan, allow_large_delta)
    write_json(plan_path, plan)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_plan_markdown(plan), encoding="utf-8")
    return BankPlanSummary(
        plan_path=plan_path,
        markdown_path=markdown_path,
        total_actions=sum(len(plan[key]) for key in action_keys()),
        requires_manual_review=bool(plan["requires_manual_review"]),
    )


def reconcile_catalogs(
    settings: Settings,
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_by_cv = by_code_version(previous_records)
    current_by_cv = by_code_version(current_records)
    local_active = local_code_versions(bank_active_root(settings))
    local_legacy = local_code_versions(bank_legacy_root(settings))
    unchanged: list[str] = []
    metadata_changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    reactivated: list[str] = []
    identity_conflicts = identity_conflicts_in_catalog(current_records)

    for code_version, current in sorted(current_by_cv.items()):
        previous = previous_by_cv.get(code_version)
        if previous is None:
            added.append(code_version)
            if code_version in local_legacy:
                reactivated.append(code_version)
            continue
        if same_identity(previous, current):
            if metadata_changed_between(previous, current):
                metadata_changed.append(code_version)
            else:
                unchanged.append(code_version)
        else:
            identity_conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_source_record_id_changed",
                    "code_version": code_version,
                    "previous_source_record_id": source_record_id_from_catalog(previous),
                    "current_source_record_id": source_record_id_from_catalog(current),
                }
            )

    for code_version in sorted(set(previous_by_cv) - set(current_by_cv)):
        removed.append(code_version)

    expected = set(current_by_cv)
    missing_locally = sorted(expected - local_active - set(reactivated))
    unexpected_local = sorted(local_active - expected)
    return {
        "previous_total": len(previous_records),
        "current_total": len(current_records),
        "unchanged": unchanged,
        "added": added,
        "missing_locally": missing_locally,
        "metadata_changed": metadata_changed,
        "removed_from_catalog": removed,
        "removed": removed,
        "reactivated": reactivated,
        "identity_conflicts": identity_conflicts,
        "unexpected_local": unexpected_local,
        "replacement_candidates": replacement_candidates(
            previous_by_cv,
            current_by_cv,
            removed,
            added,
        ),
    }


def apply_update_plan(
    settings: Settings,
    plan_path: Path,
    *,
    allow_manual_review: bool = False,
    accept_catalog: bool = True,
) -> BankApplySummary:
    plan = read_json_file(plan_path)
    if not plan:
        raise BankError(f"Plan is missing or invalid: {plan_path}")
    if plan.get("requires_manual_review") and not allow_manual_review:
        raise BankError("Plan requires manual review before apply.")
    applied_documents = 0
    timestamp = string_value(plan.get("timestamp"))
    staging_root = bank_staging_root(settings) / timestamp if timestamp else None
    for code_version in sorted(
        set(plan.get("added") or []) | set(plan.get("missing_locally") or [])
    ):
        if staging_root is not None and move_staged_to_active(
            settings,
            str(code_version),
            staging_root,
        ):
            applied_documents += 1
    if staging_root is not None and staging_root.exists() and not any(staging_root.iterdir()):
        staging_root.rmdir()
    moved_to_legacy = 0
    reactivated = 0
    for code_version in plan.get("removed_from_catalog") or plan.get("removed") or []:
        if move_active_to_legacy(settings, str(code_version), plan):
            moved_to_legacy += 1
    for code_version in plan.get("reactivated") or []:
        if move_legacy_to_active(settings, str(code_version)):
            reactivated += 1
    if accept_catalog:
        accept_current_catalog(settings, timestamp=string_value(plan.get("timestamp")))
    return BankApplySummary(
        applied=applied_documents + moved_to_legacy + reactivated,
        moved_to_legacy=moved_to_legacy,
        reactivated=reactivated,
        plan_path=plan_path,
    )


def bank_bootstrap(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    force: bool = False,
) -> dict[str, Any]:
    catalog_summary = sync_catalog(settings, client)
    plan_summary = build_update_plan(settings, timestamp=catalog_summary.timestamp)
    download_summary = download_current_documents(
        settings,
        client,
        BankRecordFilter(all_records=True, force=force),
    )
    accepted = accept_current_catalog(
        settings,
        timestamp=catalog_summary.timestamp,
        snapshot_path=catalog_summary.active.snapshot_dir,
    )
    qa_summary = run_bank_qa(settings, BankRecordFilter(all_records=True))
    if qa_summary.fatal or qa_summary.errors:
        raise BankError("Bootstrap QA failed; catalog was not accepted.")
    reference_summary = update_references(settings, client)
    enrich_summary = enrich_developers(settings, BankRecordFilter(all_records=True))
    return {
        "catalog_active_records": catalog_summary.active.records,
        "plan": str(plan_summary.plan_path),
        "downloaded": download_summary.downloaded,
        "qa_fatal": qa_summary.fatal,
        "qa_errors": qa_summary.errors,
        "accepted": accepted,
        "references": str(reference_summary.report_path),
        "developer_enrichment_updated": enrich_summary.updated,
    }


def bank_update(
    settings: Settings,
    client: ClinrecApiClient,
    *,
    apply: bool = False,
    verify_existing: bool = False,
    allow_large_delta: bool = False,
) -> dict[str, Any]:
    catalog_summary = sync_catalog(settings, client)
    plan_summary = build_update_plan(
        settings,
        timestamp=catalog_summary.timestamp,
        allow_large_delta=allow_large_delta,
    )
    result: dict[str, Any] = {
        "plan": str(plan_summary.plan_path),
        "requires_manual_review": plan_summary.requires_manual_review,
    }
    if not apply:
        return result
    plan = read_json_file(plan_summary.plan_path)
    selected_for_staging = sorted(
        set(plan.get("added") or []) | set(plan.get("missing_locally") or [])
    )
    if selected_for_staging:
        download_current_documents(
            settings,
            client,
            BankRecordFilter(code_versions=selected_for_staging, force=True),
            destination_root=bank_staging_root(settings) / catalog_summary.timestamp,
        )
    if verify_existing and plan.get("unchanged"):
        download_current_documents(
            settings,
            client,
            BankRecordFilter(code_versions=sorted(plan.get("unchanged") or []), force=True),
        )
    apply_summary = apply_update_plan(
        settings,
        plan_summary.plan_path,
        accept_catalog=False,
    )
    reference_summary = update_references(settings, client)
    enrich_summary = enrich_developers(settings, BankRecordFilter(all_records=True))
    qa_summary = run_bank_qa(settings, BankRecordFilter(all_records=True))
    if not qa_summary.fatal and not qa_summary.errors:
        accept_current_catalog(
            settings,
            timestamp=catalog_summary.timestamp,
            snapshot_path=catalog_summary.active.snapshot_dir,
        )
    result.update(
        {
            "applied": apply_summary.applied,
            "moved_to_legacy": apply_summary.moved_to_legacy,
            "reactivated": apply_summary.reactivated,
            "references": str(reference_summary.report_path),
            "developer_enrichment_updated": enrich_summary.updated,
            "qa_fatal": qa_summary.fatal,
            "qa_errors": qa_summary.errors,
        }
    )
    return result


def move_active_to_legacy(settings: Settings, code_version: str, plan: dict[str, Any]) -> bool:
    source = bank_active_root(settings) / code_version
    target = bank_legacy_root(settings) / code_version
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    source.replace(target)
    write_json(
        target / "lifecycle.json",
        {
            "code_version": code_version,
            "first_seen_active_at": None,
            "last_seen_active_at": utc_now(),
            "removed_from_active_catalog_at": utc_now(),
            "removal_snapshot": plan.get("timestamp"),
            "replacement_status": replacement_status_for(code_version, plan),
            "replacement_candidates": (plan.get("replacement_candidates") or {}).get(
                code_version,
                [],
            ),
        },
    )
    return True


def move_staged_to_active(settings: Settings, code_version: str, staging_root: Path) -> bool:
    source = staging_root / code_version
    target = bank_active_root(settings) / code_version
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    source.replace(target)
    return True


def move_legacy_to_active(settings: Settings, code_version: str) -> bool:
    source = bank_legacy_root(settings) / code_version
    target = bank_active_root(settings) / code_version
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    source.replace(target)
    return True


def identity_conflicts_in_catalog(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_id: dict[int, set[str]] = {}
    by_cv: dict[str, set[int]] = {}
    for record in records:
        code_version = string_value(record.get("code_version"))
        source_record_id = source_record_id_from_catalog(record)
        code = record.get("code")
        version = record.get("version")
        if source_record_id is not None:
            by_id.setdefault(source_record_id, set()).add(code_version)
            by_cv.setdefault(code_version, set()).add(source_record_id)
        if code is not None and version is not None and code_version != f"{code}_{version}":
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_mismatch",
                    "code_version": code_version,
                    "expected": f"{code}_{version}",
                }
            )
    for source_record_id, code_versions in sorted(by_id.items()):
        if len(code_versions) > 1:
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "source_record_id_multiple_code_versions",
                    "source_record_id": source_record_id,
                    "code_versions": sorted(code_versions),
                }
            )
    for code_version, source_record_ids in sorted(by_cv.items()):
        if len(source_record_ids) > 1:
            conflicts.append(
                {
                    "kind": "identity_conflict",
                    "code": "code_version_multiple_source_record_ids",
                    "code_version": code_version,
                    "source_record_ids": sorted(source_record_ids),
                }
            )
    return conflicts


def replacement_candidates(
    previous_by_cv: dict[str, dict[str, Any]],
    current_by_cv: dict[str, dict[str, Any]],
    removed: list[str],
    added: list[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for removed_code_version in removed:
        removed_record = previous_by_cv.get(removed_code_version, {})
        candidates: list[dict[str, Any]] = []
        for added_code_version in added:
            added_record = current_by_cv.get(added_code_version, {})
            score = replacement_score(removed_record, added_record)
            if score > 0:
                candidates.append(
                    {
                        "code_version": added_code_version,
                        "score": score,
                        "status": "probable_replacement"
                        if score >= 4
                        else "manual_review_required",
                    }
                )
        if not candidates:
            result[removed_code_version] = [
                {"status": "no_replacement_candidate", "score": 0}
            ]
        else:
            best = sorted(candidates, key=lambda item: item["score"], reverse=True)
            if len(best) > 1 and best[0]["score"] == best[1]["score"]:
                best[0]["status"] = "ambiguous_replacement"
            result[removed_code_version] = best
    return result


def replacement_score(left: dict[str, Any], right: dict[str, Any]) -> int:
    score = 0
    if left.get("code") == right.get("code"):
        score += 1
    if normalize_title(left.get("name")) == normalize_title(right.get("name")):
        score += 2
    for field in ("age_category", "publish_date"):
        if left.get(field) and left.get(field) == right.get(field):
            score += 1
    if stable_json(left.get("mkbs")) == stable_json(right.get("mkbs")):
        score += 1
    if stable_json(left.get("developers")) == stable_json(right.get("developers")):
        score += 1
    return score


def requires_manual_review(
    settings: Settings,
    plan: dict[str, Any],
    allow_large_delta: bool,
) -> bool:
    return bool(warnings_for_plan(settings, plan, allow_large_delta))


def warnings_for_plan(
    settings: Settings,
    plan: dict[str, Any],
    allow_large_delta: bool,
) -> list[str]:
    warnings: list[str] = []
    previous_total = int(plan.get("previous_total") or 0)
    current_total = int(plan.get("current_total") or 0)
    removed = len(plan.get("removed_from_catalog") or [])
    identity_conflicts = len(plan.get("identity_conflicts") or [])
    if current_total == 0:
        warnings.append("catalog_change_requires_manual_review")
    if previous_total and current_total < previous_total and not allow_large_delta:
        drop_percent = ((previous_total - current_total) / previous_total) * 100
        if drop_percent > settings.bank.max_catalog_drop_percent:
            warnings.append("catalog_change_requires_manual_review")
    if previous_total and removed and not allow_large_delta:
        removed_percent = (removed / previous_total) * 100
        if removed_percent > settings.bank.max_catalog_drop_percent:
            warnings.append("catalog_change_requires_manual_review")
    if identity_conflicts > settings.bank.max_identity_conflicts:
        warnings.append("identity_conflict")
    if removed and settings.bank.require_manual_apply_on_removed:
        warnings.append("manual_review_required")
    return sorted(set(warnings))


def by_code_version(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        string_value(row.get("code_version")): row
        for row in records
        if row.get("code_version")
    }


def local_code_versions(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        source_record_id_from_catalog(left) == source_record_id_from_catalog(right)
        and string_value(left.get("code_version")) == string_value(right.get("code_version"))
    )


def metadata_changed_between(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in METADATA_FIELDS:
        left_value = (
            normalize_title(left.get(field))
            if field == "name"
            else stable_json(left.get(field))
        )
        right_value = (
            normalize_title(right.get(field))
            if field == "name"
            else stable_json(right.get(field))
        )
        if left_value != right_value:
            return True
    return False


def action_keys() -> tuple[str, ...]:
    return (
        "added",
        "missing_locally",
        "metadata_changed",
        "removed_from_catalog",
        "reactivated",
        "identity_conflicts",
        "unexpected_local",
    )


def render_plan_markdown(plan: dict[str, Any]) -> str:
    lines = ["# Bank update plan", ""]
    for key in ("previous_total", "current_total", *action_keys()):
        value = plan.get(key)
        lines.append(f"- {key}: {len(value) if isinstance(value, list) else value}")
    if plan.get("warnings"):
        lines.append(f"- warnings: {', '.join(plan['warnings'])}")
    return "\n".join(lines) + "\n"


def replacement_status_for(code_version: str, plan: dict[str, Any]) -> str:
    candidates = (plan.get("replacement_candidates") or {}).get(code_version) or []
    if not candidates:
        return "unresolved"
    return string_value(candidates[0].get("status")) or "unresolved"


def stable_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_rows(rows: list[dict[str, Any]]) -> str:
    import hashlib
    import json

    content = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
