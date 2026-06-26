from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clinrec.api.catalog_sync import write_json
from clinrec.api.client import JsonPayloadResult
from clinrec.bank.common import (
    BankError,
    BankRecordFilter,
    accepted_catalog_path,
    bank_active_root,
    bank_legacy_root,
    manifest_for_raw_json,
    read_jsonl,
    sha256_bytes,
    write_jsonl,
)
from clinrec.bank.current import download_current_documents
from clinrec.bank.identities import analyze_identities
from clinrec.bank.previous import check_previous_documents, relation_status_for_error
from clinrec.bank.qa import run_bank_qa
from clinrec.bank.reconcile import (
    accept_current_catalog,
    apply_update_plan,
    build_update_plan,
    reconcile_catalogs,
    stage_update,
    update_plan_state,
)
from clinrec.bank.references import enrich_developers, update_references
from clinrec.bank.statuses import analyze_statuses
from clinrec.config import (
    ConcurrencySettings,
    DiscoverySettings,
    HttpSettings,
    LoggingSettings,
    PathSettings,
    RateLimitSettings,
    Settings,
)
from clinrec.models.external import ApiErrorKind, ExternalApiError

FIXTURES = Path("tests/fixtures")


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


def catalog_row(
    code_version: str,
    code: int,
    version: int,
    status: int,
    *,
    source_record_id: int | None = None,
) -> dict[str, Any]:
    return {
        "source_record_id": source_record_id or fixture_source_record_id(code_version),
        "code_version": code_version,
        "code": code,
        "version": version,
        "name": f"Fixture {code_version}",
        "status": status,
        "age_category": 1,
        "developers": [{"Name": "Association"}],
        "mkbs": [{"code": "A00"}],
    }


def fixture_source_record_id(code_version: str) -> int:
    return {
        "773_2": 2191,
        "843_1": 1737,
        "270_2": 1002,
        "270_3": 1003,
    }.get(code_version, 9000)


def write_catalog_indexes(
    settings: Settings,
    *,
    active: list[dict[str, Any]],
    all_statuses: list[dict[str, Any]] | None = None,
) -> None:
    write_jsonl(settings.paths.indexes / "catalog-active.jsonl", active)
    write_jsonl(settings.paths.indexes / "catalog-all-statuses.jsonl", all_statuses or active)


def json_result(code_version: str, content: bytes) -> JsonPayloadResult:
    return JsonPayloadResult(
        endpoint="GetClinrec2",
        status_code=200,
        content_type="application/json",
        payload=json.loads(content.decode("utf-8")),
        raw_content=content,
        response_size_bytes=len(content),
        duration_seconds=0.0,
        code_version=code_version,
    )


def document_bytes(
    code_version: str,
    *,
    code: int,
    version: int,
    status: int,
    db_id: int | None = None,
    title: str | None = None,
) -> bytes:
    payload = {
        "success": True,
        "id": code_version,
        "db_id": db_id if db_id is not None else fixture_source_record_id(code_version),
        "code": code,
        "version": version,
        "name": title or f"Fixture {code_version}",
        "status": status,
        "adult": True,
        "child": False,
        "mkbs": [{"code": "A00"}],
        "proff_associations": [{"id": 10}],
        "obj": {"sections": [{"id": "s1", "content": "<p>HTML stays raw.</p>"}]},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_valid_bank_document(
    document_root: Path,
    code_version: str,
    *,
    code: int,
    version: int,
    source_record_id: int | None = None,
    db_id: int | None = None,
) -> None:
    raw = document_bytes(
        code_version,
        code=code,
        version=version,
        status=0,
        db_id=db_id if db_id is not None else source_record_id,
    )
    current_root = document_root / "current"
    current_root.mkdir(parents=True, exist_ok=True)
    (current_root / "getclinrec.json").write_bytes(raw)
    write_json(
        current_root / "manifest.json",
        manifest_for_raw_json(
            code_version=code_version,
            code=code,
            version=version,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=raw,
            validation="valid",
            catalog_source_record_id=source_record_id,
            document_db_id=db_id if db_id is not None else source_record_id,
        ),
    )
    write_json(
        current_root / "catalog-record.json",
        catalog_row(code_version, code, version, 0, source_record_id=source_record_id),
    )
    write_json(
        document_root / "bank-manifest.json",
        {
            "code_version": code_version,
            "code": code,
            "version": version,
            "current_status": "valid",
            "previous_status": "not_checked",
            "pdf_status": "not_requested",
        },
    )


def nko_result() -> JsonPayloadResult:
    content = (FIXTURES / "nko_list_real_shape.json").read_bytes()
    return JsonPayloadResult(
        endpoint="GetNkoList",
        status_code=200,
        content_type="application/json",
        payload=json.loads(content.decode("utf-8")),
        raw_content=content,
        response_size_bytes=len(content),
        duration_seconds=0.0,
    )


def test_bank_download_current_preserves_raw_and_creates_no_parsed_outputs(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])
    raw = (FIXTURES / "clinrec_843_1_real_shape.json").read_bytes()

    class Client:
        def fetch_nko_list_payload(self) -> JsonPayloadResult:
            return nko_result()

        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            return json_result(code_version, raw)

    summary = download_current_documents(
        settings,
        Client(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["843_1"]),
    )

    document_dir = settings.paths.data_root / "bank" / "active" / "843_1"
    saved = document_dir / "current" / "getclinrec.json"
    assert summary.downloaded == 1
    assert saved.read_bytes() == raw
    assert not (document_dir / "parsed").exists()
    assert not (document_dir / "assets").exists()
    assert not (document_dir / "current" / "content.md").exists()
    assert not (document_dir / "current" / "document.json").exists()
    assert not (document_dir / "current" / "search_chunks.jsonl").exists()
    assert (document_dir / "current" / "catalog-record.json").exists()
    assert not (document_dir / "current" / "developers.json").exists()
    manifest = json.loads((document_dir / "current" / "manifest.json").read_text("utf-8"))
    assert manifest["validation"] == "valid"
    assert manifest["sha256"] == sha256_bytes(raw)
    assert manifest["catalog_source_record_id"] == 1737
    assert manifest["document_db_id"] == 1737
    assert manifest["db_id_match"] is True
    assert manifest["catalog_status_raw"] == 0
    assert manifest["document_status_raw"] == 0
    assert manifest["status_interpretation"] == "unknown"
    catalog_record = json.loads(
        (document_dir / "current" / "catalog-record.json").read_text("utf-8")
    )
    assert catalog_record["source_record_id"] == 1737
    assert "Id" not in catalog_record
    bank_manifest = json.loads((document_dir / "bank-manifest.json").read_text("utf-8"))
    assert bank_manifest["pdf_status"] == "not_requested"


def test_bank_download_current_uses_real_773_2_fixture_byte_for_byte(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("773_2", 773, 2, 0)])
    raw = (FIXTURES / "clinrec_773_2_real_shape.json").read_bytes()

    class Client:
        def fetch_nko_list_payload(self) -> JsonPayloadResult:
            return nko_result()

        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            assert code_version == "773_2"
            return json_result(code_version, raw)

    summary = download_current_documents(
        settings,
        Client(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["773_2"]),
    )

    document_dir = settings.paths.data_root / "bank" / "active" / "773_2"
    saved = document_dir / "current" / "getclinrec.json"
    manifest = json.loads((document_dir / "current" / "manifest.json").read_text("utf-8"))
    previous_preview = check_previous_documents(
        settings,
        None,
        BankRecordFilter(code_versions=["773_2"], dry_run=True),
    )

    assert summary.downloaded == 1
    assert saved.read_bytes() == raw
    assert manifest["code_version"] == "773_2"
    assert manifest["code"] == 773
    assert manifest["version"] == 2
    assert manifest["status"] == 0
    assert manifest["validation"] == "valid"
    assert manifest["sha256"] == sha256_bytes(raw)
    assert manifest["catalog_source_record_id"] == 2191
    assert manifest["document_db_id"] == 2191
    assert manifest["db_id_match"] is True
    assert (document_dir / "current" / "catalog-record.json").exists()
    assert not (document_dir / "current" / "developers.json").exists()
    assert not (document_dir / "parsed").exists()
    assert not (document_dir / "assets").exists()
    assert previous_preview.candidates_preview == ["773_1"]


def test_bank_previous_no_lower_version_does_not_fetch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])

    class Client:
        def fetch_clinrec_payload(self, _code_version: str) -> JsonPayloadResult:
            raise AssertionError("version 1 should not fetch previous")

    summary = check_previous_documents(
        settings,
        Client(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["843_1"]),
    )

    assert summary.documents[0].relation_status == "no_lower_version"
    relation_path = (
        settings.paths.data_root / "bank" / "active" / "843_1" / "previous" / "relation.json"
    )
    relation = json.loads(relation_path.read_text("utf-8"))
    assert relation["previous_code_version"] is None


def test_bank_previous_parallel_active_versions(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    active_rows = [catalog_row("270_2", 270, 2, 0), catalog_row("270_3", 270, 3, 0)]
    write_catalog_indexes(settings, active=active_rows)
    current_raw = document_bytes("270_3", code=270, version=3, status=0)
    current_dir = settings.paths.data_root / "bank" / "active" / "270_3" / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "getclinrec.json").write_bytes(current_raw)
    write_json(
        current_dir / "manifest.json",
        manifest_for_raw_json(
            code_version="270_3",
            code=270,
            version=3,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=current_raw,
            validation="valid",
        ),
    )
    previous_raw = document_bytes("270_2", code=270, version=2, status=0)

    class Client:
        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            assert code_version == "270_2"
            return json_result(code_version, previous_raw)

    summary = check_previous_documents(
        settings,
        Client(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["270_3"]),
    )

    assert summary.documents[0].relation_status == "parallel_active_versions"
    relation_path = (
        settings.paths.data_root / "bank" / "active" / "270_3" / "previous" / "relation.json"
    )
    relation = json.loads(relation_path.read_text("utf-8"))
    assert relation["relation_status"] == "parallel_active_versions"
    assert "source_identification_anomaly" in relation["warnings"]
    assert (
        settings.paths.data_root
        / "bank"
        / "active"
        / "270_3"
        / "previous"
        / "270_2"
        / "getclinrec.json"
    ).exists()


def test_relation_error_status_classification() -> None:
    assert (
        relation_status_for_error(
            ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.HTTP_STATUS,
                message="Forbidden",
                status_code=403,
            )
        )
        == "previous_unavailable"
    )
    assert (
        relation_status_for_error(
            ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.HTTP_STATUS,
                message="Server error",
                status_code=503,
            )
        )
        == "previous_temporary_failure"
    )


def test_bank_qa_writes_completeness_reports(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])
    raw = (FIXTURES / "clinrec_843_1_real_shape.json").read_bytes()
    document_dir = settings.paths.data_root / "bank" / "active" / "843_1"
    current_dir = document_dir / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "getclinrec.json").write_bytes(raw)
    write_json(
        current_dir / "manifest.json",
        manifest_for_raw_json(
            code_version="843_1",
            code=843,
            version=1,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=raw,
            validation="valid",
            catalog_source_record_id=1737,
            document_db_id=1737,
        ),
    )
    write_json(current_dir / "catalog-record.json", catalog_row("843_1", 843, 1, 0))
    write_json(
        current_dir / "developers.json",
        {
            "catalog_developers": [],
            "association_ids": [],
            "resolved_associations": [],
            "unresolved_association_ids": [],
        },
    )
    write_json(
        document_dir / "bank-manifest.json",
        {
            "code_version": "843_1",
            "code": 843,
            "version": 1,
            "current_status": "valid",
            "previous_status": "no_lower_version",
            "pdf_status": "not_requested",
        },
    )
    accept_current_catalog(settings, timestamp="20260101T000000Z")

    summary = run_bank_qa(settings)

    assert summary.fatal == 0
    assert summary.errors == 0
    completeness = json.loads(summary.completeness_path.read_text("utf-8"))
    assert completeness["expected_unique"] == 1
    assert completeness["active_expected"] == 1
    assert completeness["active_present"] == 1
    assert completeness["reference_status"] == "missing_optional"
    assert completeness["valid_current_json"] == 1
    assert completeness["identity_conflicts"] == 0
    assert summary.completeness_path.parent.parent.name == "full"
    assert (settings.paths.data_root / "bank" / "reports" / "latest-full.json").exists()


def test_bank_qa_reports_catalog_db_id_mismatch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(
        settings,
        active=[catalog_row("843_1", 843, 1, 0, source_record_id=9999)],
    )
    raw = (FIXTURES / "clinrec_843_1_real_shape.json").read_bytes()
    document_dir = settings.paths.data_root / "bank" / "active" / "843_1"
    current_dir = document_dir / "current"
    current_dir.mkdir(parents=True)
    (current_dir / "getclinrec.json").write_bytes(raw)
    write_json(
        current_dir / "manifest.json",
        manifest_for_raw_json(
            code_version="843_1",
            code=843,
            version=1,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=raw,
            validation="valid",
            catalog_source_record_id=9999,
            document_db_id=1737,
        ),
    )
    write_json(
        current_dir / "catalog-record.json",
        catalog_row("843_1", 843, 1, 0, source_record_id=9999),
    )
    write_json(
        current_dir / "developers.json",
        {
            "catalog_developers": [],
            "association_ids": [],
            "resolved_associations": [],
            "unresolved_association_ids": [],
        },
    )
    write_json(
        document_dir / "bank-manifest.json",
        {
            "code_version": "843_1",
            "code": 843,
            "version": 1,
            "current_status": "valid",
            "previous_status": "no_lower_version",
            "pdf_status": "not_requested",
        },
    )
    accept_current_catalog(settings, timestamp="20260101T000000Z")

    summary = run_bank_qa(settings)

    assert summary.errors == 1
    completeness = json.loads(summary.completeness_path.read_text("utf-8"))
    assert completeness["identity_conflicts"] == 1
    assert completeness["catalog_db_id_mismatch"][0]["kind"] == "identity_conflict"
    anomalies = summary.anomalies_path.read_text("utf-8")
    assert "catalog_db_id_mismatch" in anomalies


def test_strict_manifest_requires_v2_valid_sha_and_size(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    row = catalog_row("843_1", 843, 1, 0, source_record_id=1737)
    write_valid_bank_document(
        bank_active_root(settings) / "843_1",
        "843_1",
        code=843,
        version=1,
        source_record_id=1737,
        db_id=1737,
    )
    accept_current_catalog(settings, timestamp="20260101T000000Z", records=[row])

    assert run_bank_qa(settings, BankRecordFilter(all_records=True)).fatal == 0

    manifest_path = bank_active_root(settings) / "843_1" / "current" / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["validation"] = "invalid"
    write_json(manifest_path, manifest)

    summary = run_bank_qa(settings, BankRecordFilter(all_records=True))

    assert summary.fatal >= 1
    completeness = json.loads(summary.completeness_path.read_text("utf-8"))
    assert completeness["valid_current_json"] == 0


def test_failed_force_attempt_preserves_last_known_good_manifest(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])
    raw = (FIXTURES / "clinrec_843_1_real_shape.json").read_bytes()

    class GoodClient:
        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            return json_result(code_version, raw)

    download_current_documents(
        settings,
        GoodClient(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["843_1"]),
    )
    current_root = bank_active_root(settings) / "843_1" / "current"
    raw_before = (current_root / "getclinrec.json").read_bytes()
    manifest_before = (current_root / "manifest.json").read_text("utf-8")

    class FailingClient:
        def fetch_clinrec_payload(self, code_version: str) -> ExternalApiError:
            return ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.HTTP_STATUS,
                message="Service unavailable",
                status_code=503,
                code_version=code_version,
            )

    summary = download_current_documents(
        settings,
        FailingClient(),  # type: ignore[arg-type]
        BankRecordFilter(code_versions=["843_1"], force=True),
    )

    assert summary.failed == 1
    assert (current_root / "getclinrec.json").read_bytes() == raw_before
    assert (current_root / "manifest.json").read_text("utf-8") == manifest_before
    assert list((current_root / "attempts").glob("*.json"))


def test_db_id_state_distinguishes_missing_from_mismatch() -> None:
    match = manifest_for_raw_json(
        code_version="100_1",
        code=100,
        version=1,
        status=0,
        source="GetClinrec2",
        http_status=200,
        content_type="application/json",
        raw_content=document_bytes("100_1", code=100, version=1, status=0, db_id=10),
        validation="valid",
        catalog_source_record_id=10,
        document_db_id=10,
    )
    missing = manifest_for_raw_json(
        code_version="100_1",
        code=100,
        version=1,
        status=0,
        source="GetClinrec2",
        http_status=200,
        content_type="application/json",
        raw_content=document_bytes("100_1", code=100, version=1, status=0, db_id=10),
        validation="valid",
        catalog_source_record_id=None,
        document_db_id=10,
    )
    mismatch = manifest_for_raw_json(
        code_version="100_1",
        code=100,
        version=1,
        status=0,
        source="GetClinrec2",
        http_status=200,
        content_type="application/json",
        raw_content=document_bytes("100_1", code=100, version=1, status=0, db_id=11),
        validation="valid",
        catalog_source_record_id=10,
        document_db_id=11,
    )

    assert match["db_id_state"] == "match"
    assert missing["db_id_state"] == "catalog_id_missing"
    assert mismatch["db_id_state"] == "mismatch"


def test_bank_reconcile_categories_and_identity_conflicts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    previous = [
        catalog_row("100_1", 100, 1, 0, source_record_id=1),
        catalog_row("200_1", 200, 1, 0, source_record_id=2),
        catalog_row("300_1", 300, 1, 0, source_record_id=3),
    ]
    current = [
        catalog_row("100_1", 100, 1, 0, source_record_id=1),
        catalog_row("200_1", 200, 1, 0, source_record_id=2),
        catalog_row("400_1", 400, 1, 0, source_record_id=4),
        catalog_row("500_1", 500, 1, 0, source_record_id=2),
        catalog_row("600_2", 600, 1, 0, source_record_id=6),
    ]
    current[1]["name"] = "Fixture 200_1 changed"
    (bank_active_root(settings) / "100_1").mkdir(parents=True)
    (bank_active_root(settings) / "200_1").mkdir(parents=True)
    (bank_legacy_root(settings) / "400_1").mkdir(parents=True)

    plan = reconcile_catalogs(settings, previous, current)

    assert "100_1" in plan["unchanged"]
    assert "200_1" in plan["metadata_changed"]
    assert "400_1" in plan["added"]
    assert "300_1" in plan["removed_from_catalog"]
    assert "400_1" in plan["reactivated"]
    assert "500_1" in plan["missing_locally"]
    assert {issue["code"] for issue in plan["identity_conflicts"]} >= {
        "source_record_id_multiple_code_versions",
        "code_version_mismatch",
    }


def test_bank_plan_large_drop_requires_review(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    previous = [
        catalog_row(f"{code}_1", code, 1, 0, source_record_id=code)
        for code in range(100, 110)
    ]
    write_catalog_indexes(settings, active=previous[:7])
    accepted_records = (
        settings.paths.data_root / "bank" / "state" / "accepted-catalog-records.jsonl"
    )
    write_jsonl(accepted_records, previous)

    summary = build_update_plan(settings, timestamp="20260101T000000Z")
    plan = json.loads(summary.plan_path.read_text("utf-8"))

    assert summary.requires_manual_review is True
    assert "catalog_change_requires_manual_review" in plan["warnings"]


def test_plan_contains_hashes_and_rejects_modified_plan(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "tx-plan"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    write_jsonl(candidate_records, [catalog_row("843_1", 843, 1, 0)])

    summary = build_update_plan(
        settings,
        transaction_id="tx-plan",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )
    plan = json.loads(summary.plan_path.read_text("utf-8"))

    assert plan["schema_version"] == "2.0"
    assert plan["candidate_catalog_sha256"]
    assert plan["plan_sha256"]

    plan["candidate_total"] = 999
    write_json(summary.plan_path, plan)

    with pytest.raises(BankError, match="Plan hash mismatch"):
        apply_update_plan(settings, summary.plan_path, allow_manual_review=True)


def test_stale_candidate_catalog_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "tx-stale"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    write_jsonl(candidate_records, [catalog_row("843_1", 843, 1, 0)])
    summary = build_update_plan(
        settings,
        transaction_id="tx-stale",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )
    update_plan_state(summary.plan_path, "staged")
    write_jsonl(
        candidate_records,
        [
            catalog_row("843_1", 843, 1, 0),
            catalog_row("773_2", 773, 2, 0),
        ],
    )

    with pytest.raises(BankError, match="Candidate catalog hash mismatch"):
        apply_update_plan(settings, summary.plan_path, allow_manual_review=True)


def test_apply_without_complete_staging_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "tx-missing-stage"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    write_jsonl(candidate_records, [catalog_row("843_1", 843, 1, 0)])
    summary = build_update_plan(
        settings,
        transaction_id="tx-missing-stage",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )
    update_plan_state(summary.plan_path, "staged")

    with pytest.raises(BankError, match="Required staging"):
        apply_update_plan(settings, summary.plan_path, allow_manual_review=True)


def test_stage_circuit_breaker_reports_not_attempted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "tx-circuit"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    write_jsonl(
        candidate_records,
        [
            catalog_row("843_1", 843, 1, 0),
            catalog_row("773_2", 773, 2, 0),
        ],
    )
    summary = build_update_plan(
        settings,
        transaction_id="tx-circuit",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )

    class CircuitClient:
        def fetch_clinrec_payload(self, code_version: str) -> ExternalApiError:
            return ExternalApiError(
                endpoint="GetClinrec2",
                kind=ApiErrorKind.CIRCUIT_OPEN,
                message="Circuit open",
                code_version=code_version,
            )

    stage = stage_update(settings, CircuitClient(), summary.plan_path)  # type: ignore[arg-type]

    assert stage.failed == 1
    assert stage.not_attempted == 1
    assert stage.circuit_open is True


def test_candidate_qa_uses_plan_snapshot_and_staging(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "tx-candidate-qa"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    row = catalog_row("843_1", 843, 1, 0, source_record_id=1737)
    write_jsonl(candidate_records, [row])
    plan = build_update_plan(
        settings,
        transaction_id="tx-candidate-qa",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )
    write_valid_bank_document(
        settings.paths.data_root / "bank" / "staging" / "tx-candidate-qa" / "843_1",
        "843_1",
        code=843,
        version=1,
        source_record_id=1737,
        db_id=1737,
    )
    update_plan_state(plan.plan_path, "staged")

    summary = run_bank_qa(
        settings,
        BankRecordFilter(all_records=True),
        against="candidate",
        plan_path=plan.plan_path,
    )

    assert summary.fatal == 0
    assert summary.completeness_path.parent.parent.name == "tx-candidate-qa"


def test_bank_apply_moves_removed_to_legacy_and_reactivates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    accept_current_catalog(
        settings,
        timestamp="20251231T000000Z",
        records=[catalog_row("100_1", 100, 1, 0, source_record_id=1001)],
    )
    active_doc = bank_active_root(settings) / "100_1"
    legacy_doc = bank_legacy_root(settings) / "200_1"
    active_doc.mkdir(parents=True)
    legacy_doc.mkdir(parents=True)
    staging_current = (
        settings.paths.data_root
        / "bank"
        / "staging"
        / "20260101T000000Z"
        / "200_1"
        / "current"
    )
    staging_current.mkdir(parents=True)
    raw = document_bytes("200_1", code=200, version=1, status=0, db_id=2001)
    (staging_current / "getclinrec.json").write_bytes(raw)
    candidate_record = catalog_row("200_1", 200, 1, 0, source_record_id=2001)
    write_json(
        staging_current / "manifest.json",
        manifest_for_raw_json(
            code_version="200_1",
            code=200,
            version=1,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=raw,
            validation="valid",
            catalog_source_record_id=2001,
            document_db_id=2001,
        ),
    )
    write_json(staging_current / "catalog-record.json", candidate_record)
    write_json(
        staging_current.parent / "bank-manifest.json",
        {
            "code_version": "200_1",
            "code": 200,
            "version": 1,
            "current_status": "valid",
            "previous_status": "not_checked",
            "pdf_status": "not_requested",
        },
    )
    candidate_dir = settings.paths.data_root / "bank" / "candidates" / "20260101T000000Z"
    candidate_records = candidate_dir / "catalog-active.jsonl"
    write_jsonl(candidate_records, [candidate_record])
    plan_summary = build_update_plan(
        settings,
        transaction_id="20260101T000000Z",
        candidate_records_path=candidate_records,
        candidate_snapshot_path=candidate_dir,
    )
    update_plan_state(plan_summary.plan_path, "staged")

    summary = apply_update_plan(settings, plan_summary.plan_path, allow_manual_review=True)

    assert summary.moved_to_legacy == 1
    assert summary.reactivated == 1
    assert (bank_legacy_root(settings) / "100_1" / "lifecycle.json").exists()
    assert (bank_active_root(settings) / "200_1").exists()
    assert accepted_catalog_path(settings).exists()


def test_bank_update_references_failure_does_not_raise(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    class Client:
        def fetch_nko_list_payload(self) -> ExternalApiError:
            return ExternalApiError(
                endpoint="GetNkoList",
                kind=ApiErrorKind.HTTP_STATUS,
                message="Service unavailable",
                status_code=503,
            )

    summary = update_references(settings, Client())  # type: ignore[arg-type]

    assert summary.warnings == ["nko_fetch_failed"]
    assert summary.report_path.exists()


def test_bank_update_references_tracks_updated_and_missing_orgs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    history_path = settings.paths.data_root / "bank" / "references" / "nko-history.jsonl"
    current_path = settings.paths.data_root / "bank" / "references" / "nko-current.jsonl"
    write_jsonl(
        history_path,
        [
            {
                "nko_id": "1",
                "first_seen_at": "2025-01-01T00:00:00Z",
                "last_seen_at": "2025-01-01T00:00:00Z",
                "missing_from_latest": False,
                "raw_current": {"id": 1, "name": "Old"},
                "previous_values": [],
            },
            {
                "nko_id": "2",
                "first_seen_at": "2025-01-01T00:00:00Z",
                "last_seen_at": "2025-01-01T00:00:00Z",
                "missing_from_latest": False,
                "raw_current": {"id": 2, "name": "Gone"},
                "previous_values": [],
            },
        ],
    )
    write_jsonl(current_path, [{"id": 1, "name": "Old"}, {"id": 2, "name": "Gone"}])
    payload = {"d": {"data": [{"id": 1, "name": "New"}]}}
    raw = json.dumps(payload).encode("utf-8")

    class Client:
        def fetch_nko_list_payload(self) -> JsonPayloadResult:
            return JsonPayloadResult(
                endpoint="GetNkoList",
                status_code=200,
                content_type="application/json",
                payload=payload,
                raw_content=raw,
                response_size_bytes=len(raw),
                duration_seconds=0,
            )

    summary = update_references(settings, Client())  # type: ignore[arg-type]
    history = {row["nko_id"]: row for row in read_jsonl(history_path)}

    assert summary.updated == 1
    assert summary.missing_from_latest_reference == 1
    assert history["2"]["missing_from_latest"] is True


def test_bank_enrich_developers_resolves_nko_after_download(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])
    write_jsonl(
        settings.paths.data_root / "bank" / "references" / "nko-current.jsonl",
        [{"id": 10, "name": "Association"}],
    )
    raw = document_bytes("843_1", code=843, version=1, status=0)
    current_root = bank_active_root(settings) / "843_1" / "current"
    current_root.mkdir(parents=True)
    (current_root / "getclinrec.json").write_bytes(raw)
    write_json(current_root / "catalog-record.json", catalog_row("843_1", 843, 1, 0))

    summary = enrich_developers(settings, BankRecordFilter(all_records=True))
    developers = json.loads((current_root / "developers.json").read_text("utf-8"))

    assert summary.updated == 1
    assert developers["resolved_associations"][0]["name"] == "Association"


def test_bank_analyze_statuses_preserves_raw_values(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(
        settings,
        active=[catalog_row("100_1", 100, 1, 7)],
        all_statuses=[
            catalog_row("100_1", 100, 1, 7),
            catalog_row("100_2", 100, 2, 9),
        ],
    )
    current_root = bank_active_root(settings) / "100_1" / "current"
    current_root.mkdir(parents=True)
    write_json(
        current_root / "manifest.json",
        {
            "catalog_status_raw": 7,
            "document_status_raw": 4,
            "apply_status_raw": None,
            "apply_status_calculated_raw": 1,
        },
    )

    summary = analyze_statuses(settings)
    report = json.loads(summary.report_path.read_text("utf-8"))

    assert report["status_interpretation"] == "unknown"
    assert report["catalog_status_frequency_active"]["7"] == 1
    assert report["document_status_frequency"]["4"] == 1


def test_bank_qa_scoped_does_not_overwrite_latest_full(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_catalog_indexes(settings, active=[catalog_row("843_1", 843, 1, 0)])
    raw = (FIXTURES / "clinrec_843_1_real_shape.json").read_bytes()
    document_dir = bank_active_root(settings) / "843_1"
    current_root = document_dir / "current"
    current_root.mkdir(parents=True)
    (current_root / "getclinrec.json").write_bytes(raw)
    write_json(
        current_root / "manifest.json",
        manifest_for_raw_json(
            code_version="843_1",
            code=843,
            version=1,
            status=0,
            source="GetClinrec2",
            http_status=200,
            content_type="application/json",
            raw_content=raw,
            validation="valid",
            catalog_source_record_id=1737,
            document_db_id=1737,
        ),
    )
    write_json(current_root / "catalog-record.json", catalog_row("843_1", 843, 1, 0))
    write_json(
        document_dir / "bank-manifest.json",
        {
            "code_version": "843_1",
            "code": 843,
            "version": 1,
            "current_status": "valid",
            "previous_status": "not_checked",
            "pdf_status": "not_requested",
        },
    )
    accept_current_catalog(settings, timestamp="20260101T000000Z")

    full = run_bank_qa(settings, BankRecordFilter(all_records=True, timestamp="20260101T000000Z"))
    latest = settings.paths.data_root / "bank" / "reports" / "latest-full.json"
    before = latest.read_text("utf-8")
    scoped = run_bank_qa(
        settings,
        BankRecordFilter(code_versions=["843_1"], timestamp="20260101T010000Z"),
    )

    assert full.fatal == 0
    assert scoped.fatal == 0
    assert latest.read_text("utf-8") == before
    assert scoped.completeness_path.parent.parent.parent.name == "scoped"


def test_bank_analyze_identities_reports_duplicates_and_pairs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    rows = [
        catalog_row("100_1", 100, 1, 0, source_record_id=500),
        catalog_row("101_1", 101, 1, 0, source_record_id=500),
    ]
    rows[0]["publish_date"] = "2024-01-01"
    rows[1]["publish_date"] = "2024-01-02"
    write_catalog_indexes(settings, active=rows)
    for row in rows:
        code_version = row["code_version"]
        current_dir = settings.paths.data_root / "bank" / "active" / code_version / "current"
        current_dir.mkdir(parents=True)
        write_json(
            current_dir / "manifest.json",
            {
                "code_version": code_version,
                "catalog_source_record_id": 500,
                "document_db_id": 500,
                "db_id_match": True,
            },
        )

    summary = analyze_identities(settings)

    assert summary.unique_db_ids == 1
    assert summary.duplicate_db_ids == 1
    report = json.loads(summary.report_path.read_text("utf-8"))
    assert report["db_id_to_many_code_versions"]["500"] == ["100_1", "101_1"]
    assert report["catalog_document_db_id_matches"] == 2
