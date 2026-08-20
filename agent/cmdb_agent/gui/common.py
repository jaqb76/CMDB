"""Wspolne funkcje warstwy graficznej: uprawnienia i uruchamianie procesow.

Podzial uprawnien:
  * ikona w zasobniku dziala jako zalogowany uzytkownik i tylko CZYTA status,
  * zapis konfiguracji i wymuszona synchronizacja wymagaja administratora,
    wiec uruchamiamy je jako osobny proces z podniesieniem uprawnien (UAC).

Dzieki temu nic, co chodzi caly czas na pulpicie, nie potrzebuje praw
administratora ani dostepu do pliku z tokenem.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..proces import srodowisko_dla_potomka

log = logging.getLogger(__name__)

TASK_NAME = "CMDB Agent"


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):  # pragma: no cover - tylko Windows
        return False


def agent_executable(gui: bool = False) -> tuple[str, list[str]]:
    """Zwraca (program, argumenty poprzedzajace) do uruchomienia agenta.

    gui=True wskazuje plik zawierajacy warstwe graficzna. Ma to znaczenie,
    bo cmdb-agent.exe budowany jest bez tkintera - chodzi jako SYSTEM na
    kazdej maszynie i nie ma powodu wozic ze soba bibliotek okienkowych.
    Okno ustawien otwiera wiec ten sam plik, ktory obsluguje ikone.
    """
    if getattr(sys, "frozen", False):
        if gui:
            return sys.executable, []
        candidate = Path(sys.executable).parent / ("cmdb-agent.exe" if is_windows() else "cmdb-agent")
        if candidate.is_file():
            return str(candidate), []
        return sys.executable, []
    return sys.executable, ["-m", "cmdb_agent.main"]


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows() else 0


def run_agent(
    args: list[str],
    config_path: Path | None = None,
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
):
    """Uruchamia agenta i czeka na wynik. Zwraca CompletedProcess.

    extra_env pozwala nadpisac ustawienia na czas jednego wywolania - okno
    ustawien korzysta z tego, zeby rejestracja odpowiadala szybko zamiast
    wyczerpywac pelny budzet ponowien przewidziany dla pracy w tle.
    """
    program, prefix = agent_executable()
    command = [program, *prefix]
    if config_path:
        command += ["--config", str(config_path)]
    command += args

    log.debug("uruchamiam: %s", command)
    return subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        creationflags=_no_window_flags(),
        # Ikona jest spakowana onefile - bez wyczyszczenia zmiennych _PYI_*
        # uruchamiany agent odmawia startu.
        env=srodowisko_dla_potomka(extra_env),
    )


def run_agent_elevated(
    args: list[str], config_path: Path | None = None, gui: bool = False
) -> bool:
    """Uruchamia agenta z podniesieniem uprawnien (okno UAC).

    Zwraca True, jesli uzytkownik zaakceptowal monit. Nie czekamy na wynik -
    ShellExecute nie daje uchwytu do procesu bez dodatkowej gimnastyki,
    a i tak odswiezamy status z pliku.
    """
    if not is_windows():  # pragma: no cover - podnoszenie uprawnien tylko na Windows
        result = run_agent(args, config_path)
        return result.returncode == 0

    program, prefix = agent_executable(gui=gui)
    parameters = list(prefix)
    if config_path:
        parameters += ["--config", str(config_path)]
    parameters += args
    quoted = " ".join(f'"{p}"' if " " in str(p) else str(p) for p in parameters)

    try:
        # Wartosci > 32 oznaczaja powodzenie; 5 (ACCESS_DENIED) = uzytkownik odmowil.
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, quoted, None, 1)
        return int(result) > 32
    except (AttributeError, OSError) as exc:  # pragma: no cover - tylko Windows
        log.error("nie udalo sie podniesc uprawnien: %s", exc)
        return False


def trigger_scheduled_task() -> bool:
    """Wymusza przebieg zadania harmonogramu (dziala jako SYSTEM).

    Instalator nadaje grupie Uzytkownicy prawo uruchomienia tego zadania,
    wiec zwykle nie trzeba podnosic uprawnien.
    """
    if not is_windows():
        return False
    try:
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", TASK_NAME],
            capture_output=True,
            timeout=30,
            creationflags=_no_window_flags(),
            env=srodowisko_dla_potomka(),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("nie udalo sie uruchomic zadania '%s': %s", TASK_NAME, exc)
        return False


def open_in_shell(path: Path) -> None:
    """Otwiera plik domyslnym programem systemu (dziennik agenta)."""
    try:
        if is_windows():
            os.startfile(str(path))  # noqa: S606 - celowo, to jest akcja uzytkownika
        elif sys.platform == "darwin":  # pragma: no cover
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except (OSError, AttributeError) as exc:
        log.warning("nie moge otworzyc %s: %s", path, exc)
