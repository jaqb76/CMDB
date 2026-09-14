"""Odbieranie poczty helpdesku przez IMAP.

Jeden obieg to: polacz, wez nieprzeczytane, przerob kazda wiadomosc osobno,
oznacz ja jako obsluzona. Kazda wiadomosc dostaje wlasna transakcje - awaria
przy dziesiatej nie moze cofnac dziewieciu wczesniejszych zgloszen.

Wiadomosci nie kasujemy nigdy. Helpdesk czyta cudza skrzynke i jedyne, co
w niej zmienia, to znacznik "przeczytana" albo przeniesienie do folderu -
obie rzeczy odwracalne.

Serwer produkcyjny dziala w kilku procesach roboczych i kazdy uruchamia ten
sam kod, wiec obieg bierze blokade doradcza Postgresa: bez niej dwa procesy
pobralyby te sama wiadomosc i zalozyly dwa zgloszenia.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import ssl

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import SessionLocal, engine
from ..models import HelpdeskUstawienia
from . import helpdesk_poczta as poczta

log = logging.getLogger(__name__)

# Inna niz blokada schematu i niz blokada raportow - te trzy rzeczy nie moga
# sie nawzajem blokowac.
KLUCZ_BLOKADY = 0x434D4448  # "CMDH"

LIMIT_SEKUND = 60
# Ile wiadomosci bierzemy w jednym obiegu. Skrzynka po tygodniu przestoju ma
# ich tysiace, a obieg ma sie skonczyc, zanim ruszy nastepny.
LIMIT_WIADOMOSCI = 200


class BladOdbioru(RuntimeError):
    """Nie udalo sie pobrac poczty."""


def ustawienia(db: Session) -> HelpdeskUstawienia | None:
    wpis = db.get(HelpdeskUstawienia, "helpdesk")
    if wpis is None or not wpis.aktywne or not wpis.imap_host:
        return None
    return wpis


def _polaczenie(konfiguracja: HelpdeskUstawienia):
    kontekst = ssl.create_default_context()
    if konfiguracja.imap_szyfrowanie == "ssl":
        return imaplib.IMAP4_SSL(
            konfiguracja.imap_host, konfiguracja.imap_port,
            timeout=LIMIT_SEKUND, ssl_context=kontekst,
        )
    polaczenie = imaplib.IMAP4(
        konfiguracja.imap_host, konfiguracja.imap_port, timeout=LIMIT_SEKUND
    )
    if konfiguracja.imap_szyfrowanie == "starttls":
        polaczenie.starttls(ssl_context=kontekst)
    return polaczenie


def _zaloguj(polaczenie, konfiguracja: HelpdeskUstawienia):
    from . import sekrety

    haslo = sekrety.odszyfruj(konfiguracja.imap_haslo_szyfr)
    if konfiguracja.imap_uzytkownik and konfiguracja.imap_haslo_szyfr and not haslo:
        raise BladOdbioru("nie moge odczytac zapisanego hasla IMAP - wpisz je ponownie")
    if konfiguracja.imap_uzytkownik and haslo:
        polaczenie.login(konfiguracja.imap_uzytkownik, haslo)


def _obsluz(polaczenie, konfiguracja: HelpdeskUstawienia, identyfikator: bytes) -> None:
    """Znaczy wiadomosc jako obsluzona - zgodnie z ustawieniem skrzynki.

    Robimy to PO zapisaniu wiadomosci w bazie. Odwrotna kolejnosc gubilaby
    zgloszenia przy awarii miedzy jednym a drugim: wiadomosc przeczytana,
    a nigdzie jej nie ma.
    """
    if konfiguracja.imap_po_pobraniu == "nic":
        return
    if konfiguracja.imap_po_pobraniu == "przenies" and konfiguracja.imap_folder_docelowy:
        polaczenie.copy(identyfikator, konfiguracja.imap_folder_docelowy)
        polaczenie.store(identyfikator, "+FLAGS", "\\Deleted")
        polaczenie.expunge()
        return
    polaczenie.store(identyfikator, "+FLAGS", "\\Seen")


def pobierz(db: Session) -> dict:
    """Jeden obieg pobierania. Zwraca licznik tego, co zrobil."""
    konfiguracja = ustawienia(db)
    if konfiguracja is None:
        return {"pominiete": "skrzynka helpdesku nie jest skonfigurowana"}

    from . import helpdesk_wysylka

    wynik = {"pobrane": 0, "nowe": 0, "dopisane": 0, "nierozpoznane": 0, "zignorowane": 0}
    try:
        polaczenie = _polaczenie(konfiguracja)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as blad:
        return _zapisz_blad(db, konfiguracja, f"{type(blad).__name__}: {blad}")

    try:
        _zaloguj(polaczenie, konfiguracja)
        polaczenie.select(konfiguracja.imap_folder or "INBOX")
        stan, dane = polaczenie.search(None, "UNSEEN")
        if stan != "OK":
            return _zapisz_blad(db, konfiguracja, f"IMAP SEARCH: {stan}")

        identyfikatory = (dane[0] or b"").split()[:LIMIT_WIADOMOSCI]
        for identyfikator in identyfikatory:
            stan, tresc = polaczenie.fetch(identyfikator, "(RFC822)")
            if stan != "OK" or not tresc or not isinstance(tresc[0], tuple):
                log.warning("helpdesk: nie udalo sie pobrac wiadomosci %s", identyfikator)
                continue

            wiadomosc = poczta.przeczytaj(tresc[0][1])
            try:
                kwalifikacja = poczta.przyjmij(db, wiadomosc)
                if (kwalifikacja.decyzja == poczta.DECYZJA_NOWE
                        and kwalifikacja.zgloszenie is not None
                        and helpdesk_wysylka.potwierdzenie_nalezne(
                            db, kwalifikacja.zgloszenie,
                            automat=wiadomosc.automat, tresc_maila=wiadomosc.tresc)):
                    helpdesk_wysylka.wyslij_potwierdzenie(db, kwalifikacja.zgloszenie)
                db.commit()
            except Exception:
                # Jedna wadliwa wiadomosc nie moze zatrzymac skrzynki: cofamy
                # ja, zostawiamy nieprzeczytana i idziemy dalej. Wroci
                # w nastepnym obiegu, a w dzienniku zostaje slad.
                db.rollback()
                log.exception("helpdesk: wiadomosc %s nie zostala przyjeta", wiadomosc.message_id)
                continue

            wynik["pobrane"] += 1
            wynik[{
                poczta.DECYZJA_NOWE: "nowe",
                poczta.DECYZJA_DOPISZ: "dopisane",
                poczta.DECYZJA_NIEROZPOZNANA: "nierozpoznane",
                poczta.DECYZJA_ZIGNORUJ: "zignorowane",
            }[kwalifikacja.decyzja]] += 1

            try:
                _obsluz(polaczenie, konfiguracja, identyfikator)
            except (imaplib.IMAP4.error, OSError) as blad:
                # Wiadomosc jest juz w bazie; nieoznaczona wroci w kolejnym
                # obiegu i zostanie rozpoznana jako duplikat po Message-ID.
                log.warning("helpdesk: nie oznaczylem wiadomosci %s: %s", identyfikator, blad)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, BladOdbioru) as blad:
        return _zapisz_blad(db, konfiguracja, f"{type(blad).__name__}: {blad}")
    finally:
        try:
            polaczenie.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            polaczenie.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    konfiguracja.ostatnie_pobranie = _teraz()
    konfiguracja.ostatni_blad = None
    db.commit()
    if wynik["pobrane"]:
        log.info("helpdesk: pobrano %d wiadomosci %s", wynik["pobrane"], wynik)
    return wynik


def _teraz():
    from ..models import utcnow

    return utcnow()


def _zapisz_blad(db: Session, konfiguracja: HelpdeskUstawienia, opis: str) -> dict:
    """Blad ma byc widoczny w panelu, a nie tylko w dzienniku.

    Skrzynka, ktora przestala sie logowac, wyglada z panelu identycznie jak
    skrzynka, do ktorej nikt nie napisal - i to jest najgorszy rodzaj awarii
    helpdesku: cichy.
    """
    konfiguracja.ostatni_blad = opis[:2000]
    db.commit()
    log.error("helpdesk: odbior poczty nie powiodl sie: %s", opis)
    return {"blad": opis}


# --- obieg w tle ------------------------------------------------------------

def przebieg() -> dict | None:
    """Jeden obieg z blokada. None, gdy poczte odbiera wlasnie inny proces."""
    with engine.connect() as polaczenie:
        przejete = bool(polaczenie.execute(
            text("SELECT pg_try_advisory_lock(:klucz)"), {"klucz": KLUCZ_BLOKADY}
        ).scalar())
        if not przejete:
            return None
        try:
            polaczenie.commit()
            with SessionLocal() as db:
                return pobierz(db)
        finally:
            polaczenie.execute(
                text("SELECT pg_advisory_unlock(:klucz)"), {"klucz": KLUCZ_BLOKADY}
            )
            polaczenie.commit()


def odstep_sekund() -> int:
    """Co ile sprawdzac skrzynke. Ustawienie firmy, z rozsadnym dolnym progiem."""
    with SessionLocal() as db:
        konfiguracja = db.get(HelpdeskUstawienia, "helpdesk")
        if konfiguracja is None:
            return 120
        return max(30, konfiguracja.imap_interwal_sekund)


async def petla() -> None:
    """Zadanie w tle uruchamiane przy starcie serwera."""
    while True:
        try:
            await asyncio.sleep(await asyncio.to_thread(odstep_sekund))
            await asyncio.to_thread(przebieg)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Petla ma przezyc kazdy blad: helpdesk bez odbioru poczty
            # przestaje byc helpdeskiem.
            log.exception("helpdesk: obieg odbioru poczty przerwany bledem")
            await asyncio.sleep(60)
