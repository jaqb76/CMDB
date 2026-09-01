"""Ktora wersja portalu wlasnie chodzi.

Pytanie "na czym pracujemy" pada przy kazdym zgloszeniu bledu, a odpowiedz
"chyba najnowsza" jest bezuzyteczna: ta sama strona wyglada tak samo przed
wdrozeniem i po nim. Numer w stopce zamienia to w fakt.

Zrodla, w kolejnosci wiarygodnosci:

1. ``CMDB_WERSJA`` ze srodowiska - wpisywana przy budowaniu obrazu, wiec
   opisuje dokladnie ten obraz, ktory dziala,
2. odczyt z repozytorium (``git describe``) - tylko w developmencie; obraz
   produkcyjny nie zawiera katalogu ``.git``,
3. wersja pakietu - ostatnia deska ratunku.

Nigdy nie zgadujemy. Gdy nie wiadomo, mowimy "nieznana" - falszywy numer
w stopce jest gorszy od jego braku, bo na jego podstawie ktos stwierdzi,
ze poprawka jest wdrozona.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__

NIEZNANA = "nieznana"


def _ze_srodowiska() -> str | None:
    wartosc = (os.environ.get("CMDB_WERSJA") or "").strip()
    return wartosc or None


def _z_repozytorium() -> str | None:
    korzen = Path(__file__).resolve().parents[2]
    if not (korzen / ".git").exists():
        return None
    # Skrot commita i jego data, a nie "git describe": najblizszym tagiem
    # w tym repozytorium jest wydanie AGENTA, wiec opis wygladalby jak numer
    # wersji agenta doklejony do portalu - i mylil przy kazdym zgloszeniu.
    try:
        wynik = subprocess.run(
            ["git", "log", "-1", "--format=%h (%cs)"],
            cwd=korzen, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    opis = wynik.stdout.strip()
    if wynik.returncode != 0 or not opis:
        return None
    brudne = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=korzen, capture_output=True, text=True, timeout=5, check=False,
    )
    # Niezacommitowane zmiany znacza, ze to, co chodzi, nie jest juz tym
    # commitem - stopka ma o tym mowic, a nie udawac czystego wydania.
    return opis + (" + zmiany lokalne" if brudne.stdout.strip() else "")


@lru_cache(maxsize=1)
def opis() -> str:
    """Napis do stopki. Liczony raz - w czasie zycia procesu sie nie zmienia."""
    return _ze_srodowiska() or _z_repozytorium() or __version__ or NIEZNANA
