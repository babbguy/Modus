"""
Modus — Logging Configuration
======================================
Structured JSON logging for production. Human-readable text for development.

Every log record includes:
    timestamp   — ISO 8601 UTC
    level       — DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger      — module path (e.g. orchestrator.api.ingest)
    message     — log message
    ...extras   — any extra kwargs passed to logger.info(..., extra={...})

Log aggregators (Datadog, CloudWatch, GCP Logging, Splunk) parse JSON natively.
Structured fields make filtering and alerting straightforward.

Usage:
    logger = logging.getLogger(__name__)
    logger.info("Ingest received", extra={"batch_size": 42, "app_id": "..."})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON.
    Safe for log aggregators that parse line-delimited JSON.
    """

    RESERVED_ATTRS = frozenset({
        "args", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message",
        "module", "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "thread",
        "threadName",
    })

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        # Include exception info
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Include extra fields
        for key, value in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """
    Human-readable format for local development.
    Includes colour coding for log levels.
    """

    COLOURS = {
        "DEBUG":    "\033[0;36m",   # cyan
        "INFO":     "\033[0;32m",   # green
        "WARNING":  "\033[1;33m",   # yellow
        "ERROR":    "\033[0;31m",   # red
        "CRITICAL": "\033[1;31m",   # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        colour = self.COLOURS.get(record.levelname, "")
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%H:%M:%S"
        )
        prefix = f"{colour}{record.levelname:<8}{self.RESET}"
        line = f"{ts} {prefix} {record.name:<40} {record.message}"

        # Append extras
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k
            not in {
                "args", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message",
                "module", "msecs", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName",
            }
            and not k.startswith("_")
        }
        if extras:
            extras_str = " ".join(f"{k}={v!r}" for k, v in extras.items())
            line += f"  {extras_str}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """
    Configure root logger. Call once at application startup.

    Args:
        level:      Logging level. One of DEBUG / INFO / WARNING / ERROR.
        log_format: "json" for production, "text" for development.
    """
    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = TextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy third-party loggers
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "asyncpg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Keep uvicorn error logger at INFO
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
