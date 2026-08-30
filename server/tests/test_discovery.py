from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, DiscoveryDevice, DiscoveryScanner, InventorySnapshot, utcnow
from .test_review_improvements import enroll, login, send


def observation(**overrides):
    return {"scanned_at": utcnow().isoformat(), "ranges": ["10.0.0.0/24"], "complete": True,
            "attempted_hosts": 254, "total_hosts": 254, "errors": [], "devices": [
                {"ip": "10.0.0.2", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "printer.lan", "ports": [80, 9100],
                 "device_type": "drukarka", "os_hint": "", "manufacturer": "HP", "confidence": "medium",
                 "evidence": ["HP LaserJet", "TCP 9100 otwarty"]}], **overrides}


def setup_discovery(client, tenant):
    enrolled, report = enroll(client, tenant)
    report["report_id"] = str(uuid4())
    report["network_discovery"] = observation()
    response = send(client, enrolled, report)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        device_id = db.execute(select(DiscoveryDevice.id).where(DiscoveryDevice.tenant_id == tenant["id"])).scalar_one()
    return enrolled, report, device_id


def test_discovery_roundtrip_replay_no_inventory_or_history_noise(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    assert send(client, enrolled, report).status_code == 200
    newer = deepcopy(report)
    newer["report_id"] = str(uuid4())
    newer["network_discovery"]["scanned_at"] = utcnow().isoformat()
    newer["network_discovery"]["devices"][0]["ports"] = [9100]
    assert send(client, enrolled, newer).json()["changed"] is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Asset.id))) == 1
        assert db.scalar(select(func.count(DiscoveryDevice.id))) == 1
        assert db.scalar(select(func.count(InventorySnapshot.id))) == 1
        assert db.get(DiscoveryDevice, device_id).observation["ports"] == [9100]
    _, csrf = login(client, tenant_a, make_user)
    assert "printer.lan" in client.get("/wykrywanie").text
    assert "HP LaserJet" in client.get(f"/wykrywanie/{device_id}").text
    payload = {"csrf_token": csrf, "hostname": "Drukarka ksiegowosc", "typ": "drukarka"}
    response = client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).headers["location"] == response.headers["location"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Asset.id))) == 2
        asset = db.get(Asset, db.get(DiscoveryDevice, device_id).asset_id)
        assert asset.zrodlo == "reczne" and asset.typ == "drukarka"
        assert asset.primary_ip == "10.0.0.2" and asset.os_family is None


def test_older_or_partial_scan_does_not_refresh_absent_devices(client, tenant_a):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    initial = report["network_discovery"]["scanned_at"]
    older = deepcopy(report)
    older["report_id"] = str(uuid4())
    older["network_discovery"]["scanned_at"] = (utcnow() - timedelta(days=1)).isoformat()
    older["network_discovery"]["devices"][0]["hostname"] = "stary"
    assert send(client, enrolled, older).status_code == 200
    partial = deepcopy(report)
    partial["report_id"] = str(uuid4())
    partial["network_discovery"] = observation(devices=[], complete=False, attempted_hosts=1, errors=["limit czasu"])
    assert send(client, enrolled, partial).status_code == 200
    with SessionLocal() as db:
        device = db.get(DiscoveryDevice, device_id)
        assert device.hostname == "printer.lan" and device.last_seen.isoformat() == initial
        assert db.get(DiscoveryScanner, enrolled["asset_id"]).details["errors"] == ["limit czasu"]


def test_changed_mac_clears_previous_approval_even_after_missing_mac(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    _, csrf = login(client, tenant_a, make_user)
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={"csrf_token": csrf, "hostname": "Printer", "typ": "drukarka"}, follow_redirects=False).status_code == 303
    for mac in ("", "aa:bb:cc:dd:ee:00"):
        report["report_id"] = str(uuid4())
        report["network_discovery"]["scanned_at"] = utcnow().isoformat()
        report["network_discovery"]["devices"][0]["mac"] = mac
        assert send(client, enrolled, report).status_code == 200
    with SessionLocal() as db:
        assert db.get(DiscoveryDevice, device_id).asset_id is None
        assert db.scalar(select(func.count(Asset.id))) == 2


def test_isolation_csrf_and_link_without_overwriting_agent(client, tenant_a, tenant_b, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    foreign, _, foreign_id = setup_discovery(client, tenant_b)
    _, csrf = login(client, tenant_a, make_user)
    assert client.get(f"/wykrywanie/{foreign_id}").status_code == 404
    payload = {"csrf_token": csrf, "hostname": "should-not-overwrite", "typ": "drukarka", "asset_id": foreign["asset_id"]}
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).status_code == 404
    assert client.post(f"/wykrywanie/{foreign_id}/adopt", data=payload, follow_redirects=False).status_code == 404
    payload["asset_id"] = enrolled["asset_id"]
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={**payload, "csrf_token": ""}, follow_redirects=False).status_code == 403
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        asset = db.get(Asset, enrolled["asset_id"])
        assert asset.hostname == report["identity"]["hostname"] and asset.zrodlo == "agent"


def test_viewer_can_read_but_cannot_adopt(client, tenant_a, make_user):
    _, _, device_id = setup_discovery(client, tenant_a)
    _, csrf = login(client, tenant_a, make_user, role="viewer")
    assert client.get("/wykrywanie").status_code == 200
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={"csrf_token": csrf, "hostname": "Printer", "typ": "drukarka"}, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("bad", [
    {"devices": [{"ip": "8.8.8.8"}]}, {"devices": [{"ip": "10.1.1.1"}]},
    {"ranges": ["10.0.0.0/8"]}, {"devices": [{"ip": "10.0.0.2", "ports": [70000]}]},
    {"devices": [{"ip": "10.0.0.2", "hostname": "x" * 256}]},
    {"devices": [{"ip": "10.0.0.2"}, {"ip": "10.0.0.2"}]},
    {"attempted_hosts": 0}, {"scanned_at": "2099-01-01T00:00:00Z"},
])
def test_invalid_discovery_rejected_without_partial_writes(client, tenant_a, bad):
    enrolled, report = enroll(client, tenant_a)
    report["network_discovery"] = observation(**bad)
    assert send(client, enrolled, report).status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DiscoveryDevice.id))) == 0


def test_banners_are_escaped_in_html(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    report["report_id"] = str(uuid4())
    report["network_discovery"]["scanned_at"] = utcnow().isoformat()
    report["network_discovery"]["devices"][0]["evidence"] = ['<script>alert(1)</script>']
    assert send(client, enrolled, report).status_code == 200
    login(client, tenant_a, make_user)
    html = client.get(f"/wykrywanie/{device_id}").text
    assert '<script>alert(1)</script>' not in html and '&lt;script&gt;' in html
