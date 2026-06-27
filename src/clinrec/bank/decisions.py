from __future__ import annotations

from pathlib import Path
from typing import Any

from clinrec.bank.common import BankError, read_json_file, sha256_file, string_value, utc_now
from clinrec.bank.reconcile_helpers import plan_actions

DECISIONS_SCHEMA_VERSION = "2.0"
ALLOWED_FINAL_ACTIONS = {
    "use_staged_candidate",
    "move_current_to_quarantine_and_use_staged",
    "move_orphan_to_quarantine",
    "abort_transaction",
}


def decisions_path_for_plan(plan_path: Path) -> Path:
    return plan_path.parent / "decisions.json"


def build_decision_template(
    plan: dict[str, Any],
    *,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    if decision == "move_to_quarantine":
        decision = "move_orphan_to_quarantine"
    if decision not in ALLOWED_FINAL_ACTIONS:
        raise BankError(f"Unsupported review decision: {decision}")
    by_code_version: dict[str, set[str]] = {}
    for item in required_decision_items(plan):
        by_code_version.setdefault(item["code_version"], set()).add(item["conflict_type"])
    rows = [
        {
            "code_version": code_version,
            "conflicts": sorted(conflicts),
            "final_action": decision,
            "reason": reason,
            "decided_at": utc_now(),
        }
        for code_version, conflicts in sorted(by_code_version.items())
    ]
    return {
        "schema_version": DECISIONS_SCHEMA_VERSION,
        "transaction_id": plan["transaction_id"],
        "plan_id": plan["plan_id"],
        "decisions": rows,
    }


def required_decision_items(plan: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for issue in plan_actions(plan, "identity_conflicts"):
        if isinstance(issue, dict):
            code_versions = issue.get("code_versions")
            if isinstance(code_versions, list):
                for code_version in code_versions:
                    items.append(
                        {
                            "code_version": string_value(code_version),
                            "conflict_type": string_value(issue.get("code")),
                        }
                    )
            else:
                items.append(
                    {
                        "code_version": string_value(issue.get("code_version")),
                        "conflict_type": string_value(issue.get("code")),
                    }
                )
    for code_version in plan_actions(plan, "orphaned_local"):
        items.append(
            {
                "code_version": string_value(code_version),
                "conflict_type": "orphaned_local",
            }
        )
    for code_version in plan_actions(plan, "silent_source_candidates"):
        items.append(
            {
                "code_version": string_value(code_version),
                "conflict_type": "silent_source_change",
            }
        )
    for item in plan_actions(plan, "raw_identity_conflicts"):
        if isinstance(item, dict):
            items.append(
                {
                    "code_version": string_value(item.get("code_version")),
                    "conflict_type": "raw_identity_conflict",
                }
            )
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        code_version = item["code_version"]
        conflict_type = item["conflict_type"]
        if not code_version:
            continue
        unique[(code_version, conflict_type)] = item
    return [unique[key] for key in sorted(unique)]


def verify_decisions(plan_path: Path, plan: dict[str, Any]) -> dict[str, Any] | None:
    required = required_decision_items(plan)
    if not required:
        return None
    path = decisions_path_for_plan(plan_path)
    payload = read_json_file(path)
    if not payload:
        raise BankError("Review decisions are required before apply.")
    if payload.get("schema_version") != DECISIONS_SCHEMA_VERSION:
        raise BankError("Review decisions schema_version is invalid.")
    if payload.get("transaction_id") != plan.get("transaction_id"):
        raise BankError("Review decisions transaction_id mismatch.")
    if payload.get("plan_id") != plan.get("plan_id"):
        raise BankError("Review decisions plan_id mismatch.")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise BankError("Review decisions must contain a decisions list.")
    required_by_code_version: dict[str, set[str]] = {}
    for item in required:
        required_by_code_version.setdefault(item["code_version"], set()).add(
            item["conflict_type"]
        )
    by_code_version: dict[str, dict[str, Any]] = {}
    for row in decisions:
        if not isinstance(row, dict):
            raise BankError("Review decision row is invalid.")
        if "conflict_type" in row or "decision" in row:
            raise BankError("Review decisions schema 1.0 is no longer applicable.")
        final_action = row.get("final_action")
        if final_action not in ALLOWED_FINAL_ACTIONS:
            raise BankError(f"Unsupported review final_action: {final_action}")
        reason = string_value(row.get("reason"))
        if not reason:
            raise BankError("Review decision reason is required.")
        conflicts = row.get("conflicts")
        if not isinstance(conflicts, list) or not all(isinstance(item, str) for item in conflicts):
            raise BankError("Review decision conflicts must be a list of strings.")
        code_version = string_value(row.get("code_version"))
        if not code_version:
            raise BankError("Review decision code_version is required.")
        if code_version in by_code_version:
            raise BankError(f"Duplicate review decision for CodeVersion: {code_version}")
        by_code_version[code_version] = row
    missing = []
    for code_version, conflicts in required_by_code_version.items():
        row = by_code_version.get(code_version)
        if row is None:
            missing.append({"code_version": code_version, "conflicts": sorted(conflicts)})
            continue
        provided = set(row.get("conflicts") or [])
        if provided != conflicts:
            missing.append(
                {
                    "code_version": code_version,
                    "expected_conflicts": sorted(conflicts),
                    "actual_conflicts": sorted(provided),
                }
            )
    if missing:
        raise BankError(f"Review decisions are missing required items: {missing}")
    aborting = [
        row
        for row in by_code_version.values()
        if row.get("final_action") == "abort_transaction"
    ]
    if aborting:
        raise BankError("Review decisions request abort_transaction.")
    payload["decisions_sha256"] = sha256_file(path)
    return payload
