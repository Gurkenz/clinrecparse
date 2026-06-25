from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from clinrec.api.client import ClinrecApiClient, JsonPayloadResult
from clinrec.api.version_discovery import (
    DiscoveryOptions,
    VersionCandidate,
    check_candidate,
    classify_external_error,
    filter_candidates,
    should_check,
)
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
    ApiErrorKind,
    ExternalApiError,
    VersionAvailability,
    VersionAvailabilityRecord,
)


def make_settings() -> Settings:
    data_root = Path("data")
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


def availability_record(
    availability: VersionAvailability,
    *,
    checked_at: str | None = None,
) -> VersionAvailabilityRecord:
    return VersionAvailabilityRecord(
        requested_code_version="270_1",
        code=270,
        version=1,
        availability=availability,
        http_status=200,
        checked_at=checked_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        attempts=1,
    )


def test_filter_candidates_by_code_range() -> None:
    candidates = [
        VersionCandidate(270, 1),
        VersionCandidate(270, 2),
        VersionCandidate(843, 1),
    ]

    filtered = filter_candidates(candidates, DiscoveryOptions(code=270))
    assert [item.code_version for item in filtered] == [
        "270_1",
        "270_2",
    ]
    assert [
        item.code_version
        for item in filter_candidates(candidates, DiscoveryOptions(from_code=300, to_code=900))
    ] == ["843_1"]


def test_should_check_resume_rules() -> None:
    settings = make_settings()
    candidate = VersionCandidate(270, 1)
    old_date = (datetime.now(UTC) - timedelta(days=8)).isoformat().replace("+00:00", "Z")

    assert should_check(candidate, None, settings, DiscoveryOptions())
    assert not should_check(
        candidate,
        availability_record(VersionAvailability.AVAILABLE_JSON),
        settings,
        DiscoveryOptions(),
    )
    assert should_check(
        candidate,
        availability_record(VersionAvailability.AVAILABLE_JSON),
        settings,
        DiscoveryOptions(force=True),
    )
    assert not should_check(
        candidate,
        availability_record(VersionAvailability.SERVER_ERROR),
        settings,
        DiscoveryOptions(),
    )
    assert should_check(
        candidate,
        availability_record(VersionAvailability.SERVER_ERROR),
        settings,
        DiscoveryOptions(retry_failed=True),
    )
    assert should_check(
        candidate,
        availability_record(VersionAvailability.FORBIDDEN_403, checked_at=old_date),
        settings,
        DiscoveryOptions(),
    )


def test_classify_external_errors() -> None:
    assert (
        classify_external_error(
            ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.HTTP_STATUS,
                message="Forbidden",
                status_code=403,
            )
        )
        == VersionAvailability.FORBIDDEN_403
    )
    assert (
        classify_external_error(
            ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.HTML_ERROR,
                message="HTML",
                status_code=200,
            )
        )
        == VersionAvailability.HTML_ERROR
    )
    assert (
        classify_external_error(
            ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.REQUEST_ERROR,
                message="timed out",
                error_type="timeout",
            )
        )
        == VersionAvailability.TIMEOUT
    )


def test_check_candidate_available_and_mismatch() -> None:
    class Client:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            return JsonPayloadResult(
                endpoint="GetClinrec2",
                status_code=200,
                content_type="application/json",
                payload=self.payload,
                raw_content=b"{}",
                response_size_bytes=2,
                duration_seconds=0.1,
                code_version=code_version,
            )

    available_payload: dict[str, object] = {
        "success": True,
        "obj": {
            "id": "270_2",
            "code": 270,
            "version": 2,
            "title": "Title",
            "sections": [{"id": 1, "title": "Section"}],
        },
    }
    result = check_candidate(Client(available_payload), VersionCandidate(270, 2))
    assert result.availability == VersionAvailability.AVAILABLE_JSON

    mismatch_payload: dict[str, object] = {
        "success": True,
        "obj": {
            "id": "270_3",
            "code": 270,
            "version": 3,
            "title": "",
            "sections": [],
        },
    }
    mismatch = check_candidate(Client(mismatch_payload), VersionCandidate(270, 2))
    assert mismatch.availability == VersionAvailability.ID_MISMATCH
    assert "missing_sections" in (mismatch.error or "")


def test_check_candidate_timeout() -> None:
    class Client:
        def fetch_clinrec_payload(self, _code_version: str) -> ExternalApiError:
            return ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.REQUEST_ERROR,
                message="timeout",
                error_type="timeout",
            )

    result = check_candidate(Client(), VersionCandidate(270, 1))
    assert result.availability == VersionAvailability.TIMEOUT


def test_timeout_exception_from_mock_transport_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout", request=request)

    settings = make_settings()
    with ClinrecApiClient(
        settings.http,
        settings.rate_limit,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = check_candidate(client, VersionCandidate(270, 1))

    assert result.availability == VersionAvailability.TIMEOUT
    assert result.attempts == 1
