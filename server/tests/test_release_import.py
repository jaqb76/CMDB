import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import Request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from sqlalchemy import select, func

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import AgentRelease, GlobalAgentTarget, TenantAgentTarget, ReleaseProvenance, ReleaseImportStatus
from cmdb_server.release_manifest import sign, verify, key_id, ManifestError
from cmdb_server.services import release_import, release_trust
from .test_admin import _superadmin, _wgraj_wersje, _oznacz_oficjalna
from .test_pobieranie import _plik_gui


@pytest.fixture
def signed_release(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "release_dir", str(tmp_path / "releases"))
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    monkeypatch.setattr(settings, "release_public_keys", {key_id(public): base64.b64encode(public).decode()})
    version = "0.6.42+1"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in {"cmdb-agent/cmdb_agent/__init__.py": f'__version__ = "{version}"\n'.encode(),
                              "cmdb-agent/packaging/install-agent.sh": b"#!/bin/sh\n"}.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    contents = {"worker": _plik_gui(), "setup": _plik_gui() + b"setup", "source": stream.getvalue()}
    names = {"worker": "cmdb-agent.exe", "setup": f"CMDB-Agent-Setup-{version}.exe", "source": "cmdb-agent-zrodla.tar.gz"}
    payload = {"schema": 2, "repository": settings.release_repository, "ref": settings.release_ref,
        "workflow": ".github/workflows/cmdb-tests.yml", "commit": "a" * 40, "run_id": 123,
        "version": version, "tag": "agent-v" + version,
        "changelog": [{"category": "fixed", "text": "Poprawiono uruchamianie agenta w zasobniku systemowym."}],
        "artifacts": [{"kind": kind, "os": "linux" if kind == "source" else "windows",
            "arch": "zrodla" if kind == "source" else "x86_64", "name": names[kind],
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "protocol": "cmdb-policy-v1"}
            for kind, content in contents.items()]}
    envelope = sign(payload, base64.b64encode(key.private_bytes_raw()).decode())
    blobs = {1: json.dumps(envelope).encode(), 2: contents["worker"], 3: contents["setup"], 4: contents["source"]}
    row = {"tag_name": payload["tag"], "draft": False, "prerelease": False,
           "assets": [{"id": index, "name": name} for index, name in enumerate(
               ["cmdb-release.json", names["worker"], names["setup"], names["source"]], 1)]}
    class FakeGithub:
        deadline = float("inf")
        def releases(self, page):
            return [row] if page == 1 else []
        def asset(self, identifier, limit, destination=None):
            data = blobs[identifier]
            if len(data) > limit:
                raise release_import.ImportFailure("limit")
            if destination is None:
                return data
            destination.write(data)
    return settings, row, FakeGithub(), blobs, payload, envelope


def test_import_is_atomic_idempotent_and_does_not_select_any_target(signed_release):
    settings, row, github, _, _, _ = signed_release
    with SessionLocal() as db:
        assert release_import.import_release(db, row, github, settings) == 2
        assert release_import.import_release(db, row, github, settings) == 0
        releases = db.scalars(select(AgentRelease)).all()
        assert len(releases) == 2 and all(release_trust.distributable(r) for r in releases)
        assert db.scalar(select(func.count(GlobalAgentTarget.id))) == 0
        assert db.scalar(select(func.count(TenantAgentTarget.id))) == 0
        worker = next(r for r in releases if r.os_family == "windows")
        assert worker.provenance.setup_storage_name
        assert worker.changelog == payload["changelog"]


@pytest.mark.parametrize("fault", ["bytes", "signature", "tag", "missing", "unknown_key", "oversize"])
def test_invalid_release_never_enters_catalog(signed_release, fault):
    settings, row, github, blobs, _, _ = signed_release
    if fault == "bytes":
        blobs[2] = b"X" + blobs[2][1:]
    elif fault == "signature":
        envelope = json.loads(blobs[1]); envelope["signature"] = base64.b64encode(b"x" * 64).decode()
        blobs[1] = json.dumps(envelope).encode()
    elif fault == "tag":
        row["tag_name"] = "agent-v0.6.43+1"
    elif fault == "missing":
        row["assets"] = row["assets"][:-1]
    elif fault == "unknown_key":
        settings.release_public_keys = {}
    else:
        blobs[2] += b"too large"
    with SessionLocal() as db:
        with pytest.raises((ManifestError, release_import.ImportFailure)):
            release_import.import_release(db, row, github, settings)
        assert db.scalar(select(func.count(AgentRelease.id))) == 0
        assert db.scalar(select(func.count(ReleaseProvenance.id))) == 0
    assert not list(Path(settings.release_dir).glob("*.exe"))


def test_modified_manifest_wrong_repository_and_key_revocation(signed_release):
    settings, row, github, _, payload, envelope = signed_release
    assert verify(envelope, settings.release_public_keys, settings.release_repository, settings.release_ref) == payload
    with pytest.raises(ManifestError):
        verify(envelope, settings.release_public_keys, "another/repo", settings.release_ref)
    modified = deepcopy(envelope)
    payload["version"] = "0.6.99+1"
    modified["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ManifestError):
        verify(modified, settings.release_public_keys, settings.release_repository, settings.release_ref)
    with SessionLocal() as db:
        release_import.import_release(db, row, github, settings)
        releases = db.scalars(select(AgentRelease)).all()
        settings.release_public_keys = {}
        assert all(not release_trust.distributable(r) for r in releases)


def test_deleted_release_is_not_resurrected(signed_release):
    settings, row, github, _, _, _ = signed_release
    with SessionLocal() as db:
        release_import.import_release(db, row, github, settings)
        for release in db.scalars(select(AgentRelease)).all():
            db.delete(release)
        db.commit()
        assert release_import.import_release(db, row, github, settings) == 0
        assert db.scalar(select(func.count(AgentRelease.id))) == 0


def test_tampered_files_and_evidence_block_distribution(signed_release):
    settings, row, github, _, _, _ = signed_release
    with SessionLocal() as db:
        release_import.import_release(db, row, github, settings)
        release = db.scalar(select(AgentRelease).where(AgentRelease.os_family == "windows"))
        path = Path(settings.release_dir) / release.storage_name
        path.write_bytes(path.read_bytes() + b"tampered")
        assert not release_trust.distributable(release)


def test_import_status_and_existing_rollout_controls(client, signed_release, make_user, monkeypatch):
    settings, row, github, _, _, _ = signed_release
    csrf = _superadmin(client, make_user)
    monkeypatch.setattr(settings, "release_import_enabled", True)
    monkeypatch.setattr(release_import, "Github", lambda _: github)
    assert release_import.sync_once() == {"imported": 2, "errors": []}
    assert release_import.sync_once() is None  # throttled across workers by DB state
    page = client.get("/admin/wersje")
    assert page.status_code == 200 and "Automatyczny katalog" in page.text
    assert "Wgraj nowa wersje" not in page.text
    assert "Paczki Linux pochodzą" in page.text
    assert "Serwer nie zbudowal paczki" not in page.text
    assert "Serwer zbudowal paczke w wersji" not in page.text
    assert _wgraj_wersje(client, csrf, trust_worker=False).status_code == 403
    with SessionLocal() as db:
        release = db.scalar(select(AgentRelease).where(AgentRelease.os_family == "windows"))
        release_id = release.id
        assert db.get(ReleaseImportStatus, 1).state == "ok"
    assert client.get(f"/admin/releases/{release_id}/setup").status_code == 200
    assert _oznacz_oficjalna(client, csrf, release_id).status_code == 200
    client.cookies.clear()
    assert client.get(f"/admin/releases/{release_id}/setup", follow_redirects=False).status_code in (303, 403)


def test_redirect_never_forwards_repository_token():
    request = Request("https://api.github.com/repos/owner/repo/releases/assets/1",
                      headers={"Authorization": "Bearer SECRET"})
    handler = release_import.SafeRedirect()
    redirected = handler.redirect_request(request, None, 302, "found", {},
        "https://release-assets.githubusercontent.com/file?signature=example")
    assert not redirected.has_header("Authorization")
    for target in ("http://api.github.com/file", "https://evil.example/file", "https://user@api.github.com/file"):
        with pytest.raises(release_import.ImportFailure):
            handler.redirect_request(request, None, 302, "found", {}, target)
