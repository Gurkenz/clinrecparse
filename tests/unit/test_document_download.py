from __future__ import annotations

from pathlib import Path

from clinrec.api.client import JsonPayloadResult
from clinrec.api.document_download import (
    DownloadOptions,
    document_directory,
    ensure_json_source,
    ensure_pdf_source,
    filter_download_candidates,
    is_valid_pdf_file,
    load_available_records,
    sha256_bytes,
)
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
from clinrec.models.external import (
    PdfDownloadResult,
    VersionAvailability,
    VersionAvailabilityRecord,
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


def availability(
    code_version: str,
    code: int,
    version: int,
    status: VersionAvailability = VersionAvailability.AVAILABLE_JSON,
) -> VersionAvailabilityRecord:
    return VersionAvailabilityRecord(
        requested_code_version=code_version,
        code=code,
        version=version,
        availability=status,
        http_status=200,
        checked_at="2026-06-25T00:00:00Z",
    )


def test_load_available_records_selects_only_available_json(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_availability_index(
        settings.paths.indexes / "version-availability.jsonl",
        {
            "270_1": availability("270_1", 270, 1, VersionAvailability.FORBIDDEN_403),
            "270_2": availability("270_2", 270, 2),
        },
    )

    records = load_available_records(settings)

    assert [record.requested_code_version for record in records] == ["270_2"]


def test_filter_download_candidates_and_document_directory(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    records = [availability("270_2", 270, 2), availability("843_1", 843, 1)]

    filtered = filter_download_candidates(records, DownloadOptions(code_versions=["843_1"]))

    assert [record.requested_code_version for record in filtered] == ["843_1"]
    assert document_directory(settings, filtered[0]) == settings.paths.documents / "843" / "843_1"


def test_existing_valid_json_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "getclinrec.json"
    content = b'{"success": true}'
    path.write_bytes(content)
    previous = {
        "sha256": sha256_bytes(content),
        "size": len(content),
        "fetched_at": "2026-06-25T00:00:00Z",
    }

    class Client:
        def fetch_clinrec_payload(self, _code_version: str) -> JsonPayloadResult:
            raise AssertionError("should not fetch")

    info = ensure_json_source(Client(), availability("843_1", 843, 1), path, previous, False)

    assert info["status"] == "already_valid"
    assert info["fetched_at"] == "2026-06-25T00:00:00Z"


def test_corrupted_pdf_is_redownloaded(tmp_path: Path) -> None:
    path = tmp_path / "official.pdf"
    path.write_bytes(b"not a pdf")
    previous = {"sha256": sha256_bytes(b"not a pdf"), "size": 9}

    class Client:
        def fetch_pdf(self, code_version: str) -> PdfDownloadResult:
            content = b"%PDF-1.4\n%%EOF"
            return PdfDownloadResult(
                code_version=code_version,
                status_code=200,
                content_type="application/pdf",
                content=content,
                response_size_bytes=len(content),
                duration_seconds=0.1,
            )

    info = ensure_pdf_source(Client(), availability("843_1", 843, 1), path, previous, False)

    assert info["status"] == "downloaded"
    assert is_valid_pdf_file(path)
    assert not path.with_suffix(".pdf.part").exists()
