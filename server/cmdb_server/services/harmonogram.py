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
