from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clinrec.api.catalog_sync import sync_catalog
from clinrec.api.client import ClinrecApiClient
from clinrec.bank.common import BankRecordFilter
from clinrec.bank.current import BankDownloadSummary, download_current_documents
from clinrec.bank.previous import BankPreviousSummary, check_previous_documents
from clinrec.bank.qa import BankQaSummary, run_bank_qa


@dataclass(frozen=True)
class BankRunSummary:
    catalog_active_records: int | None
    download: BankDownloadSummary
    previous: BankPreviousSummary
    qa: BankQaSummary | None


def run_bank_pipeline(
    settings: Any,
    client: ClinrecApiClient | None,
    options: BankRecordFilter,
) -> BankRunSummary:
    if options.dry_run:
        download = download_current_documents(settings, None, options)
        previous = check_previous_documents(settings, None, options)
        return BankRunSummary(
            catalog_active_records=None,
            download=download,
            previous=previous,
            qa=None,
        )
    if client is None:
        raise RuntimeError("HTTP client is required unless --dry-run is used.")

    catalog_summary = sync_catalog(settings, client)
    download = download_current_documents(settings, client, options)
    previous = check_previous_documents(settings, client, options)
    qa = run_bank_qa(settings, options)
    return BankRunSummary(
        catalog_active_records=catalog_summary.active.records,
        download=download,
        previous=previous,
        qa=qa,
    )
