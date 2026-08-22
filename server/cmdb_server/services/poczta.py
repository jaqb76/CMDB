"""Wysylka raportow poczta, z poswiadczeniami kazdej firmy osobno.

Kazda firma podaje wlasny serwer SMTP. Wspolna skrzynka operatora oznaczalaby,
ze wiadomosci o jednej organizacji ida infrastruktura, do ktorej ma dostep
inna - a caly system jest zbudowany wokol tego, ze firmy sie nie widza.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.orm import Session

from ..models import UstawieniaPoczty
from . import sekrety

log = logging.getLogger(__name__)

# Serwer poczty bywa wolny, a wysylka idzie w tle - ale nie moze wisiec
# w nieskonczonosc, bo zablokowalaby kolejne raporty.
LIMIT_SEKUND = 30


class BladPoczty(RuntimeError):
    """Nie udalo sie wyslac wiadomosci."""


def ustawienia(db: Session, tenant_id: str) -> UstawieniaPoczty | None:
    wpis = db.get(UstawieniaPoczty, tenant_id)
    return wpis if wpis is not None and wpis.aktywne else None


def _polaczenie(konfiguracja: UstawieniaPoczty):
    kontekst = ssl.create_default_context()
    if konfiguracja.szyfrowanie == "ssl":
        return smtplib.SMTP_SSL(konfiguracja.host, konfiguracja.port,
                                timeout=LIMIT_SEKUND, context=kontekst)

    polaczenie = smtplib.SMTP(konfiguracja.host, konfiguracja.port, timeout=LIMIT_SEKUND)
    if konfiguracja.szyfrowanie == "starttls":
        polaczenie.starttls(context=kontekst)
    return polaczenie


def wyslij(db: Session, tenant_id: str, adresaci: list[str], temat: str,
           tresc_html: str, tresc_tekst: str) -> None:
    """Wysyla wiadomosc przez serwer wskazany przez firme."""
    konfiguracja = ustawienia(db, tenant_id)
    if konfiguracja is None:
        raise BladPoczty("firma nie ma skonfigurowanej poczty")
    if not adresaci:
        raise BladPoczty("nie wskazano adresatow")

    wiadomosc = EmailMessage()
    wiadomosc["Subject"] = temat
    wiadomosc["From"] = formataddr((konfiguracja.nazwa_nadawcy or "CMDB", konfiguracja.nadawca))
    wiadomosc["To"] = ", ".join(adresaci)
    # Wersja tekstowa nie jest ozdoba: czesc klientow i filtrow ocenia
    # wiadomosc bez alternatywy tekstowej jako podejrzana.
    wiadomosc.set_content(tresc_tekst)
    wiadomosc.add_alternative(tresc_html, subtype="html")

    haslo = sekrety.odszyfruj(konfiguracja.haslo_szyfr)
    # Sprawdzamy PRZED nawiazaniem polaczenia. Inaczej niedostepny serwer
    # przykrywa prawdziwa przyczyne bledem o odmowie polaczenia, a
    # administrator szuka problemu w sieci zamiast wpisac haslo ponownie.
    # Nieczytelne haslo znaczy zwykle, ze zmienil sie CMDB_SECRET_KEY.
    if konfiguracja.uzytkownik and konfiguracja.haslo_szyfr and not haslo:
        raise BladPoczty("nie moge odczytac zapisanego hasla SMTP - wpisz je ponownie")

    try:
        with _polaczenie(konfiguracja) as polaczenie:
            if konfiguracja.uzytkownik and haslo:
                polaczenie.login(konfiguracja.uzytkownik, haslo)
            polaczenie.send_message(wiadomosc)
    except BladPoczty:
        raise
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise BladPoczty(f"{type(exc).__name__}: {exc}") from exc

    log.info("wyslano raport do %d adresatow przez %s", len(adresaci), konfiguracja.host)


def sprawdz(db: Session, tenant_id: str, adres: str) -> None:
    """Wysyla wiadomosc probna - jedyny pewny sposob sprawdzenia ustawien."""
    wyslij(
        db, tenant_id, [adres],
        "CMDB: wiadomosc probna",
        "<p>Ustawienia poczty dzialaja. Ta wiadomosc zostala wyslana z panelu CMDB.</p>",
        "Ustawienia poczty dzialaja. Ta wiadomosc zostala wyslana z panelu CMDB.",
    )
