"""
Tests — SDK __init__.py
Covers: exports, decorators (force_model, allow_routing), __version__.
"""
from __future__ import annotations


def test_version():
    import modus
    assert modus.__version__ == "1.0.0"


def test_exports():
    import modus
    assert hasattr(modus, "ModusAgent")
    assert hasattr(modus, "PolicyViolationError")
    assert hasattr(modus, "get_agent")
    assert hasattr(modus, "force_model")
    assert hasattr(modus, "allow_routing")


def test_all_exports():
    import modus
    assert "ModusAgent" in modus.__all__
    assert "PolicyViolationError" in modus.__all__
    assert "get_agent" in modus.__all__
    assert "force_model" in modus.__all__
    assert "allow_routing" in modus.__all__


def test_force_model_decorator():
    import modus
    from modus.routing_interceptor import _routing_context

    @modus.force_model("claude-opus-4-6")
    def my_func():
        # Inside the function, force_model should be set
        return _routing_context.force_model

    # Before call
    assert _routing_context.force_model is None

    result = my_func()
    assert result == "claude-opus-4-6"

    # After call, should be reset
    assert _routing_context.force_model is None


def test_force_model_decorator_cleans_up_on_error():
    import modus
    from modus.routing_interceptor import _routing_context
    import pytest

    @modus.force_model("claude-opus-4-6")
    def bad_func():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        bad_func()

    # Should still be cleaned up
    assert _routing_context.force_model is None
    assert _routing_context.call_site_id is None


def test_allow_routing_decorator():
    import modus
    from modus.routing_interceptor import _routing_context

    @modus.allow_routing(max_misroute_rate=0.005)
    def my_func():
        return _routing_context.max_misroute_rate

    assert _routing_context.max_misroute_rate is None

    result = my_func()
    assert result == 0.005

    assert _routing_context.max_misroute_rate is None


def test_allow_routing_decorator_cleans_up_on_error():
    import modus
    from modus.routing_interceptor import _routing_context
    import pytest

    @modus.allow_routing(max_misroute_rate=0.01)
    def bad_func():
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        bad_func()

    assert _routing_context.max_misroute_rate is None
    assert _routing_context.call_site_id is None


def test_force_model_sets_call_site_id():
    import modus
    from modus.routing_interceptor import _routing_context

    @modus.force_model("gpt-4o")
    def named_func():
        return _routing_context.call_site_id

    result = named_func()
    assert "named_func" in result


def test_allow_routing_sets_call_site_id():
    import modus
    from modus.routing_interceptor import _routing_context

    @modus.allow_routing()
    def another_func():
        return _routing_context.call_site_id

    result = another_func()
    assert "another_func" in result


def test_policy_violation_error():
    from modus import PolicyViolationError
    assert issubclass(PolicyViolationError, Exception)
