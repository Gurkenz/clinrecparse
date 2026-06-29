from __future__ import annotations

import hashlib
import re

from clinrec.bank.common import string_value

CANONICAL_SCHEMA_VERSION = "0.4-pilot"
CANONICAL_PARSER_VERSION = "parsed-canonical-0.5"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip()).strip("_")
    return cleaned or "section"


def extension_for_mime(mime_type: str | None) -> str:
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/svg+xml": "svg",
    }.get(string_value(mime_type).casefold(), "bin")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
