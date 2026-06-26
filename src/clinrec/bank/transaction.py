from __future__ import annotations

from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import write_json
from clinrec.bank.common import (
    TRANSACTION_SCHEMA_VERSION,
    BankError,
    bank_active_root,
    bank_history_root,
    bank_legacy_root,
    bank_transactions_root,
    compact_timestamp,
    copy_directory,
    move_directory,
    read_json_file,
    utc_now,
)
from clinrec.config import Settings


def journal_path(settings: Settings, transaction_id: str) -> Path:
    return bank_transactions_root(settings) / transaction_id / "journal.json"


def transaction_root(settings: Settings, transaction_id: str) -> Path:
    return bank_transactions_root(settings) / transaction_id


def create_journal(
    settings: Settings,
    *,
    transaction_id: str,
    plan_id: str,
    candidate_catalog_sha256: str,
    previous_catalog_sha256: str | None,
) -> dict[str, Any]:
    path = journal_path(settings, transaction_id)
    if path.exists():
        journal = read_journal(settings, transaction_id)
        if journal.get("state") == "completed":
            raise BankError(f"Transaction already completed: {transaction_id}")
        return journal
    journal = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "plan_id": plan_id,
        "state": "created",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "candidate_catalog_sha256": candidate_catalog_sha256,
        "previous_catalog_sha256": previous_catalog_sha256,
        "operations": [],
        "rollback_operations": [],
        "backups": {"active": {}, "legacy": {}},
        "errors": [],
    }
    write_json(path, journal)
    return journal


def read_journal(settings: Settings, transaction_id: str) -> dict[str, Any]:
    journal = read_json_file(journal_path(settings, transaction_id))
    if not journal:
        raise BankError(f"Transaction journal is missing: {transaction_id}")
    return journal


def set_journal_state(settings: Settings, transaction_id: str, state: str) -> None:
    journal = read_journal(settings, transaction_id)
    journal["state"] = state
    journal["updated_at"] = utc_now()
    write_json(journal_path(settings, transaction_id), journal)


def record_operation(
    settings: Settings,
    transaction_id: str,
    *,
    operation_type: str,
    code_version: str,
    source: Path | None,
    target: Path | None,
    backup: Path | None = None,
    state: str = "completed",
    error: str | None = None,
) -> None:
    journal = read_journal(settings, transaction_id)
    operation = {
        "operation_id": f"op-{len(journal['operations']) + 1:04d}",
        "type": operation_type,
        "code_version": code_version,
        "source": source.as_posix() if source is not None else None,
        "target": target.as_posix() if target is not None else None,
        "backup": backup.as_posix() if backup is not None else None,
        "state": state,
        "started_at": utc_now(),
        "completed_at": utc_now() if state == "completed" else None,
        "error": error,
    }
    journal["operations"].append(operation)
    if error:
        journal["errors"].append(operation)
    journal["updated_at"] = utc_now()
    write_json(journal_path(settings, transaction_id), journal)


def ensure_area_backup(
    settings: Settings,
    transaction_id: str,
    *,
    area: str,
    code_version: str,
    source: Path,
) -> Path | None:
    journal = read_journal(settings, transaction_id)
    backups: dict[str, Any] = journal.setdefault("backups", {}).setdefault(area, {})
    if code_version in backups:
        value = backups[code_version]
        return Path(value) if value else None
    if source.exists():
        backup = transaction_root(settings, transaction_id) / "backups" / area / code_version
        copy_directory(source, backup)
        backups[code_version] = backup.as_posix()
    else:
        backup = None
        backups[code_version] = None
    journal["updated_at"] = utc_now()
    write_json(journal_path(settings, transaction_id), journal)
    return backup


def archive_existing_target(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
    target: Path,
    event_type: str,
) -> Path | None:
    if not target.exists():
        return None
    archive = unique_path(
        bank_history_root(settings)
        / code_version
        / compact_timestamp()
        / event_type
        / target.name
    )
    move_directory(target, archive)
    record_operation(
        settings,
        transaction_id,
        operation_type="archive_existing_target",
        code_version=code_version,
        source=target,
        target=archive,
    )
    return archive


def promote_staged_to_active(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
    staging_document: Path,
) -> bool:
    target = bank_active_root(settings) / code_version
    ensure_area_backup(
        settings,
        transaction_id,
        area="active",
        code_version=code_version,
        source=target,
    )
    archive_existing_target(
        settings,
        transaction_id,
        code_version=code_version,
        target=target,
        event_type="active-target-conflict",
    )
    moved = move_directory(staging_document, target)
    if moved:
        record_operation(
            settings,
            transaction_id,
            operation_type="promote_staged_to_active",
            code_version=code_version,
            source=staging_document,
            target=target,
        )
    return moved


def move_active_to_legacy(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
) -> bool:
    source = bank_active_root(settings) / code_version
    target = bank_legacy_root(settings) / code_version
    ensure_area_backup(
        settings,
        transaction_id,
        area="active",
        code_version=code_version,
        source=source,
    )
    ensure_area_backup(
        settings,
        transaction_id,
        area="legacy",
        code_version=code_version,
        source=target,
    )
    archive_existing_target(
        settings,
        transaction_id,
        code_version=code_version,
        target=target,
        event_type="legacy-target-conflict",
    )
    moved = move_directory(source, target)
    if moved:
        record_operation(
            settings,
            transaction_id,
            operation_type="move_active_to_legacy",
            code_version=code_version,
            source=source,
            target=target,
        )
    return moved


def remove_legacy_for_reactivation(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
) -> None:
    target = bank_legacy_root(settings) / code_version
    ensure_area_backup(
        settings,
        transaction_id,
        area="legacy",
        code_version=code_version,
        source=target,
    )
    archive_existing_target(
        settings,
        transaction_id,
        code_version=code_version,
        target=target,
        event_type="reactivated-legacy",
    )


def rollback_transaction(settings: Settings, transaction_id: str) -> dict[str, Any]:
    journal = read_journal(settings, transaction_id)
    if journal.get("state") == "rolled_back":
        return journal
    set_journal_state(settings, transaction_id, "rollback_started")
    backups_value = journal.get("backups")
    backups: dict[str, Any] = backups_value if isinstance(backups_value, dict) else {}
    rollback_rows: list[dict[str, Any]] = []
    for area, root in (
        ("active", bank_active_root(settings)),
        ("legacy", bank_legacy_root(settings)),
    ):
        area_value = backups.get(area)
        area_backups: dict[str, Any] = area_value if isinstance(area_value, dict) else {}
        for code_version, backup_value in sorted(area_backups.items()):
            target = root / code_version
            if target.exists():
                rollback_archive = (
                    transaction_root(settings, transaction_id)
                    / "rollback-displaced"
                    / area
                    / code_version
                )
                move_directory(target, unique_path(rollback_archive))
            if backup_value:
                backup = Path(str(backup_value))
                copy_directory(backup, target)
                rollback_rows.append(
                    {
                        "type": "restore_backup",
                        "area": area,
                        "code_version": code_version,
                        "backup": backup.as_posix(),
                        "target": target.as_posix(),
                    }
                )
            else:
                rollback_rows.append(
                    {
                        "type": "remove_created_target",
                        "area": area,
                        "code_version": code_version,
                        "target": target.as_posix(),
                    }
                )
    journal = read_journal(settings, transaction_id)
    journal["rollback_operations"].extend(rollback_rows)
    journal["state"] = "rolled_back"
    journal["updated_at"] = utc_now()
    write_json(journal_path(settings, transaction_id), journal)
    return journal


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}-{index:03d}")
        if not candidate.exists():
            return candidate
    raise BankError(f"Could not allocate unique path for {path}")
