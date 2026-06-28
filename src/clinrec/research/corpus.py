from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clinrec.api.catalog_sync import sync_catalog, to_int, write_json
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import (
    BankError,
    add_catalog_status_fields,
    bank_root,
    db_id_state,
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
    write_atomic_bytes,
)
from clinrec.config import PathSettings, Settings
from clinrec.models.external import ApiErrorKind, ExternalApiError
from clinrec.research.catalog import records_by_code_version, resolve_catalog_candidates
from clinrec.research.migration import research_layout
from clinrec.research.schema import profile_corpus_offline

RESEARCH_SCHEMA_VERSION = "2.0"
SELECTION_ALGORITHM_VERSION = "research-selection-2"


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
    validate_research_options(options)
    ensure_research_output_safe(settings, output)
    ensure_research_start_state(output, options)
    output.mkdir(parents=True, exist_ok=True)
    reports_root(output).mkdir(parents=True, exist_ok=True)
    existing_status = corpus_status(output)
    if (
        existing_status
        and existing_status not in {"created", "dry_run"}
        and not options.resume
        and not options.profile_only
        and not options.dry_run
    ):
        if existing_status == "completed":
            active_records, all_records, _ = load_or_sync_research_catalog(settings, None, options)
            profile_corpus(output, all_records)
            return write_reports(output, active_records, all_records, options, existing_status)
        raise BankError("Existing incomplete corpus requires --resume or a new output path.")

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
    valid_current = selected_valid_current_code_versions(output, selection)
    if len(valid_current) >= options.current_count:
        download_legacy_documents(output, client, all_records, valid_current, options)
        profile_corpus(output, all_records)
    status = final_status(output, options)
    write_corpus_state(output, options, selection, catalog_sha, status)
    return write_reports(output, active_records, all_records, options, status)


def validate_research_options(options: ResearchCorpusOptions) -> None:
    if options.current_count <= 0:
        raise BankError("current_count must be greater than 0.")
    if options.legacy_target < 0:
        raise BankError("previous_target must be greater than or equal to 0.")
    if options.legacy_minimum < 0:
        raise BankError("previous_minimum must be greater than or equal to 0.")
    if options.legacy_attempt_limit < 0:
        raise BankError("previous_attempt_limit must be greater than or equal to 0.")
    if options.legacy_minimum > options.legacy_target:
        raise BankError("previous_minimum must be less than or equal to previous_target.")
    if options.legacy_target == 0 and options.legacy_minimum != 0:
        raise BankError("previous_minimum must be 0 when previous_target is 0.")
    if options.legacy_target > 0 and options.legacy_target > options.legacy_attempt_limit:
        raise BankError("previous_target must not exceed previous_attempt_limit.")
    seen: set[str] = set()
    duplicates: list[str] = []
    malformed: list[str] = []
    for code_version in options.include:
        if code_version in seen:
            duplicates.append(code_version)
            continue
        seen.add(code_version)
        try:
            parse_code_version_or_raise(code_version)
        except BankError:
            malformed.append(code_version)
    if duplicates:
        raise BankError(f"Duplicate mandatory include values: {duplicates}")
    if malformed:
        raise BankError(f"Malformed mandatory include values: {malformed}")


def ensure_research_output_safe(settings: Settings, output: Path) -> None:
    resolved = output.resolve()
    bank = bank_root(settings).resolve()
    if resolved == bank or bank in resolved.parents or resolved in bank.parents:
        raise BankError("Research output must not be inside data/bank.")


def ensure_research_start_state(output: Path, options: ResearchCorpusOptions) -> None:
    if options.resume or options.profile_only or options.dry_run or not output.exists():
        return
    if (output / "corpus.json").exists():
        return
    mutable_markers = (
        output / "current",
        output / "previous",
        output / "legacy",
        output / "attempts",
        output / "selection.json",
    )
    if any(path.exists() for path in mutable_markers):
        raise BankError("Research output path contains corpus data; use --resume.")
    entries = [entry for entry in output.iterdir() if entry.name not in {"catalog", "reports"}]
    if entries:
        raise BankError("Research output path must be empty or use --resume.")


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
    desired_quotas = stratum_targets(options.current_count)
    available_by_stratum = available_counts_by_stratum(records)
    selected_by_stratum = selected_counts_by_stratum(selected)
    all_statuses_path = output / "catalog" / "catalog-all-statuses.jsonl"
    selection = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "algorithm_version": SELECTION_ALGORITHM_VERSION,
        "seed": options.seed,
        "catalog_sha256": catalog_sha,
        "catalog_all_statuses_sha256": sha256_file(all_statuses_path)
        if all_statuses_path.exists()
        else None,
        "requested_current_count": options.current_count,
        "mandatory_includes": list(options.include),
        "desired_version_quotas": desired_quotas,
        "available_by_stratum": available_by_stratum,
        "selected_by_stratum": selected_by_stratum,
        "date_quintile_boundaries": date_quintile_boundaries(records),
        "initially_selected": selected,
        "replacements": [],
        "final_selected": [],
        "forced_failures": [],
        "failed_candidates": [],
        "quota_shortfalls": quota_shortfalls(desired_quotas, available_by_stratum),
        "quota_redistributions": quota_redistributions(desired_quotas, available_by_stratum),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    write_json(path, selection)
    return selection


def select_current_records(
    records: list[dict[str, Any]],
    options: ResearchCorpusOptions,
) -> list[str]:
    by_code_version = unique_records_by_code_version(records)
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
            code_version
            for code_version, row in by_code_version.items()
            if stratum_for_record(row) == stratum
            and code_version not in selected_set
        ]
        for code_version in stratified_order(candidates, by_code_version, options.seed):
            if len(selected) >= options.current_count or stratum_counts[stratum] >= target:
                break
            if code_version in selected_set:
                continue
            selected.append(code_version)
            selected_set.add(code_version)
            stratum_counts[stratum] += 1
    if len(selected) < options.current_count:
        remaining = [
            code_version for code_version in by_code_version if code_version not in selected_set
        ]
        for code_version in stratified_order(remaining, by_code_version, options.seed):
            if len(selected) >= options.current_count:
                break
            selected.append(code_version)
            selected_set.add(code_version)
    if len(selected) < options.current_count:
        raise BankError("Active catalog does not contain enough records for research selection.")
    return selected[: options.current_count]


def unique_records_by_code_version(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped = records_by_code_version(records)
    result: dict[str, dict[str, Any]] = {}
    for code_version, rows in sorted(grouped.items()):
        result[code_version] = sorted(
            rows,
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
        )[0]
    return result


def stratum_targets(current_count: int) -> dict[str, int]:
    if current_count == 250:
        return {"version_1": 90, "version_2": 90, "version_3_plus": 70}
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


def stratum_for_code_version(code_version: str) -> str:
    try:
        _, version = parse_code_version_or_raise(code_version)
    except BankError:
        return "version_3_plus"
    if version == 1:
        return "version_1"
    if version == 2:
        return "version_2"
    return "version_3_plus"


def available_counts_by_stratum(records: list[dict[str, Any]]) -> dict[str, int]:
    counter = {"version_1": 0, "version_2": 0, "version_3_plus": 0}
    for row in unique_records_by_code_version(records).values():
        counter[stratum_for_record(row)] += 1
    return counter


def selected_counts_by_stratum(selected: list[str]) -> dict[str, int]:
    counter = {"version_1": 0, "version_2": 0, "version_3_plus": 0}
    for code_version in selected:
        try:
            _, version = parse_code_version_or_raise(code_version)
        except BankError:
            continue
        if version == 1:
            counter["version_1"] += 1
        elif version == 2:
            counter["version_2"] += 1
        else:
            counter["version_3_plus"] += 1
    return counter


def quota_shortfalls(
    desired: dict[str, int],
    available: dict[str, int],
) -> dict[str, int]:
    return {
        key: max(0, desired.get(key, 0) - available.get(key, 0))
        for key in sorted(desired)
        if desired.get(key, 0) > available.get(key, 0)
    }


def quota_redistributions(
    desired: dict[str, int],
    available: dict[str, int],
) -> list[dict[str, Any]]:
    shortage = sum(quota_shortfalls(desired, available).values())
    rows: list[dict[str, Any]] = []
    if shortage <= 0:
        return rows
    for stratum in ("version_1", "version_2", "version_3_plus"):
        extra = max(0, available.get(stratum, 0) - desired.get(stratum, 0))
        if extra <= 0:
            continue
        moved = min(shortage, extra)
        rows.append({"to_stratum": stratum, "count": moved})
        shortage -= moved
        if shortage == 0:
            break
    return rows


def stratified_order(
    code_versions: list[str],
    rows_by_code_version: dict[str, dict[str, Any]],
    seed: int,
) -> list[str]:
    buckets: dict[tuple[str, str], list[str]] = {}
    for code_version in code_versions:
        row = rows_by_code_version[code_version]
        key = (date_bucket(row, rows_by_code_version.values()), age_group(row))
        buckets.setdefault(key, []).append(code_version)
    ordered: list[str] = []
    bucket_keys = sorted(buckets)
    while True:
        changed = False
        for key in bucket_keys:
            values = buckets[key]
            if not values:
                continue
            if len(values) > 1:
                buckets[key] = deterministic_order(values, seed)
                values = buckets[key]
            ordered.append(values.pop(0))
            changed = True
        if not changed:
            break
    return ordered


def deterministic_order(code_versions: list[str], seed: int) -> list[str]:
    return sorted(
        code_versions,
        key=lambda code_version: hashlib.sha256(f"{seed}:{code_version}".encode()).hexdigest(),
    )


def date_quintile_boundaries(records: list[dict[str, Any]]) -> list[str]:
    dates = sorted(
        {
            parsed
            for row in unique_records_by_code_version(records).values()
            if (parsed := normalized_publish_date(row)) is not None
        }
    )
    if not dates:
        return []
    boundaries: list[str] = []
    for numerator in (1, 2, 3, 4):
        index = min(len(dates) - 1, max(0, (len(dates) * numerator) // 5))
        boundaries.append(dates[index])
    return boundaries


def date_bucket(row: dict[str, Any], records: Any) -> str:
    publish_date = normalized_publish_date(row)
    if publish_date is None:
        return "unknown_date"
    boundaries = date_quintile_boundaries(list(records))
    for index, boundary in enumerate(boundaries, start=1):
        if publish_date <= boundary:
            return f"q{index}"
    return "q5"


def normalized_publish_date(row: dict[str, Any]) -> str | None:
    value = string_value(
        row.get("publish_date")
        or row.get("PublishDate")
        or row.get("date")
        or row.get("Date")
    )
    if len(value) < 10:
        return None
    candidate = value[:10]
    parts = candidate.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    year, month, day = (int(part) for part in parts)
    if not (1 <= month <= 12 and 1 <= day <= 31 and year >= 1900):
        return None
    return candidate


def age_group(row: dict[str, Any]) -> str:
    adult = bool_value(row.get("adult") if "adult" in row else row.get("Adult"))
    child = bool_value(row.get("child") if "child" in row else row.get("Child"))
    if adult is True and child is True:
        return "adult_and_child"
    if adult is True and child is False:
        return "adult_only"
    if adult is False and child is True:
        return "child_only"
    return "neither_or_unknown"


def bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    text = string_value(value).casefold()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def download_current_documents(
    output: Path,
    client: ClinrecApiClient,
    active_records: list[dict[str, Any]],
    selection: dict[str, Any],
    options: ResearchCorpusOptions,
) -> None:
    by_code_version = records_by_code_version(active_records)
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
    pending_replacements: list[str] = []
    while len(selected_valid_current_code_versions(output, selection)) < options.current_count:
        code_version = next_pending_replacement(pending_replacements, final_selected, attempted)
        if code_version is None:
            code_version = next_selection_candidate(
                selected,
                final_selected,
                attempted,
                pool,
                cursor,
            )
        if code_version is None:
            break
        if code_version in pool:
            cursor = pool.index(code_version) + 1
        attempted.add(code_version)
        result = download_one_research_current(
            output,
            client,
            code_version,
            by_code_version.get(code_version, []),
            options,
        )
        if result["result"] in {"downloaded", "already_valid", "valid_with_identity_warning"}:
            if code_version not in final_selected:
                final_selected.append(code_version)
        else:
            if code_version in options.include:
                forced_failures = list(selection.get("forced_failures") or [])
                forced_failures.append(result)
                selection["forced_failures"] = forced_failures
            replacements = list(selection.get("replacements") or [])
            replacement = stratum_replacement(
                active_records,
                selected=selected,
                attempted=attempted,
                final_selected=final_selected,
                failed_code_version=code_version,
                seed=options.seed,
            )
            if replacement is not None:
                pending_replacements.append(replacement["replacement_code_version"])
            replacements.append(
                {
                    "failed_code_version": code_version,
                    "failed_result": result["result"],
                    "failed_stratum": stratum_for_code_version(code_version),
                    "replacement_code_version": replacement["replacement_code_version"]
                    if replacement is not None
                    else None,
                    "replacement_stratum": replacement["replacement_stratum"]
                    if replacement is not None
                    else None,
                    "relaxed_dimensions": replacement["relaxed_dimensions"]
                    if replacement is not None
                    else [],
                    "selection_reason": replacement["selection_reason"]
                    if replacement is not None
                    else "no_replacement_available",
                }
            )
            selection["replacements"] = replacements
            failed_candidates = list(selection.get("failed_candidates") or [])
            failed_candidates.append(result)
            selection["failed_candidates"] = failed_candidates
            if result["result"] == "circuit_open":
                break
        selection["final_selected"] = final_selected
        selection["selected_by_stratum"] = selected_counts_by_stratum(final_selected)
        selection["updated_at"] = utc_now()
        write_json(output / "selection.json", selection)


def replacement_pool(records: list[dict[str, Any]], selected: list[str], seed: int) -> list[str]:
    selected_set = set(selected)
    return stratified_order(
        [
            code_version
            for code_version in unique_records_by_code_version(records)
            if code_version not in selected_set
        ],
        unique_records_by_code_version(records),
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


def next_pending_replacement(
    pending: list[str],
    final_selected: list[str],
    attempted: set[str],
) -> str | None:
    while pending:
        code_version = pending.pop(0)
        if code_version not in final_selected and code_version not in attempted:
            return code_version
    return None


def stratum_replacement(
    records: list[dict[str, Any]],
    *,
    selected: list[str],
    attempted: set[str],
    final_selected: list[str],
    failed_code_version: str,
    seed: int,
) -> dict[str, Any] | None:
    by_code_version = unique_records_by_code_version(records)
    failed_record = by_code_version[failed_code_version]
    failed_dimensions = {
        "version_stratum": stratum_for_record(failed_record),
        "date_bucket": date_bucket(failed_record, by_code_version.values()),
        "age_group": age_group(failed_record),
    }
    unavailable = set(selected) | attempted | set(final_selected)
    relaxation_order = (
        (),
        ("age_group",),
        ("date_bucket",),
        ("date_bucket", "age_group"),
        ("version_stratum", "date_bucket", "age_group"),
    )
    for relaxed in relaxation_order:
        candidates = []
        for code_version, row in by_code_version.items():
            if code_version in unavailable:
                continue
            dimensions = {
                "version_stratum": stratum_for_record(row),
                "date_bucket": date_bucket(row, by_code_version.values()),
                "age_group": age_group(row),
            }
            if all(
                dimensions[key] == failed_dimensions[key]
                for key in failed_dimensions
                if key not in relaxed
            ):
                candidates.append(code_version)
        for code_version in stratified_order(candidates, by_code_version, seed):
            return {
                "replacement_code_version": code_version,
                "replacement_stratum": stratum_for_record(by_code_version[code_version]),
                "relaxed_dimensions": list(relaxed),
                "selection_reason": "replacement_for_failed_current",
            }
    return None


def download_one_research_current(
    output: Path,
    client: ClinrecApiClient,
    code_version: str,
    catalog_candidates: list[dict[str, Any]],
    options: ResearchCorpusOptions,
) -> dict[str, Any]:
    root = output / "current" / code_version
    raw_path = root / "getclinrec.json"
    manifest_path = root / "manifest.json"
    if (
        raw_path.exists()
        and manifest_path.exists()
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
        catalog_candidates=catalog_candidates,
        raw_content=result.raw_content,
        http_status=result.status_code,
        content_type=result.content_type,
    )


def save_research_document(
    root: Path,
    *,
    code_version: str,
    catalog_candidates: list[dict[str, Any]],
    raw_content: bytes,
    http_status: int,
    content_type: str,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    info, errors = minimal_validate_raw_document(raw_content, expected_code_version=code_version)
    raw_path = root / "getclinrec.json"
    write_atomic_bytes(raw_path, raw_content)
    if info is None:
        row = {
            "code_version": code_version,
            "result": "invalid_json",
            "error": "; ".join(errors),
        }
        write_json(root / "manifest.json", {"validation": "invalid", **row})
        return row
    resolution = resolve_catalog_candidates(
        {code_version: catalog_candidates},
        code_version,
        document_db_id=info.db_id,
    )
    catalog_record = resolution.resolved_record
    if resolution.candidates:
        write_json(root / "catalog-candidates.json", {"candidates": resolution.candidates})
    if catalog_record is None:
        catalog_record = {"code_version": code_version} if resolution.state == "missing" else {}
    else:
        write_json(root / "catalog-record.json", catalog_record)
    catalog_source_id = source_record_id_from_catalog(catalog_record)
    manifest = add_catalog_status_fields(
        {
            **manifest_for_raw_json(
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
            **resolution.manifest_fields(),
        },
        catalog_record,
        info.payload,
    )
    write_json(
        root / "manifest.json",
        manifest,
    )
    if resolution.metadata_ambiguous or resolution.state == "missing":
        return {"code_version": code_version, "result": "valid_with_identity_warning"}
    if db_id_state(catalog_source_id, info.db_id) == "mismatch":
        return {"code_version": code_version, "result": "valid_with_identity_warning"}
    return {"code_version": code_version, "result": "downloaded"}


def research_manifest_matches(raw_path: Path, manifest_path: Path, code_version: str) -> bool:
    if not raw_path.exists() or not manifest_path.exists():
        return False
    manifest = read_json_file(manifest_path)
    raw_bytes = raw_path.read_bytes()
    info, errors = minimal_validate_raw_document(
        raw_bytes,
        expected_code_version=code_version,
    )
    return (
        info is not None
        and not errors
        and manifest.get("schema_version") == "2.0"
        and manifest.get("validation") == "valid"
        and manifest.get("sha256") == sha256_bytes(raw_bytes)
        and manifest.get("size") == len(raw_bytes)
        and manifest.get("code_version") == code_version
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


def selected_valid_current_code_versions(
    output: Path,
    selection: dict[str, Any] | None = None,
) -> list[str]:
    selection = selection or read_json_file(output / "selection.json")
    final_selected = {
        string_value(value)
        for value in selection.get("final_selected", [])
        if isinstance(value, str)
    }
    if not final_selected:
        return []
    return [
        code_version
        for code_version in valid_current_code_versions(output)
        if code_version in final_selected
    ]


def download_legacy_documents(
    output: Path,
    client: ClinrecApiClient,
    all_records: list[dict[str, Any]],
    current_code_versions: list[str],
    options: ResearchCorpusOptions,
) -> None:
    all_by_code_version = records_by_code_version(all_records)
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
        existing_attempt = latest_attempt(attempts, current_code_version, previous_code_version)
        if existing_attempt is not None and not should_retry_previous_attempt(
            existing_attempt,
            options,
        ):
            continue
        root = output / "previous" / current_code_version / previous_code_version
        result = download_one_legacy(
            root,
            client,
            current_code_version=current_code_version,
            previous_code_version=previous_code_version,
            catalog_candidates=all_by_code_version.get(previous_code_version, []),
        )
        attempts.append(result)
        append_jsonl(output / "attempts" / "previous-attempts.jsonl", result)
        if result["result"] in {"downloaded", "already_valid", "valid_with_identity_warning"}:
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
    catalog_candidates: list[dict[str, Any]],
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
        catalog_candidates=catalog_candidates,
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


def valid_legacy_pairs(
    output: Path,
    selected_current: set[str] | None = None,
) -> set[tuple[str, str]]:
    root = research_layout(output).previous_root
    pairs: set[tuple[str, str]] = set()
    if not root.exists():
        return pairs
    for current_dir in root.iterdir():
        if not current_dir.is_dir():
            continue
        if selected_current is not None and current_dir.name not in selected_current:
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
    layout = research_layout(output)
    if layout.previous_attempts_path.exists():
        return read_jsonl(layout.previous_attempts_path)
    legacy_attempts = output / "attempts" / "legacy-attempts.jsonl"
    if legacy_attempts.exists():
        return read_jsonl(legacy_attempts)
    return []


def latest_attempt(
    attempts: list[dict[str, Any]],
    current_code_version: str,
    previous_code_version: str,
) -> dict[str, Any] | None:
    for row in reversed(attempts):
        if (
            row.get("current_code_version") == current_code_version
            and row.get("previous_code_version") == previous_code_version
        ):
            return row
    return None


def should_retry_previous_attempt(row: dict[str, Any], options: ResearchCorpusOptions) -> bool:
    result = string_value(row.get("result"))
    if result in {"downloaded", "already_valid", "valid_with_identity_warning", "403", "404"}:
        return False
    if not options.retry_failed:
        return False
    return result in {"timeout", "429", "5xx", "circuit_open", "invalid_json"}


def profile_corpus(output: Path, all_records: list[dict[str, Any]]) -> None:
    _ = all_records
    profile_corpus_offline(output)


def write_reports(
    output: Path,
    active_records: list[dict[str, Any]],
    all_records: list[dict[str, Any]],
    options: ResearchCorpusOptions,
    status: str,
) -> ResearchCorpusSummary:
    selection = read_json_file(output / "selection.json")
    valid_current = selected_valid_current_code_versions(output, selection)
    valid_legacy = valid_legacy_pairs(output, set(valid_current))
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
        "previous_target": options.legacy_target,
        "previous_minimum": options.legacy_minimum,
        "previous_attempt_limit": options.legacy_attempt_limit,
        "legacy_attempts": len(existing_legacy_attempts(output)),
        "previous_attempts": len(existing_legacy_attempts(output)),
        "valid_legacy_count": len(valid_legacy),
        "valid_previous_count": len(valid_legacy),
        "updated_at": utc_now(),
    }
    write_json(reports_root(output) / "corpus-summary.json", summary)
    if not (reports_root(output) / "research-findings.md").exists():
        write_findings(output, summary)
    write_corpus_state(output, options, selection, "", status)
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
    valid_current = selected_valid_current_code_versions(output, selection)
    valid_previous = valid_legacy_pairs(output, set(valid_current))
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
            "valid_current_count": len(valid_current),
            "legacy_target": options.legacy_target,
            "legacy_minimum": options.legacy_minimum,
            "legacy_attempt_limit": options.legacy_attempt_limit,
            "previous_target": options.legacy_target,
            "previous_minimum": options.legacy_minimum,
            "previous_attempt_limit": options.legacy_attempt_limit,
            "valid_legacy_count": len(valid_previous),
            "valid_previous_count": len(valid_previous),
            "forced_code_versions": list(options.include),
            "catalog_active_total": catalog_active_total(output, selection),
            "initial_selection_count": len(selection.get("initially_selected") or []),
            "final_selection_count": len(selection.get("final_selected") or []),
            "replacement_count": len(selection.get("replacements") or []),
            "forced_failure_count": len(selection.get("forced_failures") or []),
            "layout_version": "2.0" if (output / "previous").exists() else "1.0",
            "selection_sha256": sha256_bytes(
                json.dumps(selection, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            if selection
            else None,
            "catalog_sha256": catalog_sha or existing.get("catalog_sha256"),
            "status": status,
        },
    )


def catalog_active_total(output: Path, selection: dict[str, Any]) -> int:
    active_path = output / "catalog" / "catalog-active.jsonl"
    if active_path.exists():
        return len(read_jsonl(active_path))
    return len(selection.get("initially_selected") or [])


def final_status(output: Path, options: ResearchCorpusOptions) -> str:
    selection = read_json_file(output / "selection.json")
    valid_current = selected_valid_current_code_versions(output, selection)
    current_count = len(valid_current)
    legacy_count = len(valid_legacy_pairs(output, set(valid_current)))
    attempts = len(existing_legacy_attempts(output))
    if current_count < options.current_count:
        return "failed"
    if legacy_count >= options.legacy_minimum:
        return "completed"
    if attempts >= options.legacy_attempt_limit:
        return "partial" if legacy_count >= options.legacy_minimum else "failed"
    return "previous_downloaded"


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
        f"- Pairs profiled: {current_previous_pair_count(output)}",
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


def current_previous_pair_count(output: Path) -> int:
    preferred = reports_root(output) / "current-previous-pairs.jsonl"
    if preferred.exists():
        return len(read_jsonl(preferred))
    return len(read_jsonl(reports_root(output) / "current-legacy-pairs.jsonl"))


def reports_root(output: Path) -> Path:
    return output / "reports"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_error(output: Path, kind: str, row: dict[str, Any]) -> None:
    append_jsonl(reports_root(output) / "errors.jsonl", {"kind": kind, **row})
