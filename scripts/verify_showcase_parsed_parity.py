from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ENTITY_SPECS = {
    "documents": ("document_id", "document.json", "documents.jsonl"),
    "sections": ("section_id", "sections.jsonl", "sections.jsonl"),
    "blocks": ("block_id", "blocks.jsonl", "blocks.jsonl"),
    "tables": ("table_id", "tables.jsonl", "tables.jsonl"),
    "table_cells": ("cell_id", "table-cells.jsonl", "table-cells.jsonl"),
    "table_placements": ("placement_id", "table-placements.jsonl", "table-placements.jsonl"),
    "images": ("image_id", "images.jsonl", "images.jsonl"),
    "assets": ("asset_id", "assets.jsonl", "assets.jsonl"),
    "recommendations": (
        "recommendation_id",
        "recommendations.jsonl",
        "recommendations.jsonl",
    ),
    "references": ("reference_id", "references.jsonl", "references.jsonl"),
    "chunks": ("chunk_id", "chunks.jsonl", "rag/chunks.jsonl"),
    "citations": ("chunk_id", "ml/citation-index.jsonl", "rag/citation-index.jsonl"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify showcase/parsed canonical parity.")
    parser.add_argument("--showcase", required=True, type=Path)
    parser.add_argument("--parsed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = verify_parity(args.showcase, args.parsed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if report["passed"] else 2


def verify_parity(showcase: Path, parsed: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for name, (key, showcase_relative, parsed_relative) in ENTITY_SPECS.items():
        showcase_base = showcase if showcase_relative.startswith("ml/") else showcase / "canonical"
        showcase_rows = read_entity(showcase_base / showcase_relative, key)
        parsed_rows = read_entity(parsed / parsed_relative, key)
        checks.append(compare_entity(name, key, showcase_rows, parsed_rows))
    passed = all(check["passed"] for check in checks)
    return {
        "schema_version": "0.4-pilot",
        "passed": passed,
        "checks_passed": sum(1 for check in checks if check["passed"]),
        "checks_total": len(checks),
        "checks": checks,
    }


def read_entity(path: Path, key: str) -> dict[str, dict[str, Any]]:
    if path.name == "document.json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path} is not an object")
        return {string_value(value.get(key)): value}
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} contains a non-object row")
        rows[string_value(value.get(key))] = value
    return rows


def compare_entity(
    name: str,
    key: str,
    showcase_rows: dict[str, dict[str, Any]],
    parsed_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    showcase_ids = set(showcase_rows)
    parsed_ids = set(parsed_rows)
    missing = sorted(showcase_ids - parsed_ids)
    extra = sorted(parsed_ids - showcase_ids)
    mismatches = [
        mismatch
        for stable_id in sorted(showcase_ids & parsed_ids)
        if (
            mismatch := compare_record(
                name,
                stable_id,
                showcase_rows[stable_id],
                parsed_rows[stable_id],
            )
        )
    ]
    return {
        "name": name,
        "key": key,
        "passed": not missing and not extra and not mismatches,
        "showcase_count": len(showcase_rows),
        "parsed_count": len(parsed_rows),
        "missing_ids": missing,
        "extra_ids": extra,
        "mismatches": mismatches,
    }


def compare_record(
    name: str,
    stable_id: str,
    showcase_row: dict[str, Any],
    parsed_row: dict[str, Any],
) -> dict[str, Any] | None:
    fields_by_entity = {
        "documents": ["title", "source_raw_sha256"],
        "sections": ["plain_text_sha256", "raw_path", "parent_raw_path"],
        "blocks": ["text_sha256", "block_type", "recommendation_ids"],
        "tables": ["plain_text_sha256", "classification"],
        "table_cells": ["text_sha256", "rowspan", "colspan"],
        "table_placements": ["text_sha256", "origin_cell_id"],
        "images": ["asset_sha256", "asset_path", "source_type"],
        "assets": ["asset_sha256", "size_bytes", "occurrence_ids"],
        "recommendations": [
            "group_text_sha256",
            "recommendation_block_ids",
            "comment_block_ids",
            "grade_block_ids",
            "reference_block_ids",
        ],
        "references": ["source_text", "numbers"],
        "chunks": ["text", "source_fragments", "recommendation_ids"],
        "citations": ["citation"],
    }
    fields = fields_by_entity.get(name, [])
    field_mismatches = [
        field
        for field in fields
        if normalize_value(showcase_row.get(field)) != normalize_value(parsed_row.get(field))
    ]
    if not field_mismatches:
        return None
    return {"id": stable_id, "fields": field_mismatches}


def normalize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        ignored = {"schema_version", "dataset_id", "document_kind", "code_version"}
        return {
            key: normalize_value(item)
            for key, item in sorted(value.items())
            if key not in ignored
        }
    return value


def string_value(value: Any) -> str:
    return "" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
