"""Tests for output validator — refusal detection, truncation, JSON validation."""

from modus.output_validator import OutputValidator


class TestRefusalDetection:
    def test_detects_unable_to(self):
        result = OutputValidator.validate(
            "I'm unable to help with that request.", None, 100
        )
        assert not result.passed
        assert result.failure_reason == "refusal_detected"

    def test_detects_cannot_help(self):
        result = OutputValidator.validate(
            "I cannot and will not assist with this.", None, 100
        )
        assert not result.passed
        assert result.failure_reason == "refusal_detected"

    def test_detects_sorry_cant(self):
        result = OutputValidator.validate(
            "Sorry, I can't do that for you.", None, 100
        )
        assert not result.passed
        assert result.failure_reason == "refusal_detected"

    def test_normal_text_passes(self):
        result = OutputValidator.validate(
            "The capital of France is Paris. It has a population of about 2.1 million.",
            None, 100,
        )
        assert result.passed
        assert result.structural_passed

    def test_short_text_with_sorry_in_context_passes(self):
        """Text containing 'sorry' in a non-refusal context."""
        result = OutputValidator.validate(
            "We are sorry for the inconvenience. Your order will arrive tomorrow at 3 PM.",
            None, 100,
        )
        assert result.passed


class TestEmptyResponse:
    def test_empty_string(self):
        result = OutputValidator.validate("", None, 100)
        assert not result.passed
        assert result.failure_reason == "empty_response"

    def test_whitespace_only(self):
        result = OutputValidator.validate("   \n\t  ", None, 100)
        assert not result.passed
        assert result.failure_reason == "empty_response"

    def test_very_short(self):
        result = OutputValidator.validate("OK", None, 100)
        assert not result.passed
        assert result.failure_reason == "empty_response"


class TestTruncation:
    def test_trailing_ellipsis(self):
        result = OutputValidator.validate(
            "The answer involves several complex factors including economics, politics, "
            "social dynamics, and more importantly...",
            None, 100,
        )
        assert not result.passed
        assert result.failure_reason == "truncation_detected"

    def test_truncated_marker(self):
        result = OutputValidator.validate(
            "Here is the full analysis of the data [truncated]", None, 100
        )
        assert not result.passed
        assert result.failure_reason == "truncation_detected"

    def test_normal_period_passes(self):
        result = OutputValidator.validate(
            "The analysis is complete. All results are within normal parameters.",
            None, 100,
        )
        assert result.passed


class TestJsonValidation:
    def test_valid_json_passes(self):
        result = OutputValidator.validate(
            '{"name": "Alice", "age": 30}', None, 100
        )
        assert result.passed

    def test_invalid_json_fails(self):
        result = OutputValidator.validate(
            '{"name": "Alice", "age": }', None, 100
        )
        assert not result.passed
        assert result.failure_reason == "invalid_json"

    def test_valid_json_array_passes(self):
        result = OutputValidator.validate(
            '[1, 2, 3, "hello"]', None, 100
        )
        assert result.passed

    def test_non_json_text_passes(self):
        result = OutputValidator.validate(
            "This is not JSON, just regular text about data processing.",
            None, 100,
        )
        assert result.passed


class TestDictResponse:
    def test_anthropic_format(self):
        resp = {
            "content": [
                {"type": "text", "text": "The answer is 42. This is a well-known constant."}
            ]
        }
        result = OutputValidator.validate(resp, None, 100)
        assert result.passed

    def test_anthropic_empty_content(self):
        resp = {"content": []}
        result = OutputValidator.validate(resp, None, 100)
        assert not result.passed
        assert result.failure_reason == "empty_response"
