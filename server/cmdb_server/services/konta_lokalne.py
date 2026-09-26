"""Konta lokalne CMDB: zaproszenia, reset hasla, weryfikacja dwuetapowa.

Konto lokalne to e-mail i haslo w CMDB - dla firm bez AD, serwisantow
z zewnatrz i awaryjnego superadmina. Uwierzytelnienie samym haslem jest tu
najslabszym ogniwem, stad TOTP (kody z aplikacji Google/Microsoft
Authenticator), ktore firma moze wymusic.

TOTP liczymy sami (RFC 6238, HMAC-SHA1, 30 s, 6 cyfr) - to kilkanascie linii
i nie ma sensu dokladac biblioteki.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from datetime import timedelta
from html import escape
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    LINK_MOBILE,
    LINK_RESET,
    LINK_ZAPROSZENIE,
    ZRODLO_LOKALNE,
    JednorazowyLink,
    PortalUser,
    Tenant,
    as_utc,
    utcnow,
)
from ..security import hash_token
from . import sekrety

log = logging.getLogger(__name__)


# --- adresy kont lokalnych --------------------------------------------------

def sprawdz_adres(db: Session, email: str) -> None:
    """ValueError, gdy adres nalezy do domeny katalogu AD.

    Konto lokalne w domenie AD byloby niejednoznaczne: po wpisaniu loginu
    nie wiadomo, czy haslo sprawdza AD, czy CMDB. Serwisant z zewnatrz
    dostaje konto na swoj adres.
    """
    from .tozsamosc import katalog_dla_adresu

    kat = katalog_dla_adresu(db, email)
    if kat is not None:
        raise ValueError(
            f"adres {email} jest w domenie katalogu AD ({kat.nazwa}) - ta osoba loguje się "
            "kontem domenowym. Konto lokalne załóż na adres spoza tej domeny."
        )


# --- linki jednorazowe ------------------------------------------------------

def utworz_link(db: Session, rodzaj: str, email: str, *, user: PortalUser | None = None,
                tenant_id: str | None = None, zakres: str | None = None,
                rola: str | None = None, full_name: str | None = None,
                utworzyl: str | None = None, wyzwanie: str | None = None) -> str:
    """Zapisuje link i zwraca jawny token - pokazywany tylko raz."""
    ustawienia = get_settings()
    if rodzaj == LINK_ZAPROSZENIE:
        waznosc = timedelta(days=ustawienia.zaproszenie_dni)
    elif rodzaj == LINK_MOBILE:
        waznosc = timedelta(minutes=2)
    else:
        waznosc = timedelta(minutes=ustawienia.reset_hasla_minut)
    # Nowy link uniewaznia poprzednie tego samego rodzaju dla tego adresu
    # (poza kodami aplikacji - ktos moze logowac sie na dwoch telefonach).
    for stary in [] if rodzaj == LINK_MOBILE else db.execute(select(JednorazowyLink).where(
        JednorazowyLink.email == email, JednorazowyLink.rodzaj == rodzaj,
        JednorazowyLink.uzyto.is_(None),
    )).scalars():
        stary.uzyto = utcnow()
    token = secrets.token_urlsafe(32)
    db.add(JednorazowyLink(
        rodzaj=rodzaj, token_hash=hash_token(token), email=email,
        user_id=user.id if user else None, tenant_id=tenant_id, zakres=zakres, rola=rola,
        full_name=full_name, wygasa=utcnow() + waznosc, utworzyl=utworzyl, wyzwanie=wyzwanie,
    ))
    return token


def znajdz_link(db: Session, token: str, rodzaj: str) -> JednorazowyLink | None:
    if not token or len(token) > 200:
        return None
    link = db.execute(select(JednorazowyLink).where(
        JednorazowyLink.token_hash == hash_token(token)
    )).scalar_one_or_none()
    if link is None or link.rodzaj != rodzaj or link.uzyto is not None:
        return None
    if as_utc(link.wygasa) < utcnow():
        return None
    return link


def adres_linku(baza: str, rodzaj: str, token: str) -> str:
    sciezka = "zaproszenie" if rodzaj == LINK_ZAPROSZENIE else "haslo/nowe"
    return f"{baza.rstrip('/')}/{sciezka}/{quote(token)}"


def wyslij_link(db: Session, tenant_id: str | None, email: str, rodzaj: str,
                adres: str) -> bool:
    """Wysyla link poczta firmy. False, gdy firma nie ma poczty albo sie nie udalo.

    Konta bez firmy (superadmin, technik) nie maja skad wyslac - administrator
    przekazuje link sam.
    """
    from . import poczta

    if not tenant_id or poczta.ustawienia(db, tenant_id) is None:
        return False
    if rodzaj == LINK_ZAPROSZENIE:
        temat = "Zaproszenie do CMDB"
        tekst = (f"Masz zaproszenie do panelu CMDB. Ustaw hasło, otwierając link:\n\n{adres}\n\n"
                 f"Link jest ważny {get_settings().zaproszenie_dni} dni.")
    else:
        temat = "CMDB: ustawienie nowego hasła"
        tekst = (f"Ktoś poprosił o nowe hasło do CMDB dla adresu {email}. "
                 f"Jeśli to Ty, otwórz link:\n\n{adres}\n\n"
                 f"Link jest ważny {get_settings().reset_hasla_minut} minut. "
                 "Jeśli to nie Ty, zignoruj tę wiadomość - hasło się nie zmieni.")
    html = "<p>" + escape(tekst).replace("\n\n", "</p><p>").replace(
        escape(adres), f'<a href="{escape(adres)}">{escape(adres)}</a>') + "</p>"
    try:
        poczta.wyslij(db, tenant_id, [email], temat, html, tekst)
        return True
    except poczta.BladPoczty as blad:
        log.warning("nie wyslalem linku %s do %s: %s", rodzaj, email, blad)
        return False


def linkow_w_ostatniej_godzinie(db: Session, email: str) -> int:
    from sqlalchemy import func

    return db.execute(select(func.count(JednorazowyLink.id)).where(
        JednorazowyLink.email == email, JednorazowyLink.rodzaj == LINK_RESET,
        JednorazowyLink.utworzono > utcnow() - timedelta(hours=1),
    )).scalar_one()


def konto_do_resetu(db: Session, email: str) -> PortalUser | None:
    user = db.execute(
        select(PortalUser).where(PortalUser.email == (email or "").strip().lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active or user.zrodlo != ZRODLO_LOKALNE:
        return None
    return user


# --- weryfikacja dwuetapowa (TOTP) ------------------------------------------

KROK_SEKUND = 30
CYFR = 6


def nowy_sekret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _kod(sekret: str, krok: int) -> str:
    klucz = base64.b32decode(sekret + "=" * (-len(sekret) % 8), casefold=True)
    skrot = hmac.new(klucz, struct.pack(">Q", krok), hashlib.sha1).digest()
    przesuniecie = skrot[-1] & 0x0F
    liczba = struct.unpack(">I", skrot[przesuniecie:przesuniecie + 4])[0] & 0x7FFFFFFF
    return str(liczba % 10 ** CYFR).zfill(CYFR)


def kod_teraz(sekret: str, czas: float | None = None) -> str:
    return _kod(sekret, int((czas if czas is not None else time.time()) // KROK_SEKUND))


def sprawdz_kod(sekret: str, kod: str, ostatni_krok: int | None = None,
                czas: float | None = None) -> int | None:
    """Numer okna, w ktorym kod pasuje - albo None.

    Dopuszczamy jedno okno wstecz i w przod (zegar telefonu). Kod z okna nie
    nowszego niz ostatnio uzyte jest odrzucany - przechwycony kod nie
    zadziala drugi raz.
    """
    kod = "".join(c for c in (kod or "") if c.isdigit())
    if len(kod) != CYFR:
        return None
    teraz = int((czas if czas is not None else time.time()) // KROK_SEKUND)
    for krok in (teraz - 1, teraz, teraz + 1):
        if ostatni_krok is not None and krok <= ostatni_krok:
            continue
        if hmac.compare_digest(_kod(sekret, krok), kod):
            return krok
    return None


def adres_otpauth(sekret: str, email: str) -> str:
    wystawca = "CMDB"
    return (f"otpauth://totp/{quote(wystawca)}:{quote(email)}"
            f"?secret={sekret}&issuer={quote(wystawca)}&digits={CYFR}&period={KROK_SEKUND}")


def sekret_konta(user: PortalUser) -> str | None:
    return sekrety.odszyfruj(user.totp_szyfr)


def mfa_wymagane(db: Session, user: PortalUser) -> bool:
    """Czy to konto musi miec weryfikacje dwuetapowa (niezaleznie, czy ma)."""
    if user.zrodlo != ZRODLO_LOKALNE:
        return False
    if user.is_superadmin and get_settings().mfa_superadmin:
        return True
    if user.tenant_id:
        firma = db.get(Tenant, user.tenant_id)
        return bool(firma and firma.wymagaj_mfa)
    return False


def potrzebny_drugi_krok(db: Session, user: PortalUser) -> bool:
    return user.zrodlo == ZRODLO_LOKALNE and (user.totp_wlaczone or mfa_wymagane(db, user))


def zweryfikuj_i_zapisz(user: PortalUser, kod: str) -> bool:
    """Sprawdza kod wlaczonego TOTP i zapamietuje okno. True = kod dobry."""
    sekret = sekret_konta(user)
    if not user.totp_wlaczone or not sekret:
        return False
    krok = sprawdz_kod(sekret, kod, user.totp_ostatni_krok)
    if krok is None:
        return False
    user.totp_ostatni_krok = krok
    return True
