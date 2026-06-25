from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from clinrec.api.client import ClinrecApiClient
from clinrec.config import HttpSettings, RateLimitSettings
from clinrec.models.external import (
    ApiErrorKind,
    CatalogResponse,
    ClinrecResponse,
    ExternalApiError,
    NkoListResponse,
    PdfDownloadResult,
)

FIXTURES = Path("tests/fixtures")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def response_from_fixture(
    status_code: int,
    fixture_name: str,
    content_type: str,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": content_type},
        content=fixture_bytes(fixture_name),
    )


def make_client(handler: httpx.MockTransport) -> ClinrecApiClient:
    return ClinrecApiClient(
        HttpSettings(
            timeout_seconds=5,
            retries=0,
            backoff_initial_seconds=0.01,
            backoff_max_seconds=0.01,
        ),
        RateLimitSettings(requests_per_second=2),
        transport=handler,
    )


def mock_handler(request: httpx.Request) -> httpx.Response:
    op = request.url.params.get("op")
    code_version = request.url.params.get("id")

    routes: dict[tuple[str | None, str | None], tuple[int, str, str]] = {
        ("GetJsonClinrecsFilterV2", None): (
            200,
            "catalog_843_1_real_shape.json",
            "application/json",
        ),
        ("GetClinrec2", "843_1"): (200, "clinrec_843_1_real_shape.json", "application/json"),
        ("GetClinrec2", "270_2"): (200, "clinrec_270_2_real_shape.json", "application/json"),
        ("GetClinrec2", "270_3"): (200, "clinrec_270_3_real_shape.json", "application/json"),
        ("GetClinrec2", "270_1"): (403, "270_1_403.html", "text/html"),
        ("GetClinrec2", "html_200"): (200, "html_error_200.html", "text/html"),
        ("GetClinrec2", "bad_json"): (200, "invalid_json.json", "application/json"),
        ("GetClinrec2", "text_200"): (200, "unexpected_content.txt", "text/plain"),
        ("GetClinrecPdf", "843_1"): (200, "pdf_sample.pdf", "application/pdf"),
        ("GetNkoList", None): (200, "nko_list_real_shape.json", "application/json"),
    }
    status_code, fixture_name, content_type = routes[(op, code_version)]
    return response_from_fixture(status_code, fixture_name, content_type)


def test_fetch_catalog_with_mock_http() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_catalog({"pageSize": 1000})

    assert isinstance(result, CatalogResponse)
    assert result.data[0].code_version == "843_1"


def test_fetch_clinrec_success_with_mock_http() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_clinrec("270_2")

    assert isinstance(result, ClinrecResponse)
    assert result.obj.code_version == "270_2"
    assert result.obj.sections[0].title == "Introduction"


def test_fetch_nko_list_with_mock_http() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_nko_list()

    assert isinstance(result, NkoListResponse)
    assert result.d.data[0].short_name == "FMA"


def test_fetch_pdf_success_with_mock_http() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_pdf("843_1")

    assert isinstance(result, PdfDownloadResult)
    assert result.content.startswith(b"%PDF")


def test_fetch_clinrec_403_is_unavailable_error() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_clinrec("270_1")

    assert isinstance(result, ExternalApiError)
    assert result.kind == ApiErrorKind.HTTP_STATUS
    assert result.status_code == 403
    assert result.code_version == "270_1"


def test_fetch_clinrec_html_200_is_not_success() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_clinrec("html_200")

    assert isinstance(result, ExternalApiError)
    assert result.kind == ApiErrorKind.HTML_ERROR


def test_fetch_clinrec_corrupted_json() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_clinrec("bad_json")

    assert isinstance(result, ExternalApiError)
    assert result.kind == ApiErrorKind.INVALID_JSON


def test_fetch_clinrec_unexpected_content_type() -> None:
    with make_client(httpx.MockTransport(mock_handler)) as client:
        result = client.fetch_clinrec("text_200")

    assert isinstance(result, ExternalApiError)
    assert result.kind == ApiErrorKind.UNEXPECTED_CONTENT_TYPE


def test_request_error_does_not_expose_sensitive_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture connect error", request=request)

    with make_client(httpx.MockTransport(handler)) as client:
        result = client.fetch_clinrec("843_1")

    assert isinstance(result, ExternalApiError)
    dumped: dict[str, Any] = result.model_dump()
    assert "authorization" not in dumped
    assert "cookie" not in dumped


def test_http_error_context_redacts_body_preview_and_sends_user_agent() -> None:
    seen_user_agent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_user_agent.append(request.headers["user-agent"])
        return httpx.Response(
            500,
            headers={"content-type": "application/json", "retry-after": "1"},
            content=b'{"token":"secret-value","message":"failed"}',
        )

    with make_client(httpx.MockTransport(handler)) as client:
        result = client.fetch_clinrec("843_1")

    assert isinstance(result, ExternalApiError)
    assert seen_user_agent == ["clinrecparse/0.1 contact: local-development"]
    assert result.retry_after == "1"
    assert result.safe_body_preview is not None
    assert "secret-value" not in result.safe_body_preview
