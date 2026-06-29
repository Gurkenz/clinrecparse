from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from clinrec.bank.common import string_value
from clinrec.parsed.html import nearest_table, normalize_text, positive_span, table_cell_text
from clinrec.parsed.models import sha256_text
from clinrec.research.sections import section_html

RAW_CHILD_SECTION_KEYS = (
    "sections",
    "Sections",
    "children",
    "Children",
    "items",
    "Items",
    "subsections",
    "Subsections",
)
HEADING_TAGS = {f"h{level}" for level in range(1, 7)}


@dataclass(frozen=True)
class RawSectionRecord:
    section: dict[str, Any]
    raw_path: str
    parent_raw_path: str | None
    source_order: int
    depth: int


@dataclass(frozen=True)
class RawTextUnitRecord:
    source_unit_id: str
    section_raw_path: str
    dom_path: str
    unit_type: str
    text: str
    text_sha256: str
    source_order: int


@dataclass(frozen=True)
class RawTableRecord:
    raw_table_id: str
    section_raw_path: str
    dom_path: str
    table_occurrence_index: int
    source_html_sha256: str


@dataclass(frozen=True)
class RawTableCellRecord:
    raw_cell_id: str
    raw_table_id: str
    physical_row: int
    physical_column: int
    rowspan: int
    colspan: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class RawTablePlacementRecord:
    raw_placement_id: str
    raw_cell_id: str
    raw_table_id: str
    logical_row: int
    logical_column: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class RawImageRecord:
    raw_image_id: str
    section_raw_path: str
    dom_path: str
    occurrence_index: int
    source_type: str
    src_sha256: str | None
    alt: str
    title: str


@dataclass(frozen=True)
class RawSourceInventory:
    sections: list[RawSectionRecord]
    text_units: list[RawTextUnitRecord]
    tables: list[RawTableRecord]
    table_cells: list[RawTableCellRecord]
    table_placements: list[RawTablePlacementRecord]
    images: list[RawImageRecord]
    errors: list[dict[str, Any]]


def build_raw_source_inventory(payload: dict[str, Any]) -> RawSourceInventory:
    obj_value = payload.get("obj")
    obj = obj_value if isinstance(obj_value, dict) else {}
    sections_value = obj.get("sections")
    records: list[RawSectionRecord] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(sections_value, list):
        errors.append(issue("obj.sections", "raw_sections_not_list", type(sections_value).__name__))
        return RawSourceInventory(
            sections=records,
            text_units=[],
            tables=[],
            table_cells=[],
            table_placements=[],
            images=[],
            errors=errors,
        )
    append_raw_section_records(
        records,
        errors,
        sections_value,
        parent_raw_path=None,
        container_path="obj.sections",
        depth=0,
    )
    text_units: list[RawTextUnitRecord] = []
    tables: list[RawTableRecord] = []
    table_cells: list[RawTableCellRecord] = []
    table_placements: list[RawTablePlacementRecord] = []
    images: list[RawImageRecord] = []
    for record in records:
        append_raw_section_inventory_details(
            record,
            text_units=text_units,
            tables=tables,
            table_cells=table_cells,
            table_placements=table_placements,
            images=images,
        )
    return RawSourceInventory(
        sections=records,
        text_units=text_units,
        tables=tables,
        table_cells=table_cells,
        table_placements=table_placements,
        images=images,
        errors=errors,
    )


def append_raw_section_records(
    records: list[RawSectionRecord],
    errors: list[dict[str, Any]],
    items: list[Any],
    *,
    parent_raw_path: str | None,
    container_path: str,
    depth: int,
) -> None:
    for index, item in enumerate(items):
        raw_path = f"{container_path}[{index}]"
        if not isinstance(item, dict):
            errors.append(issue(raw_path, "raw_section_not_object", type(item).__name__))
            continue
        source_order = len(records)
        records.append(
            RawSectionRecord(
                section=item,
                raw_path=raw_path,
                parent_raw_path=parent_raw_path,
                source_order=source_order,
                depth=depth,
            )
        )
        for child_key in RAW_CHILD_SECTION_KEYS:
            if child_key not in item:
                continue
            child_value = item.get(child_key)
            child_path = f"{raw_path}.{child_key}"
            if not isinstance(child_value, list):
                errors.append(
                    issue(child_path, "raw_child_sections_not_list", type(child_value).__name__)
                )
                continue
            section_like_children = [
                child for child in child_value if is_raw_section_like_child(child)
            ]
            malformed_children = [
                child for child in child_value if not is_raw_section_like_child(child)
            ]
            if malformed_children and section_like_children:
                errors.append(
                    issue(
                        child_path,
                        "raw_child_section_container_mixed",
                        {"items": len(child_value), "section_like": len(section_like_children)},
                    )
                )
            if not section_like_children:
                continue
            append_raw_section_records(
                records,
                errors,
                child_value,
                parent_raw_path=raw_path,
                container_path=child_path,
                depth=depth + 1,
            )


def is_raw_section_like_child(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    section_keys = {
        "id",
        "Id",
        "ID",
        "title",
        "Title",
        "name",
        "Name",
        "content",
        "Content",
        "html",
        "Html",
        "HTML",
        "text",
        "Text",
    }
    return bool(section_keys.intersection(value) or set(RAW_CHILD_SECTION_KEYS).intersection(value))


def append_raw_section_inventory_details(
    record: RawSectionRecord,
    *,
    text_units: list[RawTextUnitRecord],
    tables: list[RawTableRecord],
    table_cells: list[RawTableCellRecord],
    table_placements: list[RawTablePlacementRecord],
    images: list[RawImageRecord],
) -> None:
    raw_html = section_html(record.section)
    soup = BeautifulSoup(raw_html, "lxml")
    root = soup.body if soup.body is not None else soup
    for unit_index, unit in enumerate(raw_text_units(root)):
        text = normalize_text(unit.get_text(" ", strip=True)) if isinstance(unit, Tag) else unit
        if not text:
            continue
        unit_type = block_type_for_tag(unit.name.lower()) if isinstance(unit, Tag) else "paragraph"
        dom_path = f"{record.raw_path}.text[{unit_index}]"
        text_units.append(
            RawTextUnitRecord(
                source_unit_id=f"raw-text:{sha256_text(dom_path)[:16]}",
                section_raw_path=record.raw_path,
                dom_path=dom_path,
                unit_type=unit_type,
                text=text,
                text_sha256=sha256_text(text),
                source_order=len(text_units),
            )
        )
    raw_tables = [table for table in root.find_all("table") if isinstance(table, Tag)]
    for table_index, table in enumerate(raw_tables):
        raw_table_id = f"raw-table:{sha256_text(f'{record.raw_path}.table[{table_index}]')[:16]}"
        tables.append(
            RawTableRecord(
                raw_table_id=raw_table_id,
                section_raw_path=record.raw_path,
                dom_path=f"{record.raw_path}.table[{table_index}]",
                table_occurrence_index=table_index,
                source_html_sha256=sha256_text(str(table)),
            )
        )
        raw_cells, _, raw_placements = raw_table_cells_and_grid(table, raw_table_id=raw_table_id)
        table_cells.extend(raw_cells)
        table_placements.extend(raw_placements)
    raw_images = [image for image in root.find_all("img") if isinstance(image, Tag)]
    for image_index, image in enumerate(raw_images):
        src = string_value(image.get("src"))
        raw_image_path = f"{record.raw_path}.image[{image_index}]"
        images.append(
            RawImageRecord(
                raw_image_id=f"raw-image:{sha256_text(raw_image_path)[:16]}",
                section_raw_path=record.raw_path,
                dom_path=raw_image_path,
                occurrence_index=image_index,
                source_type=classify_image_src(src, src_present=image.has_attr("src")),
                src_sha256=sha256_text(src) if src else None,
                alt=string_value(image.get("alt")),
                title=string_value(image.get("title")),
            )
        )


def raw_text_units(root: Tag | BeautifulSoup) -> list[Tag | str]:
    units: list[Tag | str] = []
    for child in root.children:
        if isinstance(child, NavigableString):
            text = normalize_text(str(child))
            if text:
                units.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        tag_name = child.name.lower()
        if tag_name in {"table", "img"}:
            continue
        if tag_name in {"html", "body", "div", "section", "article", "main", "ul", "ol"}:
            nested = raw_text_units(child)
            if nested:
                units.extend(nested)
                continue
        text = normalize_text(child.get_text(" ", strip=True))
        if text:
            units.append(child)
    return units


def raw_table_cells_and_grid(
    table: Tag,
    *,
    raw_table_id: str,
) -> tuple[
    list[RawTableCellRecord],
    list[list[dict[str, Any]]],
    list[RawTablePlacementRecord],
]:
    rows = [
        row
        for row in table.find_all("tr")
        if isinstance(row, Tag) and nearest_table(row) is table
    ]
    cells: list[RawTableCellRecord] = []
    occupied: dict[tuple[int, int], dict[str, Any]] = {}
    grid: list[list[dict[str, Any]]] = []
    placements: list[RawTablePlacementRecord] = []
    for row_index, row in enumerate(rows):
        grid_row: list[dict[str, Any]] = []
        column_index = 0
        direct_cells = [
            cell
            for cell in row.find_all(["td", "th"], recursive=False)
            if isinstance(cell, Tag)
        ]
        for cell_index, cell in enumerate(direct_cells):
            while (row_index, column_index) in occupied:
                carried = dict(occupied[(row_index, column_index)])
                carried["is_origin"] = False
                grid_row.append(carried)
                column_index += 1
            rowspan = positive_span(cell.get("rowspan"))
            colspan = positive_span(cell.get("colspan"))
            raw_cell_id = f"{raw_table_id}:cell#{row_index}:{column_index}:{cell_index}"
            text = table_cell_text(cell)
            cells.append(
                RawTableCellRecord(
                    raw_cell_id=raw_cell_id,
                    raw_table_id=raw_table_id,
                    physical_row=row_index,
                    physical_column=column_index,
                    rowspan=rowspan,
                    colspan=colspan,
                    text=text,
                    text_sha256=sha256_text(text),
                )
            )
            origin = {
                "raw_cell_id": raw_cell_id,
                "grid_row": row_index,
                "grid_column": column_index,
                "text": text,
                "rowspan": rowspan,
                "colspan": colspan,
                "is_origin": True,
            }
            grid_row.append(origin)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    if row_offset == 0 and column_offset == 0:
                        continue
                    occupied[(row_index + row_offset, column_index + column_offset)] = origin
            column_index += colspan
        while (row_index, column_index) in occupied:
            carried = dict(occupied[(row_index, column_index)])
            carried["is_origin"] = False
            grid_row.append(carried)
            column_index += 1
        grid.append(grid_row)
    for logical_row, grid_row_items in enumerate(grid):
        for logical_column, logical_placement in enumerate(grid_row_items):
            raw_cell_id = string_value(logical_placement.get("raw_cell_id"))
            text = string_value(logical_placement.get("text"))
            placements.append(
                RawTablePlacementRecord(
                    raw_placement_id=f"{raw_table_id}:placement#{logical_row}:{logical_column}",
                    raw_cell_id=raw_cell_id,
                    raw_table_id=raw_table_id,
                    logical_row=logical_row,
                    logical_column=logical_column,
                    text=text,
                    text_sha256=sha256_text(text),
                )
            )
    return cells, grid, placements


def block_type_for_tag(tag_name: str) -> str:
    if tag_name in HEADING_TAGS:
        return "heading"
    if tag_name == "table":
        return "table_placeholder"
    if tag_name == "img":
        return "image_placeholder"
    if tag_name == "li":
        return "list_item"
    if tag_name in {"ul", "ol"}:
        return "list"
    if tag_name == "caption":
        return "caption"
    return "paragraph"


def classify_image_src(src: str, *, src_present: bool) -> str:
    if not src_present:
        return "missing"
    if not src:
        return "empty"
    lowered = src.casefold()
    if lowered.startswith("data:") and ";base64," in lowered:
        return "base64"
    if lowered.startswith("http://"):
        return "http"
    if lowered.startswith("https://"):
        return "https"
    return "relative"


def issue(path: str, code: str, details: Any) -> dict[str, Any]:
    return {"path": path, "code": code, "details": details}
