"""Small, payload-free structured diagnostics for CLI and MCP operations."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import TextIO

LOGGER_NAME = "repobrain"
LOG_LEVEL_ENV = "REPOBRAIN_LOG_LEVEL"
_SAFE_FIELDS = {
    "auto_index",
    "can_query",
    "command",
    "edges_created",
    "files_changed",
    "files_deleted",
    "files_scanned",
    "incremental",
    "nodes_created",
    "status",
    "transport",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "repobrain_fields", {})
        payload.update({
            key: value for key, value in fields.items()
            if key in _SAFE_FIELDS and isinstance(value, (str, int, float, bool))
        })
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure RepoBrain-only JSON logging.

    With no explicit level, ``REPOBRAIN_LOG_LEVEL`` is consulted.  If neither
    is set, the logger is silent.  The handler always targets stderr unless a
    test or embedding supplies another stream.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_repobrain_handler", False):
            logger.removeHandler(handler)

    selected = level if level is not None else os.environ.get(LOG_LEVEL_ENV)
    if selected is None:
        logger.setLevel(logging.CRITICAL + 1)
        return logger
    if isinstance(selected, str):
        numeric = logging.getLevelName(selected.upper())
        if not isinstance(numeric, int):
            logger.setLevel(logging.CRITICAL + 1)
            return logger
    else:
        numeric = selected

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler._repobrain_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(numeric)
    return logger


def configure_logging_from_env() -> logging.Logger:
    """Apply an environment override without disabling an existing CLI choice."""
    selected = os.environ.get(LOG_LEVEL_ENV)
    if selected is None:
        return logging.getLogger(LOGGER_NAME)
    return configure_logging(selected)


def log_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    """Emit one bounded event; unknown fields are discarded by the formatter."""
    logging.getLogger(LOGGER_NAME).log(
        level,
        event,
        extra={"repobrain_fields": fields},
    )


# Library use remains silent unless an entry point explicitly opts in.
configure_logging()
