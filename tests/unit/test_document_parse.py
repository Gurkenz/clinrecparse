from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from clinrec.cli import app
from clinrec.config import (
    ConcurrencySettings,
    DiscoverySettings,
    HttpSettings,
    LoggingSettings,
    PathSettings,
    RateLimitSettings,
    Settings,
)
from clinrec.parsing.document import ParseOptions, parse_documents

runner = CliRunner()


def make_settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    return Settings(
        paths=PathSettings(
            data_root=data_root,
            snapshots=data_root / "snapshots",
            references=data_root / "references",
            documents=data_root / "documents",
            indexes=data_root / "indexes",
            reports=data_root / "reports",
            logs=data_root / "logs",
        ),
        http=HttpSettings(
            timeout_seconds=5,
            retries=0,
            backoff_initial_seconds=0.01,
            backoff_max_seconds=0.01,
        ),
        rate_limit=RateLimitSettings(requests_per_second=2),
        concurrency=ConcurrencySettings(default=1, max=2),
        discovery=DiscoverySettings(unavailable_retry_ttl_days=7),
        logging=LoggingSettings(level="INFO", jsonl_path=data_root / "logs" / "test.jsonl"),
    )


def write_config(path: Path, settings: Settings) -> None:
    path.write_text(
        f"""
paths:
  data_root: {settings.paths.data_root.as_posix()}
  snapshots: {settings.paths.snapshots.as_posix()}
  references: {settings.paths.references.as_posix()}
  documents: {settings.paths.documents.as_posix()}
  indexes: {settings.paths.indexes.as_posix()}
  reports: {settings.paths.reports.as_posix()}
  logs: {settings.paths.logs.as_posix()}
http:
  timeout_seconds: 5
  retries: 0
  backoff_initial_seconds: 0.01
  backoff_max_seconds: 0.01
rate_limit:
  requests_per_second: 2
concurrency:
  default: 1
  max: 2
discovery:
  unavailable_retry_ttl_days: 7
logging:
  level: INFO
  jsonl_path: {settings.paths.logs.as_posix()}/test.jsonl
""".lstrip(),
        encoding="utf-8",
    )


def seed_document(settings: Settings) -> Path:
    document_dir = settings.paths.documents / "843" / "843_1"
    source_dir = document_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    image_bytes = b"\x89PNG\r\n\x1a\n"
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "success": True,
        "obj": {
            "code_version": "843_1",
            "code": 843,
            "version": 1,
            "title": "Глаукомы вторичные",
            "sections": [
                {
                    "id": "doc_crat_info_1_2",
                    "title": "1.2 Диагностика",
                    "found": True,
                    "donotsearch": False,
                    "required": True,
                    "rules": [{"kind": "fixture"}],
                    "html": f"""
<h3>1.1.1. Этиология заболевания</h3>
<p><strong>Рекомендуется</strong> выполнить обследование [8-10, 109].</p>
<p>УУР: С; УДД: 5</p>
<p>Комментарий: учитывать клиническую картину.</p>
<p><custom-tag>Нестандартный тег сохранен.</custom-tag></p>
<table>
  <tr><th>Показатель</th><th>Значение</th></tr>
  <tr><td>А</td><td>1</td></tr>
</table>
<table>
  <caption>Таблица 2 - изображение</caption>
  <tr><th>Схема</th><th>Описание</th></tr>
  <tr><td><img alt="Схема" src="data:image/png;base64,{image_base64}"></td><td>Текст</td></tr>
</table>
""",
                }
            ],
        },
    }
    (source_dir / "getclinrec.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    catalog = {
        "code": 843,
        "version": 1,
        "code_version": "843_1",
        "name": "Глаукомы вторичные",
        "age_category": 1,
        "mkbs": [{"MkbCode": "H40.3"}],
        "developers": [{"NkoName": "Ассоциация"}],
    }
    (source_dir / "catalog-record.json").write_text(
        json.dumps(catalog, ensure_ascii=False),
        encoding="utf-8",
    )
    return document_dir


def test_parse_documents_writes_normalized_outputs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    document_dir = seed_document(settings)

    summary = parse_documents(
        settings,
        ParseOptions(code_versions=["843_1"], timestamp="20260625T000000Z"),
    )

    assert summary.parsed == 1
    parsed = json.loads((document_dir / "parsed" / "document.json").read_text(encoding="utf-8"))
    assert parsed["document"]["code_version"] == "843_1"
    assert parsed["document"]["age"] == {"category": 1}
    assert parsed["sections"][0]["raw_data"]["found"] is True
    assert parsed["parser_version"] == "parsed-canonical-0.5"
    assert parsed["validation"]["valid"] is True

    assert len(parsed["tables"]) == 2
    assert parsed["tables"][0]["classification"] == "simple_rectangular"
    assert parsed["tables"][1]["caption"] == "Таблица 2 - изображение"

    image = parsed["images"][0]
    assert image["mime"] == "image/png"
    assert image["sha256"] == hashlib.sha256(b"\x89PNG\r\n\x1a\n").hexdigest()
    assert image["path"].startswith("assets/by-sha256/")
    assert (document_dir / image["path"]).read_bytes() == b"\x89PNG\r\n\x1a\n"

    recommendation = parsed["recommendations"][0]
    assert recommendation["uur"] == "C"
    assert recommendation["udd"] == "5"
    reference = parsed["references"][0]
    assert reference["source_text"] == "[8-10, 109]"
    assert reference["numbers"] == [8, 9, 10, 109]
    assert recommendation["comments"] == ["Комментарий: учитывать клиническую картину."]

    markdown = (document_dir / "parsed" / "content.md").read_text(encoding="utf-8")
    assert "## 1.2" in markdown
    assert "<table" in markdown
    assert "assets/by-sha256/" in markdown

    chunks = [
        json.loads(line)
        for line in (document_dir / "parsed" / "search_chunks.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert any(chunk["type"] == "text" for chunk in chunks)
    assert any(chunk["uur"] == "C" for chunk in chunks)

    qa = json.loads((document_dir / "qa" / "parse-report.json").read_text(encoding="utf-8"))
    issue_codes = {issue["code"] for issue in qa["issues"]}
    assert "unknown_html_tag_removed" in issue_codes
    assert qa["validation"]["valid"] is True


def test_parse_cli_reports_missing_selected_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    document_dir = settings.paths.documents / "843" / "843_1"
    document_dir.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, settings)

    result = runner.invoke(app, ["parse", "--config", str(config_path), "--code-version", "843_1"])

    assert result.exit_code == 1
    assert "843_1: status=failed" in result.output
    qa = json.loads((document_dir / "qa" / "parse-report.json").read_text(encoding="utf-8"))
    assert qa["issues"][0]["code"] == "missing_source_json"


def test_parse_cli_writes_artifacts_for_selected_document(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    document_dir = seed_document(settings)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, settings)

    result = runner.invoke(app, ["parse", "--config", str(config_path), "--code-version", "843_1"])

    assert result.exit_code == 0
    assert "parse completed" in result.output
    assert "843_1: status=parsed" in result.output
    assert (document_dir / "parsed" / "document.json").exists()


def test_qa_cli_checks_parsed_document_and_strict_pdf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_document(settings)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, settings)

    parse_result = runner.invoke(
        app,
        ["parse", "--config", str(config_path), "--code-version", "843_1"],
    )
    assert parse_result.exit_code == 0

    qa_result = runner.invoke(
        app,
        ["qa", "--config", str(config_path), "--code-version", "843_1"],
    )
    assert qa_result.exit_code == 0
    assert "qa completed" in qa_result.output

    strict_result = runner.invoke(
        app,
        ["qa", "--config", str(config_path), "--code-version", "843_1", "--strict-pdf"],
    )
    assert strict_result.exit_code == 1
    assert "errors: 1" in strict_result.output
