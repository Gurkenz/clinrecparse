from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from clinrec.api.client import ClinrecApiClient
from clinrec.api.version_discovery import DiscoveryOptions, discover_versions
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


def write_catalog_index(settings: Settings, rows: list[dict[str, Any]]) -> None:
    settings.paths.indexes.mkdir(parents=True, exist_ok=True)
    with (settings.paths.indexes / "catalog.jsonl").open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def clinrec_payload(code: int, version: int) -> dict[str, Any]:
    return {
        "success": True,
        "obj": {
            "id": f"{code}_{version}",
            "code": code,
            "version": version,
            "title": f"Document {code}_{version}",
            "sections": [{"id": 1, "title": "Section"}],
        },
    }


def response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def test_discover_versions_270_checkpoint_and_statuses(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_index(
        settings,
        [
            {"code": 270, "version": 3, "code_version": "270_3"},
            {"code": 843, "version": 1, "code_version": "843_1"},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        code_version = request.url.params["id"]
        if code_version == "270_1":
            return response({"error": "forbidden"}, status_code=403)
        if code_version == "270_2":
            return response(clinrec_payload(270, 2))
        if code_version == "270_3":
            return response(clinrec_payload(270, 3))
        raise AssertionError(f"Unexpected request {code_version}")

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=httpx.MockTransport(handler),
    ) as client:
        summary = discover_versions(
            settings,
            client,
            DiscoveryOptions(code=270, force=True, timestamp="20260625T030000Z"),
        )

    assert summary.checked == 3
    rows = [
        json.loads(line)
        for line in (settings.paths.indexes / "version-availability.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    by_id = {row["requested_code_version"]: row for row in rows}
    assert by_id["270_1"]["availability"] == "forbidden_403"
    assert by_id["270_2"]["availability"] == "available_json"
    assert by_id["270_3"]["availability"] == "available_json"
    assert summary.report_path.exists()


def test_discover_versions_dry_run_does_not_call_http_or_write_index(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_index(settings, [{"code": 270, "version": 2, "code_version": "270_2"}])
    summary = discover_versions(
        settings,
        None,
        DiscoveryOptions(code=270, dry_run=True, timestamp="20260625T040000Z"),
    )

    assert summary.dry_run is True
    assert summary.planned == 2
    assert summary.checked == 0
    assert not (settings.paths.indexes / "version-availability.jsonl").exists()
    assert summary.report_path.exists()


def test_discover_versions_cli_dry_run_filters(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_index(
        settings,
        [
            {"code": 270, "version": 2, "code_version": "270_2"},
            {"code": 843, "version": 1, "code_version": "843_1"},
        ],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
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
  jsonl_path: {(settings.paths.logs / "test.jsonl").as_posix()}
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["discover-versions", "--config", str(config_path), "--code", "270", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "planned: 2" in result.output
    assert "dry_run: True" in result.output
