"""Regresje bezpieczenstwa, kolejnosci raportow i nowych widokow."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (AgentCredential, Asset, AssetCurrentReport, AssetRelation,
                                AssetChange, InventorySnapshot, PortalUser, ReportReceipt, utcnow)
from cmdb_server.security import issue_csrf_token
from cmdb_server.services import quality
from cmdb_server.services.scoping import TenantContext
from .factories import build_report

PASSWORD = "bardzo-dlugie-haslo"


def login(client, tenant, make_user, role="admin"):
    uid = make_user(tenant["id"], "review@example.com", PASSWORD, role)
    assert client.post("/login", data={"email": "review@example.com", "password": PASSWORD},
                       follow_redirects=False).status_code == 303
    return uid, issue_csrf_token(uid)


def enroll(client, tenant, machine="machine-review-001"):
    report = build_report(machine_id=machine)
    response = client.post("/api/v1/agents/enroll", headers={"Authorization": "Bearer " + tenant["token"]},
                           json={"machine_id": machine, "identity": report["identity"], "agent_version": "1.0"})
    assert response.status_code == 201, response.text
    return response.json(), report


def send(client, enrollment, report):
    return client.post("/api/v1/inventory", headers={"Authorization": "Bearer " + enrollment["agent_token"]}, json=report)


def test_password_change_invalidates_stolen_session(client, tenant_a, make_user):
    uid, csrf = login(client, tenant_a, make_user)
    stolen = client.cookies.get("cmdb_session")
    assert client.post("/konto/haslo", data={"csrf_token": csrf, "obecne": PASSWORD,
        "nowe": "nowe-bardzo-dlugie-haslo", "powtorzone": "nowe-bardzo-dlugie-haslo"}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    client.cookies.set("cmdb_session", stolen)
    assert client.get("/konto", follow_redirects=False).headers["location"] == "/login"


def test_logout_all_invalidates_cookie(client, tenant_a, make_user):
    uid, csrf = login(client, tenant_a, make_user)
    stolen = client.cookies.get("cmdb_session")
    assert client.post("/konto/wyloguj-wszystkie", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    client.cookies.set("cmdb_session", stolen)
    assert client.get("/konto", follow_redirects=False).status_code == 303


def test_revoke_blocks_even_old_agents_and_explicit_unblock_requires_new_key(client, tenant_a, make_user):
    enrolled, report = enroll(client, tenant_a)
    uid, csrf = login(client, tenant_a, make_user)
    with SessionLocal() as db:
        credential = db.execute(select(AgentCredential)).scalar_one()
        cid = credential.id
    asset_id = enrolled["asset_id"]
    assert client.post(f"/assets/{asset_id}/credentials/{cid}/revoke", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert send(client, enrolled, report).status_code == 401
    payload = {"machine_id": report["machine_id"], "identity": report["identity"], "agent_version": "1.0"}
    assert client.post("/api/v1/agents/enroll", headers={"Authorization": "Bearer " + tenant_a["token"]}, json=payload).status_code == 403
    assert client.post(f"/assets/{asset_id}/enrollment/unblock", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert send(client, enrolled, report).status_code == 401
    fresh, _ = enroll(client, tenant_a)
    assert send(client, fresh, report).status_code == 200


def test_spoofed_forwarded_proto_does_not_bypass_https(client, monkeypatch):
    from cmdb_server.config import get_settings
    monkeypatch.setattr(get_settings(), "require_https", True)
    assert client.post("/api/v1/inventory", headers={"X-Forwarded-Proto": "https"}, json={}).status_code == 403


def test_forwarded_ip_is_not_trusted_by_application():
    from starlette.requests import Request
    from cmdb_server.services.auth import client_ip
    request = Request({"type": "http", "headers": [(b"x-forwarded-for", b"1.2.3.4")], "client": ("10.0.0.2", 80)})
    assert client_ip(request) == "10.0.0.2"


@pytest.mark.parametrize("section,key,bad", [
    ("hardware", "cpu", "text"), ("hardware", "storage", []),
    ("software", "packages", ["not-an-object"]), ("software", "services", {}),
    ("network", "interfaces", [None]), ("users", "local_accounts", 9),
])
def test_malformed_report_is_422_not_500(client, tenant_a, section, key, bad):
    enrolled, report = enroll(client, tenant_a)
    report[section][key] = bad
    assert send(client, enrolled, report).status_code == 422


def test_nested_bad_package_name_is_422(client, tenant_a):
    enrolled, report = enroll(client, tenant_a)
    report["software"]["packages"][0]["name"] = 123
    assert send(client, enrolled, report).status_code == 422


def test_unknown_fields_remain_supported(client, tenant_a):
    enrolled, report = enroll(client, tenant_a)
    report["hardware"]["future_extension"] = {"name": {"any": "json"}, "id": 7}
    assert send(client, enrolled, report).status_code == 200


def test_latest_reading_changes_without_history_noise(client, tenant_a, make_user):
    enrolled, report = enroll(client, tenant_a)
    assert send(client, enrolled, report).json()["changed"]
    newer = deepcopy(report)
    newer["agent"]["collected_at"] = utcnow().isoformat()
    newer["hardware"]["storage"]["logical_disks"][0]["free_bytes"] = 12345
    newer["errors"] = [{"collector": "test", "message": "nowy blad"}]
    assert send(client, enrolled, newer).json()["changed"] is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InventorySnapshot)) == 1
        assert db.get(AssetCurrentReport, enrolled["asset_id"]).payload["errors"] == newer["errors"]
    login(client, tenant_a, make_user)
    assert "nowy blad" in client.get(f'/assets/{enrolled["asset_id"]}').text
    assert client.get(f'/assets/{enrolled["asset_id"]}/raw.json').json()["hardware"]["storage"]["logical_disks"][0]["free_bytes"] == 12345


def test_reordering_and_retry_are_idempotent(client, tenant_a):
    enrolled, report = enroll(client, tenant_a)
    report["report_id"] = str(uuid4())
    assert send(client, enrolled, report).json()["changed"] is True
    assert send(client, enrolled, report).json()["changed"] is False
    second = deepcopy(report)
    second["report_id"] = str(uuid4())
    second["agent"]["collected_at"] = utcnow().isoformat()
    second["software"]["packages"].reverse()
    assert send(client, enrolled, second).json()["changed"] is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InventorySnapshot)) == 1
        assert db.scalar(select(func.count()).select_from(ReportReceipt)) == 2
    second["identity"]["hostname"] = "conflicting-report"
    assert send(client, enrolled, second).status_code == 409


def test_delayed_report_preserves_current_state_and_events(client, tenant_a):
    enrolled, report = enroll(client, tenant_a)
    assert send(client, enrolled, report).status_code == 200
    old = deepcopy(report)
    old["agent"]["collected_at"] = (utcnow() - timedelta(days=3)).isoformat()
    old["identity"]["hostname"] = "old-host"
    old["software"]["packages"] = []
    assert send(client, enrolled, old).json()["changed"] is False
    assert send(client, enrolled, old).json()["changed"] is False
    with SessionLocal() as db:
        assert db.get(Asset, enrolled["asset_id"]).hostname == report["identity"]["hostname"]
        assert db.scalar(select(func.count()).select_from(AssetChange)) == 0
        assert db.scalar(select(func.count()).select_from(InventorySnapshot)) == 2


def assets_for_relations(tenant):
    with SessionLocal() as db:
        assets = [Asset(tenant_id=tenant["id"], machine_id=str(uuid4()), hostname=name,
                        typ=kind, zrodlo="reczne") for name, kind in
                  [("VM", "vm"), ("Host", "host"), ("Cluster", "klaster"), ("App", "aplikacja")]]
        db.add_all(assets)
        db.commit()
        return [a.id for a in assets]


def test_relations_create_list_delete_and_audit(client, tenant_a, make_user):
    uid, csrf = login(client, tenant_a, make_user)
    vm, host, cluster, app = assets_for_relations(tenant_a)
    for source, target, kind in [(vm, host, "vm_host"), (host, cluster, "host_cluster"), (app, host, "application_server")]:
        response = client.post("/relacje", data={"source_id": source, "target_id": target, "kind": kind, "csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 303, response.text
    assert "Cluster" in client.get("/relacje").text
    assert "Host" in client.get(f"/relacje?asset_id={vm}").text
    with SessionLocal() as db:
        rows = db.execute(select(AssetRelation)).scalars().all()
        assert len(rows) == 3
        rid = rows[0].id
    assert client.post(f"/relacje/{rid}/delete", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert "asset.relation_added" in client.get("/audit").text


def test_relations_reject_cross_tenant_self_wrong_types_and_csrf(client, tenant_a, tenant_b, make_user):
    uid, csrf = login(client, tenant_a, make_user)
    vm, host, cluster, app = assets_for_relations(tenant_a)
    foreign = assets_for_relations(tenant_b)[1]
    for target, kind, code in [(foreign, "vm_host", 404), (vm, "vm_host", 422), (cluster, "vm_host", 422), (host, "invalid", 422)]:
        assert client.post("/relacje", data={"source_id": vm, "target_id": target, "kind": kind, "csrf_token": csrf}).status_code == code
    assert client.post("/relacje", data={"source_id": vm, "target_id": host, "kind": "vm_host"}).status_code == 403
    assert client.get(f"/relacje?asset_id={foreign}").status_code == 404


def test_viewer_cannot_add_relation(client, tenant_a, make_user):
    uid, csrf = login(client, tenant_a, make_user, "viewer")
    vm, host, _, _ = assets_for_relations(tenant_a)
    assert client.post("/relacje", data={"source_id": vm, "target_id": host, "kind": "vm_host", "csrf_token": csrf}).status_code == 403
    assert 'action="/relacje"' not in client.get("/relacje").text


def test_quality_categories_and_tenant_isolation(client, tenant_a, tenant_b, make_user):
    uid, csrf = login(client, tenant_a, make_user)
    enrolled, report = enroll(client, tenant_a)
    report["errors"] = [{"collector": "network", "message": "blad"}]
    assert send(client, enrolled, report).status_code == 200
    other, _ = enroll(client, tenant_b, "other-machine-001")
    with SessionLocal() as db:
        current = db.get(AssetCurrentReport, enrolled["asset_id"])
        current.collected_at = utcnow() - timedelta(days=10)
        db.get(Asset, other["asset_id"]).hostname = "SECRET-OTHER-TENANT"
        db.add(Asset(tenant_id=tenant_a["id"], machine_id="manual-machine-001", hostname="ManualDevice",
                     zrodlo="reczne", serial_number="SN-ABC-123"))
        db.commit()
        rows, counts, total = quality.quality_rows(db, TenantContext(tenant_a["id"], "a", "test"))
        assert total == 2
        assert counts == {"errors": 1, "stale": 1, "owner": 2, "duplicates": 2}
    text = client.get("/jakosc").text
    assert "SECRET-OTHER-TENANT" not in text
    assert "ManualDevice" not in client.get("/jakosc?issue=stale").text
    assert client.get("/jakosc?issue=invalid").status_code == 422


def test_relation_duplicate_cardinality_and_cycle(client, tenant_a, make_user):
    _, csrf = login(client, tenant_a, make_user)
    vm, host, cluster, app = assets_for_relations(tenant_a)
    # Komputer jest kompatybilnym typem dla obu koncow VM/host.
    with SessionLocal() as db:
        db.get(Asset, vm).typ = "komputer"
        db.get(Asset, host).typ = "komputer"
        db.commit()
    data = {"source_id": vm, "target_id": host, "kind": "vm_host", "csrf_token": csrf}
    for _ in range(2):
        assert client.post("/relacje", data=data, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AssetRelation)) == 1
    reverse = {**data, "source_id": host, "target_id": vm}
    assert client.post("/relacje", data=reverse).status_code == 409
    with SessionLocal() as db:
        db.get(Asset, app).typ = "komputer"
        db.commit()
    assert client.post("/relacje", data={**data, "target_id": app}).status_code == 409


def test_foreign_relation_cannot_be_deleted(client, tenant_a, tenant_b, make_user):
    from cmdb_server.services.relations import add_relation
    _, csrf = login(client, tenant_a, make_user)
    vm, host, _, _ = assets_for_relations(tenant_b)
    with SessionLocal() as db:
        row, _ = add_relation(db, TenantContext(tenant_b["id"], "b", "test", can_write=True), vm, host, "vm_host")
        rid = row.id
        db.commit()
    assert client.post(f"/relacje/{rid}/delete", data={"csrf_token": csrf}).status_code == 404


def test_concurrent_retries_create_one_snapshot(client, tenant_a):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from cmdb_server.schemas import InventoryReport
    from cmdb_server.services.inventory import store_report
    enrolled, report = enroll(client, tenant_a)
    report["report_id"] = str(uuid4())
    barrier = Barrier(2)
    def write():
        with SessionLocal() as db:
            asset = db.get(Asset, enrolled["asset_id"])
            barrier.wait(timeout=10)
            _, changed = store_report(db, TenantContext(tenant_a["id"], "a", "test"),
                                      asset, InventoryReport.model_validate(report))
            db.commit()
            return changed
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [False, True]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(InventorySnapshot)) == 1


def test_new_columns_migrate_without_removing_existing_users(client, tenant_a, make_user):
    from sqlalchemy import text
    from cmdb_server.db import engine, init_db
    uid = make_user(tenant_a["id"], "migration@example.com", PASSWORD)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE portal_users DROP COLUMN session_version"))
        conn.execute(text("ALTER TABLE assets DROP COLUMN enrollment_blocked"))
    init_db()
    with SessionLocal() as db:
        assert db.get(PortalUser, uid).session_version == 1
