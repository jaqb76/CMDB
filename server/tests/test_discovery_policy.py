from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import AuditLog, DiscoveryPolicy
from .test_review_improvements import enroll, login

NONCE = "a" * 32


def fetch(client, enrolled):
    return client.get("/api/v1/agent/discovery-policy?nonce=" + NONCE,
        headers={"Authorization": "Bearer " + enrolled["agent_token"]})


def form(csrf, revision="unassigned", **extra):
    return {"csrf_token": csrf, "revision": revision, "enabled": "true", "auto_subnets": "false",
            "cidrs": "10.10.0.0/24", "max_hosts": "256", "rate": "8", "budget_seconds": "120",
            "interval_seconds": "86400", **extra}


def test_policy_default_denies_and_only_own_asset_is_returned(client, tenant_a, tenant_b):
    a, _ = enroll(client, tenant_a)
    b, _ = enroll(client, tenant_b)
    response = fetch(client, a)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    policy = response.json()
    assert policy["nonce"] == NONCE and policy["asset_id"] == a["asset_id"]
    assert policy["asset_id"] != b["asset_id"] and policy["policy"]["enabled"] is False
    assert client.get("/api/v1/agent/discovery-policy?nonce=" + NONCE).status_code == 401


def test_admin_policy_roundtrip_audit_conflict_and_disable(client, tenant_a, make_user):
    enrolled, _ = enroll(client, tenant_a)
    _, csrf = login(client, tenant_a, make_user)
    path = f"/assets/{enrolled['asset_id']}/discovery-policy"
    assert client.post(path, data=form(csrf), follow_redirects=False).status_code == 303
    policy = fetch(client, enrolled).json()
    assert policy["policy"]["enabled"] and policy["policy"]["cidrs"] == ["10.10.0.0/24"]
    assert "administrator" in client.get(path).text
    assert client.post(path, data=form(csrf)).status_code == 409
    assert client.post(path, data=form(csrf, policy["revision"], enabled="false"), follow_redirects=False).status_code == 303
    assert fetch(client, enrolled).json()["policy"]["enabled"] is False
    with SessionLocal() as db:
        assert len(db.scalars(select(AuditLog).where(AuditLog.action == "discovery.policy_changed")).all()) == 2


def test_viewer_csrf_and_cross_tenant_policy_edits_are_rejected(client, tenant_a, tenant_b, make_user):
    a, _ = enroll(client, tenant_a)
    b, _ = enroll(client, tenant_b)
    _, csrf = login(client, tenant_a, make_user, role="viewer")
    path = f"/assets/{a['asset_id']}/discovery-policy"
    assert client.post(path, data=form(csrf)).status_code == 403
    assert "disabled" in client.get(path).text
    assert client.get(f"/assets/{b['asset_id']}/discovery-policy").status_code == 404
    assert client.post(path, data=form("bad")).status_code == 403
    with SessionLocal() as db:
        assert db.scalar(select(DiscoveryPolicy)) is None


def test_admin_cannot_set_other_tenant_policy_or_public_ranges(client, tenant_a, tenant_b, make_user):
    a, _ = enroll(client, tenant_a)
    b, _ = enroll(client, tenant_b)
    _, csrf = login(client, tenant_a, make_user)
    assert client.post(f"/assets/{b['asset_id']}/discovery-policy", data=form(csrf)).status_code == 404
    for extra in ({"cidrs": "8.8.8.8/32"}, {"cidrs": "10.0.0.0/8"}, {"rate": "999"}, {"budget_seconds": "901"}):
        assert client.post(f"/assets/{a['asset_id']}/discovery-policy", data=form(csrf, **extra)).status_code == 422
