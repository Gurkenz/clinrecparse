from __future__ import annotations

import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError

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
SAFE_BODY_PREVIEW_LIMIT = 4096
SENSITIVE_BODY_RE = re.compile(
    r"(?i)(\"?(?:authorization|cookie|set-cookie|token|password|secret)\"?\s*[:=]\s*\"?)"
    r"([^\"\s;&,}]+)"
)

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
    attempts: int = 1
    code_version: str | None = None


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
        self._consecutive_5xx = 0
        self._client = httpx.Client(
            timeout=http.timeout_seconds,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": http.user_agent},
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

        response, duration_seconds, attempts = response_or_error
        content_type = response.headers.get("content-type", "")
        content = response.content
        common = self._error_context(
            response,
            "GetClinrecPdf",
            duration_seconds,
            code_version,
            attempts,
        )

        if not response.is_success:
            return ExternalApiError(
                **common,
                kind=self._status_error_kind(response.status_code),
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

    def health_probe(self) -> ExternalApiError | None:
        result = self.fetch_catalog_payload({"currentPage": 1, "pageSize": 1, "filters": []})
        if isinstance(result, ExternalApiError):
            return result
        return None

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

        response, duration_seconds, attempts = response_or_error
        common = self._error_context(response, endpoint, duration_seconds, code_version, attempts)

        if not response.is_success:
            return ExternalApiError(
                **common,
                kind=self._status_error_kind(response.status_code),
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
            attempts=attempts,
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
                attempts=payload_or_error.attempts,
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
    ) -> tuple[httpx.Response, float, int] | ExternalApiError:
        if self._consecutive_5xx >= self._http.circuit_breaker_5xx_threshold:
            return ExternalApiError(
                endpoint=endpoint,
                kind=ApiErrorKind.CIRCUIT_OPEN,
                message="Circuit breaker is open after consecutive 5xx responses",
                code_version=code_version,
                attempts=0,
            )

        max_attempts = self._http.retries + 1
        last_request_error: httpx.RequestError | None = None
        total_duration = 0.0
        for attempt_number in range(1, max_attempts + 1):
            try:
                response, duration_seconds = self._send_once(
                    method,
                    endpoint,
                    params=params,
                    json=json,
                    code_version=code_version,
                )
            except httpx.TimeoutException as exc:
                last_request_error = exc
                if attempt_number >= max_attempts:
                    return ExternalApiError(
                        endpoint=endpoint,
                        kind=ApiErrorKind.REQUEST_ERROR,
                        message=str(exc),
                        code_version=code_version,
                        error_type="timeout",
                        attempts=attempt_number,
                    )
                self._sleep_before_retry(attempt_number, None)
                continue
            except httpx.RequestError as exc:
                last_request_error = exc
                if attempt_number >= max_attempts:
                    return ExternalApiError(
                        endpoint=endpoint,
                        kind=ApiErrorKind.REQUEST_ERROR,
                        message=str(exc),
                        code_version=code_version,
                        error_type=exc.__class__.__name__,
                        attempts=attempt_number,
                    )
                self._sleep_before_retry(attempt_number, None)
                continue

            total_duration += duration_seconds
            if response.status_code >= 500:
                self._consecutive_5xx += 1
            else:
                self._consecutive_5xx = 0

            if response.status_code in {429} or response.status_code >= 500:
                if self._consecutive_5xx >= self._http.circuit_breaker_5xx_threshold:
                    return ExternalApiError(
                        **self._error_context(
                            response,
                            endpoint,
                            total_duration,
                            code_version,
                            attempt_number,
                        ),
                        kind=ApiErrorKind.CIRCUIT_OPEN,
                        message="Circuit breaker opened after consecutive 5xx responses",
                    )
                if attempt_number < max_attempts:
                    self._sleep_before_retry(attempt_number, response)
                    continue

            return response, total_duration, attempt_number

        if last_request_error is not None:
            return ExternalApiError(
                endpoint=endpoint,
                kind=ApiErrorKind.REQUEST_ERROR,
                message=str(last_request_error),
                code_version=code_version,
                error_type=last_request_error.__class__.__name__,
                attempts=max_attempts,
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

        return response, duration_seconds

    def _error_context(
        self,
        response: httpx.Response,
        endpoint: str,
        duration_seconds: float,
        code_version: str | None,
        attempts: int,
    ) -> dict[str, Any]:
        return {
            "endpoint": endpoint,
            "status_code": response.status_code,
            "code_version": code_version,
            "content_type": response.headers.get("content-type"),
            "response_size_bytes": len(response.content),
            "duration_seconds": duration_seconds,
            "attempts": attempts,
            "retry_after": response.headers.get("retry-after"),
            "server": response.headers.get("server"),
            "date": response.headers.get("date"),
            "safe_body_preview": self._safe_body_preview(response.content),
        }

    def _sleep_before_retry(
        self,
        attempt_number: int,
        response: httpx.Response | None,
    ) -> None:
        retry_after = self._retry_after_seconds(response) if response is not None else None
        if retry_after is None:
            retry_after = min(
                self._http.backoff_initial_seconds * (2 ** max(0, attempt_number - 1)),
                self._http.backoff_max_seconds,
            )
            retry_after += random.uniform(0, min(retry_after, self._http.backoff_initial_seconds))
        time.sleep(retry_after)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("retry-after")
        if not value:
            return None
        if value.isdigit():
            return max(0.0, float(value))
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, retry_at.timestamp() - time.time())

    @staticmethod
    def _status_error_kind(status_code: int) -> ApiErrorKind:
        if status_code == 429:
            return ApiErrorKind.RATE_LIMITED_429
        return ApiErrorKind.HTTP_STATUS

    @staticmethod
    def _safe_body_preview(content: bytes) -> str:
        preview = content[:SAFE_BODY_PREVIEW_LIMIT].decode("utf-8", errors="replace")
        return SENSITIVE_BODY_RE.sub(r"\1[redacted]", preview)

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
