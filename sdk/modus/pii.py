"""
Modus — PII Scrubbing Utility (INACTIVE)
============================================
This module is inactive and retained for future architectural review only.
Modus does not process PII — it handles only telemetry and metrics data.
PII scanning paths in the agent are force-disabled regardless of env var settings.
"""

from __future__ import annotations

import re
from typing import Any

# Standard regexes for common PII
PII_PATTERNS = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"),
    "ssn": re.compile(r"\d{3}-\d{2}-\d{4}"),
    "credit_card": re.compile(r"\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"),
    "api_key": re.compile(r"(?:sk-|pk-|mds_|Bearer\s+)[A-Za-z0-9\-_]{20,}"),
    "ipv4": re.compile(r"(?<![v=\d.])\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b(?!\.\d)"),
    "connection_string": re.compile(r"(?:postgres|mysql|redis|mongodb)://[^\s]+"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}

def scrub_pii(text: str) -> str:
    """Mask common PII patterns in the given text."""
    if not isinstance(text, str):
        return text
    for pii_type, pattern in PII_PATTERNS.items():
        text = pattern.sub(f"<{pii_type.upper()}>", text)
    return text


def scan_pii(text: str) -> list[dict]:
    """
    Scan text for PII patterns. Returns list of findings.

    Each finding is {"type": <pattern_name>, "count": <int>}.
    Never includes the matched content — only pattern names and counts.
    """
    if not isinstance(text, str):
        return []
    findings = []
    for pii_type, pattern in PII_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.append({"type": pii_type, "count": len(matches)})
    return findings


def scan_and_redact_pii(text: str) -> tuple[str, list[dict]]:
    """
    Scan text for PII and redact in a single pass.

    Returns (redacted_text, findings) where findings is a list of
    {"type": <pattern_name>, "count": <int>}.
    More efficient than calling scan_pii + scrub_pii separately.
    """
    if not isinstance(text, str):
        return text, []
    findings = []
    for pii_type, pattern in PII_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.append({"type": pii_type, "count": len(matches)})
            text = pattern.sub(f"<{pii_type.upper()}>", text)
    return text, findings


def scrub_pii_recursive(data: Any) -> Any:
    """Recursively scrub PII from strings within nested data structures."""
    if isinstance(data, str):
        return scrub_pii(data)
    elif isinstance(data, list):
        return [scrub_pii_recursive(i) for i in data]
    elif isinstance(data, tuple):
        return tuple(scrub_pii_recursive(i) for i in data)
    elif isinstance(data, dict):
        return {k: scrub_pii_recursive(v) for k, v in data.items()}
    return data
