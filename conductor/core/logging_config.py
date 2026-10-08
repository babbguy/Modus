"""
Modus Conductor — Logging Configuration
===============================================
Structured JSON logging for production, text logging for development.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal


def configure_logging(
    level: str = "INFO",
    log_format: Literal["json", "text"] = "json",
) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    if log_format == "json":
        import json
        from datetime import datetime, timezone

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                doc = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info and record.exc_info[1]:
                    doc["exception"] = self.formatException(record.exc_info)
                if hasattr(record, "extra") and isinstance(getattr(record, "extra", None), dict):
                    doc.update(record.extra)
                return json.dumps(doc, default=str)

        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Quiet noisy libraries
    for name in ("uvicorn.access", "sqlalchemy.engine", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
