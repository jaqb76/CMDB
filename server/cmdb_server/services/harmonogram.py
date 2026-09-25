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


def _stan_pobierania(**pola) -> None:
    """Zapisuje postep w osobnej sesji - panel widzi go od razu."""
    from ..models import CveSyncStatus, utcnow

    with SessionLocal() as db:
        stan = db.get(CveSyncStatus, 1)
        if stan is None:
            stan = CveSyncStatus(id=1)
            db.add(stan)
        for nazwa, wartosc in pola.items():
            setattr(stan, nazwa, wartosc)
        if pola.get("state") == "running" and "started_at" not in pola:
            stan.started_at = utcnow()
        db.commit()


def przebieg_podatnosci(co_ile_godzin: float, klucz_nvd: str = "",
                        kanaly: bool = True, oceny: bool = True) -> dict | None:
    """Jeden obieg odswiezania danych o podatnosciach.

    None, gdy robote wykonuje wlasnie inny proces. co_ile_godzin=0 wymusza
    pobranie wszystkich kanalow.
    """
    from ..models import utcnow
    from . import cve

    with engine.connect() as conn:
        if not conn.execute(
            text("SELECT pg_try_advisory_lock(:klucz)"), {"klucz": KLUCZ_BLOKADY_CVE}
        ).scalar():
            return None
        try:
            conn.commit()
            _stan_pobierania(state="running", phase="przygotowanie", done=0, total=0,
                             finished_at=None, detail=None, started_at=utcnow())

            def postep(etap):
                return lambda zrobione, wszystkie: _stan_pobierania(
                    phase=etap, done=zrobione, total=wszystkie)

            wynik: dict = {"kanaly": {}, "oceny": None}
            with SessionLocal() as db:
                if kanaly:
                    do_odswiezenia = cve.kanaly_do_odswiezenia(db, co_ile_godzin)
                    if do_odswiezenia:
                        _stan_pobierania(phase="kanały dystrybucji")
                        wynik["kanaly"] = cve.odswiez(db, do_odswiezenia)
                if oceny:
                    # Oceny dobieramy przy kazdym obiegu, porcjami. Jeden
                    # przebieg bez klucza NVD to do 200 ocen, wiec nowa flota
                    # dostaje komplet po kilku godzinach bez niczyjego klikania.
                    _stan_pobierania(phase="wyszukiwanie podatności we flocie")
                    znalezione = cve.cve_we_flocie(db)
                    if znalezione:
                        wynik["oceny"] = cve.pobierz_oceny(
                            db, znalezione, klucz_api=klucz_nvd, postep=postep("oceny NVD"))
                    # Ubuntu ocenia swieze CVE szybciej niz NVD i ma wlasny
                    # priorytet - pobieramy go dla maszyn z Ubuntu.
                    ubuntu = cve.cve_we_flocie(db, source="ubuntu")
                    if ubuntu:
                        wynik["ubuntu"] = cve.pobierz_oceny_ubuntu(
                            db, ubuntu, postep=postep("oceny Ubuntu"))
            _stan_pobierania(state="ok", phase=None, finished_at=utcnow(),
                             detail=_opis_wyniku(wynik))
            return wynik
        except Exception as exc:
            _stan_pobierania(state="error", finished_at=utcnow(), detail=str(exc)[:500])
            raise
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:klucz)"), {"klucz": KLUCZ_BLOKADY_CVE}
            )
            conn.commit()


def _opis_wyniku(wynik: dict) -> str:
    czesci = []
    for nazwa, stan in (wynik.get("kanaly") or {}).items():
        czesci.append(f"kanał {nazwa}: {stan}")
    for klucz, etykieta in (("oceny", "NVD"), ("ubuntu", "Ubuntu")):
        o = wynik.get(klucz)
        if o:
            czesci.append(f"oceny {etykieta}: pobrano {o.get('pobrane', 0)}, "
                          f"bez oceny {o.get('bez_oceny', 0)}, błędów {o.get('bledy', 0)}, "
                          f"zostało {o.get('pozostalo', 0)}")
    return "; ".join(czesci) or "Wszystko aktualne - nic do pobrania."


def uruchom_w_tle(klucz_nvd: str, kanaly: bool, oceny: bool) -> None:
    """Start pobierania z panelu - w osobnym watku, zeby nie wisiec na HTTP."""
    import threading

    def praca():
        try:
            przebieg_podatnosci(0 if kanaly else 10**6, klucz_nvd, kanaly=kanaly, oceny=oceny)
        except Exception as exc:
            log.error("podatnosci: pobieranie z panelu zakonczylo sie bledem: %s", exc)

    threading.Thread(target=praca, name="cmdb-podatnosci", daemon=True).start()


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
