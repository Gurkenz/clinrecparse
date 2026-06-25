from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

from clinrec.config import LoggingSettings

SECRET_KEYS = frozenset({"authorization", "cookie", "set-cookie", "token", "password", "secret"})


def redact_secrets(
    _logger: logging.Logger,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in SECRET_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(settings: LoggingSettings) -> None:
    log_path = Path(settings.jsonl_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.level.upper())

    stream_handler = logging.StreamHandler(sys.stderr)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    for handler in (stream_handler, file_handler):
        handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(root_logger.level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
