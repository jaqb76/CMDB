"""Wykrywanie brakujacych aktualizacji na Linuksie.

Zainstalowane poprawki to tylko polowa obrazu. Do oceny bezpieczenstwa
potrzebne jest to, czego BRAKUJE - i to z rozroznieniem, ktore z brakow
sa poprawkami bezpieczenstwa.

Dwie zasady, ktore ksztaltuja caly ten modul:

1. Nigdy nie odswiezamy indeksu pakietow. "apt update" pobiera dane z sieci,
   bierze blokade i zmienia stan maszyny - agent ma maszyne inwentaryzowac,
   a nie modyfikowac. Czytamy to, co system juz wie.

2. Skoro nie odswiezamy indeksu, to jego wiek jest czescia wyniku. "Brak
   brakujacych aktualizacji" przy indeksie sprzed trzech miesiecy nie znaczy
   "maszyna aktualna" - znaczy "nie wiemy". Bez tej informacji raport
   uspokajalby zamiast ostrzegac.

Stad rozroznienie miedzy pusta lista brakow a stanem "nieznany": pierwsze
znaczy, ze sprawdzilismy i nic nie brakuje, drugie - ze sprawdzic sie nie
udalo. Zlanie tych dwoch przypadkow w jeden jest w narzedziu do oceny
bezpieczenstwa blednem, ktory kosztuje najwiecej.
"""
from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .common import CommandError, run_command_with_code as _uruchom

STATUS_OK = "ok"
STATUS_NIEZNANY = "nieznany"

# Katalogi, po ktorych poznajemy wiek indeksu pakietow.
ZNACZNIKI_APT = (
    "/var/lib/apt/periodic/update-success-stamp",
    "/var/cache/apt/pkgcache.bin",
    "/var/lib/apt/lists",
)
ZNACZNIKI_DNF = (
    "/var/cache/dnf",
    "/var/cache/yum",
)

# Linia symulacji apt-get:
#   Inst libssl3 [3.0.2-0ubuntu1.10] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
WZORZEC_APT = re.compile(
    r"^Inst\s+(?P<pakiet>\S+)"
    r"(?:\s+\[(?P<obecna>[^\]]+)\])?"
    r"\s+\((?P<nowa>\S+)\s+(?P<zrodlo>.+?)\)\s*$"
)


def _teraz() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wynik(
    source: str,
    status: str = STATUS_OK,
    detail: str | None = None,
    entries: list | None = None,
    index_age_hours: float | None = None,
) -> dict:
    """Jednolita postac wyniku, niezalezna od menedzera pakietow."""
    pozycje = entries or []
    return {
        "status": status,
        "detail": detail,
        "source": source,
        "checked_at": _teraz(),
        "index_age_hours": index_age_hours,
        "count": len(pozycje) if status == STATUS_OK else None,
        "security_count": (
            sum(1 for p in pozycje if p.get("security")) if status == STATUS_OK else None
        ),
        "entries": pozycje,
    }


def wiek_indeksu(sciezki: tuple[str, ...]) -> float | None:
    """Ile godzin temu ostatnio odswiezono indeks pakietow."""
    najnowszy = None
    for sciezka in sciezki:
        plik = Path(sciezka)
        try:
            if plik.is_dir():
                czasy = [wpis.stat().st_mtime for wpis in plik.iterdir()]
                if not czasy:
                    continue
                znacznik = max(czasy)
            elif plik.exists():
                znacznik = plik.stat().st_mtime
            else:
                continue
        except OSError:
            continue
        najnowszy = znacznik if najnowszy is None else max(najnowszy, znacznik)

    if najnowszy is None:
        return None
    return round(max(0.0, (time.time() - najnowszy) / 3600), 1)


def parsuj_apt(wyjscie: str) -> list[dict]:
    """Braki z symulacji "apt-get dist-upgrade".

    Poprawki bezpieczenstwa poznajemy po zrodle: Debian i Ubuntu wydaja je
    z osobnej kieszeni, ktorej nazwa konczy sie na "-security".
    """
    braki = []
    for linia in wyjscie.splitlines():
        dopasowanie = WZORZEC_APT.match(linia.strip())
        if dopasowanie is None:
            continue
        zrodlo = dopasowanie.group("zrodlo")
        # Ostatni czlon to architektura w nawiasach kwadratowych - odcinamy.
        zrodlo = re.sub(r"\s*\[[^\]]*\]\s*$", "", zrodlo).strip()
        braki.append(
            {
                "id": dopasowanie.group("pakiet"),
                "title": dopasowanie.group("pakiet"),
                "current_version": dopasowanie.group("obecna"),
                "new_version": dopasowanie.group("nowa"),
                "source_repo": zrodlo,
                "security": "-security" in zrodlo.lower(),
                "severity": None,
            }
        )
    return braki


def parsuj_dnf(wyjscie: str, bezpieczenstwa: set[str] | None = None) -> list[dict]:
    """Braki z "dnf check-update".

    Format to trzy kolumny: nazwa.architektura, wersja, repozytorium.
    Naglowki i puste linie pomijamy, tak samo sekcje "Obsoleting Packages".
    """
    braki = []
    bezpieczenstwa = bezpieczenstwa or set()
    for linia in wyjscie.splitlines():
        surowa = linia.rstrip()
        if not surowa or surowa.startswith((" ", "\t")):
            continue
        if surowa.lower().startswith(("last metadata", "obsoleting", "security:")):
            continue
        czesci = surowa.split()
        if len(czesci) < 3:
            continue
        nazwa, wersja, repo = czesci[0], czesci[1], czesci[2]
        if "." not in nazwa:
            continue
        pakiet = nazwa.rsplit(".", 1)[0]
        braki.append(
            {
                "id": pakiet,
                "title": pakiet,
                "current_version": None,
                "new_version": wersja,
                "source_repo": repo,
                "security": pakiet in bezpieczenstwa or "security" in repo.lower(),
                "severity": None,
            }
        )
    return braki


def braki_apt(timeout: int = 120) -> dict:
    """Brakujace aktualizacje w Debianie i Ubuntu."""
    if not shutil.which("apt-get"):
        return wynik("apt", STATUS_NIEZNANY, "brak apt-get")

    wiek = wiek_indeksu(ZNACZNIKI_APT)
    try:
        # -s symuluje, NoLocking pozwala dzialac bez blokady - nie wchodzimy
        # w droge aktualizacjom uruchomionym przez administratora.
        wyjscie, kod = _uruchom(
            [
                "apt-get", "-s", "-q",
                "-o", "Debug::NoLocking=1",
                "-o", "APT::Get::Show-User-Simulation-Note=0",
                "dist-upgrade",
            ],
            timeout=timeout,
        )
    except (CommandError, OSError) as exc:
        return wynik("apt", STATUS_NIEZNANY, f"apt-get nie odpowiedzial: {exc}", index_age_hours=wiek)

    if kod != 0:
        return wynik("apt", STATUS_NIEZNANY, f"apt-get zakonczyl sie kodem {kod}", index_age_hours=wiek)

    return wynik("apt", STATUS_OK, entries=parsuj_apt(wyjscie), index_age_hours=wiek)


def braki_dnf(timeout: int = 180) -> dict:
    """Brakujace aktualizacje w RHEL, Fedorze i pochodnych."""
    narzedzie = "dnf" if shutil.which("dnf") else ("yum" if shutil.which("yum") else None)
    if narzedzie is None:
        return wynik("dnf", STATUS_NIEZNANY, "brak dnf i yum")

    wiek = wiek_indeksu(ZNACZNIKI_DNF)
    try:
        # -C uzywa wylacznie danych z cache - bez ruchu sieciowego.
        wyjscie, kod = _uruchom([narzedzie, "-q", "-C", "check-update"], timeout=timeout)
    except (CommandError, OSError) as exc:
        return wynik(narzedzie, STATUS_NIEZNANY, f"{narzedzie} nie odpowiedzial: {exc}",
                     index_age_hours=wiek)

    # 0 = nic do aktualizacji, 100 = sa aktualizacje. Reszta to blad.
    if kod not in (0, 100):
        return wynik(narzedzie, STATUS_NIEZNANY, f"{narzedzie} zakonczyl sie kodem {kod}",
                     index_age_hours=wiek)
    if kod == 0:
        return wynik(narzedzie, STATUS_OK, entries=[], index_age_hours=wiek)

    bezpieczenstwa: set[str] = set()
    try:
        wyjscie_sec, kod_sec = _uruchom(
            [narzedzie, "-q", "-C", "check-update", "--security"], timeout=timeout
        )
        if kod_sec in (0, 100):
            bezpieczenstwa = {p["id"] for p in parsuj_dnf(wyjscie_sec)}
    except (CommandError, OSError):
        # Lista ogolna zostaje - brak podzialu na bezpieczenstwo nie jest
        # powodem, zeby wyrzucic caly wynik.
        pass

    return wynik(narzedzie, STATUS_OK, entries=parsuj_dnf(wyjscie, bezpieczenstwa),
                 index_age_hours=wiek)


def braki_linux(timeout: int = 180) -> dict:
    """Brakujace aktualizacje - dobiera narzedzie do dystrybucji."""
    if shutil.which("apt-get"):
        return braki_apt(timeout=timeout)
    if shutil.which("dnf") or shutil.which("yum"):
        return braki_dnf(timeout=timeout)
    return wynik(
        "nieznany",
        STATUS_NIEZNANY,
        "nie rozpoznano menedzera pakietow (obslugiwane: apt, dnf, yum)",
    )
