from __future__ import annotations

import hashlib
import os
import socket
import uuid
from pathlib import Path
from typing import Any

from clinrec.bank.accepted import (
    accepted_current_pointer_path,
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
    string_value,
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
) -> str:
    path = writer_lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if recover_stale and path.exists():
        raise BankError("Writer lock stale recovery requires explicit audited force recovery.")
    owner_token = uuid.uuid4().hex
    created_at = utc_now()
    payload = {
        "owner_token": owner_token,
        "transaction_id": transaction_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_started_at": created_at,
        "lock_created_at": created_at,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags)
    except FileExistsError as exc:
        existing = read_json_file(path)
        raise BankError(f"Another writer transaction is active: {existing}") from exc
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
        import json

        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    return owner_token


def release_writer_lock(settings: Settings, transaction_id: str, owner_token: str) -> None:
    path = writer_lock_path(settings)
    if not path.exists():
        return
    payload = read_json_file(path)
    if payload.get("transaction_id") != transaction_id or payload.get("owner_token") != owner_token:
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
    decisions_sha256: str | None = None,
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
            decisions_sha256=decisions_sha256,
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
        "decisions_sha256": decisions_sha256,
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
    decisions_sha256: str | None,
) -> None:
    checks = {
        "plan_id": plan_id,
        "candidate_catalog_sha256": candidate_catalog_sha256,
        "previous_catalog_sha256": previous_catalog_sha256,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "decisions_sha256": decisions_sha256,
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


def operation_by_idempotency_key(
    settings: Settings,
    transaction_id: str,
    key: str,
) -> dict[str, Any] | None:
    journal = read_journal(settings, transaction_id)
    for operation in journal.get("operations") or []:
        if operation.get("idempotency_key") == key:
            return operation if isinstance(operation, dict) else None
    return None


def operation_is_completed(settings: Settings, transaction_id: str, key: str) -> bool:
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    return bool(operation and operation.get("state") == "completed")


def completed_move_is_consistent(
    settings: Settings,
    transaction_id: str,
    *,
    key: str,
    expected_target: Path | None = None,
) -> bool:
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    if not operation or operation.get("state") != "completed":
        return False
    target_value = operation.get("target")
    target = Path(str(target_value)) if target_value else expected_target
    if expected_target is not None and target is None:
        target = expected_target
    expected_sha = operation.get("expected_source_sha256") or operation.get(
        "expected_target_sha256"
    )
    if expected_sha is None:
        if target is not None and target.exists():
            raise BankError(f"transaction_inconsistent: completed {key} has unexpected target")
        return True
    if target is None or not target.exists():
        raise BankError(f"transaction_inconsistent: completed {key} target is missing")
    actual_sha = path_sha256(target)
    if actual_sha != expected_sha:
        raise BankError(f"transaction_inconsistent: completed {key} target hash mismatch")
    return True


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


def complete_operation_by_key(settings: Settings, transaction_id: str, key: str) -> None:
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    if operation is None:
        raise BankError(f"Unknown journal operation: {key}")
    complete_operation(settings, transaction_id, string_operation_id(operation))


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
    key = f"backup_{area}:{code_version}"
    existing = operation_by_idempotency_key(settings, transaction_id, key)
    if existing and existing.get("state") == "completed":
        value = backups.get(code_version)
        expected_sha = existing.get("expected_source_sha256")
        if expected_sha is None:
            if value:
                raise BankError(f"transaction_inconsistent: backup {key} should be empty")
            return None
        if not value:
            raise BankError(f"transaction_inconsistent: backup {key} path is missing")
        backup = Path(str(value))
        if not backup.exists() or path_sha256(backup) != expected_sha:
            raise BankError(f"transaction_inconsistent: backup {key} hash mismatch")
        return backup
    if not source.exists():
        operation_id = begin_operation(
            settings,
            transaction_id,
            operation_type=f"backup_{area}",
            code_version=code_version,
            source=source,
            target=None,
            idempotency_key=key,
        )
        journal = read_journal(settings, transaction_id)
        journal.setdefault("backups", {}).setdefault(area, {})[code_version] = None
        write_journal(settings, transaction_id, journal)
        complete_operation(settings, transaction_id, operation_id)
        return None

    backup = transaction_root(settings, transaction_id) / "backups" / area / code_version
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type=f"backup_{area}",
        code_version=code_version,
        source=source,
        target=backup,
        idempotency_key=key,
    )
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    expected_sha = operation.get("expected_source_sha256") if operation else path_sha256(source)
    if backup.exists():
        if path_sha256(backup) != expected_sha:
            fail_operation(settings, transaction_id, operation_id, "backup_hash_mismatch")
            raise BankError(f"Backup hash mismatch for {area}/{code_version}.")
    else:
        try:
            copy_directory(source, backup)
        except Exception as exc:
            fail_operation(settings, transaction_id, operation_id, str(exc))
            raise
        if path_sha256(backup) != expected_sha:
            fail_operation(settings, transaction_id, operation_id, "backup_hash_mismatch")
            raise BankError(f"Backup hash mismatch for {area}/{code_version}.")
    journal = read_journal(settings, transaction_id)
    journal.setdefault("backups", {}).setdefault(area, {})[code_version] = backup.as_posix()
    write_journal(settings, transaction_id, journal)
    complete_operation(settings, transaction_id, operation_id)
    return backup


def ensure_state_backup(settings: Settings, transaction_id: str) -> dict[str, Any] | None:
    journal = read_journal(settings, transaction_id)
    state_backups: dict[str, Any] = journal.setdefault("backups", {}).setdefault("state", {})
    key = "backup_state:accepted_pointer"
    existing = operation_by_idempotency_key(settings, transaction_id, key)
    if "accepted_pointer" in state_backups:
        value = state_backups["accepted_pointer"]
        pointer = value if isinstance(value, dict) else None
        expected_hash = state_backups.get("accepted_pointer_sha256")
        actual_hash = stable_payload_sha256(pointer)
        if expected_hash and expected_hash != actual_hash:
            raise BankError("State backup payload hash mismatch.")
        if existing and existing.get("state") == "completed":
            return pointer
        if existing:
            complete_operation(settings, transaction_id, string_operation_id(existing))
            return pointer
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="backup_state",
        code_version="__accepted__",
        source=accepted_current_pointer_path(settings),
        target=None,
        idempotency_key=key,
    )
    pointer = read_accepted_pointer(settings)
    journal = read_journal(settings, transaction_id)
    state_backups = journal.setdefault("backups", {}).setdefault("state", {})
    state_backups["accepted_pointer"] = pointer
    state_backups["accepted_pointer_sha256"] = stable_payload_sha256(pointer)
    write_journal(settings, transaction_id, journal)
    complete_operation(settings, transaction_id, operation_id)
    return pointer


def archive_existing_target(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
    target: Path,
    event_type: str,
) -> Path | None:
    key = f"archive:{event_type}:{code_version}:{target.as_posix()}"
    completed = operation_by_idempotency_key(settings, transaction_id, key)
    if completed and completed.get("state") == "completed":
        completed_move_is_consistent(settings, transaction_id, key=key)
        value = completed.get("target")
        return Path(str(value)) if value else None
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
        idempotency_key=key,
    )
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    if operation and operation.get("target"):
        archive = Path(str(operation["target"]))
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
    key = f"promote_staged_to_active:{code_version}"
    if completed_move_is_consistent(settings, transaction_id, key=key, expected_target=target):
        return False
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
        idempotency_key=key,
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
    key = f"move_active_to_legacy:{code_version}"
    if completed_move_is_consistent(settings, transaction_id, key=key, expected_target=target):
        return False
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
        idempotency_key=key,
    )
    try:
        moved = move_directory(source, target)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    if moved:
        complete_operation(settings, transaction_id, operation_id)
    return moved


def write_lifecycle_file(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
    payload: dict[str, Any],
) -> bool:
    target = bank_legacy_root(settings) / code_version
    lifecycle_path = target / "lifecycle.json"
    key = f"lifecycle_write:{code_version}"
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    if operation and operation.get("state") == "completed":
        if not lifecycle_file_matches(lifecycle_path, payload):
            raise BankError(f"transaction_inconsistent: completed {key} content mismatch")
        return False
    if not target.exists():
        raise BankError(f"Cannot write lifecycle for missing legacy document: {code_version}")
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="lifecycle_write",
        code_version=code_version,
        source=None,
        target=lifecycle_path,
        idempotency_key=key,
    )
    try:
        atomic_write_json_fsync(lifecycle_path, payload)
        if not lifecycle_file_matches(lifecycle_path, payload):
            raise BankError("lifecycle_content_mismatch")
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    complete_operation(settings, transaction_id, operation_id)
    return True


def lifecycle_file_matches(path: Path, expected: dict[str, Any]) -> bool:
    if not path.exists():
        return False
    actual = read_json_file(path)
    required_keys = (
        "code_version",
        "removal_snapshot",
        "replacement_status",
        "replacement_candidates",
    )
    for key in required_keys:
        if actual.get(key) != expected.get(key):
            return False
    expected_events = expected.get("events")
    actual_events = actual.get("events")
    return isinstance(expected_events, list) and actual_events == expected_events


def remove_legacy_for_reactivation(
    settings: Settings,
    transaction_id: str,
    *,
    code_version: str,
) -> None:
    target = bank_legacy_root(settings) / code_version
    key = f"archive:reactivated-legacy:{code_version}:{target.as_posix()}"
    if operation_is_completed(settings, transaction_id, key):
        completed_move_is_consistent(settings, transaction_id, key=key)
        return
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
    key = f"move_active_to_quarantine:{code_version}"
    if completed_move_is_consistent(settings, transaction_id, key=key):
        return False
    ensure_area_backup(
        settings,
        transaction_id,
        area="active",
        code_version=code_version,
        source=source,
    )
    operation_id = begin_operation(
        settings,
        transaction_id,
        operation_type="move_active_to_quarantine",
        code_version=code_version,
        source=source,
        target=target,
        idempotency_key=key,
    )
    operation = operation_by_idempotency_key(settings, transaction_id, key)
    if operation and operation.get("target"):
        target = Path(str(operation["target"]))
    try:
        moved = move_directory(source, target)
    except Exception as exc:
        fail_operation(settings, transaction_id, operation_id, str(exc))
        raise
    if moved:
        from clinrec.api.catalog_sync import write_json

        write_json(
            target.parent / f"{target.name}-quarantine-event.json",
            {
                "code_version": code_version,
                "event_type": "move_to_quarantine",
                "transaction_id": transaction_id,
                "reason": reason,
                "source_path": source.as_posix(),
                "target_path": target.as_posix(),
                "moved_at": utc_now(),
            },
        )
        complete_operation(settings, transaction_id, operation_id)
    return moved


def rollback_transaction(
    settings: Settings,
    transaction_id: str,
    *,
    owner_token: str | None = None,
) -> dict[str, Any]:
    acquired_owner_token: str | None = None
    if owner_token is None:
        acquired_owner_token = acquire_writer_lock(settings, transaction_id)
        owner_token = acquired_owner_token
    journal = read_journal(settings, transaction_id)
    if journal.get("state") == "rolled_back":
        if acquired_owner_token is not None:
            release_writer_lock(settings, transaction_id, acquired_owner_token)
        return journal
    set_journal_state(settings, transaction_id, "rollback_started")
    try:
        backups_value = journal.get("backups")
        backups: dict[str, Any] = backups_value if isinstance(backups_value, dict) else {}
        for area, root in (
            ("active", bank_active_root(settings)),
            ("legacy", bank_legacy_root(settings)),
        ):
            area_value = backups.get(area)
            area_backups: dict[str, Any] = area_value if isinstance(area_value, dict) else {}
            for code_version, backup_value in sorted(area_backups.items()):
                target = root / code_version
                if target.exists():
                    rollback_archive_existing(
                        settings,
                        transaction_id,
                        area=area,
                        code_version=code_version,
                        target=target,
                    )
                if backup_value:
                    backup = Path(str(backup_value))
                    rollback_restore_backup(
                        settings,
                        transaction_id,
                        area=area,
                        code_version=code_version,
                        backup=backup,
                        target=target,
                    )
                else:
                    rollback_record_remove_created(
                        settings,
                        transaction_id,
                        area=area,
                        code_version=code_version,
                        target=target,
                    )
        rollback_quarantine_targets(settings, transaction_id, journal)
        state_backup = backups.get("state") if isinstance(backups.get("state"), dict) else {}
        pointer = state_backup.get("accepted_pointer") if isinstance(state_backup, dict) else None
        rollback_restore_state(
            settings,
            transaction_id,
            pointer if isinstance(pointer, dict) else None,
        )
        run_rollback_qa(settings)
        journal = read_journal(settings, transaction_id)
        journal["state"] = "rolled_back"
        write_journal(settings, transaction_id, journal)
    except Exception as exc:
        journal = read_journal(settings, transaction_id)
        journal["state"] = "rollback_failed"
        journal.setdefault("errors", []).append({"error": str(exc), "stage": "rollback"})
        write_journal(settings, transaction_id, journal)
        raise
    finally:
        if acquired_owner_token is not None:
            try:
                release_writer_lock(settings, transaction_id, acquired_owner_token)
            except BankError:
                pass
    return read_journal(settings, transaction_id)


def rollback_operation_by_key(
    settings: Settings,
    transaction_id: str,
    key: str,
) -> dict[str, Any] | None:
    journal = read_journal(settings, transaction_id)
    for operation in journal.get("rollback_operations") or []:
        if isinstance(operation, dict) and operation.get("idempotency_key") == key:
            return operation
    return None


def begin_rollback_operation(
    settings: Settings,
    transaction_id: str,
    *,
    operation_type: str,
    area: str,
    code_version: str,
    source: Path | None,
    target: Path | None,
    backup: Path | None = None,
    idempotency_key: str,
) -> str:
    journal = read_journal(settings, transaction_id)
    for operation in journal.get("rollback_operations") or []:
        if not isinstance(operation, dict) or operation.get("idempotency_key") != idempotency_key:
            continue
        if operation.get("state") == "completed":
            return string_operation_id(operation)
        operation["state"] = "started"
        operation["started_at"] = operation.get("started_at") or utc_now()
        write_journal(settings, transaction_id, journal)
        return string_operation_id(operation)
    operation_id = f"rollback-op-{len(journal.setdefault('rollback_operations', [])) + 1:04d}"
    operation = {
        "operation_id": operation_id,
        "idempotency_key": idempotency_key,
        "type": operation_type,
        "area": area,
        "code_version": code_version,
        "source": source.as_posix() if source is not None else None,
        "target": target.as_posix() if target is not None else None,
        "backup": backup.as_posix() if backup is not None else None,
        "expected_source_sha256": path_sha256(source),
        "expected_target_sha256": path_sha256(target),
        "expected_backup_sha256": path_sha256(backup),
        "state": "planned",
        "created_at": utc_now(),
        "started_at": None,
        "completed_at": None,
        "error": None,
    }
    journal["rollback_operations"].append(operation)
    write_journal(settings, transaction_id, journal)
    journal = read_journal(settings, transaction_id)
    for item in journal.get("rollback_operations") or []:
        if item.get("operation_id") == operation_id:
            item["state"] = "started"
            item["started_at"] = utc_now()
            break
    write_journal(settings, transaction_id, journal)
    return operation_id


def complete_rollback_operation(
    settings: Settings,
    transaction_id: str,
    operation_id: str,
) -> None:
    journal = read_journal(settings, transaction_id)
    for operation in journal.get("rollback_operations") or []:
        if operation.get("operation_id") == operation_id:
            operation["state"] = "completed"
            operation["completed_at"] = utc_now()
            operation["error"] = None
            write_journal(settings, transaction_id, journal)
            return
    raise BankError(f"Unknown rollback operation: {operation_id}")


def fail_rollback_operation(
    settings: Settings,
    transaction_id: str,
    operation_id: str,
    error: str,
) -> None:
    journal = read_journal(settings, transaction_id)
    for operation in journal.get("rollback_operations") or []:
        if operation.get("operation_id") == operation_id:
            operation["state"] = "failed"
            operation["error"] = error
            journal.setdefault("errors", []).append(operation)
            write_journal(settings, transaction_id, journal)
            return
    raise BankError(f"Unknown rollback operation: {operation_id}")


def rollback_archive_existing(
    settings: Settings,
    transaction_id: str,
    *,
    area: str,
    code_version: str,
    target: Path,
) -> None:
    key = f"rollback_archive_existing:{area}:{code_version}"
    existing = rollback_operation_by_key(settings, transaction_id, key)
    if existing and existing.get("state") == "completed":
        return
    archive = (
        transaction_root(settings, transaction_id)
        / "rollback-displaced"
        / area
        / code_version
    )
    if existing and existing.get("target"):
        archive = Path(str(existing["target"]))
    elif archive.exists():
        archive = unique_path(archive)
    operation_id = begin_rollback_operation(
        settings,
        transaction_id,
        operation_type="rollback_archive_existing",
        area=area,
        code_version=code_version,
        source=target,
        target=archive,
        idempotency_key=key,
    )
    try:
        moved = move_directory(target, archive)
        if moved and not archive.exists():
            raise BankError("rollback_archive_missing")
    except Exception as exc:
        fail_rollback_operation(settings, transaction_id, operation_id, str(exc))
        raise
    complete_rollback_operation(settings, transaction_id, operation_id)


def rollback_restore_backup(
    settings: Settings,
    transaction_id: str,
    *,
    area: str,
    code_version: str,
    backup: Path,
    target: Path,
) -> None:
    key = f"rollback_restore_backup:{area}:{code_version}"
    existing = rollback_operation_by_key(settings, transaction_id, key)
    expected_sha = path_sha256(backup)
    if existing and existing.get("state") == "completed":
        if path_sha256(target) != expected_sha:
            raise BankError(f"rollback_inconsistent: restored {area}/{code_version} hash mismatch")
        return
    operation_id = begin_rollback_operation(
        settings,
        transaction_id,
        operation_type="rollback_restore_backup",
        area=area,
        code_version=code_version,
        source=backup,
        target=target,
        backup=backup,
        idempotency_key=key,
    )
    try:
        if target.exists():
            if path_sha256(target) != expected_sha:
                raise BankError("rollback_target_exists_with_wrong_hash")
        else:
            copy_directory(backup, target)
        if path_sha256(target) != expected_sha:
            raise BankError("rollback_restore_hash_mismatch")
    except Exception as exc:
        fail_rollback_operation(settings, transaction_id, operation_id, str(exc))
        raise
    complete_rollback_operation(settings, transaction_id, operation_id)


def rollback_record_remove_created(
    settings: Settings,
    transaction_id: str,
    *,
    area: str,
    code_version: str,
    target: Path,
) -> None:
    key = f"rollback_remove_created_target:{area}:{code_version}"
    existing = rollback_operation_by_key(settings, transaction_id, key)
    if existing and existing.get("state") == "completed":
        return
    operation_id = begin_rollback_operation(
        settings,
        transaction_id,
        operation_type="rollback_remove_created_target",
        area=area,
        code_version=code_version,
        source=None,
        target=target,
        idempotency_key=key,
    )
    if target.exists():
        fail_rollback_operation(settings, transaction_id, operation_id, "target_still_exists")
        raise BankError(f"Rollback target still exists after archive: {target}")
    complete_rollback_operation(settings, transaction_id, operation_id)


def rollback_quarantine_targets(
    settings: Settings,
    transaction_id: str,
    journal: dict[str, Any],
) -> None:
    for operation in journal.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        if operation.get("type") != "move_active_to_quarantine":
            continue
        target_value = operation.get("target")
        code_version = string_value(operation.get("code_version"))
        if not target_value:
            continue
        target = Path(str(target_value))
        if not target.exists():
            continue
        key = f"rollback_archive_quarantine_target:{target.as_posix()}"
        existing = rollback_operation_by_key(settings, transaction_id, key)
        if existing and existing.get("state") == "completed":
            continue
        archive = (
            transaction_root(settings, transaction_id)
            / "rollback-displaced"
            / "quarantine"
            / target.parent.name
            / target.name
        )
        operation_id = begin_rollback_operation(
            settings,
            transaction_id,
            operation_type="rollback_archive_quarantine_target",
            area="quarantine",
            code_version=code_version,
            source=target,
            target=archive,
            idempotency_key=key,
        )
        try:
            moved = move_directory(target, archive)
            sidecar = target.parent / f"{target.name}-quarantine-event.json"
            if sidecar.exists():
                archive.parent.mkdir(parents=True, exist_ok=True)
                sidecar.replace(archive.parent / sidecar.name)
            if moved and not archive.exists():
                raise BankError("rollback_quarantine_archive_missing")
        except Exception as exc:
            fail_rollback_operation(settings, transaction_id, operation_id, str(exc))
            raise
        complete_rollback_operation(settings, transaction_id, operation_id)


def rollback_restore_state(
    settings: Settings,
    transaction_id: str,
    pointer: dict[str, Any] | None,
) -> None:
    key = "rollback_restore_state:accepted_pointer"
    existing = rollback_operation_by_key(settings, transaction_id, key)
    if existing and existing.get("state") == "completed":
        return
    operation_id = begin_rollback_operation(
        settings,
        transaction_id,
        operation_type="rollback_restore_state",
        area="state",
        code_version="__accepted__",
        source=None,
        target=accepted_current_pointer_path(settings),
        idempotency_key=key,
    )
    try:
        restore_accepted_pointer(settings, pointer)
    except Exception as exc:
        fail_rollback_operation(settings, transaction_id, operation_id, str(exc))
        raise
    complete_rollback_operation(settings, transaction_id, operation_id)


def run_rollback_qa(settings: Settings) -> None:
    from clinrec.bank.common import BankRecordFilter
    from clinrec.bank.qa import run_bank_qa

    qa = run_bank_qa(settings, BankRecordFilter(all_records=True), against="accepted")
    if qa.fatal or qa.errors:
        raise BankError("Accepted QA failed after rollback.")


def reconcile_started_operations(
    settings: Settings,
    transaction_id: str,
    *,
    owner_token: str | None = None,
) -> None:
    acquired_owner_token: str | None = None
    if owner_token is None:
        acquired_owner_token = acquire_writer_lock(settings, transaction_id)
    journal = read_journal(settings, transaction_id)
    try:
        changed = False
        for operation in journal.get("operations") or []:
            if operation.get("state") != "started":
                continue
            source = Path(str(operation.get("source"))) if operation.get("source") else None
            target = Path(str(operation.get("target"))) if operation.get("target") else None
            expected_source_sha = operation.get("expected_source_sha256")
            expected_target_sha = operation.get("expected_target_sha256")
            if source and target and not source.exists() and target.exists():
                actual_target_sha = path_sha256(target)
                expected_sha = expected_source_sha or expected_target_sha
                if expected_sha in {None, actual_target_sha}:
                    operation["state"] = "completed"
                    operation["completed_at"] = operation.get("completed_at") or utc_now()
                    changed = True
                else:
                    operation["state"] = "failed"
                    operation["error"] = "target_hash_mismatch"
                    journal.setdefault("errors", []).append(operation)
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
    finally:
        if acquired_owner_token is not None:
            release_writer_lock(settings, transaction_id, acquired_owner_token)


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


def stable_payload_sha256(payload: Any) -> str:
    import json

    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def string_operation_id(operation: dict[str, Any]) -> str:
    value = operation.get("operation_id")
    if not isinstance(value, str):
        raise BankError("Journal operation_id is invalid.")
    return value
