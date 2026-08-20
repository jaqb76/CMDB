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

    # Ostatnia UDANA synchronizacja - to jest data pokazywana w oknie statusu.
    # Ustawiana wylacznie po potwierdzeniu przyjecia raportu przez serwer.
    last_sync_at: str = ""
    # Ostatnia proba, niezaleznie od wyniku - pozwala odroznic "cisza w eterze"
    # od "agent probuje, ale serwer odmawia".
    last_attempt_at: str = ""
    last_status: str = "never"      # never | ok | error | offline
    last_error: str = ""
    last_sync_changed: bool = False
    last_report_hash: str = ""

    @property
    def is_enrolled(self) -> bool:
        return bool(self.agent_token and self.asset_id)


def load_state(path: Path) -> AgentState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AgentState()
    except PermissionError:
        # Plik zawiera poswiadczenie maszyny, wiec czyta go tylko SYSTEM
        # i administratorzy. Dla pozostalych agent zachowuje sie jak
        # niezarejestrowany - stan i tak pokazuje im plik statusu.
        log.debug("brak dostepu do %s - biezacy uzytkownik nie odczyta stanu", path)
        return AgentState()
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("nie udalo sie odczytac stanu (%s) - traktuje jako brak rejestracji", exc)
        return AgentState()
    # Bierzemy tylko znane pola i tylko te obecne w pliku - reszta zostaje na
    # wartosciach domyslnych. Dzieki temu stan zapisany przez nowsza wersje
    # agenta nie wywraca starszej, a brakujace pole nie nadpisuje domyslnego
    # typu pustym stringiem.
    known = {name: raw[name] for name in AgentState.__dataclass_fields__ if name in raw}
    try:
        return AgentState(**known)
    except TypeError as exc:
        log.warning("niezgodny format stanu (%s) - traktuje jako brak rejestracji", exc)
        return AgentState()


class StateWriteError(RuntimeError):
    """Nie udalo sie zapisac stanu - zwykle brak uprawnien."""


def save_state(path: Path, state: AgentState) -> None:
    """Zapisuje stan atomowo, z dostepem tylko dla SYSTEM i administratorow.

    Katalog zawezamy PRZED zapisem, zeby plik tymczasowy z poswiadczeniem
    odziedziczyl restrykcyjne uprawnienia juz w chwili powstania - nie ma
    wtedy okna, w ktorym token lezy szeroko dostepny.

    Nie uzywamy tempfile.mkstemp: gdy proces nie ma prawa zapisu w katalogu,
    mkstemp na Windows nie zglasza bledu, tylko ponawia probe do 10 000 razy
    (os.access sprawdza atrybut "tylko do odczytu", a nie liste ACL). Agent
    sprawia wtedy wrazenie zawieszonego. Jedna proba przez os.open daje
    natychmiastowy, zrozumialy blad.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_directory(path.parent)

    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.unlink(missing_ok=True)
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except PermissionError as exc:
        raise StateWriteError(
            f"brak uprawnien do zapisu w {path.parent}. Katalog z poswiadczeniem "
            "agenta jest zastrzezony dla SYSTEM i administratorow - uruchom "
            "agenta jako administrator albo przez zadanie harmonogramu."
        ) from exc
    except OSError as exc:
        raise StateWriteError(
            f"nie moge utworzyc pliku tymczasowego w {path.parent}: {exc}"
        ) from exc

    try:
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


def harden_public_directory(path: Path) -> None:
    """Katalog na plik statusu - do odczytu takze dla zwyklych uzytkownikow.

    Agent chodzi jako SYSTEM, a ikona w zasobniku jako zalogowany uzytkownik.
    Zamiast rozluzniac dostep do katalogu z tokenem, publikujemy status
    w osobnym podkatalogu, w ktorym nie ma zadnych sekretow.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o755)
        return
    if sys.platform != "win32":  # pragma: no cover
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/grant:r", "*S-1-5-32-545:(OI)(CI)RX"],  # BUILTIN\Users
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - tylko Windows
        log.warning("nie udalo sie nadac praw odczytu do %s: %s", path, exc)


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
