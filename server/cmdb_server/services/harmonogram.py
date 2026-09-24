"""Cykliczne wysylanie raportow w tle.

Serwer produkcyjny dziala w kilku procesach roboczych i kazdy uruchamia ten
sam kod. Bez uzgodnienia raport poszedlby tylokrotnie, ile jest procesow -
a wiadomosc wyslana cztery razy jest gorsza niz niewyslana wcale, bo uczy
odbiorcow ignorowania raportow.

Uzgadniamy sie blokada doradcza Postgresa, pobierana bez czekania: proces,
ktory jej nie dostanie, po prostu pomija ten obieg. Nie ma potrzeby ustawiac
sie w kolejce, skoro robota i tak zostanie wykonana przez tego, kto wygral.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from ..db import SessionLocal, engine

log = logging.getLogger(__name__)

# Inna niz blokada schematu - te dwie rzeczy nie moga sie nawzajem blokowac.
KLUCZ_BLOKADY = 0x434D4252  # "CMBR"

# Co ile sprawdzamy, czy cos jest do wyslania. Najkrotszy odstep miedzy
# raportami to doba, wiec kwadrans daje zapas i nie obciaza bazy.
ODSTEP_SEKUND = 900


def _sprobuj_przejac(conn) -> bool:
    return bool(conn.execute(
        text("SELECT pg_try_advisory_lock(:klucz)"), {"klucz": KLUCZ_BLOKADY}
    ).scalar())


def _zwolnij(conn) -> None:
    conn.execute(text("SELECT pg_advisory_unlock(:klucz)"), {"klucz": KLUCZ_BLOKADY})


def przebieg() -> dict | None:
    """Jeden obieg. None, gdy robote wykonuje wlasnie inny proces."""
    from . import monitoring, raporty

    with engine.connect() as conn:
        if not _sprobuj_przejac(conn):
            return None
        try:
            conn.commit()
            with SessionLocal() as db:
                wynik = raporty.wyslij_zalegle(db)
                # Sprzatanie historii monitorowania jedzie tu, a nie we
                # wlasnej petli: to jedno zapytanie na kwadrans, a osobne
                # zadanie w tle znaczyloby druga blokade do uzgodnienia.
                wynik["monitorowanie"] = monitoring.usun_stara_historie(db)
                return wynik
        finally:
            _zwolnij(conn)
            conn.commit()


async def petla() -> None:
    """Zadanie w tle uruchamiane przy starcie serwera."""
    while True:
        try:
            await asyncio.sleep(ODSTEP_SEKUND)
            wynik = await asyncio.to_thread(przebieg)
            if wynik and wynik["wyslane"]:
                log.info("harmonogram: wyslano %d raportow", wynik["wyslane"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Blad jednego obiegu nie moze zatrzymac harmonogramu na zawsze -
            # inaczej jedna nieudana wysylka wylaczalaby raporty do restartu.
            log.error("harmonogram: obieg zakonczyl sie bledem: %s", exc)


# --- dane o podatnosciach ---------------------------------------------------
#
# Osobna petla i osobna blokada: pobranie kanalu Debiana (86 MB) i ocen CVSS
# z NVD (kilka sekund na zapytanie bez klucza) potrafi trwac kilkanascie
# minut. Pod wspolna blokada wstrzymywaloby to wysylke raportow.

KLUCZ_BLOKADY_CVE = 0x434D4356  # "CMCV"

# Co ile sprawdzamy, czy dane sa do odswiezenia. Samo sprawdzenie to dwa
# zapytania; pobieranie rusza dopiero, gdy dane sa starsze niz cve_refresh_hours.
# Godzina oznacza tez, ze nieudane pobranie ponawia sie po godzinie, a nie
# w kolko co kwadrans.
ODSTEP_CVE_SEKUND = 3600


def przebieg_podatnosci(co_ile_godzin: float, klucz_nvd: str = "") -> dict | None:
    """Jeden obieg odswiezania danych o podatnosciach.

    None, gdy robote wykonuje wlasnie inny proces.
    """
    from . import cve

    with engine.connect() as conn:
        if not conn.execute(
            text("SELECT pg_try_advisory_lock(:klucz)"), {"klucz": KLUCZ_BLOKADY_CVE}
        ).scalar():
            return None
        try:
            conn.commit()
            wynik: dict = {"kanaly": {}, "oceny": None}
            with SessionLocal() as db:
                do_odswiezenia = cve.kanaly_do_odswiezenia(db, co_ile_godzin)
                if do_odswiezenia:
                    wynik["kanaly"] = cve.odswiez(db, do_odswiezenia)
                # Oceny dobieramy przy kazdym obiegu, porcjami. Jeden przebieg
                # bez klucza NVD to do 200 ocen, wiec nowa flota dostaje
                # komplet po kilku godzinach bez niczyjego klikania.
                znalezione = cve.cve_we_flocie(db)
                if znalezione:
                    wynik["oceny"] = cve.pobierz_oceny(db, znalezione, klucz_api=klucz_nvd)
            return wynik
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:klucz)"), {"klucz": KLUCZ_BLOKADY_CVE}
            )
            conn.commit()


async def petla_podatnosci() -> None:
    """Zadanie w tle: dane o podatnosciach odswiezaja sie same."""
    from ..config import get_settings

    while True:
        try:
            # Pierwszy obieg krotko po starcie, a nie po godzinie - swiezo
            # postawiony serwer ma od razu pobrac dane, zamiast czekac.
            await asyncio.sleep(120)
            ustawienia = get_settings()
            if ustawienia.cve_refresh_hours > 0:
                wynik = await asyncio.to_thread(
                    przebieg_podatnosci, ustawienia.cve_refresh_hours, ustawienia.nvd_api_key
                )
                if wynik and (wynik["kanaly"] or (wynik["oceny"] or {}).get("pobrane")):
                    log.info("podatnosci: kanaly %s, oceny %s", wynik["kanaly"], wynik["oceny"])
            await asyncio.sleep(ODSTEP_CVE_SEKUND - 120)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("podatnosci: obieg zakonczyl sie bledem: %s", exc)
