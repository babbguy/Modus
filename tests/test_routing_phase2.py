"""
Tests for Phase 2 routing intercept — GenericClassifier, RoutingDecision,
make_routing_decision, validate_quick, prompt extraction, response text extraction,
and agent routing integration.
"""

import pytest
import threading
from unittest.mock import MagicMock
from dataclasses import dataclass

from modus.routing_interceptor import (
    RoutingDecision,
    GenericClassifier,
    extract_structural_features,
    make_routing_decision,
    _default_cheap_model,
    compute_fingerprint,
    update_routing_table,
    _routing_table,
    _routing_table_lock,
)
from modus.output_validator import OutputValidator


@pytest.fixture(autouse=True)
def clear_routing_table():
    """Clear routing table and reset GenericClassifier singleton before each test."""
    with _routing_table_lock:
        _routing_table.clear()
    # Reset singleton so tests don't leak state
    GenericClassifier._instance = None
    yield
    with _routing_table_lock:
        _routing_table.clear()
    GenericClassifier._instance = None


# ── extract_structural_features ──────────────────────────────────────────


class TestExtractStructuralFeatures:
    def test_basic_features(self):
        features = extract_structural_features(
            "You are a classifier.", "Classify this text."
        )
        assert features["system_prompt_length"] == len("You are a classifier.")
        assert features["user_prompt_length"] == len("Classify this text.")
        assert features["total_tokens"] > 0
        assert features["system_word_count"] == 4
        assert features["user_word_count"] == 3

    def test_json_instruction_detection(self):
        features = extract_structural_features(
            "Return JSON output only.", "Input data"
        )
        assert features["has_json_instruction"] == 1

    def test_code_instruction_detection(self):
        features = extract_structural_features(
            "Write a function to compute fibonacci.", "n=10"
        )
        assert features["has_code_instruction"] == 1

    def test_classification_detection(self):
        features = extract_structural_features(
            "Classify the following support ticket.", "My order is late"
        )
        assert features["has_classification"] == 1

    def test_extraction_detection(self):
        features = extract_structural_features(
            "Extract all names from the text.", "John and Jane went to the park."
        )
        assert features["has_extraction"] == 1

    def test_no_special_flags(self):
        features = extract_structural_features(
            "You are a helpful assistant.", "Tell me a joke."
        )
        assert features["has_json_instruction"] == 0
        assert features["has_code_instruction"] == 0
        assert features["has_classification"] == 0
        assert features["has_extraction"] == 0

    def test_prompt_ratio(self):
        features = extract_structural_features("Short.", "A much longer user prompt here.")
        assert features["prompt_ratio"] > 1.0

    def test_empty_prompts(self):
        features = extract_structural_features("", "")
        assert features["system_prompt_length"] == 0
        assert features["user_prompt_length"] == 0
        assert features["total_tokens"] == 1  # max(1, 0)
        assert features["prompt_ratio"] == 0.0  # 0 / max(0, 1)


# ── _default_cheap_model ─────────────────────────────────────────────────


class TestDefaultCheapModel:
    def test_known_providers(self):
        assert _default_cheap_model("anthropic") == "claude-haiku-4-5-20251001"
        assert _default_cheap_model("openai") == "gpt-4o-mini"
        assert _default_cheap_model("google") == "gemini-2.0-flash"
        assert _default_cheap_model("groq") == "llama-3.1-8b-instant"
        assert _default_cheap_model("mistral") == "mistral-small-latest"
        assert _default_cheap_model("cohere") == "command-r"

    def test_unknown_provider(self):
        assert _default_cheap_model("bedrock") is None
        assert _default_cheap_model("unknown") is None


# ── GenericClassifier ─────────────────────────────────────────────────────


class TestGenericClassifier:
    def test_singleton(self):
        c1 = GenericClassifier.get_instance()
        c2 = GenericClassifier.get_instance()
        assert c1 is c2

    def test_no_model_returns_zero(self):
        classifier = GenericClassifier.get_instance()
        features = extract_structural_features("System prompt.", "User prompt.")
        score = classifier.predict(features)
        assert score == 0.0  # No pkl file loaded

    def test_predict_with_mock_model(self):
        """When a model is loaded, predict delegates to it."""
        classifier = GenericClassifier.get_instance()
        mock_model = MagicMock()
        mock_model.predict.return_value = 0.85
        classifier._model = mock_model

        features = extract_structural_features("Classify.", "Input text.")
        score = classifier.predict(features)
        assert score == 0.85
        mock_model.predict.assert_called_once_with(features)

    def test_predict_clamps_to_bounds(self):
        classifier = GenericClassifier.get_instance()
        mock_model = MagicMock()
        classifier._model = mock_model

        mock_model.predict.return_value = 1.5
        assert classifier.predict({}) == 1.0

        mock_model.predict.return_value = -0.3
        assert classifier.predict({}) == 0.0

    def test_predict_handles_exception(self):
        classifier = GenericClassifier.get_instance()
        mock_model = MagicMock()
        mock_model.predict.side_effect = RuntimeError("model error")
        classifier._model = mock_model

        assert classifier.predict({}) == 0.0


# ── RoutingDecision ──────────────────────────────────────────────────────


class TestRoutingDecision:
    def test_fields(self):
        d = RoutingDecision(
            should_route=True,
            confidence=0.95,
            cheap_model="gpt-4o-mini",
            source="table",
            reason="calibrated_routing",
        )
        assert d.should_route is True
        assert d.confidence == 0.95
        assert d.cheap_model == "gpt-4o-mini"
        assert d.source == "table"
        assert d.reason == "calibrated_routing"


# ── make_routing_decision ────────────────────────────────────────────────


class TestMakeRoutingDecision:
    def _make_fp(self, **overrides):
        """Helper to insert a routing table entry."""
        entry = {
            "fingerprint_hash": "test-fp",
            "phase": "routing",
            "routing_confidence": 0.95,
            "conformal_threshold": 0.5,
            "cheap_model": "claude-haiku-4-5-20251001",
            "expensive_model": "claude-opus-4-6",
            "allow_routing": True,
        }
        entry.update(overrides)
        update_routing_table([entry])
        return entry["fingerprint_hash"]

    def test_routing_phase_high_confidence(self):
        fp = self._make_fp()
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        assert decision.should_route is True
        assert decision.source == "table"
        assert decision.reason == "calibrated_routing"
        assert decision.cheap_model == "claude-haiku-4-5-20251001"

    def test_routing_phase_low_confidence(self):
        fp = self._make_fp(routing_confidence=0.3, conformal_threshold=0.5)
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        assert decision.should_route is False
        assert decision.reason == "confidence_below_threshold"

    def test_observe_phase(self):
        fp = self._make_fp(phase="observe")
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        assert decision.should_route is False
        assert decision.reason == "phase_observe"

    def test_drift_flagged_phase(self):
        fp = self._make_fp(phase="drift_flagged")
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        assert decision.should_route is False
        assert decision.reason == "phase_drift_flagged"

    def test_force_model_override(self):
        fp = self._make_fp(force_model="claude-sonnet-4-6")
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        assert decision.should_route is True
        assert decision.cheap_model == "claude-sonnet-4-6"
        assert decision.reason == "force_model_override"
        assert decision.confidence == 1.0

    def test_no_entry_generic_classifier_below_threshold(self):
        """No routing table entry + generic classifier returns 0.0 → no route."""
        decision = make_routing_decision(
            fingerprint_hash="unknown-fp",
            system_prompt="Hello.",
            user_prompt="World.",
            provider="openai",
            model="gpt-4o",
        )
        assert decision.should_route is False
        assert decision.source == "none"
        assert decision.reason == "below_generic_threshold"

    def test_no_entry_generic_classifier_above_threshold(self):
        """Generic classifier above threshold → provisional route."""
        # Inject a mock model that returns high confidence
        classifier = GenericClassifier.get_instance()
        mock_model = MagicMock()
        mock_model.predict.return_value = 0.85
        classifier._model = mock_model

        decision = make_routing_decision(
            fingerprint_hash="new-fp",
            system_prompt="Classify this ticket.",
            user_prompt="My order is late",
            provider="openai",
            model="gpt-4o",
        )
        assert decision.should_route is True
        assert decision.source == "generic"
        assert decision.reason == "generic_classifier_provisional"
        assert decision.cheap_model == "gpt-4o-mini"
        assert decision.confidence == 0.85

    def test_routing_table_takes_precedence_over_generic(self):
        """When table entry exists, generic classifier is not consulted."""
        fp = self._make_fp(phase="observe")

        # Even with a high-confidence generic classifier
        classifier = GenericClassifier.get_instance()
        mock_model = MagicMock()
        mock_model.predict.return_value = 0.99
        classifier._model = mock_model

        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="anthropic",
            model="claude-opus-4-6",
        )
        # Table says observe → no route (generic not consulted)
        assert decision.should_route is False
        assert decision.source == "table"
        mock_model.predict.assert_not_called()

    def test_default_cheap_model_for_provider(self):
        """When table entry has no cheap_model, uses provider default."""
        fp = self._make_fp(cheap_model=None)
        decision = make_routing_decision(
            fingerprint_hash=fp,
            system_prompt="Classify.",
            user_prompt="Input.",
            provider="openai",
            model="gpt-4o",
        )
        assert decision.should_route is True
        assert decision.cheap_model == "gpt-4o-mini"


# ── OutputValidator.validate_quick ───────────────────────────────────────


class TestValidateQuick:
    def test_normal_text_passes(self):
        passed, reason = OutputValidator.validate_quick(
            "The capital of France is Paris. It has a rich history."
        )
        assert passed is True
        assert reason is None

    def test_refusal_detected(self):
        passed, reason = OutputValidator.validate_quick(
            "I'm unable to help with that request."
        )
        assert passed is False
        assert reason == "refusal_detected"

    def test_empty_response(self):
        passed, reason = OutputValidator.validate_quick("")
        assert passed is False
        assert reason == "empty_response"

    def test_short_response(self):
        passed, reason = OutputValidator.validate_quick("OK")
        assert passed is False
        assert reason == "empty_response"

    def test_truncation_detected(self):
        passed, reason = OutputValidator.validate_quick(
            "The answer involves several complex factors including economics, "
            "social dynamics, and more importantly..."
        )
        assert passed is False
        assert reason == "truncation_detected"

    def test_invalid_json(self):
        passed, reason = OutputValidator.validate_quick('{"name": "Alice", "age": }')
        assert passed is False
        assert reason == "invalid_json"

    def test_valid_json_passes(self):
        passed, reason = OutputValidator.validate_quick('{"name": "Alice", "age": 30}')
        assert passed is True
        assert reason is None

    def test_dict_response(self):
        resp = {"content": [{"type": "text", "text": "This is a valid response with enough text."}]}
        passed, reason = OutputValidator.validate_quick(resp)
        assert passed is True
        assert reason is None


# ── ModusAgent._extract_prompts_for_routing ───────────────────────────


class TestExtractPromptsForRouting:
    @staticmethod
    def _extract(provider, kwargs):
        from modus.agent import ModusAgent
        return ModusAgent._extract_prompts_for_routing(provider, kwargs)

    def test_anthropic(self):
        system, user = self._extract("anthropic", {
            "system": "You are a classifier.",
            "messages": [
                {"role": "user", "content": "Classify: hello world"},
            ],
        })
        assert system == "You are a classifier."
        assert user == "Classify: hello world"

    def test_anthropic_list_system(self):
        system, user = self._extract("anthropic", {
            "system": [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}],
            "messages": [{"role": "user", "content": "Input"}],
        })
        assert "Part 1" in system
        assert "Part 2" in system

    def test_anthropic_list_content(self):
        system, user = self._extract("anthropic", {
            "system": "System prompt.",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Block 1"},
                    {"type": "text", "text": "Block 2"},
                ]},
            ],
        })
        assert "Block 1" in user
        assert "Block 2" in user

    def test_openai(self):
        system, user = self._extract("openai", {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "What is 2+2?"},
            ],
        })
        assert system == "You are helpful."
        assert user == "What is 2+2?"

    def test_groq(self):
        system, user = self._extract("groq", {
            "messages": [
                {"role": "system", "content": "Summarize."},
                {"role": "user", "content": "Long text here."},
            ],
        })
        assert system == "Summarize."
        assert user == "Long text here."

    def test_mistral(self):
        system, user = self._extract("mistral", {
            "messages": [
                {"role": "system", "content": "Translate."},
                {"role": "user", "content": "Bonjour."},
            ],
        })
        assert system == "Translate."
        assert user == "Bonjour."

    def test_google(self):
        system, user = self._extract("google", {
            "config": {"system_instruction": "Be brief."},
            "contents": "Explain quantum computing.",
        })
        assert system == "Be brief."
        assert user == "Explain quantum computing."

    def test_cohere(self):
        system, user = self._extract("cohere", {
            "preamble": "You are an expert.",
            "message": "Explain gravity.",
        })
        assert system == "You are an expert."
        assert user == "Explain gravity."

    def test_empty_kwargs(self):
        system, user = self._extract("anthropic", {})
        assert system == ""
        assert user == ""

    def test_unknown_provider(self):
        system, user = self._extract("bedrock", {"messages": []})
        assert system == ""
        assert user == ""


# ── ModusAgent._extract_response_text ─────────────────────────────────


class TestExtractResponseText:
    @staticmethod
    def _extract(provider, resp):
        from modus.agent import ModusAgent
        return ModusAgent._extract_response_text(provider, resp)

    def test_anthropic_response(self):
        @dataclass
        class ContentBlock:
            text: str

        @dataclass
        class Response:
            content: list

        resp = Response(content=[ContentBlock(text="Hello world.")])
        assert self._extract("anthropic", resp) == "Hello world."

    def test_openai_response(self):
        @dataclass
        class Message:
            content: str

        @dataclass
        class Choice:
            message: Message

        @dataclass
        class Response:
            choices: list

        resp = Response(choices=[Choice(message=Message(content="Answer is 42."))])
        assert self._extract("openai", resp) == "Answer is 42."

    def test_groq_response(self):
        @dataclass
        class Message:
            content: str

        @dataclass
        class Choice:
            message: Message

        @dataclass
        class Response:
            choices: list

        resp = Response(choices=[Choice(message=Message(content="Groq result."))])
        assert self._extract("groq", resp) == "Groq result."

    def test_google_response(self):
        @dataclass
        class Response:
            text: str

        resp = Response(text="Gemini answer.")
        assert self._extract("google", resp) == "Gemini answer."

    def test_cohere_response(self):
        @dataclass
        class Response:
            text: str

        resp = Response(text="Cohere answer.")
        assert self._extract("cohere", resp) == "Cohere answer."

    def test_none_response(self):
        assert self._extract("anthropic", None) == ""

    def test_unknown_provider(self):
        result = self._extract("unknown", "raw text")
        assert result == "raw text"


# ── _try_route_sync integration ──────────────────────────────────────────


class TestTryRouteSync:
    def _make_agent(self):
        """Create a minimal agent-like object for testing."""
        from modus.agent import ModusAgent

        # We can't fully construct ModusAgent without env vars,
        # so we mock the minimal state
        agent = object.__new__(ModusAgent)
        agent._routing_enabled = True
        agent._app_id = "test-app"
        agent._routing_generic_threshold = 0.70
        agent._routing_outcomes = []
        agent._routing_outcomes_lock = threading.Lock()
        agent._max_buffer_size = 10000
        return agent

    def test_routing_disabled_returns_false(self):
        agent = self._make_agent()
        agent._routing_enabled = False
        routed, resp = agent._try_route_sync("anthropic", "opus", {}, None, None)
        assert routed is False

    def test_no_app_id_returns_false(self):
        agent = self._make_agent()
        agent._app_id = None
        routed, resp = agent._try_route_sync("anthropic", "opus", {}, None, None)
        assert routed is False

    def test_no_prompts_returns_false(self):
        agent = self._make_agent()
        routed, resp = agent._try_route_sync("anthropic", "opus", {}, None, None)
        assert routed is False

    def test_routes_when_table_entry_exists(self):
        """With a routing table entry, routes to cheap model."""
        agent = self._make_agent()

        kwargs = {
            "system": "You are a classifier.",
            "messages": [{"role": "user", "content": "Classify: hello world test input"}],
            "model": "claude-opus-4-6",
            "max_tokens": 100,
        }
        fp = compute_fingerprint("test-app", "You are a classifier.")
        update_routing_table([{
            "fingerprint_hash": fp,
            "phase": "routing",
            "routing_confidence": 0.95,
            "conformal_threshold": 0.5,
            "cheap_model": "claude-haiku-4-5-20251001",
            "expensive_model": "claude-opus-4-6",
            "allow_routing": True,
        }])

        # Mock orig_fn to return a valid-looking response
        @dataclass
        class ContentBlock:
            text: str

        @dataclass
        class MockResp:
            content: list
            model: str = "claude-haiku-4-5-20251001"

        def orig_fn(self_sdk, **kw):
            return MockResp(content=[ContentBlock(
                text="This is a valid classification result from haiku model."
            )])

        routed, resp = agent._try_route_sync("anthropic", "claude-opus-4-6", kwargs, orig_fn, None)
        assert routed is True
        assert resp.model == "claude-haiku-4-5-20251001"

    def test_escalates_on_refusal(self):
        """Cheap model refusal → escalation → returns False."""
        agent = self._make_agent()

        kwargs = {
            "system": "You are a classifier.",
            "messages": [{"role": "user", "content": "Classify: hello world test input"}],
            "model": "claude-opus-4-6",
        }
        fp = compute_fingerprint("test-app", "You are a classifier.")
        update_routing_table([{
            "fingerprint_hash": fp,
            "phase": "routing",
            "routing_confidence": 0.95,
            "conformal_threshold": 0.5,
            "cheap_model": "claude-haiku-4-5-20251001",
            "allow_routing": True,
        }])

        @dataclass
        class ContentBlock:
            text: str

        @dataclass
        class MockResp:
            content: list

        def orig_fn(self_sdk, **kw):
            return MockResp(content=[ContentBlock(
                text="I'm unable to help with that request."
            )])

        routed, resp = agent._try_route_sync("anthropic", "claude-opus-4-6", kwargs, orig_fn, None)
        assert routed is False  # Escalation: caller should use original model

        # Check that escalation was logged
        assert len(agent._routing_outcomes) > 0
        last = agent._routing_outcomes[-1]
        assert last["routed_to"] == "escalated"
        assert last["escalation_reason"] == "refusal_detected"

    def test_fail_open_on_exception(self):
        """Any exception in routing returns (False, None) — fail-open."""
        agent = self._make_agent()

        kwargs = {
            "system": "System.",
            "messages": [{"role": "user", "content": "User prompt with enough content."}],
        }
        fp = compute_fingerprint("test-app", "System.")
        update_routing_table([{
            "fingerprint_hash": fp,
            "phase": "routing",
            "routing_confidence": 0.95,
            "conformal_threshold": 0.5,
            "cheap_model": "claude-haiku-4-5-20251001",
            "allow_routing": True,
        }])

        def orig_fn(self_sdk, **kw):
            raise ConnectionError("API unreachable")

        routed, resp = agent._try_route_sync("anthropic", "opus", kwargs, orig_fn, None)
        assert routed is False
        assert resp is None

    def test_no_route_for_observe_phase(self):
        agent = self._make_agent()

        kwargs = {
            "system": "Classify.",
            "messages": [{"role": "user", "content": "Input text for classification."}],
        }
        fp = compute_fingerprint("test-app", "Classify.")
        update_routing_table([{
            "fingerprint_hash": fp,
            "phase": "observe",
        }])

        routed, resp = agent._try_route_sync("anthropic", "opus", kwargs, None, None)
        assert routed is False


# ── Routing outcome buffering ────────────────────────────────────────────


class TestRoutingOutcomeBuffering:
    def _make_agent(self):
        from modus.agent import ModusAgent

        agent = object.__new__(ModusAgent)
        agent._routing_enabled = True
        agent._app_id = "test-app"
        agent._routing_generic_threshold = 0.70
        agent._routing_outcomes = []
        agent._routing_outcomes_lock = threading.Lock()
        agent._max_buffer_size = 5
        return agent

    def test_logs_outcome(self):
        agent = self._make_agent()
        agent._log_routing_outcome(
            fingerprint_hash="fp1",
            routed_to="cheap",
            provider="anthropic",
        )
        assert len(agent._routing_outcomes) == 1
        assert agent._routing_outcomes[0]["fingerprint_hash"] == "fp1"
        assert "timestamp" in agent._routing_outcomes[0]
        assert agent._routing_outcomes[0]["app_id"] == "test-app"

    def test_buffer_cap(self):
        agent = self._make_agent()
        for i in range(10):
            agent._log_routing_outcome(fingerprint_hash=f"fp{i}", routed_to="cheap")
        # Buffer should be capped at max_buffer_size=5
        assert len(agent._routing_outcomes) == 5
        # Oldest should have been dropped
        assert agent._routing_outcomes[0]["fingerprint_hash"] == "fp5"

    def test_thread_safety(self):
        agent = self._make_agent()
        agent._max_buffer_size = 10000

        def log_many(start):
            for i in range(100):
                agent._log_routing_outcome(
                    fingerprint_hash=f"fp{start}_{i}", routed_to="cheap"
                )

        threads = [threading.Thread(target=log_many, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(agent._routing_outcomes) == 500
