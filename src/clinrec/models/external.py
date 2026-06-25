from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ApiErrorKind(StrEnum):
    HTTP_STATUS = "http_status"
    REQUEST_ERROR = "request_error"
    EMPTY_RESPONSE = "empty_response"
    HTML_ERROR = "html_error"
    INVALID_JSON = "invalid_json"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    VALIDATION_ERROR = "validation_error"


class VersionAvailability(StrEnum):
    AVAILABLE_JSON = "available_json"
    FORBIDDEN_403 = "forbidden_403"
    NOT_FOUND_404 = "not_found_404"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    HTML_ERROR = "html_error"
    EMPTY_RESPONSE = "empty_response"
    ID_MISMATCH = "id_mismatch"


class ExternalApiError(BaseModel):
    model_config = ConfigDict(extra="allow")

    endpoint: str
    kind: ApiErrorKind
    message: str
    status_code: int | None = None
    code_version: str | None = None
    content_type: str | None = None
    response_size_bytes: int = 0
    duration_seconds: float = 0.0
    error_type: str | None = None


class CatalogRecord(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_record_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("Id", "ID", "id", "SourceRecordId", "source_record_id"),
    )
    code_version: str = Field(
        validation_alias=AliasChoices("CodeVersion", "codeVersion", "code_version", "Id", "id")
    )
    code: int | str | None = Field(default=None, validation_alias=AliasChoices("Code", "code"))
    version: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("Version", "version"),
    )
    title: str | None = Field(default=None, validation_alias=AliasChoices("Name", "Title", "name"))
    status: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("Status", "status"),
    )
    npc_approved: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("NpcApproved", "NPCApproved", "npc_approved"),
    )
    age_category: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("AgeCategory", "ageCategory", "age_category"),
    )
    age_category_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AgeCategoryName", "ageCategoryName", "age_category_name"),
    )
    publish_date: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PublishDate", "publishdate", "publish_date"),
    )
    developers: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("Developers", "developers", "Developer", "developer"),
    )
    mkbs: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("MKBs", "Mkbs", "mkbs", "Mkb", "mkb"),
    )


class CatalogResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    data: list[CatalogRecord] = Field(
        default_factory=list,
        validation_alias=AliasChoices("Data", "data", "Records", "records", "Rows", "rows"),
    )
    total: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TotalRecords",
            "totalRecords",
            "Total",
            "total",
            "TotalRows",
            "totalRows",
        ),
    )
    page_size: int | None = Field(
        default=None,
        validation_alias=AliasChoices("PageSize", "pageSize", "page_size"),
    )
    current_page: int | None = Field(
        default=None,
        validation_alias=AliasChoices("CurrentPage", "currentPage", "current_page"),
    )
    success: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("Success", "success"),
    )
    errors: list[str] | str | None = Field(
        default=None,
        validation_alias=AliasChoices("Errors", "errors", "Erorrs"),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_bare_record_list(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"Data": value}
        return value


class ClinrecSection(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | int | None = Field(default=None, validation_alias=AliasChoices("id", "Id", "ID"))
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "Title", "name", "Name"),
    )
    text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("text", "Text", "content", "Content"),
    )
    html: str | None = Field(default=None, validation_alias=AliasChoices("html", "Html", "HTML"))
    sections: list[ClinrecSection] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sections", "Sections", "children", "Children"),
    )


class ClinrecDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "Id", "ID"),
    )
    code_version: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "code_version",
            "CodeVersion",
            "codeVersion",
            "CodeVersionId",
            "id",
            "Id",
            "ID",
        ),
    )
    code: int | str | None = Field(default=None, validation_alias=AliasChoices("code", "Code"))
    version: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("version", "Version"),
    )
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "Title", "name", "Name"),
    )
    sections: list[ClinrecSection] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sections", "Sections"),
    )


class ClinrecResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    success: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("success", "Success"),
    )
    obj: ClinrecDocument = Field(validation_alias=AliasChoices("obj", "Obj", "data", "Data"))
    errors: list[str] | str | None = Field(
        default=None,
        validation_alias=AliasChoices("errors", "Errors", "Erorrs"),
    )

    @model_validator(mode="before")
    @classmethod
    def merge_top_level_document_metadata(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "obj" not in value or not isinstance(value["obj"], dict):
            return value
        obj = dict(value["obj"])
        for key in ("id", "code", "version", "name", "Name", "title", "Title"):
            if key in value and key not in obj:
                obj[key] = value[key]
        merged = dict(value)
        merged["obj"] = obj
        return merged


class NkoOrganization(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | int | None = Field(default=None, validation_alias=AliasChoices("id", "Id", "ID"))
    name: str = Field(validation_alias=AliasChoices("name", "Name", "FullName", "fullname"))
    short_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("short_name", "ShortName", "shortName"),
    )


class NkoPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    success: bool | None = None
    data: list[NkoOrganization] = Field(default_factory=list)
    errors: list[str] | str | None = Field(
        default=None,
        validation_alias=AliasChoices("errors", "Errors", "Erorrs"),
    )


class NkoListResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    d: NkoPayload


class PdfDownloadResult(BaseModel):
    endpoint: Literal["GetClinrecPdf"] = "GetClinrecPdf"
    code_version: str
    status_code: int
    content_type: str
    content: bytes
    response_size_bytes: int
    duration_seconds: float


class NormalizedDate(BaseModel):
    raw: str | None = None
    epoch_ms: int | None = None
    utc: str | None = None
    source_timezone: str = "Europe/Moscow"


class NormalizedCatalogRecord(BaseModel):
    source_record_id: int | None
    code: int | None
    version: int | None
    code_version: str
    name: str
    status: int | None
    npc_approved: bool | None
    age_category: int | None
    age_category_name: str | None
    publish_date_raw: str | None
    publish_date_epoch_ms: int | None
    publish_date_utc: str | None
    publish_date_source_timezone: str
    publish_date_source: str | None
    developers: list[Any]
    mkbs: list[Any]


class QaIssue(BaseModel):
    severity: str
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class CatalogQaReport(BaseModel):
    timestamp: str
    active_records: int
    all_statuses_records: int
    issues: list[QaIssue] = Field(default_factory=list)


class ReferenceOrganization(BaseModel):
    id: str | int | None
    name: str
    short_name: str | None = None


class VersionAvailabilityRecord(BaseModel):
    requested_code_version: str
    code: int
    version: int
    availability: VersionAvailability
    http_status: int | None = None
    checked_at: str
    attempts: int = 1
    title: str | None = None
    error: str | None = None
    source: str = "GetClinrec2"


ApiResult = (
    CatalogResponse
    | ClinrecResponse
    | NkoListResponse
    | PdfDownloadResult
    | ExternalApiError
)
