"""
Tests — Permission Resolver (RBAC)
Covers: wildcard expansion, ResolvedPermissions, cache, user lookup helpers.
"""
from __future__ import annotations

# ── Wildcard expansion ───────────────────────────────────────────────────────

def test_expand_wildcards_star():
    from orchestrator.core.permissions import _expand_wildcards, ALL_PERMISSIONS
    result = _expand_wildcards(["*"])
    assert result == ALL_PERMISSIONS


def test_expand_wildcards_resource_star():
    from orchestrator.core.permissions import _expand_wildcards
    result = _expand_wildcards(["apps:*"])
    assert "apps:read" in result
    assert "apps:write" in result
    assert "apps:delete" in result
    assert "apps:keys" in result
    assert "users:read" not in result


def test_expand_wildcards_specific():
    from orchestrator.core.permissions import _expand_wildcards
    result = _expand_wildcards(["apps:read", "billing:write"])
    assert result == {"apps:read", "billing:write"}


def test_expand_wildcards_empty():
    from orchestrator.core.permissions import _expand_wildcards
    result = _expand_wildcards([])
    assert result == set()


def test_expand_wildcards_mixed():
    from orchestrator.core.permissions import _expand_wildcards
    result = _expand_wildcards(["users:*", "apps:read"])
    assert "users:read" in result
    assert "users:write" in result
    assert "apps:read" in result
    assert "apps:write" not in result


# ── ResolvedPermissions ──────────────────────────────────────────────────────

def test_resolved_permissions_has():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset({"apps:read", "apps:write"}),
        team_ids=frozenset({"t1"}),
        is_platform_admin=False,
    )
    assert rp.has("apps:read") is True
    assert rp.has("billing:read") is False


def test_resolved_permissions_platform_admin():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset(),
        team_ids=frozenset(),
        is_platform_admin=True,
    )
    assert rp.has("apps:read") is True
    assert rp.has("anything:else") is True


def test_resolved_permissions_platform_admin_denied():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset(),
        team_ids=frozenset(),
        is_platform_admin=True,
        _denied=frozenset({"secrets:read"}),
    )
    assert rp.has("secrets:read") is False
    assert rp.has("apps:read") is True


def test_resolved_permissions_has_any():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset({"apps:read"}),
        team_ids=frozenset(),
        is_platform_admin=False,
    )
    assert rp.has_any("apps:read", "apps:write") is True
    assert rp.has_any("billing:read", "billing:write") is False


def test_resolved_permissions_has_all():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset({"apps:read", "apps:write"}),
        team_ids=frozenset(),
        is_platform_admin=False,
    )
    assert rp.has_all("apps:read", "apps:write") is True
    assert rp.has_all("apps:read", "billing:read") is False


def test_resolved_permissions_can_access_team():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset(),
        team_ids=frozenset({"t1", "t2"}),
        is_platform_admin=False,
    )
    assert rp.can_access_team("t1") is True
    assert rp.can_access_team("t3") is False


def test_resolved_permissions_platform_admin_all_teams():
    from orchestrator.core.permissions import ResolvedPermissions
    rp = ResolvedPermissions(
        permissions=frozenset(),
        team_ids=frozenset(),
        is_platform_admin=True,
    )
    assert rp.can_access_team("any-team") is True


# ── Cache ────────────────────────────────────────────────────────────────────

def test_clear_permissions_cache_all():
    from orchestrator.core.permissions import _cache, clear_permissions_cache, ResolvedPermissions
    import time
    rp = ResolvedPermissions(
        permissions=frozenset(), team_ids=frozenset(), is_platform_admin=False,
    )
    _cache["user1"] = (rp, time.monotonic() + 9999)
    _cache["user2"] = (rp, time.monotonic() + 9999)
    clear_permissions_cache()
    assert len(_cache) == 0


def test_clear_permissions_cache_single():
    from orchestrator.core.permissions import _cache, clear_permissions_cache, ResolvedPermissions
    import time
    rp = ResolvedPermissions(
        permissions=frozenset(), team_ids=frozenset(), is_platform_admin=False,
    )
    _cache["user1"] = (rp, time.monotonic() + 9999)
    _cache["user2"] = (rp, time.monotonic() + 9999)
    clear_permissions_cache("user1")
    assert "user1" not in _cache
    assert "user2" in _cache
    # Cleanup
    _cache.clear()


def test_clear_permissions_cache_nonexistent():
    from orchestrator.core.permissions import clear_permissions_cache
    # Should not raise
    clear_permissions_cache("nonexistent-user")


# ── ALL_PERMISSIONS ──────────────────────────────────────────────────────────

def test_all_permissions_not_empty():
    from orchestrator.core.permissions import ALL_PERMISSIONS
    assert len(ALL_PERMISSIONS) > 20
    assert "platform:manage" in ALL_PERMISSIONS
    assert "apps:read" in ALL_PERMISSIONS
