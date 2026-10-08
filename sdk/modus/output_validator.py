"""
modus.output_validator
==============================
Lightweight structural validator for cheap model outputs.
Runs in-process, no external calls, target execution time < 1ms.

Checks:
  1. Refusal detection — cheap models sometimes refuse where expensive models don't
  2. Empty/near-empty response
  3. Truncation signals
  4. Output length ratio against fingerprint baseline
  5. JSON schema completeness (if response is expected to be JSON)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from modus.routing_interceptor import RoutingEntry


@dataclass(frozen=True, slots=True)
class ValidationResult:
    passed: bool
    structural_passed: bool
    failure_reason: Optional[str]


class OutputValidator:
    """
    Lightweight structural validator for cheap model outputs.
    Runs in-process, no external calls, target execution time < 1ms.
    """

    # Refusal patterns — cheap models sometimes refuse where expensive models don't
    REFUSAL_PATTERNS = [
        r"i(?:'m| am) (?:unable|not able) to",
        r"i cannot (?:and will not|help with)",
        r"(?:sorry|apologize),? (?:but )?i (?:can't|cannot|won't)",
        r"(?:this|that) (?:request )?(?:is )?(?:beyond|outside) (?:my|the model's)",
        r"i (?:don't|do not) have (?:the ability|access|capability)",
    ]
    _refusal_re = re.compile("|".join(REFUSAL_PATTERNS), re.IGNORECASE)

    # Truncation signals
    TRUNCATION_PATTERNS = [
        r"\.\.\.$",
        r"\[(?:truncated|cut off|continued)\]",
        r"(?<!\w)etc\.$",
    ]
    _truncation_re = re.compile("|".join(TRUNCATION_PATTERNS), re.IGNORECASE)

    @classmethod
    def validate(
        cls,
        response: object,
        fingerprint: Optional[RoutingEntry],
        input_tokens: int,
    ) -> ValidationResult:
        """
        Runs structural checks against the response.
        fingerprint: RoutingEntry with baseline statistics.
        """
        text = cls._extract_text(response)

        # Check 1: Refusal detection
        if cls._refusal_re.search(text):
            return ValidationResult(
                passed=False,
                structural_passed=False,
                failure_reason="refusal_detected",
            )

        # Check 2: Empty or near-empty response
        if len(text.strip()) < 10:
            return ValidationResult(
                passed=False,
                structural_passed=False,
                failure_reason="empty_response",
            )

        # Check 3: Truncation signals
        if cls._truncation_re.search(text.strip()):
            return ValidationResult(
                passed=False,
                structural_passed=False,
                failure_reason="truncation_detected",
            )

        # Check 4: Output length ratio against fingerprint baseline
        output_tokens = max(1, len(text) // 4)
        if fingerprint and fingerprint.routing_confidence > 0:
            ratio = output_tokens / max(input_tokens, 1)
            expected_min = getattr(fingerprint, "output_ratio_p10", None)
            if expected_min and ratio < expected_min * 0.2:
                return ValidationResult(
                    passed=False,
                    structural_passed=True,
                    failure_reason="output_too_short",
                )

        # Check 5: JSON schema completeness (if response looks like JSON)
        if cls._looks_like_json_request(text):
            if not cls._valid_json(text):
                return ValidationResult(
                    passed=False,
                    structural_passed=False,
                    failure_reason="invalid_json",
                )

        return ValidationResult(
            passed=True, structural_passed=True, failure_reason=None
        )

    @classmethod
    def validate_quick(cls, response: object) -> tuple[bool, Optional[str]]:
        """
        Quick validation for routing intercept (Phase 2).
        Returns (passed, failure_reason) tuple.
        Runs the same structural checks as validate() but without fingerprint context.
        """
        text = cls._extract_text(response)

        if cls._refusal_re.search(text):
            return (False, "refusal_detected")
        if len(text.strip()) < 10:
            return (False, "empty_response")
        if cls._truncation_re.search(text.strip()):
            return (False, "truncation_detected")
        if cls._looks_like_json_request(text) and not cls._valid_json(text):
            return (False, "invalid_json")

        return (True, None)

    @staticmethod
    def _extract_text(response: object) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            # Anthropic response format
            content = response.get("content", [])
            if isinstance(content, list):
                return " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            return str(content)
        return str(response)

    @staticmethod
    def _looks_like_json_request(text: str) -> bool:
        stripped = text.strip()
        return stripped.startswith("{") or stripped.startswith("[")

    @staticmethod
    def _valid_json(text: str) -> bool:
        try:
            json.loads(text.strip())
            return True
        except (json.JSONDecodeError, ValueError):
            return False
