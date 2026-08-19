"""Trwaly stan agenta - przede wszystkim jego wlasne poswiadczenie.

Plik stanu zawiera sekret, wiec zapisujemy go z restrykcyjnymi uprawnieniami:
  * POSIX  - chmod 0600 jeszcze przed zapisem tresci,
  * Windows - icacls: tylko SYSTEM i Administratorzy (dziedziczenie wylaczone).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class AgentState:
    agent_token: str = ""
    asset_id: str = ""
    tenant_slug: str = ""
    machine_id: str = ""
    server_url: str = ""
    enrolled_at: str = ""
    last_report_at: str = ""
    last_report_hash: str = ""

    @property
    def is_enrolled(self) -> bool:
        return bool(self.agent_token and self.asset_id)


def load_state(path: Path) -> AgentState:
    if not path.is_file():
        return AgentState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("nie udalo sie odczytac stanu (%s) - traktuje jako brak rejestracji", exc)
        return AgentState()
    known = {f: raw.get(f, "") for f in AgentState.__dataclass_fields__}
    return AgentState(**known)


def save_state(path: Path, state: AgentState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_directory(path.parent)

    # Zapis atomowy: plik tymczasowy w tym samym katalogu + rename.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".agent-state-")
    tmp_path = Path(tmp_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    _harden_file(path)


def _harden_file(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600)
        return
    if sys.platform == "win32":
        _icacls(path)


def _harden_directory(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o700)
        return
    if sys.platform == "win32":
        _icacls(path)


def _icacls(path: Path) -> None:
    """Zdejmuje dziedziczenie i zostawia dostep tylko SYSTEM + Administratorzy."""
    try:
        subprocess.run(
            [
                "icacls", str(path), "/inheritance:r",
                "/grant:r", "*S-1-5-18:(OI)(CI)F",   # NT AUTHORITY\\SYSTEM
                "/grant:r", "*S-1-5-32-544:(OI)(CI)F",  # BUILTIN\\Administrators
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - tylko Windows
        log.warning("nie udalo sie zawezic uprawnien do %s: %s", path, exc)
