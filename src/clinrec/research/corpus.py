from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from clinrec.api.catalog_sync import sync_catalog, to_int, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    BankError,
    add_catalog_status_fields,
    bank_root,
    catalog_record_for_bank,
    db_id_state,
    list_value,
    manifest_for_raw_json,
    minimal_validate_raw_document,
    parse_code_version_or_raise,
    read_json_file,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    source_record_id_from_catalog,
    string_value,
    utc_now,
    write_jsonl,
)
from clinrec.config import PathSettings, Settings
from clinrec.models.external import ApiErrorKind, ExternalApiError

RESEARCH_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class ResearchCorpusOptions:
    output: Path
    current_count: int = 50
    legacy_target: int = 10
    legacy_minimum: int = 5
    legacy_attempt_limit: int = 20
    seed: int = 20260627
    include: tuple[str, ...] = ()
    resume: bool = False
    retry_failed: bool = False
    profile_only: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ResearchCorpusSummary:
    output: Path
    status: str
    valid_current_count: int
    valid_legacy_count: int
    legacy_attempts: int
    corpus_path: Path
    summary_path: Path


def build_research_corpus(
    settings: Settings,
    client: ClinrecApiClient | None,
    options: ResearchCorpusOptions,
) -> ResearchCorpusSummary:
    output = options.output
    ensure_research_output_safe(settings, output)
    output.mkdir(parents=True, exist_ok=True)
    reports_root(output).mkdir(parents=True, exist_ok=True)

    active_records, all_records, catalog_sha = load_or_sync_research_catalog(
        settings,
        client,
        options,
    )
    selection = load_or_create_selection(output, active_records, catalog_sha, options)
    if options.dry_run:
        write_corpus_state(output, options, selection, catalog_sha, "dry_run")
        return write_reports(output, active_records, all_records, options, "dry_run")
    if options.profile_only:
        profile_corpus(output, all_records)
        return write_reports(output, active_records, all_records, options, corpus_status(output))
    if client is None:
        raise BankError("HTTP client is required unless --dry-run or --profile-only is used.")

    download_current_documents(output, client, active_records, selection, options)
    profile_corpus(output, all_records)
    valid_current = valid_current_code_versions(output)
    if len(valid_current) >= options.current_count:
        download_legacy_documents(output, client, all_records, valid_current, options)
        profile_corpus(output, all_records)
    status = final_status(output, options)
    write_corpus_state(output, options, selection, catalog_sha, status)
    return write_reports(output, active_records, all_records, options, status)


def ensure_research_output_safe(settings: Settings, output: Path) -> None:
    resolved = output.resolve()
    bank = bank_root(settings).resolve()
    if resolved == bank or bank in resolved.parents:
        raise BankError("Research output must not be inside data/bank.")


def research_paths(settings: Settings, output: Path) -> PathSettings:
    return PathSettings(
        data_root=output,
        snapshots=output / "snapshots",
        references=output / "references",
        documents=output / "documents",
        indexes=output / "catalog",
        reports=output / "reports",
        logs=output / "logs",
    )


def research_settings(settings: Settings, output: Path) -> Settings:
    return settings.model_copy(update={"paths": research_paths(settings, output)})


def load_or_sync_research_catalog(
    settings: Settings,
    client: ClinrecApiClient | None,
    options: ResearchCorpusOptions,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    active_path = options.output / "catalog" / "catalog-active.jsonl"
    all_path = options.output / "catalog" / "catalog-all-statuses.jsonl"
    if active_path.exists() and all_path.exists():
        active = read_jsonl(active_path)
        all_records = read_jsonl(all_path)
        return active, all_records, sha256_file(active_path)
    if options.profile_only:
        raise BankError("Research catalog is missing; --profile-only cannot perform HTTP calls.")
    if options.dry_run and client is None:
        source_active = settings.paths.indexes / "catalog-active.jsonl"
        source_all = settings.paths.indexes / "catalog-all-statuses.jsonl"
        if not source_active.exists() or not source_all.exists():
            raise BankError("No local catalog is available for research --dry-run.")
        active = read_jsonl(source_active)
        all_records = read_jsonl(source_all)
        return active, all_records, sha256_file(source_active)
    if client is None:
        raise BankError("HTTP client is required to fetch a research catalog.")
    summary = sync_catalog(research_settings(settings, options.output), client)
    return (
        read_jsonl(summary.active_index_path),
        read_jsonl(summary.all_statuses_index_path),
        sha256_file(summary.active_index_path),
    )


def load_or_create_selection(
    output: Path,
    records: list[dict[str, Any]],
    catalog_sha: str,
    options: ResearchCorpusOptions,
) -> dict[str, Any]:
    path = output / "selection.json"
    if path.exists():
        selection = read_json_file(path)
        if selection.get("catalog_sha256") != catalog_sha:
            raise BankError("Research catalog hash changed; create a new corpus output.")
        return selection
    selected = select_current_records(records, options)
    selection = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "seed": options.seed,
        "catalog_sha256": catalog_sha,
        "initially_selected": selected,
        "replacements": [],
        "final_selected": [],
        "forced_failures": [],
        "created_at": utc_now(),
    }
    write_json(path, selection)
    return selection


def select_current_records(
    records: list[dict[str, Any]],
    options: ResearchCorpusOptions,
) -> list[str]:
    by_code_version = {string_value(row.get("code_version")): row for row in records}
    forced = list(dict.fromkeys(options.include))
    missing = [code_version for code_version in forced if code_version not in by_code_version]
    if missing:
        raise BankError(f"Forced research records are missing from active catalog: {missing}")
    targets = stratum_targets(options.current_count)
    selected: list[str] = []
    selected_set: set[str] = set()
    stratum_counts = {"version_1": 0, "version_2": 0, "version_3_plus": 0}
    for code_version in forced:
        selected.append(code_version)
        selected_set.add(code_version)
        stratum_counts[stratum_for_record(by_code_version[code_version])] += 1
    for stratum, target in targets.items():
        candidates = [
            string_value(row.get("code_version"))
            for row in records
            if stratum_for_record(row) == stratum
            and string_value(row.get("code_version")) not in selected_set
        ]
        for code_version in deterministic_order(candidates, options.seed):
            if len(selected) >= options.current_count or stratum_counts[stratum] >= target:
                break
            selected.append(code_version)
            selected_set.add(code_version)
            stratum_counts[stratum] += 1
    if len(selected) < options.current_count:
        remaining = [
            string_value(row.get("code_version"))
            for row in records
            if string_value(row.get("code_version")) not in selected_set
        ]
        for code_version in deterministic_order(remaining, options.seed):
            if len(selected) >= options.current_count:
                break
            selected.append(code_version)
            selected_set.add(code_version)
    if len(selected) < options.current_count:
        raise BankError("Active catalog does not contain enough records for research selection.")
    return selected[: options.current_count]


def stratum_targets(current_count: int) -> dict[str, int]:
    base = {
        "version_1": int(current_count * 0.4),
        "version_2": int(current_count * 0.4),
    }
    base["version_3_plus"] = current_count - base["version_1"] - base["version_2"]
    return base


def stratum_for_record(record: dict[str, Any]) -> str:
    version = to_int(record.get("version"))
    if version == 1:
        return "version_1"
    if version == 2:
        return "version_2"
    return "version_3_plus"


def deterministic_order(code_versions: list[str], seed: int) -> list[str]:
    return sorted(
        code_versions,
        key=lambda code_version: hashlib.sha256(f"{seed}:{code_version}".encode()).hexdigest(),
    )


def download_current_documents(
    output: Path,
    client: ClinrecApiClient,
    active_records: list[dict[str, Any]],
    selection: dict[str, Any],
    options: ResearchCorpusOptions,
) -> None:
    by_code_version = {string_value(row.get("code_version")): row for row in active_records}
    selected = list(selection.get("initially_selected") or [])
    final_selected: list[str] = list(selection.get("final_selected") or [])
    failed = {
        string_value(row.get("code_version"))
        for row in selection.get("forced_failures", [])
        if isinstance(row, dict)
    }
    attempted = set(final_selected) | failed
    pool = replacement_pool(active_records, selected, options.seed)
    cursor = 0
    while len(valid_current_code_versions(output)) < options.current_count:
        code_version = next_selection_candidate(selected, final_selected, attempted, pool, cursor)
        if code_version is None:
            break
        if code_version in pool:
            cursor = pool.index(code_version) + 1
        attempted.add(code_version)
        record = by_code_version[code_version]
        result = download_one_research_current(output, client, record, options)
        if result["result"] in {"downloaded", "already_valid"}:
            if code_version not in final_selected:
                final_selected.append(code_version)
        else:
            if code_version in options.include:
                forced_failures = list(selection.get("forced_failures") or [])
                forced_failures.append(result)
                selection["forced_failures"] = forced_failures
            replacements = list(selection.get("replacements") or [])
            replacements.append({"failed": code_version, "result": result["result"]})
            selection["replacements"] = replacements
            if result["result"] == "circuit_open":
                break
        selection["final_selected"] = final_selected
        write_json(output / "selection.json", selection)


def replacement_pool(records: list[dict[str, Any]], selected: list[str], seed: int) -> list[str]:
    selected_set = set(selected)
    return deterministic_order(
        [
            string_value(row.get("code_version"))
            for row in records
            if string_value(row.get("code_version")) not in selected_set
        ],
        seed,
    )


def next_selection_candidate(
    selected: list[str],
    final_selected: list[str],
    attempted: set[str],
    pool: list[str],
    cursor: int,
) -> str | None:
    for code_version in selected:
        if code_version not in final_selected and code_version not in attempted:
            return code_version
    for code_version in pool[cursor:]:
        if code_version not in final_selected and code_version not in attempted:
            return code_version
    return None


def download_one_research_current(
    output: Path,
    client: ClinrecApiClient,
    record: dict[str, Any],
    options: ResearchCorpusOptions,
) -> dict[str, Any]:
    code_version = string_value(record.get("code_version"))
    root = output / "current" / code_version
    raw_path = root / "getclinrec.json"
    manifest_path = root / "manifest.json"
    if (
        raw_path.exists()
        and manifest_path.exists()
        and not options.retry_failed
        and research_manifest_matches(raw_path, manifest_path, code_version)
    ):
        return {"code_version": code_version, "result": "already_valid"}
    result = client.fetch_clinrec_payload(code_version)
    if isinstance(result, ExternalApiError):
        row = result_row(code_version, classify_api_error(result), result.message, result)
        write_error(output, "current", row)
        return row
    return save_research_document(
        root,
        code_version=code_version,
        catalog_record=record,
        raw_content=result.raw_content,
        http_status=result.status_code,
        content_type=result.content_type,
    )


def save_research_document(
    root: Path,
    *,
    code_version: str,
    catalog_record: dict[str, Any],
    raw_content: bytes,
    http_status: int,
    content_type: str,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    info, errors = minimal_validate_raw_document(raw_content, expected_code_version=code_version)
    if info is None:
        row = {
            "code_version": code_version,
            "result": "invalid_json",
            "error": "; ".join(errors),
        }
        write_json(root / "manifest.json", {"validation": "invalid", **row})
        return row
    raw_path = root / "getclinrec.json"
    raw_path.write_bytes(raw_content)
    catalog_record = catalog_record_for_bank(catalog_record)
    catalog_source_id = source_record_id_from_catalog(catalog_record)
    write_json(root / "catalog-record.json", catalog_record)
    write_json(
        root / "manifest.json",
        add_catalog_status_fields(
            manifest_for_raw_json(
                code_version=code_version,
                code=info.code,
                version=info.version,
                status=info.status,
                source="GetClinrec2",
                http_status=http_status,
                content_type=content_type,
                raw_content=raw_content,
                validation="valid",
                catalog_source_record_id=catalog_source_id,
                document_db_id=info.db_id,
            ),
            catalog_record,
            info.payload,
        ),
    )
    if db_id_state(catalog_source_id, info.db_id) == "mismatch":
        return {"code_version": code_version, "result": "db_id_mismatch"}
    return {"code_version": code_version, "result": "downloaded"}


def research_manifest_matches(raw_path: Path, manifest_path: Path, code_version: str) -> bool:
    if not raw_path.exists() or not manifest_path.exists():
        return False
    manifest = read_json_file(manifest_path)
    info, errors = minimal_validate_raw_document(
        raw_path.read_bytes(),
        expected_code_version=code_version,
    )
    return (
        info is not None
        and not errors
        and manifest.get("validation") == "valid"
        and manifest.get("sha256") == sha256_bytes(raw_path.read_bytes())
    )


def valid_current_code_versions(output: Path) -> list[str]:
    root = output / "current"
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and research_manifest_matches(
            path / "getclinrec.json",
            path / "manifest.json",
            path.name,
        ):
            result.append(path.name)
    return result


def download_legacy_documents(
    output: Path,
    client: ClinrecApiClient,
    all_records: list[dict[str, Any]],
    current_code_versions: list[str],
    options: ResearchCorpusOptions,
) -> None:
    all_by_code_version = {string_value(row.get("code_version")): row for row in all_records}
    valid = valid_legacy_pairs(output)
    attempts = existing_legacy_attempts(output)
    for current_code_version in legacy_order(current_code_versions, options.seed):
        if len(valid) >= options.legacy_target or len(attempts) >= options.legacy_attempt_limit:
            break
        code, version = parse_code_version_or_raise(current_code_version)
        if version <= 1:
            continue
        previous_code_version = f"{code}_{version - 1}"
        if (current_code_version, previous_code_version) in valid:
            continue
        root = output / "legacy" / current_code_version / previous_code_version
        result = download_one_legacy(
            root,
            client,
            current_code_version=current_code_version,
            previous_code_version=previous_code_version,
            catalog_record=all_by_code_version.get(previous_code_version),
        )
        attempts.append(result)
        append_jsonl(output / "attempts" / "legacy-attempts.jsonl", result)
        if result["result"] in {"downloaded", "already_valid"}:
            valid.add((current_code_version, previous_code_version))
        if result["result"] == "circuit_open":
            break


def legacy_order(current_code_versions: list[str], seed: int) -> list[str]:
    version_three_plus: list[str] = []
    version_two: list[str] = []
    for code_version in current_code_versions:
        _, version = parse_code_version_or_raise(code_version)
        if version >= 3:
            version_three_plus.append(code_version)
        elif version == 2:
            version_two.append(code_version)
    return deterministic_order(version_three_plus, seed) + deterministic_order(version_two, seed)


def download_one_legacy(
    root: Path,
    client: ClinrecApiClient,
    *,
    current_code_version: str,
    previous_code_version: str,
    catalog_record: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_path = root / "getclinrec.json"
    manifest_path = root / "manifest.json"
    if raw_path.exists() and research_manifest_matches(
        raw_path,
        manifest_path,
        previous_code_version,
    ):
        return legacy_result(current_code_version, previous_code_version, "already_valid")
    result = client.fetch_clinrec_payload(previous_code_version)
    if isinstance(result, ExternalApiError):
        return legacy_result(
            current_code_version,
            previous_code_version,
            classify_api_error(result),
            error=result.message,
            http_status=result.status_code,
        )
    row = save_research_document(
        root,
        code_version=previous_code_version,
        catalog_record=catalog_record or {"code_version": previous_code_version},
        raw_content=result.raw_content,
        http_status=result.status_code,
        content_type=result.content_type,
    )
    return legacy_result(
        current_code_version,
        previous_code_version,
        row["result"],
        error=row.get("error"),
        http_status=result.status_code,
    )


def classify_api_error(error: ExternalApiError) -> str:
    if error.status_code in {403, 404, 429}:
        return str(error.status_code)
    if error.kind == ApiErrorKind.CIRCUIT_OPEN:
        return "circuit_open"
    if error.kind == ApiErrorKind.REQUEST_ERROR and error.error_type == "timeout":
        return "timeout"
    if error.status_code is not None and error.status_code >= 500:
        return "5xx"
    if error.kind == ApiErrorKind.INVALID_JSON:
        return "invalid_json"
    return "other_error"


def result_row(
    code_version: str,
    result: str,
    error: str | None,
    api_error: ExternalApiError | None = None,
) -> dict[str, Any]:
    return {
        "code_version": code_version,
        "result": result,
        "error": error,
        "http_status": api_error.status_code if api_error else None,
        "api_error_kind": api_error.kind.value if api_error else None,
        "attempted_at": utc_now(),
    }


def legacy_result(
    current_code_version: str,
    previous_code_version: str,
    result: str,
    *,
    error: str | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "current_code_version": current_code_version,
        "previous_code_version": previous_code_version,
        "result": result,
        "error": error,
        "http_status": http_status,
        "attempted_at": utc_now(),
    }


def valid_legacy_pairs(output: Path) -> set[tuple[str, str]]:
    root = output / "legacy"
    pairs: set[tuple[str, str]] = set()
    if not root.exists():
        return pairs
    for current_dir in root.iterdir():
        if not current_dir.is_dir():
            continue
        for previous_dir in current_dir.iterdir():
            if not previous_dir.is_dir():
                continue
            if research_manifest_matches(
                previous_dir / "getclinrec.json",
                previous_dir / "manifest.json",
                previous_dir.name,
            ):
                pairs.add((current_dir.name, previous_dir.name))
    return pairs


def existing_legacy_attempts(output: Path) -> list[dict[str, Any]]:
    return read_jsonl(output / "attempts" / "legacy-attempts.jsonl")


def profile_corpus(output: Path, all_records: list[dict[str, Any]]) -> None:
    documents: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    all_by_code_version = {string_value(row.get("code_version")): row for row in all_records}
    for kind, root in (("current", output / "current"), ("legacy", output / "legacy")):
        if not root.exists():
            continue
        if kind == "current":
            raw_files = sorted(root.glob("*/getclinrec.json"))
        else:
            raw_files = sorted(root.glob("*/*/getclinrec.json"))
        for raw_path in raw_files:
            code_version = raw_path.parent.name
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            manifest = read_json_file(raw_path.parent / "manifest.json")
            catalog = all_by_code_version.get(code_version, {})
            document_row, section_rows = profile_document(
                kind,
                code_version,
                raw_path,
                payload,
                manifest,
                catalog,
            )
            documents.append(document_row)
            sections.extend(section_rows)
    write_jsonl(reports_root(output) / "documents.jsonl", documents)
    write_jsonl(reports_root(output) / "sections.jsonl", sections)
    write_json(reports_root(output) / "schema-profile.json", schema_profile(documents, sections))
    write_jsonl(reports_root(output) / "current-legacy-pairs.jsonl", pair_rows(output))


def profile_document(
    kind: str,
    code_version: str,
    raw_path: Path,
    payload: dict[str, Any],
    manifest: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    obj_value = payload.get("obj")
    obj: dict[str, Any] = obj_value if isinstance(obj_value, dict) else {}
    sections_value = obj.get("sections")
    section_values: list[Any] = sections_value if isinstance(sections_value, list) else []
    age_category = (
        payload.get("age_category")
        or obj.get("age_category")
        or catalog.get("age_category")
    )
    document_row = {
        "document_kind": kind,
        "code_version": code_version,
        "db_id": manifest.get("document_db_id"),
        "catalog_source_record_id": manifest.get("catalog_source_record_id"),
        "db_id_state": manifest.get("db_id_state"),
        "code": manifest.get("code"),
        "version": manifest.get("version"),
        "raw_status": manifest.get("document_status_raw"),
        "name": string_value(payload.get("name") or obj.get("name") or catalog.get("name")),
        "adult": payload.get("adult") if "adult" in payload else obj.get("adult"),
        "child": payload.get("child") if "child" in payload else obj.get("child"),
        "age_category": age_category,
        "publish_date": catalog.get("publish_date"),
        "file_size_bytes": raw_path.stat().st_size,
        "sha256": sha256_file(raw_path),
        "top_level_keys": sorted(payload),
        "obj_keys": sorted(obj.keys()),
        "sections_count": len(section_values),
        "mkb_count": len(payload.get("mkbs") or obj.get("mkbs") or catalog.get("mkbs") or []),
        "professional_association_count": len(payload.get("proff_associations") or []),
        "catalog_developer_count": len(catalog.get("developers") or []),
    }
    section_rows = [
        profile_section(code_version, index, section)
        for index, section in enumerate(section_values)
        if isinstance(section, dict)
    ]
    return document_row, section_rows


def profile_section(
    code_version: str,
    index: int,
    section: dict[str, Any],
) -> dict[str, Any]:
    content = section.get("content") or section.get("Content") or section.get("text")
    data = section.get("data") or section.get("Data")
    html = content if isinstance(content, str) else ""
    soup = BeautifulSoup(html, "html.parser") if html else None
    return {
        "document_code_version": code_version,
        "section_index": index,
        "section_id": section.get("id") or section.get("Id"),
        "section_name": section.get("name") or section.get("Name") or section.get("title"),
        "section_keys": sorted(section),
        "content_present": content is not None,
        "content_type": type(content).__name__ if content is not None else None,
        "content_length_chars": len(html),
        "data_present": data is not None,
        "data_type": type(data).__name__ if data is not None else None,
        "data_item_count": len(data) if isinstance(data, list) else None,
        "html_table_count": len(soup.find_all("table")) if soup else 0,
        "html_img_count": len(soup.find_all("img")) if soup else 0,
        "base64_image_count": html.count("data:image/"),
        "estimated_base64_bytes": estimated_base64_bytes(html),
        "link_count": len(soup.find_all("a")) if soup else 0,
        "ul_count": len(soup.find_all("ul")) if soup else 0,
        "ol_count": len(soup.find_all("ol")) if soup else 0,
        "li_count": len(soup.find_all("li")) if soup else 0,
    }


def estimated_base64_bytes(html: str) -> int:
    total = 0
    marker = "base64,"
    for item in html.split(marker)[1:]:
        token = item.split('"', 1)[0].split("'", 1)[0]
        total += int(len(token) * 0.75)
    return total


def schema_profile(
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "document_count": len(documents),
        "section_count": len(sections),
        "top_level_keys": count_values(documents, "top_level_keys"),
        "obj_keys": count_values(documents, "obj_keys"),
        "section_keys": count_values(sections, "section_keys"),
        "status_values": Counter(str(row.get("raw_status")) for row in documents),
        "db_id_states": Counter(str(row.get("db_id_state")) for row in documents),
        "documents_containing_tables": sum(
            1
            for row in documents
            if section_count_for(sections, row["code_version"], "html_table_count")
        ),
        "documents_containing_base64_images": sum(
            1
            for row in documents
            if section_count_for(sections, row["code_version"], "base64_image_count")
        ),
        "largest_documents": sorted(
            documents,
            key=lambda row: int(row.get("file_size_bytes") or 0),
            reverse=True,
        )[:10],
        "largest_sections": sorted(
            sections,
            key=lambda row: int(row.get("content_length_chars") or 0),
            reverse=True,
        )[:10],
    }


def count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            counter.update(str(item) for item in value)
    return dict(sorted(counter.items()))


def section_count_for(sections: list[dict[str, Any]], code_version: str, key: str) -> int:
    return sum(
        int(row.get(key) or 0)
        for row in sections
        if row["document_code_version"] == code_version
    )


def pair_rows(output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for current_code_version, previous_code_version in sorted(valid_legacy_pairs(output)):
        current = load_research_payload(
            output / "current" / current_code_version / "getclinrec.json"
        )
        previous = load_research_payload(
            output / "legacy" / current_code_version / previous_code_version / "getclinrec.json"
        )
        current_obj_value = current.get("obj")
        previous_obj_value = previous.get("obj")
        current_obj: dict[str, Any] = (
            current_obj_value if isinstance(current_obj_value, dict) else {}
        )
        previous_obj: dict[str, Any] = (
            previous_obj_value if isinstance(previous_obj_value, dict) else {}
        )
        current_sections = list_value(current_obj.get("sections"))
        previous_sections = list_value(previous_obj.get("sections"))
        rows.append(
            {
                "current_code_version": current_code_version,
                "previous_code_version": previous_code_version,
                "current_db_id": current.get("db_id"),
                "previous_db_id": previous.get("db_id"),
                "current_raw_status": current.get("status"),
                "previous_raw_status": previous.get("status"),
                "title_similarity": title_similarity(current, previous),
                "current_section_count": len(current_sections),
                "previous_section_count": len(previous_sections),
                "section_count_delta": len(current_sections) - len(previous_sections),
                "file_size_delta": (
                    output / "current" / current_code_version / "getclinrec.json"
                ).stat().st_size
                - (
                    output
                    / "legacy"
                    / current_code_version
                    / previous_code_version
                    / "getclinrec.json"
                ).stat().st_size,
                "top_level_key_additions": sorted(set(current) - set(previous)),
                "top_level_key_removals": sorted(set(previous) - set(current)),
                "obj_key_additions": sorted(set(current_obj) - set(previous_obj)),
                "obj_key_removals": sorted(set(previous_obj) - set(current_obj)),
            }
        )
    return rows


def load_research_payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def title_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = string_value(left.get("name") or left.get("title"))
    right_title = string_value(right.get("name") or right.get("title"))
    return round(SequenceMatcher(a=left_title, b=right_title).ratio() * 100, 1)


def write_reports(
    output: Path,
    active_records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    options: ResearchCorpusOptions,
    status: str,
) -> ResearchCorpusSummary:
    valid_current = valid_current_code_versions(output)
    valid_legacy = valid_legacy_pairs(output)
    summary = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "status": status,
        "active_catalog_total": len(active_records),
        "all_statuses_catalog_total": len(all_records),
        "requested_current_count": options.current_count,
        "valid_current_count": len(valid_current),
        "legacy_target": options.legacy_target,
        "legacy_minimum": options.legacy_minimum,
        "legacy_attempt_limit": options.legacy_attempt_limit,
        "legacy_attempts": len(existing_legacy_attempts(output)),
        "valid_legacy_count": len(valid_legacy),
        "updated_at": utc_now(),
    }
    write_json(reports_root(output) / "corpus-summary.json", summary)
    write_findings(output, summary)
    write_corpus_state(output, options, read_json_file(output / "selection.json"), "", status)
    return ResearchCorpusSummary(
        output=output,
        status=status,
        valid_current_count=len(valid_current),
        valid_legacy_count=len(valid_legacy),
        legacy_attempts=len(existing_legacy_attempts(output)),
        corpus_path=output / "corpus.json",
        summary_path=reports_root(output) / "corpus-summary.json",
    )


def write_corpus_state(
    output: Path,
    options: ResearchCorpusOptions,
    selection: dict[str, Any],
    catalog_sha: str,
    status: str,
) -> None:
    existing = read_json_file(output / "corpus.json")
    write_json(
        output / "corpus.json",
        {
            **existing,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "corpus_id": output.name,
            "created_at": existing.get("created_at") or utc_now(),
            "updated_at": utc_now(),
            "source": "GetJsonClinrecsFilterV2/GetClinrec2",
            "seed": options.seed,
            "requested_current_count": options.current_count,
            "valid_current_count": len(valid_current_code_versions(output)),
            "legacy_target": options.legacy_target,
            "legacy_minimum": options.legacy_minimum,
            "legacy_attempt_limit": options.legacy_attempt_limit,
            "valid_legacy_count": len(valid_legacy_pairs(output)),
            "forced_code_versions": list(options.include),
            "catalog_active_total": len(selection.get("initially_selected") or []),
            "selection_sha256": sha256_bytes(
                json.dumps(selection, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            if selection
            else None,
            "catalog_sha256": catalog_sha or existing.get("catalog_sha256"),
            "status": status,
        },
    )


def final_status(output: Path, options: ResearchCorpusOptions) -> str:
    current_count = len(valid_current_code_versions(output))
    legacy_count = len(valid_legacy_pairs(output))
    attempts = len(existing_legacy_attempts(output))
    if current_count < options.current_count:
        return "partial"
    if legacy_count >= options.legacy_minimum:
        return "completed"
    if attempts >= options.legacy_attempt_limit:
        return "partial"
    return "legacy_downloaded"


def corpus_status(output: Path) -> str:
    return string_value(read_json_file(output / "corpus.json").get("status")) or "created"


def write_findings(output: Path, summary: dict[str, Any]) -> None:
    profile = read_json_file(reports_root(output) / "schema-profile.json")
    lines = [
        "# Research findings",
        "",
        "## Corpus composition",
        f"- Valid current JSON: {summary['valid_current_count']}",
        f"- Valid previous JSON: {summary['valid_legacy_count']}",
        "",
        "## Download success and failures",
        f"- Legacy attempts: {summary['legacy_attempts']}",
        "",
        "## Observed top-level schemas",
        f"- Top-level keys observed: {len(profile.get('top_level_keys') or {})}",
        "",
        "## Observed section schemas",
        f"- Sections profiled: {profile.get('section_count', 0)}",
        "",
        "## db_id findings",
        f"- db_id states: {profile.get('db_id_states', {})}",
        "",
        "## Raw status findings",
        f"- Raw status values: {profile.get('status_values', {})}",
        "",
        "## Current/previous pair findings",
        f"- Pairs profiled: {len(read_jsonl(reports_root(output) / 'current-legacy-pairs.jsonl'))}",
        "",
        "## HTML and embedded asset findings",
        f"- Documents with tables: {profile.get('documents_containing_tables', 0)}",
        f"- Documents with embedded images: {profile.get('documents_containing_base64_images', 0)}",
        "",
        "## Fields stable across documents",
        "- Observed facts only; larger corpus required for stability claims.",
        "",
        "## Fields unstable across documents",
        "- Observed facts only; larger corpus required for instability claims.",
        "",
        "## Potentially redundant lifecycle checks",
        "- No production lifecycle check is marked redundant from this corpus alone.",
        "",
        "## Checks that remain necessary",
        "- Raw hash, id/code/version, and db_id checks remain necessary.",
        "",
        "## Recommended next parser architecture",
        "- Parse from raw JSON with schema-tolerant extractors and preserve original HTML.",
        "",
        "## Recommended next diff architecture",
        "- Compare raw identity fields first, then section/key-level structural metrics.",
        "",
        "## Open questions requiring a larger corpus",
        "- Status distributions and predecessor identity need a larger run before policy changes.",
        "",
    ]
    (reports_root(output) / "research-findings.md").write_text("\n".join(lines), encoding="utf-8")


def reports_root(output: Path) -> Path:
    return output / "reports"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_error(output: Path, kind: str, row: dict[str, Any]) -> None:
    append_jsonl(reports_root(output) / "errors.jsonl", {"kind": kind, **row})
