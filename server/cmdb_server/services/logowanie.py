"""Ograniczanie prob logowania do panelu.

Tokeny agentow mialy limit prob od poczatku, panel nie mial zadnego - a to
on jest jedynym miejscem, gdzie zgadywanie hasla ma sens. Serwer wystawiony
do internetu dostaje staly ruch skanerow, wiec brak limitu jest zaproszeniem.

Stan trzymamy w bazie, nie w pamieci procesu. Serwer produkcyjny dziala
w kilku procesach roboczych, wiec licznik w pamieci dawalby tylokrotnie wiecej
prob, ile jest procesow, a restart zerowalby go doszczetnie.

Blokujemy dwie rzeczy naraz: konto (chroni haslo konkretnego uzytkownika)
oraz adres, z ktorego przychodza proby (chroni przed rozproszeniem po wielu
kontach). Wystarczy jedna z nich, zeby odmowic.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import BlokadaLogowania, as_utc, utcnow

log = logging.getLogger(__name__)

# Po ilu godzinach bez zadnej proby licznik przestaje byc aktualny. Bez tego
# trzy pomylki rozlozone na pol roku konczylyby sie blokada konta.
OKNO_GODZIN = 12


def _klucze(email: str, ip: str) -> list[str]:
    return [f"konto:{(email or '').strip().lower()[:300]}", f"ip:{(ip or 'nieznany')[:300]}"]


def zablokowane_do(db: Session, email: str, ip: str):
    """Do kiedy logowanie jest zablokowane, albo None."""
    teraz = utcnow()
    wpisy = db.execute(
        select(BlokadaLogowania).where(BlokadaLogowania.klucz.in_(_klucze(email, ip)))
    ).scalars().all()

    najdalsza = None
    for wpis in wpisy:
        do_kiedy = as_utc(wpis.blokada_do)
        if do_kiedy and do_kiedy > teraz:
            najdalsza = do_kiedy if najdalsza is None else max(najdalsza, do_kiedy)
    return najdalsza


def odnotuj_niepowodzenie(db: Session, email: str, ip: str, prog: int, godziny: int):
    """Zlicza nieudana probe i w razie potrzeby zaklada blokade."""
    teraz = utcnow()
    zalozona = None

    for klucz in _klucze(email, ip):
        wpis = db.get(BlokadaLogowania, klucz)
        if wpis is None:
            wpis = BlokadaLogowania(klucz=klucz, licznik=0)
            db.add(wpis)
        else:
            ostatnia = as_utc(wpis.ostatnia_proba)
            if ostatnia and teraz - ostatnia > timedelta(hours=OKNO_GODZIN):
                # Proby sprzed dawna nie swiadcza o ataku - liczymy od nowa.
                wpis.licznik = 0

        wpis.licznik += 1
        wpis.ostatnia_proba = teraz
        if wpis.licznik >= prog:
            wpis.blokada_do = teraz + timedelta(hours=godziny)
            wpis.licznik = 0
            zalozona = wpis.blokada_do
            log.warning("blokada logowania dla %s do %s", klucz, wpis.blokada_do)

    db.commit()
    return zalozona


def wyczysc(db: Session, email: str, ip: str) -> None:
    """Kasuje licznik i blokade po udanym logowaniu."""
    db.execute(delete(BlokadaLogowania).where(BlokadaLogowania.klucz.in_(_klucze(email, ip))))
    db.commit()


def odblokuj(db: Session, email: str | None = None, ip: str | None = None,
             wszystko: bool = False) -> int:
    """Zdejmuje blokade logowania. Uzywane przez administratora z wiersza polecen.

    Blokada ma nieprzyjemna wlasciwosc: kto zna adres e-mail administratora,
    moze go zablokowac trzema bledymi haslami. Musi wiec istniec droga
    wyjscia, ktora nie wymaga czekania do konca blokady.

    Blokujemy konto ORAZ adres, wiec administrator, ktory pomylil haslo trzy
    razy u siebie, ma zalozone obie - zdjecie samego konta nic mu nie da.
    Stad mozliwosc wskazania jednego, drugiego albo wyczyszczenia wszystkiego.
    """
    if wszystko:
        usuniete = db.execute(delete(BlokadaLogowania)).rowcount
    else:
        klucze = []
        if email:
            klucze.append(f"konto:{email.strip().lower()[:300]}")
        if ip:
            klucze.append(f"ip:{ip.strip()[:300]}")
        if not klucze:
            return 0
        usuniete = db.execute(
            delete(BlokadaLogowania).where(BlokadaLogowania.klucz.in_(klucze))
        ).rowcount
    db.commit()
    return usuniete or 0
