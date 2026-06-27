from __future__ import annotations

from typing import Any


def plan_actions(plan: dict[str, Any], key: str) -> list[Any]:
    actions = plan.get("actions") if isinstance(plan.get("actions"), dict) else {}
    value = actions.get(key, plan.get(key)) if isinstance(actions, dict) else plan.get(key)
    return list(value) if isinstance(value, list) else []
