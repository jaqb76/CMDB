"""Pull signed GitHub releases into the existing catalog, never set targets."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from sqlalchemy import select, text

from ..config import get_settings
from ..db import SessionLocal, engine
from ..models import AgentRelease, ReleaseProvenance, ReleaseImportStatus, as_utc, utcnow
from ..release_manifest import verify, ManifestError, MAX_MANIFEST
from . import architektura, pakiet
from .scoping import audit

log = logging.getLogger(__name__)
LOCK = 0x434D4255
DOWNLOAD_HOSTS = {"api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class ImportFailure(ValueError):
    pass


def safe_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS or
            parts.username or parts.password or parts.port not in (None, 443)):
        raise ImportFailure("Niedozwolony adres pobierania wydania")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result is not None:
            # urllib normally copies auth headers across hosts. Do not leak the
            # read-only repository token to a CDN or pre-signed storage URL.
            result.remove_header("Authorization")
        return result


class Github:
    def __init__(self, settings, deadline=None):
        self.repo = settings.release_repository
        self.token = settings.release_github_token.get_secret_value()
        self.opener = build_opener(SafeRedirect())
        self.deadline = deadline or time.monotonic() + 180

    def _read(self, path, limit, destination=None, binary=False):
        url = "https://api.github.com/repos/" + self.repo + path
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "CMDB-release-import", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data, size = bytearray(), 0
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ImportFailure("Limit czasu importu; kolejna proba w nastepnym cyklu")
            with self.opener.open(Request(url, headers=headers), timeout=min(20, remaining)) as response:
                while True:
                    if time.monotonic() >= self.deadline:
                        raise ImportFailure("Limit czasu importu")
                    block = response.read(min(65536, limit + 1 - size))
                    if not block:
                        break
                    size += len(block)
                    if size > limit:
                        raise ImportFailure("Plik wydania przekracza dozwolony rozmiar")
                    if destination is None:
                        data.extend(block)
                    else:
                        destination.write(block)
        except HTTPError as exc:
            # No token, signed URL or response body in logs or the admin panel.
            raise ImportFailure(f"GitHub HTTP {exc.code}; sprawdz dostep i limit API") from None
        except (URLError, TimeoutError, OSError):
            raise ImportFailure("Blad polaczenia TLS/sieci podczas importu") from None
        return bytes(data)

    def releases(self, page):
        rows = json.loads(self._read(f"/releases?per_page=100&page={page}", 4 * 1024 * 1024))
        if not isinstance(rows, list) or len(rows) > 100:
            raise ImportFailure("Niepoprawna lista wydan GitHub")
        return rows

    def asset(self, identifier, limit, destination=None):
        if type(identifier) is not int or identifier <= 0:
            raise ImportFailure("Niepoprawny identyfikator pliku GitHub")
        return self._read(f"/releases/assets/{identifier}", limit, destination, binary=True)


def check_bytes(path, artifact):
    if path.stat().st_size != artifact["size"]:
        raise ImportFailure("Rozmiar pliku nie zgadza sie z podpisanym manifestem")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != artifact["sha256"]:
        raise ImportFailure("SHA-256 pliku nie zgadza sie z podpisanym manifestem")


def import_release(db, release, github, settings):
    if release.get("draft") or release.get("prerelease") or not str(release.get("tag_name", "")).startswith("agent-v"):
        return 0
    tag = release["tag_name"]
    existing = db.scalars(select(ReleaseProvenance).where(
        ReleaseProvenance.repository == settings.release_repository, ReleaseProvenance.tag == tag)).all()
    if existing:
        # Includes user-deleted releases (release_id=NULL): never resurrect them.
        return 0
    assets = release.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 30:
        raise ImportFailure("Niepoprawna lista plikow wydania")
    by_name = {}
    for asset in assets:
        if asset["name"] in by_name:
            raise ImportFailure("Powtorzona nazwa pliku wydania")
        by_name[asset["name"]] = asset
    if "cmdb-release.json" not in by_name:
        return 0  # unrelated/legacy release, never a trusted worker
    envelope = json.loads(github.asset(by_name["cmdb-release.json"]["id"], MAX_MANIFEST))
    manifest = verify(envelope, settings.release_public_keys, settings.release_repository, settings.release_ref)
    if manifest["tag"] != tag:
        raise ImportFailure("Tag nie odpowiada podpisanemu manifestowi")
    root = Path(settings.release_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as temporary:
            paths = {}
            for artifact in manifest["artifacts"]:
                if artifact["name"] not in by_name or artifact["size"] > settings.max_release_bytes:
                    raise ImportFailure("Brak pliku wydania lub przekroczony limit")
                path = Path(temporary) / artifact["name"]  # names strictly validated by signed protocol
                with path.open("xb") as stream:
                    github.asset(by_name[artifact["name"]]["id"], artifact["size"], stream)
                check_bytes(path, artifact)
                if artifact["kind"] == "worker" and (
                        architektura.wykryj_z_pliku(path) != artifact["arch"] or
                        architektura.podsystem_pe(path) != architektura.PE_GUI):
                    raise ImportFailure("Niepoprawny format polaczonego workera Windows")
                if artifact["kind"] == "source" and pakiet.sprawdz_paczke(path) != manifest["version"]:
                    raise ImportFailure("Wersja paczki Linux nie zgadza sie z manifestem")
                if artifact["kind"] == "setup" and path.read_bytes()[:2] != b"MZ":
                    raise ImportFailure("Niepoprawny format instalatora")
                paths[artifact["kind"]] = (path, artifact)
            for kind in ("worker", "source"):
                _, artifact = paths[kind]
                collision = db.scalar(select(AgentRelease).where(AgentRelease.version == manifest["version"],
                    AgentRelease.os_family == artifact["os"], AgentRelease.arch == artifact["arch"]))
                if collision is not None:
                    raise ImportFailure("Kolizja wersji; import nigdy nie nadpisuje istniejacego wydania")
            stored = {}
            for kind, (path, artifact) in paths.items():
                suffix = ".tar.gz" if kind == "source" else ".exe"
                target = root / (str(uuid4()) + suffix)
                path.rename(target)
                moved.append(target)
                stored[kind] = target.name
            for kind in ("worker", "source"):
                _, artifact = paths[kind]
                row = AgentRelease(version=manifest["version"], os_family=artifact["os"], arch=artifact["arch"],
                    filename=artifact["name"], storage_name=stored[kind], sha256=artifact["sha256"],
                    size_bytes=artifact["size"], created_by="GitHub CI", notes="CI: " + manifest["commit"])
                db.add(row)
                db.flush()
                db.add(ReleaseProvenance(release_id=row.id, repository=settings.release_repository,
                    tag=tag, kind=kind, envelope=envelope,
                    setup_storage_name=stored["setup"] if kind == "worker" else None))
            audit(db, None, action="release.imported", target=tag,
                  detail={"repository": settings.release_repository, "commit": manifest["commit"],
                          "run_id": manifest["run_id"]}, actor="release-import")
            # No GlobalAgentTarget, TenantAgentTarget or Asset writes here.
            db.commit()
        return 2
    except Exception:
        db.rollback()
        for path in moved:
            path.unlink(missing_ok=True)
        raise


def sync_once():
    settings = get_settings()
    if not settings.release_import_enabled:
        return None
    with engine.connect() as connection:
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK}).scalar()
        connection.commit()
        if not acquired:
            return None
        try:
            with SessionLocal() as db:
                state = db.get(ReleaseImportStatus, 1)
                if state is None:
                    state = ReleaseImportStatus(id=1, next_page=2)
                    db.add(state)
                now = utcnow()
                if state.last_attempt and (now - as_utc(state.last_attempt)).total_seconds() < settings.release_import_interval:
                    return None
                state.last_attempt, state.state, state.detail = now, "running", None
                db.commit()
                github, count, errors = Github(settings), 0, []
                # Always inspect new releases first. Continue historical backfill
                # over later cycles so a long outage doesn't silently lose builds.
                for page in (1, max(2, state.next_page)):
                    try:
                        releases = github.releases(page)
                        complete = True
                        for release in releases:
                            if time.monotonic() >= github.deadline:
                                complete = False
                                errors.append("Limit czasu; pozostale wydania w nastepnym cyklu")
                                break
                            try:
                                count += import_release(db, release, github, settings)
                            except (ImportFailure, ManifestError, pakiet.BrakZrodel) as exc:
                                errors.append(str(exc)[:180])
                            except Exception:
                                db.rollback()
                                errors.append("Niepoprawne dane wydania lub blad zapisu; sprawdz konfiguracje")
                        if page > 1 and complete:
                            state.next_page = 2 if len(releases) < 100 else page + 1
                        if page == 1 and len(releases) < 100:
                            state.next_page = 2
                            break
                    except Exception as exc:
                        errors.append(str(exc)[:180] if isinstance(exc, ImportFailure) else "Blad pobierania listy wydan")
                        break
                state.state = "error" if errors else "ok"
                state.detail = "; ".join(errors[:3]) if errors else f"Dodano wydan: {count}. Przypisania maszyn bez zmian."
                if not errors:
                    state.last_success = utcnow()
                db.commit()
                return {"imported": count, "errors": errors[:3]}
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK})
            connection.commit()


async def loop():
    if not get_settings().release_import_enabled:
        return
    while True:
        try:
            result = await asyncio.to_thread(sync_once)
            if result and result["errors"]:
                log.warning("import wydań: %s", result["errors"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Import wydań przerwany; ponowienie w następnym cyklu")
        await asyncio.sleep(get_settings().release_import_interval)
