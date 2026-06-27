from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.bank.common import (
    BankError,
    bank_state_root,
    catalog_record_for_bank,
    compact_timestamp,
    read_json_file,
    read_jsonl,
    relative_to_data_root,
    sha256_file,
    source_record_id_from_catalog,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.config import Settings

ACCEPTED_POINTER_SCHEMA_VERSION = "2.0"


@dataclass(frozen=True)
class AcceptedGeneration:
    generation_id: str
    root: Path
    catalog_path: Path
    manifest_path: Path
    source_path: Path
    pointer_path: Path
    catalog_sha256: str
    total_records: int
    accepted_at: str
    transaction_id: str | None
    pointer: dict[str, Any]


def accepted_generations_root(settings: Settings) -> Path:
    return bank_state_root(settings) / "generations"


def accepted_current_pointer_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "current.json"


def legacy_accepted_catalog_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "accepted-catalog.json"


def legacy_accepted_records_path(settings: Settings) -> Path:
    return bank_state_root(settings) / "accepted-catalog-records.jsonl"


def read_accepted_pointer(settings: Settings) -> dict[str, Any] | None:
    path = accepted_current_pointer_path(settings)
    if not path.exists():
        return None
    pointer = read_json_file(path)
    if not pointer:
        raise BankError("Accepted pointer is missing or invalid JSON.")
    if pointer.get("schema_version") != ACCEPTED_POINTER_SCHEMA_VERSION:
        raise BankError("Accepted pointer schema_version is invalid.")
    return pointer


def load_accepted_generation(settings: Settings, *, migrate: bool = True) -> AcceptedGeneration:
    pointer = read_accepted_pointer(settings)
    if pointer is None and migrate:
        pointer = migrate_legacy_accepted_state(settings)
    if pointer is None:
        raise BankError("Accepted generation pointer is missing.")

    generation_id = string_value(pointer.get("generation_id"))
    if not generation_id:
        raise BankError("Accepted pointer generation_id is missing.")
    root = accepted_generations_root(settings) / generation_id
    catalog_path = root / "catalog-active.jsonl"
    manifest_path = root / "manifest.json"
    source_path = root / "source.json"
    if not root.exists() or not catalog_path.exists() or not manifest_path.exists():
        raise BankError(f"Accepted generation is incomplete: {generation_id}")

    actual_sha = sha256_file(catalog_path)
    pointer_sha = string_value(pointer.get("catalog_sha256"))
    if actual_sha != pointer_sha:
        raise BankError("Accepted generation catalog hash mismatch.")

    manifest = read_json_file(manifest_path)
    if manifest.get("catalog_sha256") != actual_sha:
        raise BankError("Accepted generation manifest hash mismatch.")
    total_records = int(pointer.get("total_records") or 0)
    if int(manifest.get("total_records") or -1) != total_records:
        raise BankError("Accepted generation total_records mismatch.")

    return AcceptedGeneration(
        generation_id=generation_id,
        root=root,
        catalog_path=catalog_path,
        manifest_path=manifest_path,
        source_path=source_path,
        pointer_path=accepted_current_pointer_path(settings),
        catalog_sha256=actual_sha,
        total_records=total_records,
        accepted_at=string_value(pointer.get("accepted_at")),
        transaction_id=string_value(pointer.get("transaction_id")) or None,
        pointer=pointer,
    )


def read_accepted_catalog_records(settings: Settings) -> list[dict[str, Any]]:
    try:
        generation = load_accepted_generation(settings)
    except BankError:
        if legacy_accepted_records_path(settings).exists():
            return read_jsonl(legacy_accepted_records_path(settings))
        return []
    return read_jsonl(generation.catalog_path)


def accepted_catalog_sha256(settings: Settings) -> str | None:
    try:
        return load_accepted_generation(settings).catalog_sha256
    except BankError:
        legacy = read_json_file(legacy_accepted_catalog_path(settings))
        value = legacy.get("sha256")
        return string_value(value) if value else None


def create_accepted_generation(
    settings: Settings,
    *,
    records: list[dict[str, Any]],
    transaction_id: str,
    generation_id: str | None = None,
    snapshot_path: Path | None = None,
    source_catalog_path: Path | None = None,
    switch_pointer: bool = True,
) -> AcceptedGeneration:
    normalized = [catalog_record_for_bank(row) for row in records]
    validate_accepted_records(normalized)

    current_generation_id = generation_id or transaction_id or compact_timestamp()
    root = accepted_generations_root(settings) / current_generation_id
    if root.exists():
        pointer = pointer_for_existing_generation(settings, current_generation_id)
        if switch_pointer:
            atomically_switch_accepted_pointer(settings, pointer)
        return AcceptedGeneration(
            generation_id=current_generation_id,
            root=root,
            catalog_path=root / "catalog-active.jsonl",
            manifest_path=root / "manifest.json",
            source_path=root / "source.json",
            pointer_path=accepted_current_pointer_path(settings),
            catalog_sha256=string_value(pointer.get("catalog_sha256")),
            total_records=int(pointer.get("total_records") or 0),
            accepted_at=string_value(pointer.get("accepted_at")),
            transaction_id=string_value(pointer.get("transaction_id")) or None,
            pointer=pointer,
        )
    part_root = root.with_name(root.name + ".part")
    if part_root.exists():
        shutil.rmtree(part_root)
    part_root.mkdir(parents=True, exist_ok=False)

    catalog_path = part_root / "catalog-active.jsonl"
    manifest_path = part_root / "manifest.json"
    source_path = part_root / "source.json"
    write_jsonl(catalog_path, normalized)
    catalog_sha = sha256_file(catalog_path)
    accepted_at = utc_now()
    manifest = {
        "schema_version": ACCEPTED_POINTER_SCHEMA_VERSION,
        "generation_id": current_generation_id,
        "catalog_path": "catalog-active.jsonl",
        "catalog_sha256": catalog_sha,
        "total_records": len(normalized),
        "accepted_at": accepted_at,
        "transaction_id": transaction_id,
    }
    atomic_write_json_fsync(manifest_path, manifest)
    atomic_write_json_fsync(
        source_path,
        {
            "transaction_id": transaction_id,
            "snapshot_path": relative_to_data_root(settings, snapshot_path)
            if snapshot_path is not None
            else None,
            "source_catalog_path": relative_to_data_root(settings, source_catalog_path)
            if source_catalog_path is not None
            else None,
            "created_at": accepted_at,
        },
    )
    os.replace(part_root, root)

    pointer = pointer_for_generation(
        settings,
        generation_id=current_generation_id,
        catalog_sha256=catalog_sha,
        total_records=len(normalized),
        accepted_at=accepted_at,
        transaction_id=transaction_id,
    )
    if switch_pointer:
        atomically_switch_accepted_pointer(settings, pointer)

    final = AcceptedGeneration(
        generation_id=current_generation_id,
        root=root,
        catalog_path=root / "catalog-active.jsonl",
        manifest_path=root / "manifest.json",
        source_path=root / "source.json",
        pointer_path=accepted_current_pointer_path(settings),
        catalog_sha256=catalog_sha,
        total_records=len(normalized),
        accepted_at=accepted_at,
        transaction_id=transaction_id,
        pointer=pointer,
    )
    verify_generation(final)
    return final


def atomically_switch_accepted_pointer(settings: Settings, pointer: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "generation_id",
        "catalog_path",
        "catalog_sha256",
        "total_records",
        "accepted_at",
        "transaction_id",
    }
    missing = sorted(required - set(pointer))
    if missing:
        raise BankError(f"Accepted pointer is missing required fields: {missing}")
    atomic_write_json_fsync(accepted_current_pointer_path(settings), pointer)


def restore_accepted_pointer(settings: Settings, pointer: dict[str, Any] | None) -> None:
    path = accepted_current_pointer_path(settings)
    if pointer is None:
        if path.exists():
            backup = path.with_suffix(path.suffix + ".removed.part")
            atomic_write_json_fsync(backup, {"removed_at": utc_now()})
            path.unlink()
            backup.unlink(missing_ok=True)
        return
    atomically_switch_accepted_pointer(settings, pointer)


def migrate_legacy_accepted_state(settings: Settings) -> dict[str, Any] | None:
    records_path = legacy_accepted_records_path(settings)
    if not records_path.exists():
        return None
    records = read_jsonl(records_path)
    if not records:
        return None
    legacy_meta = read_json_file(legacy_accepted_catalog_path(settings))
    generation_id = string_value(legacy_meta.get("timestamp")) or (
        "legacy-" + compact_timestamp()
    )
    if not generation_id.startswith("legacy-"):
        generation_id = "legacy-" + generation_id
    if (accepted_generations_root(settings) / generation_id).exists():
        pointer = pointer_for_existing_generation(settings, generation_id)
        atomically_switch_accepted_pointer(settings, pointer)
        return pointer
    generation = create_accepted_generation(
        settings,
        records=records,
        transaction_id=string_value(legacy_meta.get("transaction_id")) or "legacy-migration",
        generation_id=generation_id,
        snapshot_path=Path(string_value(legacy_meta.get("snapshot_path")))
        if legacy_meta.get("snapshot_path")
        else None,
        source_catalog_path=records_path,
        switch_pointer=True,
    )
    return generation.pointer


def pointer_for_existing_generation(settings: Settings, generation_id: str) -> dict[str, Any]:
    root = accepted_generations_root(settings) / generation_id
    manifest = read_json_file(root / "manifest.json")
    if not manifest:
        raise BankError(f"Accepted generation manifest is missing: {generation_id}")
    catalog_path = root / "catalog-active.jsonl"
    catalog_sha = sha256_file(catalog_path)
    if manifest.get("catalog_sha256") != catalog_sha:
        raise BankError("Accepted generation manifest hash mismatch.")
    return pointer_for_generation(
        settings,
        generation_id=generation_id,
        catalog_sha256=catalog_sha,
        total_records=int(manifest.get("total_records") or 0),
        accepted_at=string_value(manifest.get("accepted_at")),
        transaction_id=string_value(manifest.get("transaction_id")),
    )


def pointer_for_generation(
    settings: Settings,
    *,
    generation_id: str,
    catalog_sha256: str,
    total_records: int,
    accepted_at: str,
    transaction_id: str | None,
) -> dict[str, Any]:
    catalog_path = accepted_generations_root(settings) / generation_id / "catalog-active.jsonl"
    return {
        "schema_version": ACCEPTED_POINTER_SCHEMA_VERSION,
        "generation_id": generation_id,
        "catalog_path": relative_to_data_root(settings, catalog_path),
        "catalog_sha256": catalog_sha256,
        "total_records": total_records,
        "accepted_at": accepted_at,
        "transaction_id": transaction_id,
    }


def validate_accepted_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise BankError("Refusing to accept an empty active catalog.")
    code_versions = [string_value(row.get("code_version")) for row in records]
    if "" in code_versions:
        raise BankError("Refusing to accept catalog with missing CodeVersion.")
    if len(code_versions) != len(set(code_versions)):
        raise BankError("Refusing to accept active catalog with duplicate CodeVersion.")
    by_source: dict[int, set[str]] = {}
    for row in records:
        source_record_id = source_record_id_from_catalog(row)
        if source_record_id is not None:
            by_source.setdefault(source_record_id, set()).add(string_value(row.get("code_version")))
    duplicates = {
        source_record_id: sorted(code_versions)
        for source_record_id, code_versions in by_source.items()
        if len(code_versions) > 1
    }
    if duplicates:
        raise BankError("Refusing to accept active catalog with identity conflicts.")


def verify_generation(generation: AcceptedGeneration) -> None:
    if sha256_file(generation.catalog_path) != generation.catalog_sha256:
        raise BankError("Accepted generation catalog hash mismatch.")
    manifest = read_json_file(generation.manifest_path)
    if manifest.get("catalog_sha256") != generation.catalog_sha256:
        raise BankError("Accepted generation manifest hash mismatch.")


def atomic_write_json_fsync(path: Path, payload: Any) -> None:
    part_path = path.with_suffix(path.suffix + ".part")
    part_path.parent.mkdir(parents=True, exist_ok=True)
    with part_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(part_path, path)
