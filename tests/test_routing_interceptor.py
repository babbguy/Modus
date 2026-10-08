"""Tests for routing interceptor — happy path, escalation, force_model override."""

import pytest
from modus.routing_interceptor import (
    RoutingInterceptor,
    update_routing_table,
    get_routing_entry,
    get_routing_table_snapshot,
    compute_fingerprint,
    _routing_table,
    _routing_table_lock,
)


@pytest.fixture(autouse=True)
def clear_routing_table():
    """Clear routing table before each test."""
    with _routing_table_lock:
        _routing_table.clear()
    yield
    with _routing_table_lock:
        _routing_table.clear()


def _make_call_fn(response_text="Test response", output_tokens=50):
    """Create a mock call_model_fn."""
    def call_fn(model, system_prompt, user_prompt, **kwargs):
        return {
            "response": response_text,
            "latency_ms": 100,
            "output_tokens": output_tokens,
        }
    return call_fn


class TestRoutingTable:
    def test_update_and_get(self):
        update_routing_table([{
            "fingerprint_hash": "abc123",
            "phase": "routing",
            "routing_confidence": 0.95,
            "cheap_model": "claude-haiku-4-5-20251001",
            "expensive_model": "claude-opus-4-6",
        }])
        entry = get_routing_entry("abc123")
        assert entry is not None
        assert entry.phase == "routing"
        assert entry.routing_confidence == 0.95

    def test_get_missing(self):
        assert get_routing_entry("nonexistent") is None

    def test_snapshot(self):
        update_routing_table([
            {"fingerprint_hash": "a", "phase": "observe"},
            {"fingerprint_hash": "b", "phase": "routing"},
        ])
        snapshot = get_routing_table_snapshot()
        assert len(snapshot) == 2


class TestInterceptPassthrough:
    def test_no_entry_passes_through(self):
        """No routing entry — call goes to original model."""
        logged = []
        interceptor = RoutingInterceptor(
            app_id="app-1",
            log_outcome_fn=lambda **kw: logged.append(kw),
            call_model_fn=_make_call_fn("expensive response"),
        )
        result = interceptor.intercept(
            system_prompt="You are a classifier",
            user_prompt="Classify: hello",
            model="claude-opus-4-6",
        )
        assert result == "expensive response"
        assert len(logged) == 1
        assert logged[0]["routed_to"] == "expensive"

    def test_observe_phase_passes_through(self):
        """Observe phase — passes through to original model."""
        update_routing_table([{
            "fingerprint_hash": compute_fingerprint("app-1", "You are a classifier"),
            "phase": "observe",
        }])

        logged = []
        interceptor = RoutingInterceptor(
            app_id="app-1",
            log_outcome_fn=lambda **kw: logged.append(kw),
            call_model_fn=_make_call_fn("observe response"),
        )
        result = interceptor.intercept(
            system_prompt="You are a classifier",
            user_prompt="Classify: hello",
            model="claude-opus-4-6",
        )
        assert result == "observe response"
        assert logged[0]["routed_to"] == "expensive"


class TestInterceptRouting:
    def _setup_routing_entry(self, fp_hash):
        update_routing_table([{
            "fingerprint_hash": fp_hash,
            "phase": "routing",
            "routing_confidence": 0.95,
            "conformal_threshold": 0.5,
            "cheap_model": "claude-haiku-4-5-20251001",
            "expensive_model": "claude-opus-4-6",
            "allow_routing": True,
            "input_token_bucket_bounds": [1, 5],
        }])

    def test_routes_to_cheap_when_valid(self):
        """Routing phase + valid output → cheap model response returned."""
        fp = compute_fingerprint("app-1", "You are a classifier")
        self._setup_routing_entry(fp)

        call_count = {"cheap": 0, "expensive": 0}

        def call_fn(model, system_prompt, user_prompt, **kwargs):
            if "haiku" in model:
                call_count["cheap"] += 1
                return {"response": "This is a valid classification result.", "output_tokens": 30}
            call_count["expensive"] += 1
            return {"response": "expensive result", "output_tokens": 50}

        logged = []
        interceptor = RoutingInterceptor(
            app_id="app-1",
            log_outcome_fn=lambda **kw: logged.append(kw),
            call_model_fn=call_fn,
        )
        result = interceptor.intercept(
            system_prompt="You are a classifier",
            user_prompt="Classify: hello world test input",
            model="claude-opus-4-6",
        )
        assert result == "This is a valid classification result."
        assert call_count["cheap"] == 1
        assert call_count["expensive"] == 0
        assert logged[0]["routed_to"] == "cheap"

    def test_escalates_on_refusal(self):
        """Cheap model refuses → escalates to expensive."""
        fp = compute_fingerprint("app-1", "You are a classifier")
        self._setup_routing_entry(fp)

        def call_fn(model, system_prompt, user_prompt, **kwargs):
            if "haiku" in model:
                return {"response": "I'm unable to help with that.", "output_tokens": 10}
            return {"response": "expensive result with proper classification.", "output_tokens": 50}

        logged = []
        interceptor = RoutingInterceptor(
            app_id="app-1",
            log_outcome_fn=lambda **kw: logged.append(kw),
            call_model_fn=call_fn,
        )
        result = interceptor.intercept(
            system_prompt="You are a classifier",
            user_prompt="Classify: hello world test input",
            model="claude-opus-4-6",
        )
        assert result == "expensive result with proper classification."
        assert logged[0]["routed_to"] == "escalated"
        assert logged[0]["escalation_reason"] == "refusal_detected"


class TestForceModel:
    def test_force_model_in_entry(self):
        """Force model in routing entry — bypasses routing logic."""
        fp = compute_fingerprint("app-1", "You are a classifier")
        update_routing_table([{
            "fingerprint_hash": fp,
            "phase": "routing",
            "force_model": "claude-sonnet-4-6",
        }])

        called_model = []

        def call_fn(model, system_prompt, user_prompt, **kwargs):
            called_model.append(model)
            return {"response": "forced response", "output_tokens": 30}

        interceptor = RoutingInterceptor(
            app_id="app-1",
            call_model_fn=call_fn,
        )
        result = interceptor.intercept(
            system_prompt="You are a classifier",
            user_prompt="Classify: hello",
            model="claude-opus-4-6",
        )
        assert result == "forced response"
        assert called_model[0] == "claude-sonnet-4-6"


class TestCostEstimation:
    def test_cost_delta(self):
        saved = RoutingInterceptor._estimate_cost_delta(
            input_tokens=1000,
            output_tokens=500,
            cheap_model="claude-haiku-4-5-20251001",
            expensive_model="claude-opus-4-6",
        )
        assert saved > 0

    def test_unknown_models_have_fallback(self):
        saved = RoutingInterceptor._estimate_cost_delta(
            input_tokens=1000,
            output_tokens=500,
            cheap_model="unknown-cheap",
            expensive_model="unknown-expensive",
        )
        # Both use fallback rates — should be 0 since both default to same rates
        assert saved >= 0.0

    def test_none_models_return_zero(self):
        assert RoutingInterceptor._estimate_cost_delta(1000, 500, None, "opus") == 0.0
        assert RoutingInterceptor._estimate_cost_delta(1000, 500, "haiku", None) == 0.0
