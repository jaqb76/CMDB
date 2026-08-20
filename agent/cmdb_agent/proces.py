"""Uruchamianie procesow potomnych z poziomu spakowanego agenta.

Program spakowany PyInstallerem w trybie onefile ustawia sobie zmienne
srodowiskowe _PYI_*, ktore opisuja jego wlasne archiwum i katalog rozpakowania.
Proces potomny dziedziczy je automatycznie - i jesli tez jest programem
spakowanym onefile, jego bootloader wykrywa niezgodnosc i odmawia startu:

    [PYI-xxxxx:ERROR] Security validation failure:
    parent process has different executable!

Dotyczy to kazdego miejsca, w ktorym jeden nasz plik .exe uruchamia drugi:
sprawdzenia nowej wersji przy aktualizacji, wymuszenia synchronizacji z ikony
w zasobniku i otwierania okna ustawien. Dlatego srodowisko czyscimy w jednym
miejscu, a nie w kazdym z osobna.
"""
from __future__ import annotations

import os
import subprocess

# Zmienne ustawiane przez bootloader PyInstallera (wersja 6.x).
PREFIKS_PYINSTALLERA = "_PYI_"
DODATKOWE_ZMIENNE = ("_MEIPASS2",)  # starsze wydania PyInstallera


def srodowisko_dla_potomka(dodatkowe: dict[str, str] | None = None) -> dict[str, str]:
    """Kopia srodowiska bez zmiennych PyInstallera."""
    srodowisko = {
        klucz: wartosc
        for klucz, wartosc in os.environ.items()
        if not klucz.startswith(PREFIKS_PYINSTALLERA) and klucz not in DODATKOWE_ZMIENNE
    }
    if dodatkowe:
        srodowisko.update(dodatkowe)
    return srodowisko


def flagi_bez_okna() -> int:
    """Bez migajacego okna konsoli, gdy agent chodzi w tle."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def uruchom(
    polecenie: list[str],
    timeout: int = 300,
    dodatkowe_zmienne: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Uruchamia proces potomny i czeka na wynik."""
    return subprocess.run(
        polecenie,
        capture_output=True,
        timeout=timeout,
        creationflags=flagi_bez_okna(),
        env=srodowisko_dla_potomka(dodatkowe_zmienne),
    )
