from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
import typer

from clinrec.api.catalog_sync import SyncError
from clinrec.api.catalog_sync import sync_catalog as run_catalog_sync
from clinrec.api.catalog_sync import sync_references as run_references_sync
from clinrec.api.client import ClinrecApiClient
from clinrec.api.document_download import DownloadError, DownloadOptions
from clinrec.api.document_download import download_documents as run_download_documents
from clinrec.api.document_download import download_pdfs as run_download_pdfs
from clinrec.api.version_discovery import DiscoveryError, DiscoveryOptions
from clinrec.api.version_discovery import discover_versions as run_discover_versions
from clinrec.bank.common import BankError, BankRecordFilter
from clinrec.bank.current import download_current_documents as run_bank_download_current
from clinrec.bank.previous import check_previous_documents as run_bank_check_previous
from clinrec.bank.qa import run_bank_qa
from clinrec.bank.run import run_bank_pipeline
from clinrec.config import DEFAULT_CONFIG_PATH, Settings, ensure_data_directories, load_settings
from clinrec.logging import configure_logging
from clinrec.parsing.document import ParseError, ParseOptions
from clinrec.parsing.document import parse_documents as run_parse_documents
from clinrec.qa.checks import QaOptions
from clinrec.qa.checks import run_qa as run_qa_checks

app = typer.Typer(help="Clinical recommendations pipeline CLI.")


ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config",
        "-c",
        help="Path to YAML configuration file.",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
]


def bootstrap(config_path: Path) -> Settings:
    settings = load_settings(config_path)
    ensure_data_directories(settings)
    configure_logging(settings.logging)
    structlog.get_logger().info("command_bootstrap_complete", config=str(config_path))
    return settings


def placeholder(command_name: str, config_path: Path, *, http_planned: bool = False) -> None:
    bootstrap(config_path)
    typer.echo(f"{command_name}: command skeleton is ready.")
    if http_planned:
        typer.echo("HTTP download is not implemented for this command yet.")
    structlog.get_logger().info("placeholder_command_completed", command=command_name)


@app.command("sync-catalog")
def sync_catalog(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize active and all-status catalog snapshots."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_catalog_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"sync-catalog failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("sync-catalog completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"snapshot: {summary.snapshot_root}")
    typer.echo(
        f"active: pages={summary.active.pages}, records={summary.active.records}, "
        f"total={summary.active.total_records}"
    )
    typer.echo(
        f"all-statuses: pages={summary.all_statuses.pages}, "
        f"records={summary.all_statuses.records}, total={summary.all_statuses.total_records}"
    )
    typer.echo(f"active_index: {summary.active_index_path}")
    typer.echo(f"all_statuses_index: {summary.all_statuses_index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")
    typer.echo(f"qa_issues: {len(summary.issues)}")


@app.command("bank-sync-catalog")
def bank_sync_catalog(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize catalog indexes required by the raw JSON bank."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_catalog_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"bank-sync-catalog failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-sync-catalog completed")
    typer.echo(f"active_records: {summary.active.records}")
    typer.echo(f"active_unique_code_versions: {summary.active.unique_code_versions}")
    typer.echo(f"all_statuses_records: {summary.all_statuses.records}")
    typer.echo(f"active_index: {summary.active_index_path}")
    typer.echo(f"all_statuses_index: {summary.all_statuses_index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")


@app.command("sync-references")
def sync_references(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Synchronize external reference dictionaries."""
    settings = bootstrap(config)
    try:
        with ClinrecApiClient(settings.http, settings.rate_limit) as client:
            summary = run_references_sync(settings, client)
    except SyncError as exc:
        typer.echo(f"sync-references failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("sync-references completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"snapshot: {summary.snapshot_dir}")
    typer.echo(f"raw: {summary.raw_path}")
    typer.echo(f"index: {summary.index_path}")
    typer.echo(f"qa_report: {summary.qa_report_path}")
    typer.echo(f"organizations: {summary.organizations}")
    typer.echo(f"qa_issues: {len(summary.issues)}")


@app.command("discover-versions")
def discover_versions(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Check every catalog candidate version."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Recheck every selected version.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Recheck temporary failures."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build candidates without HTTP requests or index writes."),
    ] = False,
) -> None:
    """Discover available document versions independently."""
    settings = bootstrap(config)
    options = DiscoveryOptions(
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_discover_versions(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_discover_versions(settings, client, options)
    except DiscoveryError as exc:
        typer.echo(f"discover-versions failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("discover-versions completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"checked: {summary.checked}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"codes: {summary.codes}")
    typer.echo(f"dry_run: {summary.dry_run}")
    typer.echo(f"index: {summary.index_path}")
    typer.echo(f"report: {summary.report_path}")
    typer.echo(f"availability: {summary.availability_counts}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")


@app.command("download")
def download(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Download one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Download every available JSON version."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected documents without HTTP requests or writes."),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Include temporary discovery failures."),
    ] = False,
) -> None:
    """Download raw GetClinrec2 JSON files only."""
    settings = bootstrap(config)
    options = DownloadOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        dry_run=dry_run,
        retry_failed=retry_failed,
    )
    try:
        if dry_run:
            summary = run_download_documents(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_download_documents(settings, client, options)
    except DownloadError as exc:
        typer.echo(f"download failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("download completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"partial: {summary.partial}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    else:
        for document in summary.documents:
            typer.echo(
                f"{document.code_version}: status={document.status}, "
                f"manifest={document.manifest_path}"
            )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-download-current")
def bank_download_current(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option(
            "--code-version",
            help="Download one active bank CodeVersion; can be repeated.",
        ),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Download every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry temporary failed records."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected records without HTTP requests or writes."),
    ] = False,
) -> None:
    """Download active-bank raw GetClinrec2 JSON files only."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_bank_download_current(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_download_current(settings, client, options)
    except BankError as exc:
        typer.echo(f"bank-download-current failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-download-current completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if summary.references_index_path is not None:
        typer.echo(f"references_index: {summary.references_index_path}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: status={document.status}, "
            f"manifest={document.manifest_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-check-previous")
def bank_check_previous(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one active bank CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Check every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Recheck selected relations.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry previous_temporary_failure relations."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected previous candidates without writes."),
    ] = False,
) -> None:
    """Check only Version - 1 for active bank records."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_bank_check_previous(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_check_previous(settings, client, options)
    except BankError as exc:
        typer.echo(f"bank-check-previous failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-check-previous completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"checked: {summary.checked}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: previous={document.previous_code_version}, "
            f"relation_status={document.relation_status}, relation={document.relation_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("bank-qa")
def bank_qa(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one active bank CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Check every active catalog record."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Accepted for filter parity.")] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Accepted for filter parity."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Accepted for filter parity."),
    ] = False,
) -> None:
    """Run global active-bank completeness checks."""
    _ = (force, retry_failed, dry_run)
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records or not any((code_version, code, from_code, to_code)),
    )
    try:
        summary = run_bank_qa(settings, options)
    except BankError as exc:
        typer.echo(f"bank-qa failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-qa completed")
    typer.echo(f"expected: {summary.expected}")
    typer.echo(f"folders: {summary.folders}")
    typer.echo(f"valid_current_json: {summary.valid_current_json}")
    typer.echo(f"valid_manifests: {summary.valid_manifests}")
    typer.echo(f"fatal: {summary.fatal}")
    typer.echo(f"errors: {summary.errors}")
    typer.echo(f"completeness: {summary.completeness_path}")
    typer.echo(f"previous_relations: {summary.previous_relations_path}")
    typer.echo(f"anomalies: {summary.anomalies_path}")
    if summary.fatal or summary.errors:
        raise typer.Exit(1)


@app.command("bank-run")
def bank_run(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option(
            "--code-version",
            help="Run bank pipeline for one CodeVersion; can be repeated.",
        ),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Run one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Run Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Run Codes less than or equal to this value."),
    ] = None,
    all_records: Annotated[
        bool,
        typer.Option("--all", help="Run every active catalog record."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Redownload/recheck selected files."),
    ] = False,
    retry_failed: Annotated[
        bool,
        typer.Option("--retry-failed", help="Retry previous temporary failures."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected records without HTTP requests or writes."),
    ] = False,
) -> None:
    """Run the new raw JSON bank pipeline only."""
    settings = bootstrap(config)
    options = BankRecordFilter(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_records=all_records,
        force=force,
        retry_failed=retry_failed,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_bank_pipeline(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_bank_pipeline(settings, client, options)
    except (BankError, RuntimeError, SyncError) as exc:
        typer.echo(f"bank-run failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("bank-run completed")
    typer.echo(f"catalog_active_records: {summary.catalog_active_records}")
    typer.echo(f"download_planned: {summary.download.planned}")
    typer.echo(f"download_failed: {summary.download.failed}")
    typer.echo(f"previous_planned: {summary.previous.planned}")
    typer.echo(f"previous_failed: {summary.previous.failed}")
    if summary.qa is not None:
        typer.echo(f"qa_fatal: {summary.qa.fatal}")
        typer.echo(f"qa_errors: {summary.qa.errors}")
    if summary.download.failed or summary.previous.failed or (
        summary.qa is not None and (summary.qa.fatal or summary.qa.errors)
    ):
        raise typer.Exit(1)


@app.command("download-pdf")
def download_pdf(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Download one CodeVersion PDF; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Download one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Download Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Download Codes less than or equal to this value."),
    ] = None,
    all_versions: Annotated[
        bool,
        typer.Option("--all", help="Download every available PDF."),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Redownload selected files.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show selected PDFs without HTTP requests or writes."),
    ] = False,
) -> None:
    """Download official PDF files only."""
    settings = bootstrap(config)
    options = DownloadOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
        all_versions=all_versions,
        force=force,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            summary = run_download_pdfs(settings, None, options)
        else:
            with ClinrecApiClient(settings.http, settings.rate_limit) as client:
                summary = run_download_pdfs(settings, client, options)
    except DownloadError as exc:
        typer.echo(f"download-pdf failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("download-pdf completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"downloaded: {summary.downloaded}")
    typer.echo(f"skipped: {summary.skipped}")
    typer.echo(f"failed: {summary.failed}")
    typer.echo(f"dry_run: {summary.dry_run}")
    if dry_run:
        typer.echo(f"candidates_preview: {summary.candidates_preview}")
    else:
        for document in summary.documents:
            typer.echo(
                f"{document.code_version}: pdf_status={document.pdf_status}, "
                f"manifest={document.manifest_path}"
            )
    if summary.failed:
        raise typer.Exit(1)


@app.command("parse")
def parse(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Parse one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Parse one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Parse Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Parse Codes less than or equal to this value."),
    ] = None,
) -> None:
    """Parse downloaded raw GetClinrec2 JSON into normalized document artifacts."""
    settings = bootstrap(config)
    options = ParseOptions(
        code_versions=code_version,
        code=code,
        from_code=from_code,
        to_code=to_code,
    )
    try:
        summary = run_parse_documents(settings, options)
    except ParseError as exc:
        typer.echo(f"parse failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("parse completed")
    typer.echo(f"timestamp: {summary.timestamp}")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"parsed: {summary.parsed}")
    typer.echo(f"failed: {summary.failed}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: status={document.status}, "
            f"sections={document.sections}, tables={document.tables}, "
            f"images={document.images}, recommendations={document.recommendations}, "
            f"qa_issues={document.issues}, document={document.document_json_path}"
        )
    if summary.failed:
        raise typer.Exit(1)


@app.command("build-families")
def build_families(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Prepare revision family analysis command."""
    placeholder("build-families", config)


@app.command("qa")
def qa(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    code_version: Annotated[
        list[str] | None,
        typer.Option("--code-version", help="Check one CodeVersion; can be repeated."),
    ] = None,
    code: Annotated[int | None, typer.Option("--code", help="Check one Code only.")] = None,
    from_code: Annotated[
        int | None,
        typer.Option("--from-code", help="Check Codes greater than or equal to this value."),
    ] = None,
    to_code: Annotated[
        int | None,
        typer.Option("--to-code", help="Check Codes less than or equal to this value."),
    ] = None,
    strict_pdf: Annotated[
        bool,
        typer.Option("--strict-pdf", help="Treat missing PDF control sources as errors."),
    ] = False,
) -> None:
    """Run local source, parsed artifact, manifest, and optional PDF QA checks."""
    settings = bootstrap(config)
    summary = run_qa_checks(
        settings,
        QaOptions(
            code_versions=code_version,
            code=code,
            from_code=from_code,
            to_code=to_code,
            strict_pdf=strict_pdf,
        ),
    )
    typer.echo("qa completed")
    typer.echo(f"planned: {summary.planned}")
    typer.echo(f"fatal: {summary.fatal}")
    typer.echo(f"errors: {summary.errors}")
    typer.echo(f"warnings: {summary.warnings}")
    typer.echo(f"info: {summary.info}")
    typer.echo(f"report: {summary.report_path}")
    for document in summary.documents:
        typer.echo(
            f"{document.code_version}: fatal={document.fatal}, errors={document.errors}, "
            f"warnings={document.warnings}, info={document.info}"
        )
    if summary.fatal or summary.errors:
        raise typer.Exit(1)


@app.command("run-all")
def run_all(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Prepare full pipeline orchestration command."""
    placeholder("run-all", config, http_planned=True)


if __name__ == "__main__":
    app()
