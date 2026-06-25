from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ApiErrorKind(StrEnum):
    HTTP_STATUS = "http_status"
    RATE_LIMITED_429 = "rate_limited_429"
    REQUEST_ERROR = "request_error"
    EMPTY_RESPONSE = "empty_response"
    HTML_ERROR = "html_error"
    INVALID_JSON = "invalid_json"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    VALIDATION_ERROR = "validation_error"
    CIRCUIT_OPEN = "circuit_open"


class VersionAvailability(StrEnum):
    AVAILABLE_JSON = "available_json"
    FORBIDDEN_403 = "forbidden_403"
    NOT_FOUND_404 = "not_found_404"
    RATE_LIMITED_429 = "rate_limited_429"
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
    attempts: int = 1
    retry_after: str | None = None
    server: str | None = None
    date: str | None = None
    safe_body_preview: str | None = None
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
    apply_status: Any = Field(
        default=None,
        validation_alias=AliasChoices("ApplyStatus", "apply_status", "applyStatus"),
    )
    apply_status_calculated: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ApplyStatusCalculated",
            "apply_status_calculated",
            "applyStatusCalculated",
        ),
    )
    npc_approved: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "NPC_approved",
            "NpcApproved",
            "NPCApproved",
            "npc_approved",
        ),
    )
    age_category: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("AgeCategory", "ageCategory", "age_category"),
    )
    age_category_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AgeCategoryStr",
            "AgeCategoryName",
            "ageCategoryName",
            "age_category_name",
        ),
    )
    publish_date: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PublishDate", "publishdate", "publish_date"),
    )
    publish_date_str: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PublishDateStr", "publish_date_str"),
    )
    created: str | None = Field(
        default=None,
        validation_alias=AliasChoices("Created", "created"),
    )
    created_str: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CreatedStr", "created_str"),
    )
    prev_cr_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("PrevCrId", "prev_cr_id", "prevCrId"),
    )
    developers: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("Developers", "developers", "Developer", "developer"),
    )
    mkbs: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("MKBs", "Mkbs", "mkbs", "Mkb", "mkb"),
    )
    specialities: Any = Field(
        default=None,
        validation_alias=AliasChoices("Specialities", "specialities", "Specialties", "specialties"),
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

    db_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("db_id", "dbId", "DbId", "DB_ID"),
    )
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
        validation_alias=AliasChoices("version", "Version", "ver", "Ver"),
    )
    ver: int | str | None = Field(default=None, validation_alias=AliasChoices("ver", "Ver"))
    title: str | None = Field(
        default=None,
        validation_alias=AliasChoices("title", "Title", "name", "Name"),
    )
    created: str | None = Field(default=None, validation_alias=AliasChoices("created", "Created"))
    status: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("status", "Status"),
    )
    adult: bool | None = Field(default=None, validation_alias=AliasChoices("adult", "Adult"))
    child: bool | None = Field(default=None, validation_alias=AliasChoices("child", "Child"))
    npc_approved: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("NPC_approved", "npc_approved", "NpcApproved"),
    )
    approved: Any = Field(default=None, validation_alias=AliasChoices("approved", "Approved"))
    publish_date: str | None = Field(
        default=None,
        validation_alias=AliasChoices("publish_date", "PublishDate"),
    )
    apply_status: Any = Field(
        default=None,
        validation_alias=AliasChoices("apply_status", "ApplyStatus"),
    )
    apply_status_calculated: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("apply_status_calculated", "ApplyStatusCalculated"),
    )
    prev_cr_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("prev_cr_id", "PrevCrId"),
    )
    proff_associations: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("proff_associations", "ProffAssociations"),
    )
    mkbs: Any = Field(default_factory=list, validation_alias=AliasChoices("mkbs", "Mkbs", "MKBs"))
    specialities: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("specialities", "Specialities", "specialties"),
    )
    specialityids: Any = Field(
        default_factory=list,
        validation_alias=AliasChoices("specialityids", "SpecialityIds", "speciality_ids"),
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
        metadata_keys = (
            "db_id",
            "id",
            "code",
            "version",
            "ver",
            "name",
            "Name",
            "title",
            "Title",
            "created",
            "status",
            "adult",
            "child",
            "NPC_approved",
            "approved",
            "publish_date",
            "apply_status",
            "apply_status_calculated",
            "prev_cr_id",
            "proff_associations",
            "mkbs",
            "specialities",
            "specialityids",
        )
        for key in metadata_keys:
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
        validation_alias=AliasChoices("shortname", "short_name", "ShortName", "shortName"),
    )
    raw_short_name: str | None = None
    engname: str | None = Field(default=None, validation_alias=AliasChoices("engname", "EngName"))
    engshortname: str | None = Field(
        default=None,
        validation_alias=AliasChoices("engshortname", "EngShortName"),
    )
    profile: Any = None
    url: str | None = None

    @model_validator(mode="after")
    def keep_raw_short_name(self) -> NkoOrganization:
        self.raw_short_name = self.short_name
        if self.short_name:
            self.short_name = " ".join(self.short_name.split())
        return self


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
    apply_status: Any = None
    apply_status_calculated: int | None = None
    npc_approved: bool | None
    age_category: int | None
    age_category_name: str | None
    publish_date: str | None
    created_date: str | None
    prev_cr_id: int | None = None
    developers: list[Any]
    mkbs: list[Any]
    specialities: Any = None


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
    raw_short_name: str | None = None
    engname: str | None = None
    engshortname: str | None = None
    profile: Any = None
    url: str | None = None


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
