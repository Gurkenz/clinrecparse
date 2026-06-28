from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clinrec.api.client import JsonPayloadResult
from clinrec.bank.common import BankError, read_json_file, read_jsonl, sha256_file, write_jsonl
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
from clinrec.research.catalog import (
    records_by_code_version,
    resolve_catalog_candidates,
    write_catalog_indexes,
)
from clinrec.research.corpus import (
    ResearchCorpusOptions,
    build_research_corpus,
    ensure_research_output_safe,
    select_current_records,
)
from clinrec.research.html_profile import table_rows_for_html
from clinrec.research.migration import migrate_layout
from clinrec.research.schema import profile_corpus_offline
from clinrec.research.sections import analyze_doc_whole, parse_title_data
from clinrec.research.validation import validate_corpus


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


def catalog_row(code: int, version: int) -> dict[str, Any]:
    code_version = f"{code}_{version}"
    return {
        "source_record_id": code * 10 + version,
        "code_version": code_version,
        "code": code,
        "version": version,
        "name": f"Research {code_version}",
        "status": 0,
        "age_category": 1,
        "developers": [{"Name": "Association"}],
        "mkbs": [{"code": "A00"}],
    }


def document_bytes(code_version: str) -> bytes:
    code_text, version_text = code_version.split("_", maxsplit=1)
    code = int(code_text)
    version = int(version_text)
    payload = {
        "id": code_version,
        "db_id": code * 10 + version,
        "code": code,
        "version": version,
        "name": f"Research {code_version}",
        "status": 0,
        "adult": True,
        "child": False,
        "mkbs": [{"code": "A00"}],
        "proff_associations": [{"id": 1}],
        "obj": {
            "sections": [
                {
                    "id": "s1",
                    "name": "Section",
                    "content": "<p>Text</p><table><tr><td>A</td></tr></table>",
                }
            ]
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def document_bytes_with_sections(code_version: str, sections: list[dict[str, Any]]) -> bytes:
    code_text, version_text = code_version.split("_", maxsplit=1)
    code = int(code_text)
    version = int(version_text)
    payload = {
        "id": code_version,
        "db_id": code * 10 + version,
        "code": code,
        "version": version,
        "name": f"Research {code_version}",
        "status": 0,
        "adult": True,
        "child": False,
        "mkbs": [{"code": "A00"}],
        "proff_associations": [{"id": 1}],
        "obj": {"sections": sections},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_result(code_version: str) -> JsonPayloadResult:
    content = document_bytes(code_version)
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


def write_research_catalog(output: Path, rows: list[dict[str, Any]]) -> None:
    write_jsonl(output / "catalog" / "catalog-active.jsonl", rows)
    all_rows = rows + [catalog_row(100, 1), catalog_row(101, 1), catalog_row(102, 2)]
    write_jsonl(output / "catalog" / "catalog-all-statuses.jsonl", all_rows)


def sample_rows() -> list[dict[str, Any]]:
    return [
        catalog_row(100, 2),
        catalog_row(101, 2),
        catalog_row(102, 3),
        catalog_row(103, 1),
        catalog_row(104, 1),
        catalog_row(105, 1),
        catalog_row(106, 2),
        catalog_row(107, 3),
        catalog_row(108, 1),
        catalog_row(109, 2),
    ]


def test_research_selection_is_deterministic_and_includes_forced() -> None:
    rows = sample_rows()
    options = ResearchCorpusOptions(
        output=Path("unused"),
        current_count=6,
        seed=123,
        include=("102_3",),
    )

    first = select_current_records(rows, options)
    second = select_current_records(list(reversed(rows)), options)

    assert first == second
    assert first[0] == "102_3"
    assert len(first) == 6


def test_research_selection_missing_forced_fails() -> None:
    with pytest.raises(BankError, match="Forced research records"):
        select_current_records(
            sample_rows(),
            ResearchCorpusOptions(output=Path("unused"), current_count=3, include=("999_1",)),
        )


def test_research_all_current_dry_run_writes_universe_reports(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "all-current-dry-run"
    rows = [
        catalog_row(200, 2),
        catalog_row(200, 1),
        {**catalog_row(200, 2), "source_record_id": 20022},
        {**catalog_row(0, 0), "source_record_id": 9999, "code_version": "_"},
    ]
    write_jsonl(output / "catalog" / "catalog-active.jsonl", rows)
    write_jsonl(output / "catalog" / "catalog-all-statuses.jsonl", rows)

    summary = build_research_corpus(
        settings,
        None,
        ResearchCorpusOptions(
            output=output,
            selection_mode="all_current",
            legacy_target=0,
            legacy_minimum=0,
            dry_run=True,
        ),
    )

    selection = read_json_file(output / "selection.json")
    universe = read_json_file(output / "reports" / "current-universe.json")
    coverage = read_json_file(output / "reports" / "current-coverage.json")
    provenance = read_jsonl(output / "reports" / "selection-provenance.jsonl")

    assert summary.status == "dry_run"
    assert selection["selection_mode"] == "all_current"
    assert selection["requested_current_count"] == 2
    assert selection["initially_selected"] == ["200_1", "200_2"]
    assert selection["replacements"] == []
    assert universe["unique_valid_code_versions"] == 2
    assert universe["malformed_rows"] == 1
    assert universe["duplicate_code_version_groups"] == 1
    assert coverage["not_attempted"] == 2
    assert [row["selection_reason"] for row in provenance] == [
        "all_current_universe",
        "all_current_universe",
    ]


def test_research_all_current_keeps_failures_without_replacement(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "all-current-failure"
    rows = [catalog_row(100, 1), catalog_row(101, 1)]
    write_jsonl(output / "catalog" / "catalog-active.jsonl", rows)
    write_jsonl(output / "catalog" / "catalog-all-statuses.jsonl", rows)

    class Client:
        def fetch_clinrec_payload(
            self,
            code_version: str,
        ) -> JsonPayloadResult | ExternalApiError:
            if code_version == "100_1":
                return ExternalApiError(
                    endpoint="GetClinrec2",
                    kind=ApiErrorKind.HTTP_STATUS,
                    message="Not found",
                    status_code=404,
                    code_version=code_version,
                )
            return json_result(code_version)

    summary = build_research_corpus(
        settings,
        Client(),  # type: ignore[arg-type]
        ResearchCorpusOptions(
            output=output,
            selection_mode="all_current",
            legacy_target=0,
            legacy_minimum=0,
        ),
    )

    selection = read_json_file(output / "selection.json")
    attempts = read_jsonl(output / "attempts" / "current-attempts.jsonl")
    coverage = read_json_file(output / "reports" / "current-coverage.json")

    assert summary.status == "failed"
    assert summary.valid_current_count == 1
    assert selection["replacements"] == []
    assert selection["final_selected"] == ["101_1"]
    assert selection["failed_candidates"][0]["code_version"] == "100_1"
    assert selection["failed_candidates"][0]["result"] == "404"
    assert [row["result"] for row in attempts] == ["404", "downloaded"]
    assert coverage["downloaded_valid"] == 1
    assert coverage["permanent_unavailable"] == 1


def test_research_option_relationships_fail_before_output_mutation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "bad-options"

    with pytest.raises(BankError, match="previous_minimum"):
        build_research_corpus(
            settings,
            None,
            ResearchCorpusOptions(
                output=output,
                current_count=3,
                legacy_target=3,
                legacy_minimum=5,
                dry_run=True,
            ),
        )

    assert not output.exists()


def test_research_malformed_include_fails_before_output_mutation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "bad-include"

    with pytest.raises(BankError, match="Malformed mandatory include"):
        build_research_corpus(
            settings,
            None,
            ResearchCorpusOptions(output=output, include=("not-a-code-version",), dry_run=True),
        )

    assert not output.exists()


def test_research_output_must_not_be_inside_bank(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(BankError, match="data/bank"):
        ensure_research_output_safe(settings, settings.paths.data_root / "bank" / "research")

    with pytest.raises(BankError, match="data/bank"):
        ensure_research_output_safe(settings, settings.paths.data_root)


def test_research_build_corpus_replaces_failed_current_and_profiles(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "fixture-8"
    rows = sample_rows()
    write_research_catalog(output, rows)
    initially_selected = select_current_records(
        rows,
        ResearchCorpusOptions(output=output, current_count=8, seed=20260627),
    )
    fail_once = {initially_selected[0]}

    class Client:
        def fetch_clinrec_payload(
            self,
            code_version: str,
        ) -> JsonPayloadResult | ExternalApiError:
            if code_version in fail_once:
                fail_once.remove(code_version)
                return ExternalApiError(
                    endpoint="GetClinrec2",
                    kind=ApiErrorKind.HTTP_STATUS,
                    message="Service unavailable",
                    status_code=503,
                    code_version=code_version,
                )
            return json_result(code_version)

    summary = build_research_corpus(
        settings,
        Client(),  # type: ignore[arg-type]
        ResearchCorpusOptions(
            output=output,
            current_count=8,
            legacy_target=2,
            legacy_minimum=1,
            legacy_attempt_limit=4,
            seed=20260627,
        ),
    )

    selection = read_json_file(output / "selection.json")
    profile = read_json_file(output / "reports" / "schema-profile.json")

    assert summary.valid_current_count == 8
    assert summary.valid_legacy_count >= 1
    assert selection["replacements"]
    assert profile["document_count"] >= 8
    assert (output / "reports" / "research-findings.md").exists()
    assert not (settings.paths.data_root / "bank").exists()


def test_research_profile_only_performs_no_http(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "profile"
    rows = sample_rows()
    write_research_catalog(output, rows)

    class Client:
        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            return json_result(code_version)

    build_research_corpus(
        settings,
        Client(),  # type: ignore[arg-type]
        ResearchCorpusOptions(
            output=output,
            current_count=3,
            legacy_target=0,
            legacy_minimum=0,
            seed=1,
        ),
    )
    summary = build_research_corpus(
        settings,
        None,
        ResearchCorpusOptions(output=output, current_count=3, profile_only=True, seed=1),
    )

    assert summary.valid_current_count == 3
    assert read_jsonl(output / "reports" / "documents.jsonl")


def test_research_counts_failed_previous_attempts_without_previous_dir(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "failed-previous"
    write_research_catalog(output, sample_rows())

    class Client:
        def fetch_clinrec_payload(
            self,
            code_version: str,
        ) -> JsonPayloadResult | ExternalApiError:
            if code_version == "100_1":
                return ExternalApiError(
                    endpoint="GetClinrec2",
                    kind=ApiErrorKind.HTTP_STATUS,
                    message="Service unavailable",
                    status_code=503,
                    code_version=code_version,
                )
            return json_result(code_version)

    summary = build_research_corpus(
        settings,
        Client(),  # type: ignore[arg-type]
        ResearchCorpusOptions(
            output=output,
            current_count=1,
            legacy_target=1,
            legacy_minimum=1,
            legacy_attempt_limit=1,
            seed=1,
            include=("100_2",),
        ),
    )

    attempts = read_jsonl(output / "attempts" / "previous-attempts.jsonl")
    report = read_json_file(output / "reports" / "corpus-summary.json")

    assert summary.status == "failed"
    assert summary.legacy_attempts == 1
    assert attempts[0]["previous_code_version"] == "100_1"
    assert attempts[0]["result"] == "5xx"
    assert report["previous_attempts"] == 1
    assert report["status"] == "failed"
    assert not (output / "previous").exists()
    assert not (output / "attempts" / "legacy-attempts.jsonl").exists()


def test_catalog_indexes_preserve_duplicate_code_versions_and_malformed_rows(
    tmp_path: Path,
) -> None:
    output = tmp_path / "corpus"
    write_jsonl(output / "catalog" / "catalog-active.jsonl", [catalog_row(270, 2)])
    write_jsonl(
        output / "catalog" / "catalog-all-statuses.jsonl",
        [
            catalog_row(270, 2),
            {**catalog_row(270, 2), "source_record_id": 2703},
            {**catalog_row(0, 0), "source_record_id": 9999, "code_version": "_"},
        ],
    )

    profile = write_catalog_indexes(output)

    index = read_jsonl(output / "catalog" / "code-version-index.jsonl")
    anomalies = read_json_file(output / "reports" / "catalog-anomalies.json")
    collision = next(row for row in index if row["code_version"] == "270_2")
    assert profile.all_statuses_records == 3
    assert collision["records_count"] == 2
    assert collision["source_record_ids"] == [2702, 2703]
    assert anomalies["malformed_code_versions"] == 1


def test_catalog_resolution_uses_document_db_id_and_preserves_ambiguity() -> None:
    rows = [
        catalog_row(270, 2),
        {**catalog_row(270, 2), "source_record_id": 2703},
    ]
    indexed = records_by_code_version(rows)

    resolved = resolve_catalog_candidates(indexed, "270_2", document_db_id=2703)
    ambiguous = resolve_catalog_candidates(indexed, "270_2", document_db_id=9999)

    assert resolved.state == "resolved_by_document_db_id"
    assert resolved.resolved_source_record_id == 2703
    assert ambiguous.state == "ambiguous_no_db_id_match"
    assert ambiguous.resolved_record is None
    assert ambiguous.candidate_source_record_ids == [2702, 2703]


def test_research_migration_renames_legacy_layout(tmp_path: Path) -> None:
    output = tmp_path / "corpus"
    legacy_pair = output / "legacy" / "270_3" / "270_2"
    legacy_pair.mkdir(parents=True)
    (legacy_pair / "getclinrec.json").write_bytes(document_bytes("270_2"))
    write_jsonl(output / "attempts" / "legacy-attempts.jsonl", [{"result": "downloaded"}])
    (output / "corpus.json").write_text('{"legacy_target": 1}\n', encoding="utf-8")

    summary = migrate_layout(output)

    assert summary.migrated
    assert not (output / "legacy").exists()
    assert (output / "previous" / "270_3" / "270_2" / "getclinrec.json").exists()
    assert not (output / "attempts" / "legacy-attempts.jsonl").exists()
    assert (output / "attempts" / "previous-attempts.jsonl").exists()
    assert read_json_file(output / "corpus.json")["previous_target"] == 1


def test_validation_detects_manifest_sha_mismatch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    output = settings.paths.data_root / "research" / "corpora" / "validation"
    write_research_catalog(output, sample_rows())

    class Client:
        def fetch_clinrec_payload(self, code_version: str) -> JsonPayloadResult:
            return json_result(code_version)

    build_research_corpus(
        settings,
        Client(),  # type: ignore[arg-type]
        ResearchCorpusOptions(
            output=output,
            current_count=1,
            legacy_target=0,
            legacy_minimum=0,
            seed=1,
        ),
    )
    manifest_path = next(output.glob("current/*/manifest.json"))
    manifest = read_json_file(manifest_path)
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = validate_corpus(output)

    assert not summary.valid
    assert summary.errors == 1
    assert read_json_file(summary.report_json)["errors"][0]["code"] == "manifest_sha_mismatch"


def test_title_parser_and_doc_whole_policy_are_tolerant() -> None:
    section = {
        "id": "doc_title",
        "content": "",
        "data": [
            {"key": "name", "value": "Title"},
            {"key": "name", "value": "Repeated"},
            {"label": "Unknown label", "value": 5},
        ],
    }

    fields, anomalies = parse_title_data("100_1", "current", section)
    doc_whole = analyze_doc_whole(
        "current",
        "100_1",
        {
            "obj": {
                "sections": [
                    {"id": "section_intro", "content": "<p>Hello world</p>"},
                    {"id": "doc_whole", "content": "<p>Hello world</p>"},
                ]
            }
        },
    )

    assert [row["field_key"] for row in fields] == ["name", "name", "unknown_label"]
    assert anomalies[0]["anomaly"] == "repeated_title_item"
    assert doc_whole["estimated_duplicate"]
    assert doc_whole["recommendation"] == "exclude_from_index"


def test_offline_profile_reports_current_previous_pair_and_keeps_raw_hashes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "corpus"
    current_sections = [
        {
            "id": "doc_title",
            "content": "",
            "data": [{"key": "name", "value": "Title"}],
        },
        {
            "id": "section_intro",
            "content": "<p>Hello</p><table><tr><td>A</td></tr></table>"
            '<img src="data:image/png;base64,aGVsbG8=">',
        },
        {"id": "doc_whole", "content": "<p>Hello</p>"},
    ]
    previous_sections = [
        {"id": "doc_title", "content": "", "data": [{"key": "name", "value": "Title"}]},
        {"id": "section_intro", "content": "<p>Hello old</p>"},
        {"id": "doc_whole", "content": "<p>Hello old</p>"},
    ]
    rows = [catalog_row(270, 3), catalog_row(270, 2)]
    write_jsonl(output / "catalog" / "catalog-active.jsonl", rows)
    write_jsonl(output / "catalog" / "catalog-all-statuses.jsonl", rows)
    save_fixture_document(output / "current" / "270_3", "270_3", current_sections)
    save_fixture_document(output / "current" / "270_2", "270_2", previous_sections)
    save_fixture_document(output / "previous" / "270_3" / "270_2", "270_2", previous_sections)
    (output / "selection.json").write_text(
        '{"initially_selected": ["270_3"], "final_selected": ["270_3"]}\n',
        encoding="utf-8",
    )
    (output / "corpus.json").write_text("{}\n", encoding="utf-8")

    summary = profile_corpus_offline(output)

    pair = read_jsonl(output / "reports" / "current-previous-pairs.jsonl")[0]
    image_summary = read_json_file(output / "reports" / "image-summary.json")
    table_summary = read_json_file(output / "reports" / "table-summary.json")
    corpus = read_json_file(output / "corpus.json")
    assert summary.raw_hashes_unchanged
    assert summary.raw_files == 3
    assert len(summary.raw_hashes_by_path_before) == 3
    assert len(summary.raw_hash_set_before) == 2
    assert pair["membership_relation"] == "both_active"
    assert set(pair["changed_section_ids"]) == {"doc_whole", "section_intro"}
    assert image_summary["base64_images"] == 1
    assert table_summary["tables_total"] == 1
    assert corpus["catalog_active_total"] == 2
    report_hashes = {
        path.name: sha256_file(path)
        for path in (output / "reports").iterdir()
        if path.is_file()
    }
    read_only = profile_corpus_offline(output, rebuild_reports=False)
    assert read_only.raw_hashes_unchanged
    assert report_hashes == {
        path.name: sha256_file(path)
        for path in (output / "reports").iterdir()
        if path.is_file()
    }


def test_html_table_profile_counts_nested_tables_and_invalid_spans() -> None:
    rows = table_rows_for_html(
        code_version="100_1",
        document_kind="current",
        section_id="section_tables",
        html=(
            "<table><tr><td colspan='bad'>A<table><tr><td>B</td></tr></table></td></tr>"
            "<tr><td rowspan='0'>C</td></tr></table>"
        ),
    )

    assert rows[0]["rows"] == 2
    assert rows[0]["cells"] == 2
    assert rows[0]["nested_table_count"] == 1
    assert rows[0]["invalid_span_count"] == 2
    assert rows[0]["malformed"]


def save_fixture_document(root: Path, code_version: str, sections: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True)
    raw = document_bytes_with_sections(code_version, sections)
    (root / "getclinrec.json").write_bytes(raw)
    code_text, version_text = code_version.split("_", maxsplit=1)
    code = int(code_text)
    version = int(version_text)
    manifest = {
        "code": code,
        "version": version,
        "code_version": code_version,
        "document_db_id": code * 10 + version,
        "catalog_source_record_id": code * 10 + version,
        "db_id_state": "match",
        "document_status_raw": 0,
        "sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "size": len(raw),
        "validation": "valid",
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "catalog-record.json").write_text(
        json.dumps(catalog_row(code, version)),
        encoding="utf-8",
    )
