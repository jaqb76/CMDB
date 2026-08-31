"""Windows worker admission: exact tested bytes, not self-declared capabilities.

The configured digest catalog is an out-of-band trust root. Upload permission
does not grant permission to modify it. No uploaded executable is run here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import get_settings
from ..release_manifest import verify, ManifestError
from . import architektura


def trusted_build(digest: str, version: str, arch: str) -> bool:
    expected = get_settings().trusted_windows_builds.get(digest)
    return expected == {"version": version, "arch": arch}


def distributable(release) -> bool:
    provenance = getattr(release, "provenance", None)
    if provenance is not None:
        if verified_artifact(release) is None:
            return False
    elif (release.os_family or "").lower() != "windows":
        return True
    elif release.arch == architektura.ARCH_TRAY:
        return False
    elif not trusted_build(release.sha256, release.version, release.arch):
        return False
    # Recheck bytes at activation AND offer/download, including old database rows.
    root = Path(get_settings().release_dir).resolve()
    path = root / release.storage_name
    try:
        if path.resolve().parent != root or not path.is_file():
            return False
        if release.os_family == "windows":
            if architektura.wykryj_z_pliku(path) != release.arch:
                return False
            if architektura.podsystem_pe(path) not in (architektura.PE_GUI, architektura.PE_KONSOLA):
                return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == release.sha256
    except OSError:
        return False


def verified_artifact(release, kind=None):
    evidence = getattr(release, "provenance", None)
    if evidence is None:
        return None
    settings = get_settings()
    try:
        manifest = verify(evidence.envelope, settings.release_public_keys,
                          settings.release_repository, settings.release_ref)
        if (manifest["version"] != release.version or manifest["tag"] != evidence.tag or
                manifest["repository"] != evidence.repository):
            return None
        bound = next(a for a in manifest["artifacts"] if a["kind"] == evidence.kind)
        if (bound["sha256"] != release.sha256 or bound["os"] != release.os_family or
                bound["arch"] != release.arch or bound["size"] != release.size_bytes):
            return None
        return next(a for a in manifest["artifacts"] if a["kind"] == (kind or evidence.kind))
    except (ManifestError, StopIteration, TypeError, KeyError):
        return None
