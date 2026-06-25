from clinrec.api.catalog_sync import (
    build_catalog_request,
    normalize_catalog_record,
    parse_source_date,
    validate_catalog_records,
)
from clinrec.models.external import CatalogRecord


def test_build_catalog_request_active_and_all_statuses() -> None:
    active = build_catalog_request(active_only=True, page=1)
    all_statuses = build_catalog_request(active_only=False, page=2, page_size=25)

    assert active["pageSize"] == 1000
    assert active["filters"][0]["fieldName"] == "status"
    assert active["filters"][0]["value1"] == 0
    assert all_statuses["currentPage"] == 2
    assert all_statuses["pageSize"] == 25
    assert all_statuses["filters"] == []


def test_parse_source_date_returns_date_only() -> None:
    parsed = parse_source_date("/Date(1734476342000)/")

    assert parsed == "2024-12-17"
    assert parse_source_date("2024-12-17T22:59:02Z") == "2024-12-17"


def test_normalize_catalog_record_minimum_fields() -> None:
    record = CatalogRecord.model_validate(
        {
            "Id": 1737,
            "Code": 843,
            "Version": 1,
            "CodeVersion": "843_1",
            "Name": "Fixture name",
            "Status": 0,
            "NpcApproved": True,
            "AgeCategory": 1,
            "AgeCategoryName": "Adults",
            "PublishDate": "/Date(1734476342000)/",
            "Developers": [{"Name": "Developer"}],
            "MKBs": ["H40"],
        }
    )

    normalized = normalize_catalog_record(record)

    assert normalized.source_record_id == 1737
    assert normalized.code == 843
    assert normalized.version == 1
    assert normalized.code_version == "843_1"
    assert normalized.name == "Fixture name"
    assert normalized.publish_date == "2024-12-17"
    assert normalized.developers == [{"Name": "Developer"}]
    assert normalized.mkbs == ["H40"]


def test_validate_catalog_records_reports_conflicts() -> None:
    first = normalize_catalog_record(
        CatalogRecord.model_validate(
            {"Id": 1, "Code": 843, "Version": 1, "CodeVersion": "843_1", "Name": "A"}
        )
    )
    duplicate = normalize_catalog_record(
        CatalogRecord.model_validate(
            {"Id": 1, "Code": 843, "Version": 2, "CodeVersion": "843_1", "Name": "B"}
        )
    )
    empty_name = normalize_catalog_record(
        CatalogRecord.model_validate(
            {"Id": 3, "Code": 270, "Version": 3, "CodeVersion": "270_2", "Name": ""}
        )
    )

    issues = validate_catalog_records([first, duplicate, empty_name])
    issue_codes = {issue.code for issue in issues}

    assert "duplicate_source_record_id" in issue_codes
    assert "conflicting_code_version" in issue_codes
    assert "code_version_mismatch" in issue_codes
    assert "empty_name" in issue_codes
