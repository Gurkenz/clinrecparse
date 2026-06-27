from __future__ import annotations

from pathlib import Path
from typing import Any

from clinrec.bank.common import BankError, read_json_file, sha256_file, string_value, utc_now
from clinrec.bank.reconcile_helpers import plan_actions

DECISIONS_SCHEMA_VERSION = "1.0"
ALLOWED_DECISIONS = {
    "use_staged_candidate",
    "keep_current_and_reject_candidate",
    "move_current_to_quarantine_and_use_staged",
    "abort_transaction",
    "move_to_quarantine",
    "keep_and_abort_catalog_acceptance",
    "associate_with_candidate",
}


def decisions_path_for_plan(plan_path: Path) -> Path:
    return plan_path.parent / "decisions.json"


def build_decision_template(
    plan: dict[str, Any],
    *,
    decision: str,
    reason: str,
) -> dict[str, Any]:
    if decision not in ALLOWED_DECISIONS:
        raise BankError(f"Unsupported review decision: {decision}")
    rows = [
        {
            "code_version": item["code_version"],
            "conflict_type": item["conflict_type"],
            "decision": decision,
            "reason": reason,
            "decided_at": utc_now(),
        }
        for item in required_decision_items(plan)
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
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decisions:
        if not isinstance(row, dict):
            raise BankError("Review decision row is invalid.")
        decision = row.get("decision")
        if decision not in ALLOWED_DECISIONS:
            raise BankError(f"Unsupported review decision: {decision}")
        reason = string_value(row.get("reason"))
        if not reason:
            raise BankError("Review decision reason is required.")
        key = (string_value(row.get("code_version")), string_value(row.get("conflict_type")))
        by_key[key] = row
    missing = [
        item
        for item in required
        if (item["code_version"], item["conflict_type"]) not in by_key
    ]
    if missing:
        raise BankError(f"Review decisions are missing required items: {missing}")
    aborting = [
        row
        for row in by_key.values()
        if row.get("decision") in {"abort_transaction", "keep_and_abort_catalog_acceptance"}
    ]
    if aborting:
        raise BankError("Review decisions request abort_transaction.")
    payload["decisions_sha256"] = sha256_file(path)
    return payload
