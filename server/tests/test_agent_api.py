"""Rejestracja agenta i przyjmowanie raportow."""
from __future__ import annotations

import copy

from cmdb_server.db import SessionLocal
from cmdb_server.models import AgentCredential, Asset, InventorySnapshot
from sqlalchemy import select

from .factories import build_report


def enroll(client, token: str, machine_id: str = "win-machine-0001", hostname: str = "WS-01"):
    return client.post(
        "/api/v1/agents/enroll",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "machine_id": machine_id,
            "identity": {"hostname": hostname, "os_family": "windows", "arch": "amd64"},
            "agent_version": "0.1.0",
        },
    )


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_enroll_issues_per_agent_token(client, tenant_a):
    response = enroll(client, tenant_a["token"])
    assert response.status_code == 201
    body = response.json()

    assert body["tenant_slug"] == "firma-a"
    assert body["agent_token"].startswith("cmdb_agt_")
    # Token agenta jest inny niz firmowy token rejestracyjny.
    assert body["agent_token"] != tenant_a["token"]

    with SessionLocal() as db:
        asset = db.get(Asset, body["asset_id"])
        assert asset.tenant_id == tenant_a["id"]
        # W bazie nie ma jawnej wartosci tokenu, tylko skrot.
        cred = db.execute(select(AgentCredential)).scalar_one()
        assert body["agent_token"] not in cred.token_hash
        assert len(cred.token_hash) == 64


def test_enroll_rejects_unknown_token(client, tenant_a):
    response = enroll(client, "cmdb_ent_aaaaaaaaaaaa_nieprawidlowy-sekret")
    assert response.status_code == 401


def test_enroll_rejects_revoked_token(client, tenant_a):
    from cmdb_server.models import EnrollmentToken, utcnow

    with SessionLocal() as db:
        token = db.execute(select(EnrollmentToken)).scalar_one()
        token.revoked_at = utcnow()
        db.commit()

    assert enroll(client, tenant_a["token"]).status_code == 401


def test_agent_token_cannot_be_used_for_enrollment(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    # Token agenta ma inny typ - nie moze rejestrowac kolejnych maszyn.
    assert enroll(client, agent_token, machine_id="win-machine-0002").status_code == 401


def test_submit_report_creates_snapshot(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"], machine_id="win-machine-0001").json()[
        "agent_token"
    ]
    response = client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=build_report(machine_id="win-machine-0001"),
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True

    with SessionLocal() as db:
        asset = db.execute(select(Asset)).scalar_one()
        assert asset.hostname == "WS-KSIEGOWOSC-01"
        assert asset.serial_number == "SN-ABC-123"
        assert asset.primary_ip == "10.10.5.21"
        assert asset.os_name == "Microsoft Windows 11 Pro"
        # Podsumowanie do listy maszyn liczone przy zapisie.
        assert asset.facts["memory_gb"] == 16.0
        assert asset.facts["packages"] == 2
        assert asset.facts["services_running"] == 1

        snapshot = db.execute(select(InventorySnapshot)).scalar_one()
        # Pelny raport zapisany jako JSON - zrodlo prawdy.
        assert snapshot.payload["hardware"]["cpu"]["model"] == "Intel Core i5-11500"
        assert snapshot.tenant_id == tenant_a["id"]


def test_identical_report_is_deduplicated(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    headers = {"Authorization": f"Bearer {agent_token}"}
    report = build_report()

    first = client.post("/api/v1/inventory", headers=headers, json=report)
    # Drugi raport rozni sie tylko polami ulotnymi (uptime, czas zbierania).
    second_report = build_report(uptime=99999)
    second = client.post("/api/v1/inventory", headers=headers, json=second_report)

    assert first.json()["changed"] is True
    assert second.json()["changed"] is False

    with SessionLocal() as db:
        assert db.execute(select(InventorySnapshot)).scalars().all().__len__() == 1
        asset = db.execute(select(Asset)).scalar_one()
        assert asset.last_seen is not None


def test_real_change_creates_new_snapshot(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    headers = {"Authorization": f"Bearer {agent_token}"}

    client.post("/api/v1/inventory", headers=headers, json=build_report())
    changed_report = build_report(
        packages=[{"name": "Nowy Program", "version": "1.0", "source": "registry"}]
    )
    response = client.post("/api/v1/inventory", headers=headers, json=changed_report)

    assert response.json()["changed"] is True
    with SessionLocal() as db:
        assert len(db.execute(select(InventorySnapshot)).scalars().all()) == 2


def test_report_with_wrong_machine_id_rejected(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"], machine_id="win-machine-0001").json()[
        "agent_token"
    ]
    response = client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=build_report(machine_id="cudza-maszyna-9999"),
    )
    assert response.status_code == 409


def test_revoked_credential_stops_reporting(client, tenant_a):
    from cmdb_server.models import utcnow

    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    headers = {"Authorization": f"Bearer {agent_token}"}
    assert client.post("/api/v1/inventory", headers=headers, json=build_report()).status_code == 200

    with SessionLocal() as db:
        cred = db.execute(select(AgentCredential)).scalar_one()
        cred.revoked_at = utcnow()
        db.commit()

    assert client.post("/api/v1/inventory", headers=headers, json=build_report()).status_code == 401


def test_reenrollment_revokes_previous_credential(client, tenant_a):
    first = enroll(client, tenant_a["token"]).json()["agent_token"]
    second = enroll(client, tenant_a["token"]).json()["agent_token"]
    assert first != second

    old_headers = {"Authorization": f"Bearer {first}"}
    new_headers = {"Authorization": f"Bearer {second}"}
    assert client.post("/api/v1/inventory", headers=old_headers, json=build_report()).status_code == 401
    assert client.post("/api/v1/inventory", headers=new_headers, json=build_report()).status_code == 200


def test_report_rejects_unsupported_schema(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    report = copy.deepcopy(build_report())
    report["schema_version"] = 99
    response = client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=report,
    )
    assert response.status_code == 422


def test_partial_collector_errors_are_accepted(client, tenant_a):
    """Jeden nieudany kolektor nie moze zablokowac calego raportu."""
    agent_token = enroll(client, tenant_a["token"]).json()["agent_token"]
    report = build_report()
    report["errors"] = [{"collector": "software.updates", "message": "Access denied"}]
    response = client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {agent_token}"},
        json=report,
    )
    assert response.status_code == 200
    with SessionLocal() as db:
        asset = db.execute(select(Asset)).scalar_one()
        assert asset.facts["collector_errors"] == 1
