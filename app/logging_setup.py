"""Structured (JSON-lines) logging for lifecycle transitions."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAME = "superset_devin"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            reserved = set(payload)
            payload.update(
                {(f"ctx_{k}" if k in reserved else k): v for k, v in extra.items()}
            )
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return configure_logging()


def log_event(name: str, level: int = logging.INFO, **context: Any) -> None:
    """Emit one structured lifecycle event.

    The first parameter is ``name`` (not ``event``) so callers can pass an
    ``event=`` context field, e.g. the GitHub webhook event type.
    """
    get_logger().log(level, name, extra={"context": context})
