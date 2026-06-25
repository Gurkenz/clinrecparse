from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from clinrec.api.rate_limit import RateLimiter
from clinrec.config import HttpSettings, RateLimitSettings
from clinrec.models.external import (
    ApiErrorKind,
    CatalogResponse,
    ClinrecResponse,
    ExternalApiError,
    NkoListResponse,
    PdfDownloadResult,
)

BASE_URL = "https://apicr.minzdrav.gov.ru/api.ashx"

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JsonPayloadResult:
    endpoint: str
    status_code: int
    content_type: str
    payload: Any
    raw_content: bytes
    response_size_bytes: int
    duration_seconds: float
    code_version: str | None = None


class RetryableResponseError(Exception):
    def __init__(self, response: httpx.Response, duration_seconds: float) -> None:
        super().__init__(f"Retryable HTTP status {response.status_code}")
        self.response = response
        self.duration_seconds = duration_seconds


class ClinrecApiClient:
    def __init__(
        self,
        http: HttpSettings,
        rate_limit: RateLimitSettings,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._http = http
        self._rate_limiter = RateLimiter(rate_limit.requests_per_second)
        self._logger = structlog.get_logger()
        self._client = httpx.Client(
            timeout=http.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> ClinrecApiClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_catalog(self, payload: Mapping[str, Any]) -> CatalogResponse | ExternalApiError:
        return self._validate_payload(
            self.fetch_catalog_payload(payload),
            CatalogResponse,
        )

    def fetch_catalog_payload(
        self,
        payload: Mapping[str, Any],
    ) -> JsonPayloadResult | ExternalApiError:
        return self._fetch_json_payload(
            "GetJsonClinrecsFilterV2",
            method="POST",
            json=payload,
        )

    def fetch_clinrec(self, code_version: str) -> ClinrecResponse | ExternalApiError:
        return self._validate_payload(
            self.fetch_clinrec_payload(code_version),
            ClinrecResponse,
        )

    def fetch_clinrec_payload(
        self,
        code_version: str,
    ) -> JsonPayloadResult | ExternalApiError:
        return self._fetch_json_payload(
            "GetClinrec2",
            method="GET",
            params={"id": code_version, "ssid": "null"},
            code_version=code_version,
        )

    def fetch_pdf(self, code_version: str) -> PdfDownloadResult | ExternalApiError:
        response_or_error = self._request(
            "GET",
            "GetClinrecPdf",
            params={"id": code_version},
            code_version=code_version,
        )
        if isinstance(response_or_error, ExternalApiError):
            return response_or_error

        response, duration_seconds = response_or_error
        content_type = response.headers.get("content-type", "")
        content = response.content
        common = self._error_context(response, "GetClinrecPdf", duration_seconds, code_version)

        if not response.is_success:
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.HTTP_STATUS,
                message=f"HTTP status {response.status_code}",
            )
        if not content:
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.EMPTY_RESPONSE,
                message="Empty response body",
            )
        if self._looks_like_html(response):
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.HTML_ERROR,
                message="HTML error page returned instead of PDF",
            )
        if (
            self._main_content_type(content_type) != "application/pdf"
            or not content.startswith(b"%PDF")
        ):
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.UNEXPECTED_CONTENT_TYPE,
                message="Response is not a PDF document",
            )

        return PdfDownloadResult(
            code_version=code_version,
            status_code=response.status_code,
            content_type=content_type,
            content=content,
            response_size_bytes=len(content),
            duration_seconds=duration_seconds,
        )

    def fetch_nko_list(self) -> NkoListResponse | ExternalApiError:
        return self._validate_payload(
            self.fetch_nko_list_payload(),
            NkoListResponse,
        )

    def fetch_nko_list_payload(self) -> JsonPayloadResult | ExternalApiError:
        return self._fetch_json_payload("GetNkoList", method="GET")

    def _fetch_json(
        self,
        endpoint: str,
        response_model: type[T],
        *,
        method: str,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        code_version: str | None = None,
    ) -> T | ExternalApiError:
        payload_or_error = self._fetch_json_payload(
            endpoint,
            method=method,
            params=params,
            json=json,
            code_version=code_version,
        )
        return self._validate_payload(payload_or_error, response_model)

    def _fetch_json_payload(
        self,
        endpoint: str,
        *,
        method: str,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        code_version: str | None = None,
    ) -> JsonPayloadResult | ExternalApiError:
        response_or_error = self._request(
            method,
            endpoint,
            params=params,
            json=json,
            code_version=code_version,
        )
        if isinstance(response_or_error, ExternalApiError):
            return response_or_error

        response, duration_seconds = response_or_error
        common = self._error_context(response, endpoint, duration_seconds, code_version)

        if not response.is_success:
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.HTTP_STATUS,
                message=f"HTTP status {response.status_code}",
            )
        if not response.content:
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.EMPTY_RESPONSE,
                message="Empty response body",
            )
        if self._looks_like_html(response):
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.HTML_ERROR,
                message="HTML error page returned instead of JSON",
            )
        if not self._is_json_content_type(response.headers.get("content-type", "")):
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.UNEXPECTED_CONTENT_TYPE,
                message="Response content type is not JSON",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return ExternalApiError(
                **common,
                kind=ApiErrorKind.INVALID_JSON,
                message=f"Invalid JSON: {exc}",
            )

        return JsonPayloadResult(
            endpoint=endpoint,
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            payload=payload,
            raw_content=response.content,
            response_size_bytes=len(response.content),
            duration_seconds=duration_seconds,
            code_version=code_version,
        )

    def _validate_payload(
        self,
        payload_or_error: JsonPayloadResult | ExternalApiError,
        response_model: type[T],
    ) -> T | ExternalApiError:
        if isinstance(payload_or_error, ExternalApiError):
            return payload_or_error
        try:
            return response_model.model_validate(payload_or_error.payload)
        except ValidationError as exc:
            return ExternalApiError(
                endpoint=payload_or_error.endpoint,
                status_code=payload_or_error.status_code,
                code_version=payload_or_error.code_version,
                content_type=payload_or_error.content_type,
                response_size_bytes=payload_or_error.response_size_bytes,
                duration_seconds=payload_or_error.duration_seconds,
                kind=ApiErrorKind.VALIDATION_ERROR,
                message=f"Response schema validation failed: {exc.errors()}",
            )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        code_version: str | None = None,
    ) -> tuple[httpx.Response, float] | ExternalApiError:
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self._http.retries + 1),
                wait=wait_exponential(
                    multiplier=self._http.backoff_initial_seconds,
                    max=self._http.backoff_max_seconds,
                ),
                retry=retry_if_exception_type((httpx.RequestError, RetryableResponseError)),
                reraise=True,
            ):
                with attempt:
                    return self._send_once(
                        method,
                        endpoint,
                        params=params,
                        json=json,
                        code_version=code_version,
                    )
        except RetryableResponseError as exc:
            return (
                exc.response,
                exc.duration_seconds,
            )
        except httpx.TimeoutException as exc:
            return ExternalApiError(
                endpoint=endpoint,
                kind=ApiErrorKind.REQUEST_ERROR,
                message=str(exc),
                code_version=code_version,
                error_type="timeout",
            )
        except httpx.RequestError as exc:
            return ExternalApiError(
                endpoint=endpoint,
                kind=ApiErrorKind.REQUEST_ERROR,
                message=str(exc),
                code_version=code_version,
                error_type=exc.__class__.__name__,
            )

        raise RuntimeError("unreachable retry loop state")

    def _send_once(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None,
        json: Mapping[str, Any] | None,
        code_version: str | None,
    ) -> tuple[httpx.Response, float]:
        self._rate_limiter.acquire()
        request_params = {"op": endpoint}
        if params:
            request_params.update(params)

        started_at = time.perf_counter()
        response = self._client.request(
            method,
            self._base_url,
            params=request_params,
            json=json,
        )
        duration_seconds = time.perf_counter() - started_at
        content_type = response.headers.get("content-type")
        response_size_bytes = len(response.content)

        self._logger.info(
            "api_response_received",
            endpoint=endpoint,
            code_version=code_version,
            status_code=response.status_code,
            content_type=content_type,
            response_size_bytes=response_size_bytes,
            duration_seconds=round(duration_seconds, 6),
        )

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableResponseError(response, duration_seconds)

        return response, duration_seconds

    def _error_context(
        self,
        response: httpx.Response,
        endpoint: str,
        duration_seconds: float,
        code_version: str | None,
    ) -> dict[str, Any]:
        return {
            "endpoint": endpoint,
            "status_code": response.status_code,
            "code_version": code_version,
            "content_type": response.headers.get("content-type"),
            "response_size_bytes": len(response.content),
            "duration_seconds": duration_seconds,
        }

    @staticmethod
    def _main_content_type(content_type: str) -> str:
        return content_type.split(";", maxsplit=1)[0].strip().lower()

    @classmethod
    def _is_json_content_type(cls, content_type: str) -> bool:
        main_type = cls._main_content_type(content_type)
        return main_type == "application/json" or main_type.endswith("+json")

    @classmethod
    def _looks_like_html(cls, response: httpx.Response) -> bool:
        content_type = cls._main_content_type(response.headers.get("content-type", ""))
        if content_type in {"text/html", "application/xhtml+xml"}:
            return True
        prefix = response.content.lstrip()[:64].lower()
        return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")
