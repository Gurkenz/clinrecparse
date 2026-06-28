from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import BankError, atomic_write_json, read_json_file, utc_now


@dataclass(frozen=True)
class ResearchLayout:
    root: Path
    current_root: Path
    previous_root: Path
    previous_attempts_path: Path
    used_legacy_compat: bool


@dataclass(frozen=True)
class MigrationSummary:
    input: Path
    migrated: bool
    previous_root: Path
    attempts_path: Path
    corpus_path: Path


def research_layout(root: Path) -> ResearchLayout:
    previous = root / "previous"
    legacy = root / "legacy"
    previous_attempts = attempts_path(root, preferred=True)
    legacy_attempts = attempts_path(root, preferred=False)
    if previous.exists():
        return ResearchLayout(
            root=root,
            current_root=root / "current",
            previous_root=previous,
            previous_attempts_path=previous_attempts,
            used_legacy_compat=False,
        )
    if not legacy.exists():
        return ResearchLayout(
            root=root,
            current_root=root / "current",
            previous_root=previous,
            previous_attempts_path=previous_attempts,
            used_legacy_compat=False,
        )
    attempts = previous_attempts if previous_attempts.exists() else legacy_attempts
    return ResearchLayout(
        root=root,
        current_root=root / "current",
        previous_root=legacy,
        previous_attempts_path=attempts,
        used_legacy_compat=legacy.exists(),
    )


def attempts_path(root: Path, *, preferred: bool) -> Path:
    name = "previous-attempts.jsonl" if preferred else "legacy-attempts.jsonl"
    return root / "attempts" / name


def migrate_layout(root: Path) -> MigrationSummary:
    legacy = root / "legacy"
    previous = root / "previous"
    if legacy.exists() and previous.exists():
        raise BankError("Research migration found both legacy and previous paths.")
    migrated = False
    if legacy.exists():
        legacy.replace(previous)
        migrated = True
    legacy_attempts = attempts_path(root, preferred=False)
    previous_attempts = attempts_path(root, preferred=True)
    if legacy_attempts.exists() and previous_attempts.exists():
        raise BankError("Research migration found both legacy and previous attempts files.")
    if legacy_attempts.exists():
        previous_attempts.parent.mkdir(parents=True, exist_ok=True)
        legacy_attempts.replace(previous_attempts)
        migrated = True
    update_corpus_layout_metadata(root, migrated=migrated)
    return MigrationSummary(
        input=root,
        migrated=migrated,
        previous_root=previous,
        attempts_path=previous_attempts,
        corpus_path=root / "corpus.json",
    )


def update_corpus_layout_metadata(root: Path, *, migrated: bool) -> None:
    path = root / "corpus.json"
    payload: dict[str, Any] = read_json_file(path)
    if not payload and not path.exists():
        return
    for legacy_key, previous_key in (
        ("legacy_target", "previous_target"),
        ("legacy_minimum", "previous_minimum"),
        ("legacy_attempt_limit", "previous_attempt_limit"),
        ("valid_legacy_count", "valid_previous_count"),
    ):
        if legacy_key in payload and previous_key not in payload:
            payload[previous_key] = payload[legacy_key]
    payload["layout_version"] = "2.0"
    payload["previous_layout"] = "previous"
    if migrated:
        payload["layout_migrated_at"] = utc_now()
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload)
