from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clinrec.bank.common import sha256_bytes
from clinrec.parsed.showcase import ParsedShowcaseOptions, build_parsed_showcase

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def raw_document(code_version: str, sections: list[dict[str, Any]]) -> bytes:
    code_text, version_text = code_version.split("_", maxsplit=1)
    payload = {
        "id": code_version,
        "db_id": int(code_text) * 10 + int(version_text),
        "code": int(code_text),
        "version": int(version_text),
        "name": f"Clinical recommendation {code_version}",
        "status": 0,
        "adult": True,
        "child": False,
        "mkbs": [],
        "specialities": [],
        "proff_associations": [],
        "obj": {"sections": sections},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_corpus_document(root: Path, code_version: str, sections: list[dict[str, Any]]) -> None:
    document_root = root / "current" / code_version
    document_root.mkdir(parents=True)
    raw = raw_document(code_version, sections)
    (document_root / "getclinrec.json").write_bytes(raw)
    (document_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "code_version": code_version,
                "sha256": sha256_bytes(raw),
                "size": len(raw),
                "validation": "valid",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (document_root / "catalog-record.json").write_text(
        json.dumps({"code_version": code_version, "name": "Catalog title"}),
        encoding="utf-8",
    )
    (document_root / "catalog-candidates.json").write_text(
        json.dumps({"candidates": [{"code_version": code_version}]}),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_showcase_builds_zip_and_keeps_occurrence_identity(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    sections = [
        {
            "id": "intro",
            "title": "1. Intro",
            "content": (
                "<p>Text [1].</p>"
                "<img alt='scan one' src='data:image/png;base64,"
                f"{PNG_1X1}'>"
                "<img alt='scan two' src='data:image/png;base64,"
                f"{PNG_1X1}'>"
                "<table><tr><th>A</th></tr><tr><td>B</td></tr></table>"
            ),
        },
        {
            "id": "intro",
            "title": "1. Intro again",
            "content": "<p>Recommended follow-up text.</p>",
        },
    ]
    write_corpus_document(corpus, "270_3", sections)

    summary = build_parsed_showcase(
        ParsedShowcaseOptions(
            input_corpus=corpus,
            code_version="270_3",
            output=tmp_path / "showcase" / "270_3",
        )
    )

    output = summary.output
    section_rows = read_jsonl(output / "canonical" / "sections.jsonl")
    image_rows = read_jsonl(output / "canonical" / "images.jsonl")
    asset_rows = read_jsonl(output / "canonical" / "assets.jsonl")
    table_chunks = read_jsonl(output / "ml" / "table-chunks.jsonl")
    image_chunks = read_jsonl(output / "ml" / "image-chunks.jsonl")

    assert summary.zip_verified
    assert summary.hard_errors == 0
    assert summary.sections == 2
    assert summary.tables == 1
    assert summary.image_occurrences == 2
    assert summary.unique_assets == 1
    assert summary.table_chunks == 1
    assert summary.image_chunks == 2
    assert summary.archive.exists()
    assert [row["occurrence_index"] for row in section_rows] == [0, 1]
    assert [row["section_key"] for row in section_rows] == ["intro#0", "intro#1"]
    assert len({row["image_id"] for row in image_rows}) == 2
    assert len({row["asset_id"] for row in image_rows}) == 1
    assert asset_rows[0]["asset_id"] == image_rows[0]["asset_id"]
    assert table_chunks[0]["table_id"]
    assert all(row["image_id"] for row in image_chunks)
    assert "data:image/" not in (output / "frontend" / "document.json").read_text(
        encoding="utf-8"
    )
