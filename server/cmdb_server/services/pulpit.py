"""Dane dla pulpitu: udzialy do pierscieni, wersje agentow, statusy wierszy.

Wszystkie wykresy rysuje serwer jako SVG - polityka bezpieczenstwa nie
dopuszcza skryptow wpisanych w strone ani atrybutu style="", wiec szerokosci
i wysokosci slupkow ida w atrybutach SVG, nie w CSS.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import LIFECYCLE_AKTYWNY, ZRODLO_AGENT, Asset, AssetCurrentReport, utcnow
from . import cve, wykresy

# Rodziny systemow, ktore agent zglasza, w zapisie znanym ludziom.
NAZWY_SYSTEMOW = {
    "windows": "Windows",
    "linux": "Linux",
    "macos": "macOS",
    "darwin": "macOS",
}

# Ile pozycji pokazuje legenda, zanim reszta trafi do "Inne".
POZYCJI_W_LEGENDZIE = 4
# Ile najnowszych wersji agenta ma wlasny wiersz; starsze sa razem.
WERSJI_AGENTA = 4
# Ile maszyn pokazuje lista najbardziej podatnych.
NAJBARDZIEJ_PODATNYCH = 10
# Jak dlugo pulpit pokazuje policzone juz podatnosci. Zestawienie pakietow
# z kanalem kazdej maszyny to najciezsza rzecz na tej stronie, a kanaly
# odswiezaja sie co kilka godzin - liczenie przy kazdym wejsciu nic nie daje.
WAZNOSC_PODATNOSCI = timedelta(minutes=10)


def _udzialy(pozycje: list[tuple[str | None, str, int]], inne: str) -> dict:
    """Pierscien z legenda. Pozycja to (klucz filtra, etykieta, liczba).

    Nadmiar laczymy w "Inne" sami, a nie w wykresy.pierscien, bo legenda
    pulpitu prowadzi do filtra listy - a laczona pozycja filtra nie ma.
    """
    pozycje = sorted((p for p in pozycje if p[2]), key=lambda p: p[2], reverse=True)
    if len(pozycje) > POZYCJI_W_LEGENDZIE + 1:
        reszta = sum(p[2] for p in pozycje[POZYCJI_W_LEGENDZIE:])
        pozycje = pozycje[:POZYCJI_W_LEGENDZIE] + [(None, inne, reszta)]
    dane = wykresy.pierscien([(etykieta, ile) for _, etykieta, ile in pozycje])
    for segment, (klucz, _, _) in zip(dane["segmenty"], pozycje):
        segment["klucz"] = klucz
        segment["procent"] = round(segment["udzial"])
    return dane


def rodzaje_sprzetu(by_typ, etykiety: dict[str, str]) -> dict:
    return _udzialy(
        [(typ or "", etykiety.get(typ, typ or "nieokreślony"), ile) for typ, ile in by_typ],
        "Inne",
    )


def nazwa_systemu(rodzina: str | None) -> str:
    if not rodzina:
        return "nieznany"
    return NAZWY_SYSTEMOW.get(rodzina.lower(), rodzina)


def systemy(by_os) -> dict:
    return _udzialy([(rodzina or "", nazwa_systemu(rodzina), ile) for rodzina, ile in by_os], "Inne")


def _klucz_wersji(wersja: str) -> tuple:
    """11.10.0 jest nowsze od 11.9.2 - porownanie tekstu twierdzi inaczej."""
    return tuple(int(czesc) for czesc in re.findall(r"\d+", wersja))


def wersje_agentow(db: Session, tenant_id: str) -> dict:
    wiersze = db.execute(
        select(Asset.agent_version, func.count(Asset.id))
        .where(Asset.tenant_id == tenant_id, Asset.zrodlo == ZRODLO_AGENT,
               Asset.agent_version.is_not(None))
        .group_by(Asset.agent_version)
    ).all()
    wiersze = sorted(wiersze, key=lambda w: _klucz_wersji(w[0]), reverse=True)
    pozycje = [{"etykieta": wersja, "ile": ile} for wersja, ile in wiersze[:WERSJI_AGENTA]]
    if len(wiersze) > WERSJI_AGENTA:
        pozycje.append({"etykieta": "Starsze", "ile": sum(ile for _, ile in wiersze[WERSJI_AGENTA:])})
    suma = sum(p["ile"] for p in pozycje)
    najwiecej = max((p["ile"] for p in pozycje), default=0)
    for numer, p in enumerate(pozycje):
        p["procent"] = round(p["ile"] * 100 / suma) if suma else 0
        p["barwa"] = wykresy.PALETA[numer % len(wykresy.PALETA)]
        # Pasek w poziomie: dlugosc w jednostkach viewBox 0..100.
        p["szerokosc"] = round(p["ile"] * 100 / najwiecej, 2) if najwiecej else 0
        # Slupek w pionie: wysokosc w jednostkach obszaru wykresu 0..80.
        p["wysokosc"] = round(p["ile"] * 80 / najwiecej, 2) if najwiecej else 0
    return {"pozycje": pozycje, "suma": suma, "najwiecej": najwiecej}


def od_tygodnia() -> datetime:
    return utcnow() - timedelta(days=7)


_pamiec_podatnosci: dict[str, dict] = {}
_blokada = threading.Lock()


def najbardziej_podatne(db: Session, tenant_id: str, odswiez: bool = False) -> dict:
    """Maszyny z najwieksza liczba podatnosci do naprawienia.

    Kolejnosc: najpierw powazne (CVSS >= 7) z dostepna poprawka, potem
    wszystkie do naprawienia, potem otwarte bez poprawki. Liczymy tylko
    maszyny z agentem na Linuksie - dla innych kanal podatnosci nie istnieje.
    """
    teraz = utcnow()
    with _blokada:
        zapamietane = _pamiec_podatnosci.get(tenant_id)
    if zapamietane and not odswiez and teraz - zapamietane["policzono"] < WAZNOSC_PODATNOSCI:
        return zapamietane

    wiersze = db.execute(
        select(Asset, AssetCurrentReport.payload)
        .join(AssetCurrentReport, AssetCurrentReport.asset_id == Asset.id)
        .where(Asset.tenant_id == tenant_id, Asset.zrodlo == ZRODLO_AGENT,
               Asset.lifecycle == LIFECYCLE_AKTYWNY, Asset.os_family == "linux")
    ).all()

    pozycje, zbadanych, bez_danych = [], 0, 0
    for maszyna, raport in wiersze:
        wynik = cve.dopasuj(db, raport or {})
        if wynik["status"] != cve.STATUS_OK:
            bez_danych += 1
            continue
        zbadanych += 1
        if not (wynik["fixable_count"] or wynik["open_count"]):
            continue
        oceny = [p.get("base_score") for p in wynik["entries"] if p.get("base_score") is not None]
        pozycje.append({
            "id": maszyna.id,
            "hostname": maszyna.hostname,
            "os_family": maszyna.os_family,
            "os_name": maszyna.os_name,
            "do_naprawy": wynik["fixable_count"],
            "powazne": wynik["critical_count"],
            "bez_poprawki": wynik["open_count"],
            "najwyzsza": max(oceny) if oceny else None,
        })

    pozycje.sort(key=lambda p: (p["powazne"], p["do_naprawy"], p["bez_poprawki"]), reverse=True)
    wynik = {
        "pozycje": pozycje[:NAJBARDZIEJ_PODATNYCH],
        "z_podatnosciami": len(pozycje),
        "zbadanych": zbadanych,
        "bez_danych": bez_danych,
        "policzono": teraz,
    }
    with _blokada:
        _pamiec_podatnosci[tenant_id] = wynik
    return wynik
