from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from clinrec.api.catalog_sync import SyncError, sync_catalog, sync_references
from clinrec.api.client import ClinrecApiClient
from clinrec.config import (
    ConcurrencySettings,
    DiscoverySettings,
    HttpSettings,
    LoggingSettings,
    PathSettings,
    RateLimitSettings,
    Settings,
)


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


def catalog_record(
    record_id: int,
    code: int,
    version: int,
    name: str,
    status: int,
) -> dict[str, Any]:
    return {
        "Id": record_id,
        "Code": code,
        "Version": version,
        "CodeVersion": f"{code}_{version}",
        "Name": name,
        "Status": status,
        "NpcApproved": True,
        "AgeCategory": 1,
        "AgeCategoryName": "Adults",
        "PublishDate": "/Date(1734476342000)/",
        "Developers": [],
        "MKBs": [],
    }


def response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def make_catalog_handler(requests_seen: list[dict[str, Any]]) -> httpx.MockTransport:
    all_records = [
        catalog_record(1, 843, 1, "Fixture 843", 0),
        catalog_record(2, 270, 1, "Fixture 270 old", 1),
        catalog_record(3, 270, 2, "Fixture 270 second", 0),
        catalog_record(4, 270, 3, "Fixture 270 third", 0),
        catalog_record(5, 999, 1, "Fixture archived", 2),
    ]
    active_records = [record for record in all_records if record["Status"] == 0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        is_active = bool(body["filters"])
        records = active_records if is_active else all_records
        page = int(body["currentPage"])
        page_size = 2
        start = (page - 1) * page_size
        payload = {
            "Success": True,
            "TotalRecords": len(records),
            "PageSize": page_size,
            "CurrentPage": page,
            "Data": records[start : start + page_size],
        }
        return response(payload)

    return httpx.MockTransport(handler)


def test_sync_catalog_saves_pages_manifest_and_jsonl(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    requests_seen: list[dict[str, Any]] = []
    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=make_catalog_handler(requests_seen),
    ) as client:
        summary = sync_catalog(settings, client, timestamp="20260625T000000Z")

    assert summary.active.pages == 2
    assert summary.active.records == 3
    assert summary.all_statuses.pages == 3
    assert summary.all_statuses.records == 5
    assert requests_seen[0]["pageSize"] == 1000
    assert requests_seen[1]["pageSize"] == 2
    assert (summary.snapshot_root / "active" / "request.json").exists()
    assert (summary.snapshot_root / "active" / "page-0001.json").exists()
    assert (summary.snapshot_root / "all-statuses" / "page-0003.json").exists()
    assert summary.active_index_path.exists()
    assert summary.all_statuses_index_path.exists()
    assert summary.qa_report_path.exists()

    rows = [
        json.loads(line)
        for line in summary.active_index_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 3
    assert rows[0]["code_version"] == "843_1"
    assert rows[0]["publish_date"] == "2024-12-17"
    assert "publish_date_utc" not in rows[0]

    all_rows = [
        json.loads(line)
        for line in summary.all_statuses_index_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(all_rows) == 5
    manifest = json.loads(
        (summary.snapshot_root / "active" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["actual_records"] == 3
    assert manifest["total_records"] == 3
    assert manifest["unique_code_versions"] == 3
    assert manifest["source_pages"][0]["sha256"]


def test_sync_catalog_fails_when_active_code_versions_are_not_unique(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["filters"]
        duplicate = catalog_record(1, 843, 1, "Fixture 843", 0)
        payload = {
            "Success": True,
            "TotalRecords": 2,
            "PageSize": 2,
            "CurrentPage": 1,
            "Data": [duplicate, {**duplicate, "Id": 2}],
        }
        return response(payload)

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            sync_catalog(settings, client, timestamp="20260625T030000Z")
        except SyncError as exc:
            assert "unique CodeVersion" in str(exc)
        else:
            raise AssertionError("sync_catalog should fail on duplicate active CodeVersion")

    manifest_path = (
        settings.paths.snapshots
        / "catalog"
        / "20260625T030000Z"
        / "active"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["unique_code_versions"] == 1
    assert manifest["duplicates"] == [{"code_version": "843_1", "count": 2}]


def test_sync_references_saves_raw_normalized_and_qa(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return response(
            {
                "d": {
                    "success": True,
                    "data": [
                        {"id": 1, "name": "Organization A", "short_name": "A"},
                        {"id": 1, "name": "Organization A", "short_name": "A duplicate"},
                    ],
                }
            }
        )

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=httpx.MockTransport(handler),
    ) as client:
        summary = sync_references(settings, client, timestamp="20260625T010000Z")

    assert summary.organizations == 2
    assert summary.raw_path.exists()
    assert summary.index_path.exists()
    assert summary.qa_report_path.exists()
    issue_codes = {issue.code for issue in summary.issues}
    assert "duplicate_organization_id" in issue_codes
    assert "duplicate_organization_name" in issue_codes


def test_sync_catalog_error_writes_qa_report(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "application/json"},
            content=b'{"error": "server"}',
        )

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            sync_catalog(settings, client, timestamp="20260625T020000Z")
        except SyncError:
            pass
        else:
            raise AssertionError("sync_catalog should fail on HTTP 500")

    report_path = settings.paths.reports / "catalog-qa-20260625T020000Z.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"][0]["code"] == "catalog_fetch_failed"
