from __future__ import annotations

import base64
import binascii
import hashlib
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def table_rows_for_html(
    *,
    code_version: str,
    document_kind: str,
    section_id: str,
    html: str,
    current_code_version: str | None = None,
) -> list[dict[str, Any]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for index, table in enumerate(soup.find_all("table")):
        table_html = str(table)
        tr_items = [row for row in table.find_all("tr") if nearest_table(row) == table]
        cell_items = [
            cell for cell in table.find_all(["td", "th"]) if nearest_table(cell) == table
        ]
        rowspans, invalid_rowspans = span_values(cell_items, "rowspan")
        colspans, invalid_colspans = span_values(cell_items, "colspan")
        invalid_spans = invalid_rowspans + invalid_colspans
        rows.append(
            {
                "code_version": code_version,
                "document_kind": document_kind,
                "current_code_version": current_code_version,
                "section_id": section_id,
                "table_index": index,
                "rows": len(tr_items),
                "cells": len(cell_items),
                "th_count": sum(1 for cell in cell_items if cell.name == "th"),
                "td_count": sum(1 for cell in cell_items if cell.name == "td"),
                "rowspan_count": len(rowspans),
                "colspan_count": len(colspans),
                "max_rowspan": max(rowspans) if rowspans else 1,
                "max_colspan": max(colspans) if colspans else 1,
                "nested_table_count": len(table.find_all("table")),
                "empty_cell_count": sum(
                    1 for cell in cell_items if not cell.get_text(strip=True)
                ),
                "text_length": len(table.get_text(" ", strip=True)),
                "html_sha256": sha256_text(table_html),
                "invalid_span_count": len(invalid_spans),
                "invalid_spans": invalid_spans,
                "malformed": len(tr_items) == 0 or len(cell_items) == 0 or bool(invalid_spans),
            }
        )
    return rows


def image_rows_for_html(
    *,
    code_version: str,
    document_kind: str,
    section_id: str,
    html: str,
    current_code_version: str | None = None,
) -> list[dict[str, Any]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for index, image in enumerate(soup.find_all("img")):
        src_present = image.has_attr("src")
        src = str(image.get("src") or "") if src_present else ""
        decoded_size: int | None = None
        asset_sha: str | None = None
        mime_type: str | None = None
        decode_error: str | None = None
        src_class = classify_src(src, src_present=src_present)
        if src_class == "base64":
            mime_type, token = split_data_uri(src)
            try:
                decoded = base64.b64decode(token, validate=True)
                decoded_size = len(decoded)
                asset_sha = hashlib.sha256(decoded).hexdigest()
            except (binascii.Error, ValueError) as exc:
                decode_error = str(exc)
        rows.append(
            {
                "code_version": code_version,
                "document_kind": document_kind,
                "current_code_version": current_code_version,
                "section_id": section_id,
                "image_index": index,
                "src_class": src_class,
                "src_raw_length": len(src),
                "mime_type": mime_type,
                "decoded_size_bytes": decoded_size,
                "sha256": asset_sha,
                "width_attribute": image.get("width"),
                "height_attribute": image.get("height"),
                "alt_present": image.has_attr("alt"),
                "alt_length": len(str(image.get("alt") or "")) if image.has_attr("alt") else 0,
                "duplicate_count": 0,
                "decode_error": decode_error,
            }
        )
    duplicate_counts = Counter(row["sha256"] for row in rows if row.get("sha256"))
    for row in rows:
        sha = row.get("sha256")
        row["duplicate_count"] = duplicate_counts.get(sha, 0) if sha else 0
    return rows


def classify_src(src: str, *, src_present: bool) -> str:
    if not src_present:
        return "missing"
    if src == "":
        return "empty"
    if src.startswith("data:") and ";base64," in src:
        return "base64"
    parsed = urlparse(src)
    if parsed.scheme == "http":
        return "http"
    if parsed.scheme == "https":
        return "https"
    if parsed.scheme == "file":
        return "file"
    if not parsed.scheme:
        return "relative"
    return "other"


def split_data_uri(src: str) -> tuple[str | None, str]:
    prefix, token = src.split(",", maxsplit=1)
    mime_type = prefix[5:].split(";", maxsplit=1)[0] if prefix.startswith("data:") else None
    return mime_type or None, token


def span_values(cells: list[Any], attribute: str) -> tuple[list[int], list[dict[str, Any]]]:
    values: list[int] = []
    invalid: list[dict[str, Any]] = []
    for cell_index, cell in enumerate(cells):
        if not cell.has_attr(attribute):
            continue
        raw = cell.get(attribute)
        value = int_value(raw)
        if value is None or value <= 0:
            invalid.append(
                {
                    "cell_index": cell_index,
                    "attribute": attribute,
                    "raw_value": raw,
                    "normalized_value": 1,
                }
            )
            values.append(1)
        else:
            values.append(value)
    return values, invalid


def nearest_table(tag: Any) -> Any:
    return tag.find_parent("table")


def int_value(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
