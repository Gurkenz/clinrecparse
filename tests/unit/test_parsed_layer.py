from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clinrec.bank.common import BankError, sha256_bytes
from clinrec.parsed.layer import (
    ParsedBuildOptions,
    build_parsed_dataset,
    build_parsed_diff,
    export_parsed_dataset,
    validate_parsed_dataset,
)

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
        "obj": {"sections": sections},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_raw(root: Path, code_version: str, sections: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True)
    raw = raw_document(code_version, sections)
    (root / "getclinrec.json").write_bytes(raw)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "code_version": code_version,
                "sha256": sha256_bytes(raw),
                "size": len(raw),
                "validation": "valid",
            }
        ),
        encoding="utf-8",
    )


def test_parsed_build_validate_export_and_diff(tmp_path: Path) -> None:
    corpus = tmp_path / "research" / "corpus"
    current_sections = [
        {
            "id": "section_intro",
            "name": "Intro",
                "content": (
                    "<p onclick='bad()'>Hello <a href='javascript:bad()'>link</a></p>"
                    f"<img alt='chart' src='data:image/png;base64,{PNG_1X1}'>"
                    "<table><tr><th>A</th></tr><tr><td>B</td></tr></table>"
                ),
        }
    ]
    previous_sections = [
        {
            "id": "section_intro",
            "name": "Intro",
            "content": "<p>Hello old link</p><table><tr><td>A</td></tr></table>",
        }
    ]
    write_raw(corpus / "current" / "270_3", "270_3", current_sections)
    write_raw(corpus / "previous" / "270_3" / "270_2", "270_2", previous_sections)
    parsed = tmp_path / "parsed" / "pilot-v1"

    build = build_parsed_dataset(
        ParsedBuildOptions(
            input=corpus,
            output=parsed,
            code_versions=("270_3",),
            include_previous=True,
        )
    )
    validation = validate_parsed_dataset(parsed)
    diff = build_parsed_diff(parsed)
    export = export_parsed_dataset(parsed, tmp_path / "exports" / "pilot-v1")

    section = json.loads((parsed / "sections.jsonl").read_text(encoding="utf-8").splitlines()[0])
    image = json.loads((parsed / "images.jsonl").read_text(encoding="utf-8").splitlines()[0])

    assert build.parsed_documents == 2
    assert build.sections == 2
    assert build.tables == 2
    assert build.images == 1
    assert validation.valid
    assert validation.errors == 0
    assert diff.pairs == 1
    assert diff.section_changes >= 1
    assert export.frontend_documents == 2
    assert export.assets == 1
    assert "data:image/" not in section["normalized_html"]
    assert "onclick" not in section["normalized_html"]
    assert "javascript:" not in section["normalized_html"]
    assert image["asset_path"]
    assert (parsed / image["asset_path"]).exists()
    assert (tmp_path / "exports" / "pilot-v1" / "backend" / "documents.jsonl").exists()
    assert (tmp_path / "exports" / "pilot-v1" / "rag" / "embedding-input.jsonl").exists()
    assert not (parsed / "diff").exists()
    assert diff.output == tmp_path / "parsed" / "pilot-v1-diff"
    assert (diff.output / "checksums.sha256").exists()


def test_export_refuses_invalid_release_atomically(tmp_path: Path) -> None:
    corpus = tmp_path / "research" / "corpus"
    write_raw(
        corpus / "current" / "270_3",
        "270_3",
        [{"id": "section_intro", "name": "Intro", "content": "<p>Hello</p>"}],
    )
    parsed = tmp_path / "parsed" / "pilot-v1"
    build_parsed_dataset(
        ParsedBuildOptions(input=corpus, output=parsed, code_versions=("270_3",))
    )
    validation_path = parsed / "reports" / "document-validation.jsonl"
    validation = json.loads(validation_path.read_text(encoding="utf-8").splitlines()[0])
    validation["valid"] = False
    validation_path.write_text(json.dumps(validation) + "\n", encoding="utf-8")

    output = tmp_path / "exports" / "pilot-v1"
    with pytest.raises(BankError):
        export_parsed_dataset(parsed, output)

    assert not output.exists()
    assert not (tmp_path / "exports" / ".pilot-v1.part").exists()


def test_diff_writes_separate_output_without_mutating_release(tmp_path: Path) -> None:
    corpus = tmp_path / "research" / "corpus"
    write_raw(
        corpus / "current" / "270_3",
        "270_3",
        [{"id": "section_intro", "name": "Intro", "content": "<p>Hello</p>"}],
    )
    write_raw(
        corpus / "previous" / "270_3" / "270_2",
        "270_2",
        [{"id": "section_intro", "name": "Intro", "content": "<p>Hello old</p>"}],
    )
    parsed = tmp_path / "parsed" / "pilot-v1"
    build_parsed_dataset(
        ParsedBuildOptions(
            input=corpus,
            output=parsed,
            code_versions=("270_3",),
            include_previous=True,
        )
    )
    summary_path = parsed / "reports" / "parsed-summary.json"
    before = summary_path.read_text(encoding="utf-8")

    diff = build_parsed_diff(parsed)

    assert diff.output == tmp_path / "parsed" / "pilot-v1-diff"
    assert (diff.output / "summary.json").exists()
    assert (diff.output / "manifest.json").exists()
    assert (diff.output / "checksums.sha256").exists()
    assert not (parsed / "diff").exists()
    assert summary_path.read_text(encoding="utf-8") == before


def test_cross_document_asset_occurrences_are_merged(tmp_path: Path) -> None:
    corpus = tmp_path / "research" / "corpus"
    image_html = f"<p>Image</p><img alt='chart' src='data:image/png;base64,{PNG_1X1}'>"
    write_raw(
        corpus / "current" / "270_3",
        "270_3",
        [{"id": "section_intro", "name": "Intro", "content": image_html}],
    )
    write_raw(
        corpus / "previous" / "270_3" / "270_2",
        "270_2",
        [{"id": "section_intro", "name": "Intro", "content": image_html}],
    )
    parsed = tmp_path / "parsed" / "pilot-v1"
    build_parsed_dataset(
        ParsedBuildOptions(
            input=corpus,
            output=parsed,
            code_versions=("270_3",),
            include_previous=True,
        )
    )

    assets = [
        json.loads(line)
        for line in (parsed / "assets.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(assets) == 1
    assert len(assets[0]["occurrence_ids"]) == 2
    assert assets[0]["width"] == 1
    assert assets[0]["height"] == 1


def test_nested_table_text_not_duplicated_in_parent_cell(tmp_path: Path) -> None:
    corpus = tmp_path / "research" / "corpus"
    write_raw(
        corpus / "current" / "270_3",
        "270_3",
        [
            {
                "id": "section_intro",
                "name": "Intro",
                "content": (
                    "<table><tr><td>Outer<table><tr><td>Inner</td></tr></table>"
                    "</td></tr></table>"
                ),
            }
        ],
    )
    parsed = tmp_path / "parsed" / "pilot-v1"
    build_parsed_dataset(
        ParsedBuildOptions(input=corpus, output=parsed, code_versions=("270_3",))
    )

    cells = [
        json.loads(line)
        for line in (parsed / "table-cells.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert any(cell["text"] == "Outer" for cell in cells)
    assert any(cell["text"] == "Inner" for cell in cells)
    assert all(cell["text"] != "Outer Inner" for cell in cells)
