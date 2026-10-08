"""
Tests — Logging Configuration
Covers: JsonFormatter, TextFormatter, configure_logging.
"""
from __future__ import annotations

import json
import logging


def test_json_formatter_basic():
    from orchestrator.core.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.module", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Hello %s", args=("world",), exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.module"
    assert parsed["message"] == "Hello world"
    assert "timestamp" in parsed


def test_json_formatter_with_exception():
    from orchestrator.core.logging_config import JsonFormatter
    formatter = JsonFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname="test.py",
        lineno=1, msg="Error occurred", args=(), exc_info=exc_info,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert "exception" in parsed
    assert "ValueError" in parsed["exception"]


def test_json_formatter_with_extras():
    from orchestrator.core.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py",
        lineno=1, msg="With extras", args=(), exc_info=None,
    )
    record.batch_size = 42
    record.app_id = "my-app"
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["batch_size"] == 42
    assert parsed["app_id"] == "my-app"


def test_text_formatter_basic():
    from orchestrator.core.logging_config import TextFormatter
    formatter = TextFormatter()
    record = logging.LogRecord(
        name="test.module", level=logging.INFO, pathname="test.py",
        lineno=1, msg="Hello text", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "INFO" in output
    assert "test.module" in output
    assert "Hello text" in output


def test_text_formatter_with_extras():
    from orchestrator.core.logging_config import TextFormatter
    formatter = TextFormatter()
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname="test.py",
        lineno=1, msg="Warning msg", args=(), exc_info=None,
    )
    record.request_id = "req-123"
    output = formatter.format(record)
    assert "request_id" in output
    assert "req-123" in output


def test_text_formatter_with_exception():
    from orchestrator.core.logging_config import TextFormatter
    formatter = TextFormatter()
    try:
        raise RuntimeError("test")
    except RuntimeError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname="test.py",
        lineno=1, msg="Error", args=(), exc_info=exc_info,
    )
    output = formatter.format(record)
    assert "RuntimeError" in output


def test_text_formatter_debug_level():
    from orchestrator.core.logging_config import TextFormatter
    formatter = TextFormatter()
    record = logging.LogRecord(
        name="test", level=logging.DEBUG, pathname="test.py",
        lineno=1, msg="debug msg", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "DEBUG" in output


def test_text_formatter_critical_level():
    from orchestrator.core.logging_config import TextFormatter
    formatter = TextFormatter()
    record = logging.LogRecord(
        name="test", level=logging.CRITICAL, pathname="test.py",
        lineno=1, msg="critical", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert "CRITICAL" in output


def test_configure_logging_json():
    from orchestrator.core.logging_config import configure_logging, JsonFormatter
    configure_logging(level="DEBUG", log_format="json")
    root = logging.getLogger()
    assert len(root.handlers) >= 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.DEBUG


def test_configure_logging_text():
    from orchestrator.core.logging_config import configure_logging, TextFormatter
    configure_logging(level="WARNING", log_format="text")
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, TextFormatter)
    assert root.level == logging.WARNING


def test_configure_logging_quiets_noisy_loggers():
    from orchestrator.core.logging_config import configure_logging
    configure_logging(level="DEBUG", log_format="json")
    assert logging.getLogger("uvicorn.access").level >= logging.WARNING
    assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING


def test_json_formatter_reserved_attrs():
    from orchestrator.core.logging_config import JsonFormatter
    assert "args" in JsonFormatter.RESERVED_ATTRS
    assert "levelname" in JsonFormatter.RESERVED_ATTRS
    assert "msg" in JsonFormatter.RESERVED_ATTRS
