import hashlib
import json

import pytest
from sqlalchemy import select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import AgentRelease, GlobalAgentTarget
from cmdb_server.services import release_trust, upgrades
from .test_admin import _superadmin, _wgraj_wersje, _oznacz_oficjalna, _trust_test_worker, PLIK_AGENTA
from .test_pobieranie import _plik_gui


def windows_agent(client, tenant):
    result = client.post("/api/v1/agents/enroll", headers={"Authorization": "Bearer " + tenant["token"]},
        json={"machine_id": "trust-regression", "identity": {"hostname": "HOST", "os_family": "windows", "arch": "amd64"},
              "agent_version": "0.5.8"}).json()
    return result, {"Authorization": "Bearer " + result["agent_token"]}


def test_forged_unified_footer_does_not_admit_old_tray(client, make_user):
    csrf = _superadmin(client, make_user)
    meta = json.dumps({"version": "0.5.10", "os_family": "windows", "entry_mode": "unified"}).encode()
    forged = _plik_gui() + b"<<<CMDB-AGENT-META>>>" + meta + b"<<<KONIEC>>>"
    response = _wgraj_wersje(client, csrf, "", forged, trust_worker=False)
    assert response.status_code == 400 and "SHA-256" in response.text
    with SessionLocal() as db:
        assert db.scalar(select(AgentRelease)) is None
    assert get_settings().trusted_windows_builds == {}


def test_console_header_is_not_a_trust_bypass_and_legacy_rows_are_blocked(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    assert _wgraj_wersje(client, csrf, "0.5.10", PLIK_AGENTA, trust_worker=False).status_code == 303
    with SessionLocal() as db:
        release = db.scalar(select(AgentRelease))
        release_id = release.id
        # Simulate a target created under the old, unsafe server implementation.
        db.add(GlobalAgentTarget(os_family="windows", release_id=release.id, updated_by="old-server"))
        db.commit()
    assert _oznacz_oficjalna(client, csrf, release_id).status_code == 400
    enrolled, headers = windows_agent(client, tenant_a)
    assert client.get("/api/v1/agent/version", headers=headers).json()["available"] is False
    assert client.get("/api/v1/agent/release", headers=headers).status_code == 404
    assert client.get("/download/agent-windows.exe", headers={"Authorization": "Bearer " + tenant_a["token"]}).status_code == 404
    for scope in ("firma", "wybrane"):
        response = client.post(f"/admin/tenants/{tenant_a['id']}/upgrade",
            data={"csrf_token": csrf, "zakres": scope, "release_id": release_id,
                  "os_family": "windows", "asset_id": enrolled["asset_id"]})
        assert response.status_code == 400


def test_verified_bytes_not_footer_authorize_worker_and_tampering_revokes(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    binary = _plik_gui()  # no footer or capability declaration at all
    _trust_test_worker(binary, "0.5.10")
    assert _wgraj_wersje(client, csrf, "0.5.10", binary, trust_worker=False).status_code == 303
    with SessionLocal() as db:
        release = db.scalar(select(AgentRelease))
        assert release.arch == "x86_64" and release_trust.distributable(release)
        release_id, path = release.id, upgrades.sciezka_pliku(release)
    assert _oznacz_oficjalna(client, csrf, release_id).status_code == 200
    _, headers = windows_agent(client, tenant_a)
    assert client.get("/api/v1/agent/version", headers=headers).json()["available"] is True
    path.write_bytes(binary + b"modified")
    assert client.get("/api/v1/agent/version", headers=headers).json()["available"] is False
    assert client.get("/api/v1/agent/release", headers=headers).status_code == 404


def test_pin_must_match_version_arch_and_can_be_revoked(client, make_user):
    csrf = _superadmin(client, make_user)
    _trust_test_worker(PLIK_AGENTA, "0.5.10")
    assert _wgraj_wersje(client, csrf, "0.5.10", PLIK_AGENTA, trust_worker=False).status_code == 303
    with SessionLocal() as db:
        release = db.scalar(select(AgentRelease))
        assert release_trust.distributable(release)
        catalog = get_settings().trusted_windows_builds
        digest = hashlib.sha256(PLIK_AGENTA).hexdigest()
        for mismatch in ({"version": "0.5.9", "arch": "x86_64"}, {"version": "0.5.10", "arch": "aarch64"}):
            catalog[digest] = mismatch
            assert not release_trust.distributable(release)
        catalog.clear()
        assert not release_trust.distributable(release)
