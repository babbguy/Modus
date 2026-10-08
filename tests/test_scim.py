"""
Tests — SCIM 2.0 Provisioning API
Covers: discovery endpoints, user CRUD, group CRUD, filters, error paths.
"""
from __future__ import annotations

# ── Discovery endpoints ─────────────────────────────────────────────────────

async def test_service_provider_config(client):
    resp = await client.get("/api/v1/scim/v2/ServiceProviderConfig")
    assert resp.status_code == 200
    body = resp.json()
    assert "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig" in body["schemas"]
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True


async def test_list_schemas(client):
    resp = await client.get("/api/v1/scim/v2/Schemas")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 2
    ids = [r["id"] for r in body["Resources"]]
    assert "urn:ietf:params:scim:schemas:core:2.0:User" in ids
    assert "urn:ietf:params:scim:schemas:core:2.0:Group" in ids


async def test_resource_types(client):
    resp = await client.get("/api/v1/scim/v2/ResourceTypes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 2
    names = [r["name"] for r in body["Resources"]]
    assert "User" in names
    assert "Group" in names


# ── User CRUD ────────────────────────────────────────────────────────────────

async def test_create_user(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "alice@example.com",
        "displayName": "Alice Smith",
        "active": True,
        "externalId": "ext-001",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["userName"] == "alice@example.com"
    assert body["displayName"] == "Alice Smith"
    assert body["active"] is True
    assert body["externalId"] == "ext-001"
    return body["id"]


async def test_create_user_conflict(client):
    # Create first
    await client.post("/api/v1/scim/v2/Users", json={
        "userName": "bob@example.com",
        "displayName": "Bob",
    })
    # Duplicate
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "bob@example.com",
        "displayName": "Bob Again",
    })
    assert resp.status_code == 409


async def test_create_user_missing_email(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "displayName": "No Email",
    })
    assert resp.status_code == 400


async def test_create_user_email_from_emails_array(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "emails": [{"value": "carol@example.com", "primary": True}],
        "displayName": "Carol",
    })
    assert resp.status_code == 201
    assert resp.json()["userName"] == "carol@example.com"


async def test_create_user_email_from_string_emails(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "emails": ["dave@example.com"],
    })
    assert resp.status_code == 201
    assert resp.json()["userName"] == "dave@example.com"


async def test_create_user_display_name_from_name_formatted(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "eve@example.com",
        "name": {"formatted": "Eve Formatted"},
    })
    assert resp.status_code == 201
    assert resp.json()["displayName"] == "Eve Formatted"


async def test_create_user_display_name_fallback(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "frank@example.com",
    })
    assert resp.status_code == 201
    assert resp.json()["displayName"] == "frank"


async def test_list_users(client):
    # Create users first
    await client.post("/api/v1/scim/v2/Users", json={
        "userName": "list1@example.com",
    })
    await client.post("/api/v1/scim/v2/Users", json={
        "userName": "list2@example.com",
    })
    resp = await client.get("/api/v1/scim/v2/Users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] >= 1
    assert "Resources" in body


async def test_list_users_with_filter(client):
    await client.post("/api/v1/scim/v2/Users", json={
        "userName": "filter@example.com",
    })
    resp = await client.get(
        "/api/v1/scim/v2/Users",
        params={"filter": 'userName eq "filter@example.com"'},
    )
    assert resp.status_code == 200
    body = resp.json()
    found = [r for r in body["Resources"] if r["userName"] == "filter@example.com"]
    assert len(found) >= 1


async def test_list_users_with_externalid_filter(client):
    await client.post("/api/v1/scim/v2/Users", json={
        "userName": "extfilter@example.com",
        "externalId": "ext-filter-001",
    })
    resp = await client.get(
        "/api/v1/scim/v2/Users",
        params={"filter": 'externalId eq "ext-filter-001"'},
    )
    assert resp.status_code == 200


async def test_list_users_pagination(client):
    resp = await client.get(
        "/api/v1/scim/v2/Users",
        params={"startIndex": 1, "count": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["startIndex"] == 1
    assert body["itemsPerPage"] <= 1


async def test_get_user(client):
    create_resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "getuser@example.com",
    })
    user_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/scim/v2/Users/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == user_id


async def test_get_user_not_found(client):
    resp = await client.get("/api/v1/scim/v2/Users/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_replace_user_not_found(client):
    resp = await client.put(
        "/api/v1/scim/v2/Users/00000000-0000-0000-0000-000000000000",
        json={"userName": "x@x.com"},
    )
    assert resp.status_code == 404


async def test_patch_user_not_found(client):
    resp = await client.patch(
        "/api/v1/scim/v2/Users/00000000-0000-0000-0000-000000000000",
        json={"Operations": []},
    )
    assert resp.status_code == 404


async def test_delete_user(client):
    create_resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "deluser@example.com",
    })
    user_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/scim/v2/Users/{user_id}")
    assert resp.status_code == 204


async def test_delete_user_not_found(client):
    resp = await client.delete("/api/v1/scim/v2/Users/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ── Group CRUD ───────────────────────────────────────────────────────────────

async def test_create_group(client):
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Test Admins",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["displayName"] == "Test Admins"


async def test_create_group_missing_displayname(client):
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "",
    })
    assert resp.status_code == 400


async def test_create_group_conflict(client):
    await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Dup Group",
    })
    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Dup Group",
    })
    assert resp.status_code == 409


async def test_create_group_with_members(client):
    user_resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "grpmember@example.com",
    })
    user_id = user_resp.json()["id"]

    resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Members Group",
        "members": [{"value": user_id}],
    })
    assert resp.status_code == 201


async def test_list_groups(client):
    await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "List Group 1",
    })
    resp = await client.get("/api/v1/scim/v2/Groups")
    assert resp.status_code == 200
    body = resp.json()
    assert "Resources" in body


async def test_list_groups_with_filter(client):
    await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Filter Group",
    })
    resp = await client.get(
        "/api/v1/scim/v2/Groups",
        params={"filter": 'displayName eq "Filter Group"'},
    )
    assert resp.status_code == 200


async def test_get_group(client):
    create_resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Get Group",
    })
    group_id = create_resp.json()["id"]

    resp = await client.get(f"/api/v1/scim/v2/Groups/{group_id}")
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "Get Group"


async def test_get_group_not_found(client):
    resp = await client.get("/api/v1/scim/v2/Groups/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_replace_group_not_found(client):
    resp = await client.put(
        "/api/v1/scim/v2/Groups/00000000-0000-0000-0000-000000000000",
        json={"displayName": "X"},
    )
    assert resp.status_code == 404


async def test_patch_group_add_members(client):
    user_resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "patchgrpadd@example.com",
    })
    user_id = user_resp.json()["id"]

    group_resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Patch Add Members",
    })
    group_id = group_resp.json()["id"]

    resp = await client.patch(f"/api/v1/scim/v2/Groups/{group_id}", json={
        "Operations": [{"op": "add", "path": "members", "value": [{"value": user_id}]}],
    })
    assert resp.status_code == 200


async def test_patch_group_remove_member(client):
    user_resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "patchgrprem@example.com",
    })
    user_id = user_resp.json()["id"]

    group_resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Patch Remove Members",
        "members": [{"value": user_id}],
    })
    group_id = group_resp.json()["id"]

    resp = await client.patch(f"/api/v1/scim/v2/Groups/{group_id}", json={
        "Operations": [{
            "op": "remove",
            "path": f'members[value eq "{user_id}"]',
        }],
    })
    assert resp.status_code == 200


async def test_patch_group_not_found(client):
    resp = await client.patch(
        "/api/v1/scim/v2/Groups/00000000-0000-0000-0000-000000000000",
        json={"Operations": []},
    )
    assert resp.status_code == 404


async def test_delete_group(client):
    create_resp = await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Delete Group",
    })
    group_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/scim/v2/Groups/{group_id}")
    assert resp.status_code == 204


async def test_delete_group_not_found(client):
    resp = await client.delete("/api/v1/scim/v2/Groups/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ── Helper unit tests ────────────────────────────────────────────────────────

def test_extract_email_from_username():
    from orchestrator.api.scim import _extract_email
    assert _extract_email({"userName": "a@b.com"}) == "a@b.com"


def test_extract_email_from_emails_array():
    from orchestrator.api.scim import _extract_email
    assert _extract_email({"emails": [{"value": "c@d.com"}]}) == "c@d.com"


def test_extract_email_from_string_email():
    from orchestrator.api.scim import _extract_email
    assert _extract_email({"emails": ["e@f.com"]}) == "e@f.com"


def test_extract_email_no_at():
    from orchestrator.api.scim import _extract_email
    assert _extract_email({"userName": "noatsign"}) == "noatsign"


def test_extract_email_missing():
    from orchestrator.api.scim import _extract_email
    assert _extract_email({}) is None


def test_extract_member_id_from_path():
    from orchestrator.api.scim import _extract_member_id_from_path
    assert _extract_member_id_from_path('members[value eq "abc-123"]') == "abc-123"


def test_extract_member_id_from_path_no_match():
    from orchestrator.api.scim import _extract_member_id_from_path
    assert _extract_member_id_from_path("members") is None


def test_scim_error_format():
    from orchestrator.api.scim import _scim_error
    resp = _scim_error(400, "bad request", "invalidValue")
    assert resp.status_code == 400
    # JSONResponse body is bytes
    import json
    body = json.loads(resp.body)
    assert body["status"] == "400"
    assert body["detail"] == "bad request"


def test_scim_response_content_type():
    from orchestrator.api.scim import _scim_response
    resp = _scim_response({"test": True})
    assert resp.status_code == 200
    assert resp.media_type == "application/scim+json"


def test_user_to_scim():
    from unittest.mock import MagicMock
    from orchestrator.api.scim import _user_to_scim
    user = MagicMock()
    user.id = "uid-1"
    user.external_id = "ext-1"
    user.email = "u@test.com"
    user.display_name = "User"
    user.is_active = True
    user.created_at = None
    user.updated_at = None
    result = _user_to_scim(user, "http://test")
    assert result["id"] == "uid-1"
    assert result["userName"] == "u@test.com"


def test_role_to_scim_group():
    from unittest.mock import MagicMock
    from orchestrator.api.scim import _role_to_scim_group
    role = MagicMock()
    role.id = "rid-1"
    role.name = "Admin"
    role.created_at = None
    role.updated_at = None
    result = _role_to_scim_group(role, base_url="http://test")
    assert result["displayName"] == "Admin"
    assert result["members"] == []


# ── SCIM filter parser (multi-clause, operators, error paths) ────────────────

ENTERPRISE_URN = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


async def test_filter_and_returns_correct_rows(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "amy@corp.com", "externalId": "X-1"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "ben@corp.com", "externalId": "X-2"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "cara@corp.com", "externalId": "X-1"})

    resp = await client.get("/api/v1/scim/v2/Users", params={
        "filter": 'userName eq "amy@corp.com" and externalId eq "X-1"',
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1
    assert len(body["Resources"]) == 1
    assert body["Resources"][0]["userName"] == "amy@corp.com"


async def test_filter_or_returns_correct_rows_and_total(client):
    for u in ("or1@corp.com", "or2@corp.com", "or3@corp.com"):
        await client.post("/api/v1/scim/v2/Users", json={"userName": u})

    resp = await client.get("/api/v1/scim/v2/Users", params={
        "filter": 'userName eq "or1@corp.com" or userName eq "or2@corp.com"',
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 2
    names = {r["userName"] for r in body["Resources"]}
    assert names == {"or1@corp.com", "or2@corp.com"}


async def test_filter_grouping_parentheses(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "grp-a@corp.com", "externalId": "G-1"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "grp-b@corp.com", "externalId": "G-2"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "grp-c@corp.com", "externalId": "G-9"})

    resp = await client.get("/api/v1/scim/v2/Users", params={
        "filter": '(userName eq "grp-a@corp.com" or userName eq "grp-b@corp.com")'
                  ' and externalId eq "G-1"',
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "grp-a@corp.com"


async def test_filter_co_operator(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "contains-me@corp.com"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "other@corp.com"})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'userName co "contains-me"'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "contains-me@corp.com"


async def test_filter_sw_operator(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "startswith@corp.com"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "nomatch@corp.com"})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'userName sw "startswith"'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "startswith@corp.com"


async def test_filter_ew_operator(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "user@ends-here.com"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "user@other.net"})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'userName ew "ends-here.com"'})
    assert resp.status_code == 200
    assert resp.json()["totalResults"] == 1


async def test_filter_pr_operator(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "haveext@corp.com", "externalId": "HAS-EXT"})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "noext@corp.com"})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": "externalId pr"})
    assert resp.status_code == 200
    names = {r["userName"] for r in resp.json()["Resources"]}
    assert "haveext@corp.com" in names
    assert "noext@corp.com" not in names


async def test_filter_active_boolean(client):
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "active-yes@corp.com", "active": True})
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "active-no@corp.com", "active": False})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": "active eq false"})
    assert resp.status_code == 200
    names = {r["userName"] for r in resp.json()["Resources"]}
    assert "active-no@corp.com" in names
    assert "active-yes@corp.com" not in names


async def test_filter_co_escapes_like_wildcards(client):
    # A literal '%' must not act as a wildcard — 'a%b' should match nothing.
    await client.post("/api/v1/scim/v2/Users",
                      json={"userName": "anything@corp.com"})
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'userName co "a%b"'})
    assert resp.status_code == 200
    assert resp.json()["totalResults"] == 0


async def test_filter_unknown_attribute_returns_400(client):
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'nickName eq "x"'})
    assert resp.status_code == 400
    body = resp.json()
    assert body["scimType"] == "invalidFilter"
    assert body["status"] == "400"


async def test_filter_malformed_returns_400(client):
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": "userName eq"})
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidFilter"


async def test_filter_bad_operator_returns_400(client):
    resp = await client.get("/api/v1/scim/v2/Users",
                            params={"filter": 'userName zz "x"'})
    assert resp.status_code == 400


async def test_group_filter_co(client):
    await client.post("/api/v1/scim/v2/Groups",
                      json={"displayName": "Engineering Admins"})
    await client.post("/api/v1/scim/v2/Groups", json={"displayName": "Sales"})
    resp = await client.get("/api/v1/scim/v2/Groups",
                            params={"filter": 'displayName co "Admin"'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["displayName"] == "Engineering Admins"


async def test_group_filter_unknown_attribute_returns_400(client):
    resp = await client.get("/api/v1/scim/v2/Groups",
                            params={"filter": 'bogus eq "x"'})
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidFilter"


# ── Enterprise User extension (Microsoft Entra) ──────────────────────────────


async def test_create_user_enterprise_extension_echoed_on_get(client):
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "schemas": [
            "urn:ietf:params:scim:schemas:core:2.0:User",
            ENTERPRISE_URN,
        ],
        "userName": "entra-user@example.com",
        "displayName": "Entra User",
        ENTERPRISE_URN: {
            "employeeNumber": "E-4242",
            "department": "Platform Engineering",
            "costCenter": "CC-100",
            "manager": {"value": "mgr-001", "displayName": "The Boss"},
        },
    })
    assert resp.status_code == 201
    body = resp.json()
    assert ENTERPRISE_URN in body["schemas"]
    assert body[ENTERPRISE_URN]["employeeNumber"] == "E-4242"
    assert body[ENTERPRISE_URN]["department"] == "Platform Engineering"
    assert body[ENTERPRISE_URN]["manager"]["value"] == "mgr-001"
    user_id = body["id"]

    # Echoed back on a subsequent GET (round-trips across requests).
    get_resp = await client.get(f"/api/v1/scim/v2/Users/{user_id}")
    assert get_resp.status_code == 200
    g = get_resp.json()
    assert ENTERPRISE_URN in g["schemas"]
    assert g[ENTERPRISE_URN]["costCenter"] == "CC-100"
    assert g[ENTERPRISE_URN]["manager"]["displayName"] == "The Boss"


async def test_user_without_enterprise_extension_has_no_urn(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "plain-user@example.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert ENTERPRISE_URN not in body["schemas"]
    assert ENTERPRISE_URN not in body


async def test_create_user_enterprise_extension_unmapped_subattr_ok(client):
    # Unknown/unmapped sub-attributes must be accepted, not rejected.
    resp = await client.post("/api/v1/scim/v2/Users", json={
        "userName": "entra-unmapped@example.com",
        ENTERPRISE_URN: {
            "employeeNumber": "E-1",
            "someVendorSpecificField": {"nested": "value"},
        },
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body[ENTERPRISE_URN]["someVendorSpecificField"] == {"nested": "value"}


async def test_patch_user_enterprise_extension_path(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "entra-patch@example.com"})
    user_id = resp.json()["id"]

    patch = await client.patch(f"/api/v1/scim/v2/Users/{user_id}", json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "add", "path": f"{ENTERPRISE_URN}:department", "value": "Finance"},
            {"op": "add", "path": f"{ENTERPRISE_URN}:manager.value", "value": "mgr-9"},
        ],
    })
    assert patch.status_code == 200
    body = patch.json()
    assert body[ENTERPRISE_URN]["department"] == "Finance"
    assert body[ENTERPRISE_URN]["manager"]["value"] == "mgr-9"


# ── User PATCH add / remove / replace ────────────────────────────────────────


async def test_patch_user_replace_active(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "p-replace@corp.com", "active": True})
    uid = resp.json()["id"]
    patch = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    })
    assert patch.status_code == 200
    assert patch.json()["active"] is False


async def test_patch_user_replace_active_string_value(client):
    # Entra can send the boolean as the string "False".
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "p-strbool@corp.com", "active": True})
    uid = resp.json()["id"]
    patch = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [{"op": "replace", "path": "active", "value": "False"}],
    })
    assert patch.status_code == 200
    assert patch.json()["active"] is False


async def test_patch_user_add_displayname(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "p-add@corp.com"})
    uid = resp.json()["id"]
    patch = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [{"op": "add", "path": "displayName", "value": "Added Name"}],
    })
    assert patch.status_code == 200
    assert patch.json()["displayName"] == "Added Name"


async def test_patch_user_remove_externalid(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "p-rem@corp.com", "externalId": "REM-1"})
    uid = resp.json()["id"]
    patch = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [{"op": "remove", "path": "externalId"}],
    })
    assert patch.status_code == 200
    assert patch.json()["externalId"] == ""


async def test_patch_user_pathless_bulk_replace(client):
    resp = await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "p-bulk@corp.com", "active": True})
    uid = resp.json()["id"]
    patch = await client.patch(f"/api/v1/scim/v2/Users/{uid}", json={
        "Operations": [{"op": "replace", "value": {
            "displayName": "Bulk Name",
            "active": False,
        }}],
    })
    assert patch.status_code == 200
    body = patch.json()
    assert body["displayName"] == "Bulk Name"
    assert body["active"] is False


# ── Group PATCH add / remove / replace (full membership) ─────────────────────


async def test_patch_group_replace_members(client):
    u1 = (await client.post("/api/v1/scim/v2/Users",
                            json={"userName": "gm1@corp.com"})).json()["id"]
    u2 = (await client.post("/api/v1/scim/v2/Users",
                            json={"userName": "gm2@corp.com"})).json()["id"]
    gid = (await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Replace Members Grp",
        "members": [{"value": u1}],
    })).json()["id"]

    patch = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [{"op": "replace", "path": "members",
                        "value": [{"value": u2}]}],
    })
    assert patch.status_code == 200

    get_resp = await client.get(f"/api/v1/scim/v2/Groups/{gid}")
    member_ids = {m["value"] for m in get_resp.json()["members"]}
    assert member_ids == {u2}


async def test_patch_group_remove_all_members(client):
    u1 = (await client.post("/api/v1/scim/v2/Users",
                            json={"userName": "rall1@corp.com"})).json()["id"]
    gid = (await client.post("/api/v1/scim/v2/Groups", json={
        "displayName": "Remove All Grp",
        "members": [{"value": u1}],
    })).json()["id"]

    patch = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [{"op": "remove", "path": "members"}],
    })
    assert patch.status_code == 200

    get_resp = await client.get(f"/api/v1/scim/v2/Groups/{gid}")
    assert get_resp.json()["members"] == []


async def test_patch_group_add_then_remove_single_member(client):
    uid = (await client.post("/api/v1/scim/v2/Users",
                             json={"userName": "single-m@corp.com"})).json()["id"]
    gid = (await client.post("/api/v1/scim/v2/Groups",
                             json={"displayName": "Single Member Grp"})).json()["id"]

    add = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [{"op": "add", "path": "members", "value": [{"value": uid}]}],
    })
    assert add.status_code == 200
    get1 = await client.get(f"/api/v1/scim/v2/Groups/{gid}")
    assert {m["value"] for m in get1.json()["members"]} == {uid}

    remove = await client.patch(f"/api/v1/scim/v2/Groups/{gid}", json={
        "Operations": [{"op": "remove", "path": f'members[value eq "{uid}"]'}],
    })
    assert remove.status_code == 200
    get2 = await client.get(f"/api/v1/scim/v2/Groups/{gid}")
    assert get2.json()["members"] == []


# ── Filter parser unit tests (no DB) ─────────────────────────────────────────


def test_build_user_filter_rejects_unknown_attr():
    import pytest
    from orchestrator.api.scim import ScimFilterError, _build_user_filter
    with pytest.raises(ScimFilterError):
        _build_user_filter('nickName eq "x"')


def test_build_user_filter_accepts_multiclause():
    from orchestrator.api.scim import _build_user_filter
    # Should not raise — returns a SQLAlchemy expression.
    expr = _build_user_filter(
        'userName eq "a@b.com" and (externalId pr or active eq true)'
    )
    assert expr is not None


def test_coerce_bool_variants():
    from orchestrator.api.scim import ScimFilterError, _coerce_bool
    import pytest
    assert _coerce_bool(True) is True
    assert _coerce_bool("False") is False
    assert _coerce_bool("true") is True
    assert _coerce_bool(0) is False
    with pytest.raises(ScimFilterError):
        _coerce_bool("maybe")
