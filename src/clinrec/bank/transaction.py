from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path
from typing import Any

from clinrec.bank.accepted import (
    atomic_write_json_fsync,
    read_accepted_pointer,
    restore_accepted_pointer,
)
from clinrec.bank.common import (
    TRANSACTION_SCHEMA_VERSION,
    BankError,
    bank_active_root,
    bank_history_root,
    bank_legacy_root,
    bank_quarantine_root,
    bank_state_root,
    bank_transactions_root,
    compact_timestamp,
    copy_directory,
    move_directory,
    read_json_file,
    utc_now,
)
from clinrec.config import Settings

UNFINISHED_STATES = {
    "created",
    "staging_validated",
    "applying",
    "post_apply_validated",
    "state_committing",
    "failed",
    "rollback_started",
}


def journal_path(settings: Settings, transaction_id: str) -> Path:
    return bank_transactions_root(settings) / transaction_id / "journal.json"


def transaction_root(settings: Settings, transaction_id: str) -> Path:
    return bank_transactions_root(settings) / transaction_id


def writer_lock_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "writer.lock"


def acquire_writer_lock(
    settings: Settings,
    transaction_id: str,
    *,
    recover_stale: bool = False,
) -> None:
    path = writer_lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if recover_stale and path.exists():
        path.unlink()
    payload = {
        "transaction_id": transaction_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utc_now(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags)
    except FileExistsError as exc:
        existing = read_json_file(path)
        if existing.get("transaction_id") == transaction_id:
            return
        raise BankError(f"Another writer transaction is active: {existing}") from exc
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
        import json

        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def release_writer_lock(settings: Settings, transaction_id: str) -> None:
    path = writer_lock_path(settings)
    if not path.exists():
        return
    payload = read_json_file(path)
    if payload.get("transaction_id") != transaction_id:
        raise BankError("Writer lock belongs to another transaction.")
    path.unlink()


def create_journal(
    settings: Settings,
    *,
    transaction_id: str,
    plan_id: str,
    candidate_catalog_sha256: str,
    previous_catalog_sha256: str | None,
    candidate_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    path = journal_path(settings, transaction_id)
    if path.exists():
        journal = read_journal(settings, transaction_id)
        verify_journal_binding(
            journal,
            plan_id=plan_id,
            candidate_catalog_sha256=candidate_catalog_sha256,
            previous_catalog_sha256=previous_catalog_sha256,
            candidate_manifest_sha256=candidate_manifest_sha256,
        )
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
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "previous_catalog_sha256": previous_catalog_sha256,
        "operations": [],
        "rollback_operations": [],
        "backups": {"active": {}, "legacy": {}, "quarantine": {}, "state": {}},
        "errors": [],
    }
    atomic_write_json_fsync(path, journal)
    return journal


def verify_journal_binding(
    journal: dict[str, Any],
    *,
    plan_id: str,
    candidate_catalog_sha256: str,
    previous_catalog_sha256: str | None,
    candidate_manifest_sha256: str | None,
) -> None:
    checks = {
        "plan_id": plan_id,
        "candidate_catalog_sha256": candidate_catalog_sha256,
        "previous_catalog_sha256": previous_catalog_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
    }
    for key, expected in checks.items():
        if journal.get(key) != expected:
            raise BankError(f"Journal {key} mismatch; refusing to resume another plan.")


def read_journal(settings: Settings, transaction_id: str) -> dict[str, Any]:
    journal = read_json_file(journal_path(settings, transaction_id))
    if not journal:
        raise BankError(f"Transaction journal is missing: {transaction_id}")
    return journal


def write_journal(settings: Settings, transaction_id: str, journal: dict[str, Any]) -> None:
    journal["updated_at"] = utc_now()
    atomic_write_json_fsync(journal_path(settings, transaction_id), journal)


def set_journal_state(settings: Settings, transaction_id: str, state: str) -> None:
    journal = read_journal(settings, transaction_id)
    journal["state"] = state
    write_journal(settings, transaction_id, journal)


def begin_operation(
    settings: Settings,
    transaction_id: str,
    *,
    operation_type: str,
    code_version: str,
    source: Path | None,
    target: Path | None,
    backup: Path | None = None,
    idempotency_key: str | None = None,
) -> str:
    journal = read_journal(settings, transaction_id)
    key = idempotency_key or f"{operation_type}:{code_version}"
    for operation in journal["operations"]:
        if operation.get("idempotency_key") == key:
            if operation.get("state") == "completed":
                return string_operation_id(operation)
            operation["state"] = "started"
            operation["started_at"] = operation.get("started_at") or utc_now()
            write_journal(settings, transaction_id, journal)
            return string_operation_id(operation)
    operation_id = f"op-{len(journal['operations']) + 1:04d}"
    operation = {
        "operation_id": operation_id,
        "idempotency_key": key,
        "type": operation_type,
        "code_version": code_version,
        "source": source.as_posix() if source is not None else None,
        "target": target.as_posix() if target is not None else None,
        "backup": backup.as_posix() if backup is not None else None,
        "expected_source_sha256": path_sha256(source),
        "expected_target_sha256": path_sha256(target),
        "state": "planned",
        "created_at": utc_now(),
        "started_at": None,
        "completed_at": None,
        "error": None,
    }
    journal["operations"].append(operation)
    write_journal(settings, transaction_id, journal)
    journal = read_journal(settings, transaction_id)
    for item in journal["operations"]:
        if item.get("operation_id") == operation_id:
            item["state"] = "started"
            item["started_at"] = utc_now()
            break
    write_journal(settings, transaction_id, journal)
    return operation_id


def complete_operation(settings: Settings, transaction_id: str, operation_id: str) -> None:
    journal = read_journal(settings, transaction_id)
    for operation in journal["operations"]:
        if operation.get("operation_id") == operation_id:
            operation["state"] = "completed"
            operation["completed_at"] = utc_now()
            operation["error"] = None
            write_journal(settings, transaction_id, journal)
            return
    raise BankError(f"Unknown journal operation: {operation_id}")


def fail_operation(
    settings: Settings,
    transaction_id: str,
    operation_id: str,
    error: str,
) -> None:
    journal = read_journal(settings, transaction_id)
    for operation in journal["operations"]:
        if operation.get("operation_id") == operation_id:
            operation["state"] = "failed"
            operation["error"] = error
            journal["errors"].append(operation)
            write_journal(settings, transaction_id, journal)
            return
    raise BankError(f"Unknown journal operation: {operation_id}")


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
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type=operation_type,
        code_version=code_version,
        source=source,
        target=target,
        backup=backup,
    )
    if error:
        fail_operation(settings, transaction_id, operation_id, error)
    elif state == "completed":
        complete_operation(settings, transaction_id, operation_id)


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
    write_journal(settings, transaction_id, journal)
    return backup


def ensure_state_backup(settings: Settings, transaction_id: str) -> dict[str, Any] | None:
    journal = read_journal(settings, transaction_id)
    state_backups: dict[str, Any] = journal.setdefault("backups", {}).setdefault("state", {})
    if "accepted_pointer" in state_backups:
        value = state_backups["accepted_pointer"]
        return value if isinstance(value, dict) else None
    pointer = read_accepted_pointer(settings)
    state_backups["accepted_pointer"] = pointer
    write_journal(settings, transaction_id, journal)
    return pointer


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
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="archive_existing_target",
        code_version=code_version,
        source=target,
        target=archive,
        idempotency_key=f"archive:{event_type}:{code_version}:{target.as_posix()}",
    )
    try:
        move_directory(target, archive)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    complete_operation(settings, transaction_id, operation_id)
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
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="promote_staged_to_active",
        code_version=code_version,
        source=staging_document,
        target=target,
    )
    try:
        moved = move_directory(staging_document, target)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    if moved:
        complete_operation(settings, transaction_id, operation_id)
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
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="move_active_to_legacy",
        code_version=code_version,
        source=source,
        target=target,
    )
    try:
        moved = move_directory(source, target)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    if moved:
        complete_operation(settings, transaction_id, operation_id)
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


def move_active_to_quarantine(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
    reason: str,
) -> bool:
    source = bank_active_root(settings) / code_version
    target = bank_quarantine_root(settings) / code_version / compact_timestamp()
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
        area="quarantine",
        code_version=code_version,
        source=target,
    )
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="move_active_to_quarantine",
        code_version=code_version,
        source=source,
        target=target,
    )
    try:
        moved = move_directory(source, target)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    if moved:
        from clinrec.api.catalog_sync import write_json

        write_json(
            target / "quarantine-event.json",
            {
                "code_version": code_version,
                "event_type": "move_to_quarantine",
                "transaction_id": transaction_id,
                "reason": reason,
                "source_path": source.as_posix(),
                "moved_at": utc_now(),
            },
        )
        complete_operation(settings, transaction_id, operation_id)
    return moved


def rollback_transaction(settings: Settings, transaction_id: str) -> dict[str, Any]:
    journal = read_journal(settings, transaction_id)
    if journal.get("state") == "rolled_back":
        return journal
    set_journal_state(settings, transaction_id, "rollback_started")
    try:
        backups_value = journal.get("backups")
        backups: dict[str, Any] = backups_value if isinstance(backups_value, dict) else {}
        rollback_rows: list[dict[str, Any]] = []
        for area, root in (
            ("active", bank_active_root(settings)),
            ("legacy", bank_legacy_root(settings)),
            ("quarantine", bank_quarantine_root(settings)),
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
        state_backup = backups.get("state") if isinstance(backups.get("state"), dict) else {}
        pointer = state_backup.get("accepted_pointer") if isinstance(state_backup, dict) else None
        restore_accepted_pointer(settings, pointer if isinstance(pointer, dict) else None)
        journal = read_journal(settings, transaction_id)
        journal["rollback_operations"].extend(rollback_rows)
        journal["state"] = "rolled_back"
        write_journal(settings, transaction_id, journal)
    except Exception as exc:
        journal = read_journal(settings, transaction_id)
        journal["state"] = "rollback_failed"
        journal.setdefault("errors", []).append({"error": str(exc), "stage": "rollback"})
        write_journal(settings, transaction_id, journal)
        raise
    finally:
        try:
            release_writer_lock(settings, transaction_id)
        except BankError:
            pass
    return read_journal(settings, transaction_id)


def reconcile_started_operations(settings: Settings, transaction_id: str) -> None:
    journal = read_journal(settings, transaction_id)
    changed = False
    for operation in journal.get("operations") or []:
        if operation.get("state") != "started":
            continue
        source = Path(str(operation.get("source"))) if operation.get("source") else None
        target = Path(str(operation.get("target"))) if operation.get("target") else None
        expected_target_sha = operation.get("expected_target_sha256")
        if source and target and not source.exists() and target.exists():
            actual_target_sha = path_sha256(target)
            if expected_target_sha in {None, actual_target_sha}:
                operation["state"] = "completed"
                operation["completed_at"] = operation.get("completed_at") or utc_now()
                changed = True
        elif source and source.exists() and target and not target.exists():
            operation["state"] = "planned"
            operation["started_at"] = None
            changed = True
        elif source and source.exists() and target and target.exists():
            operation["state"] = "failed"
            operation["error"] = "source_and_target_both_exist"
            journal.setdefault("errors", []).append(operation)
            changed = True
        elif source and not source.exists() and target and not target.exists():
            operation["state"] = "failed"
            operation["error"] = "source_and_target_missing"
            journal.setdefault("errors", []).append(operation)
            changed = True
    if changed:
        write_journal(settings, transaction_id, journal)


def list_transactions(settings: Settings) -> list[dict[str, Any]]:
    root = bank_transactions_root(settings)
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            journal = read_journal(settings, path.name)
        except BankError:
            continue
        rows.append(
            {
                "transaction_id": journal.get("transaction_id"),
                "state": journal.get("state"),
                "plan_id": journal.get("plan_id"),
                "updated_at": journal.get("updated_at"),
            }
        )
    return rows


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}-{index:03d}")
        if not candidate.exists():
            return candidate
    raise BankError(f"Could not allocate unique path for {path}")


def path_sha256(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    if path.is_file():
        return file_sha256(path)
    hasher = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        hasher.update(child.relative_to(path).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_sha256(child).encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def string_operation_id(operation: dict[str, Any]) -> str:
    value = operation.get("operation_id")
    if not isinstance(value, str):
        raise BankError("Journal operation_id is invalid.")
    return value
