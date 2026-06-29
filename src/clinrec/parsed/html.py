from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

UNSAFE_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "meta",
    "link",
}
SAFE_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "caption",
    "col",
    "colgroup",
    "dd",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "section",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
SAFE_URL_SCHEMES = {"", "http", "https", "mailto"}
SAFE_ATTRS_BY_TAG = {
    "*": {"title", "aria-label"},
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"rowspan", "colspan"},
    "th": {"rowspan", "colspan", "scope"},
    "ol": {"start", "type"},
    "ul": {"type"},
}
GENERATED_SAFE_ATTRS = {
    "data-section-id",
    "data-table-id",
    "data-image-id",
    "data-asset-id",
    "data-image-status",
}


def sanitize_html_tree(root: Tag | BeautifulSoup, warnings: list[str]) -> None:
    for tag in list(root.find_all(True)):
        if not isinstance(tag, Tag):
            continue
        name = tag.name.lower()
        if name in UNSAFE_TAGS:
            tag.decompose()
            warnings.append("unsafe_tag_removed")
            continue
        if name not in SAFE_TAGS:
            tag.unwrap()
            warnings.append("unknown_html_tag_removed")
            continue
        allowed_attrs = SAFE_ATTRS_BY_TAG.get("*", set()) | SAFE_ATTRS_BY_TAG.get(name, set())
        for attr in list(tag.attrs):
            attr_name = attr.casefold()
            if attr_name.startswith("on") or attr_name == "style":
                del tag.attrs[attr]
                warnings.append("unsafe_attribute_removed")
                continue
            if attr_name not in allowed_attrs:
                del tag.attrs[attr]
                warnings.append("unknown_attribute_removed")
                continue
            if attr_name in {"href", "src"} and not is_safe_url(tag.get(attr), tag_name=name):
                del tag.attrs[attr]
                warnings.append("unsafe_url_removed")


def add_section_attributes(root: Tag | BeautifulSoup, *, section_id: str) -> None:
    for tag in root.find_all(True):
        if isinstance(tag, Tag):
            tag["data-section-id"] = section_id


def meaningful_children(root: Tag | BeautifulSoup) -> list[PageElement]:
    children: list[PageElement] = []
    for child in root.children:
        if isinstance(child, NavigableString):
            if normalize_text(str(child)):
                children.append(child)
        elif isinstance(child, Tag):
            if child.name.lower() in {"html", "body"}:
                children.extend(meaningful_children(child))
            elif (
                child.get_text(strip=True)
                or child.name.lower() in {"img", "table"}
                or child.find(["img", "table"]) is not None
            ):
                children.append(child)
    return children


def fragment_html(root: Tag | BeautifulSoup) -> str:
    return "".join(str(child) for child in meaningful_children(root))


def inner_html(tag: Tag) -> str:
    return "".join(str(child) for child in tag.contents)


def table_cell_text(cell: Tag) -> str:
    cell_soup = BeautifulSoup(str(cell), "lxml")
    clone = cell_soup.find(cell.name)
    if not isinstance(clone, Tag):
        return normalize_text(cell.get_text(" ", strip=True))
    for nested_table in clone.find_all("table"):
        if isinstance(nested_table, Tag):
            nested_table.decompose()
    return normalize_text(clone.get_text(" ", strip=True))


def nearest_table(tag: Tag) -> Tag | None:
    parent = tag.find_parent("table")
    return parent if isinstance(parent, Tag) else None


def is_javascript_url(value: Any) -> bool:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    return text.strip().casefold().startswith("javascript:")


def is_safe_url(value: Any, *, tag_name: str) -> bool:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    stripped = text.strip()
    if not stripped:
        return True
    if is_javascript_url(stripped):
        return False
    if tag_name == "img" and stripped.casefold().startswith("data:image/"):
        return ";base64," in stripped.casefold()
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return False
    if parsed.scheme.casefold() == "file":
        return False
    if parsed.scheme.casefold() == "data":
        return False
    return parsed.scheme.casefold() in SAFE_URL_SCHEMES


def positive_span(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def visible_text(html_text: str) -> str:
    return BeautifulSoup(html_text, "lxml").get_text(" ", strip=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
