from __future__ import annotations

import json
from pathlib import Path

import httpx

from clinrec.api.client import ClinrecApiClient
from clinrec.api.document_download import DownloadOptions, download_documents
from clinrec.api.version_discovery import write_availability_index
from clinrec.config import (
    ConcurrencySettings,
    DiscoverySettings,
    HttpSettings,
    LoggingSettings,
    PathSettings,
    RateLimitSettings,
    Settings,
)
from clinrec.models.external import VersionAvailability, VersionAvailabilityRecord


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


def availability(code_version: str, code: int, version: int) -> VersionAvailabilityRecord:
    return VersionAvailabilityRecord(
        requested_code_version=code_version,
        code=code,
        version=version,
        availability=VersionAvailability.AVAILABLE_JSON,
        http_status=200,
        checked_at="2026-06-25T00:00:00Z",
    )


def seed_indexes(settings: Settings, records: dict[str, VersionAvailabilityRecord]) -> None:
    write_availability_index(settings.paths.indexes / "version-availability.jsonl", records)
    settings.paths.indexes.mkdir(parents=True, exist_ok=True)
    with (settings.paths.indexes / "catalog.jsonl").open("w", encoding="utf-8") as file:
        for record in records.values():
            file.write(
                json.dumps(
                    {
                        "code": record.code,
                        "version": record.version,
                        "code_version": record.requested_code_version,
                        "name": f"Catalog {record.requested_code_version}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def json_payload(code: int, version: int) -> bytes:
    return json.dumps(
        {
            "id": f"{code}_{version}",
            "code": code,
            "version": version,
            "name": f"Document {code}_{version}",
            "obj": {"sections": [{"id": 1, "title": "Section"}]},
        },
        ensure_ascii=False,
    ).encode("utf-8")


def make_handler(*, pdf_available: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        op = request.url.params["op"]
        code_version = request.url.params["id"]
        code, version = (int(part) for part in code_version.split("_"))
        if op == "GetClinrec2":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json_payload(code, version),
            )
        if op == "GetClinrecPdf" and pdf_available:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4\n%%EOF",
            )
        return httpx.Response(
            403,
            headers={"content-type": "text/html"},
            content=b"<html>forbidden</html>",
        )

    return httpx.MockTransport(handler)


def test_download_success_saves_json_pdf_manifest_and_no_part_files(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_indexes(settings, {"843_1": availability("843_1", 843, 1)})

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=make_handler(),
    ) as client:
        summary = download_documents(
            settings,
            client,
            DownloadOptions(code_versions=["843_1"], force=True),
        )

    document = summary.documents[0]
    source_dir = document.document_dir / "source"
    manifest = json.loads(document.manifest_path.read_text(encoding="utf-8"))
    assert summary.downloaded == 1
    assert (source_dir / "getclinrec.json").read_bytes() == json_payload(843, 1)
    assert (source_dir / "official.pdf").read_bytes().startswith(b"%PDF-")
    assert not list(document.document_dir.rglob("*.part"))
    assert manifest["json"]["status"] == "downloaded"
    assert manifest["pdf"]["status"] == "downloaded"
    assert manifest["catalog_record"]["status"] == "saved"


def test_download_partial_when_pdf_unavailable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_indexes(settings, {"270_2": availability("270_2", 270, 2)})

    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=make_handler(pdf_available=False),
    ) as client:
        summary = download_documents(
            settings,
            client,
            DownloadOptions(code_versions=["270_2"], force=True),
        )

    manifest = json.loads(summary.documents[0].manifest_path.read_text(encoding="utf-8"))
    assert summary.partial == 1
    assert manifest["status"] == "partial"
    assert manifest["json"]["status"] == "downloaded"
    assert manifest["pdf"]["status"] == "unavailable"


def test_download_dry_run_does_not_call_http_or_write_documents(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_indexes(settings, {"843_1": availability("843_1", 843, 1)})

    summary = download_documents(
        settings,
        None,
        DownloadOptions(code_versions=["843_1"], dry_run=True),
    )

    assert summary.planned == 1
    assert summary.dry_run is True
    assert not settings.paths.documents.exists()
