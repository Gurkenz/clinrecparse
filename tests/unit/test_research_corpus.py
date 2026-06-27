from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clinrec.api.client import JsonPayloadResult
from clinrec.bank.common import BankError, read_json_file, read_jsonl, write_jsonl
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
from clinrec.research.corpus import (
    ResearchCorpusOptions,
    build_research_corpus,
    ensure_research_output_safe,
    select_current_records,
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


def test_research_output_must_not_be_inside_bank(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(BankError, match="data/bank"):
        ensure_research_output_safe(settings, settings.paths.data_root / "bank" / "research")


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
        ResearchCorpusOptions(output=output, current_count=3, legacy_target=0, seed=1),
    )
    summary = build_research_corpus(
        settings,
        None,
        ResearchCorpusOptions(output=output, current_count=3, profile_only=True, seed=1),
    )

    assert summary.valid_current_count == 3
    assert read_jsonl(output / "reports" / "documents.jsonl")
