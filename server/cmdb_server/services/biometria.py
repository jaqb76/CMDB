"""Logowanie biometria w aplikacji Android.

Biometria niczego nie przyznaje - potwierdza, ze telefon, ktory juz raz
przeszedl pelne logowanie (haslo, AD, konto zewnetrzne), nadal jest w rekach
wlasciciela. Przebieg:

  1. po pelnym logowaniu aplikacja tworzy w Android Keystore pare kluczy EC
     P-256 wymagajaca silnej biometrii przy kazdym uzyciu i rejestruje klucz
     publiczny (``zarejestruj``),
  2. przy kolejnym uruchomieniu prosi o wyzwanie (``wyzwanie``), telefon
     podpisuje je po przylozeniu palca, a serwer sprawdza podpis
     (``zaloguj``) i stan konta - dopiero wtedy wydaje KROTKI token.

Wyzwanie jest bezstanowe: podpisane przez serwer, wazne minute, z czasem
wystawienia w ms. Urzadzenie pamieta czas ostatnio przyjetego wyzwania, wiec
kazde przejdzie raz, a starsze od juz uzytego wcale.
"""
from __future__ import annotations

import base64
import binascii
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PortalUser, Tenant, UrzadzenieMobilne, as_utc, utcnow

WAZNOSC_WYZWANIA = 60
# Token po biometrii zyje godzine; potem kolejne przylozenie palca.
WAZNOSC_TOKENU_BIOMETRII = 3600
# Ile czasu po pelnym logowaniu wolno zarejestrowac urzadzenie.
SWIEZOSC_REJESTRACJI = 10 * 60
# Po ilu sekundach od biometrii operacje zapisu wymagaja jej ponownie.
SWIEZOSC_POTWIERDZENIA = 5 * 60
MAKS_URZADZEN = 10

POLITYKI = ("dozwolona", "wymagana", "wylaczona")


class BladBiometrii(RuntimeError):
    def __init__(self, komunikat: str, kod: str = "odmowa"):
        super().__init__(komunikat)
        self.kod = kod


@dataclass(frozen=True)
class Polityka:
    biometria: str = "dozwolona"
    dni: int = 30
    pin: bool = False
    blokada_minut: int = 5

    def jako_slownik(self) -> dict:
        return {"biometrics": self.biometria, "full_login_days": self.dni,
                "allow_device_credential": self.pin, "lock_after_minutes": self.blokada_minut,
                "token_seconds": WAZNOSC_TOKENU_BIOMETRII}


def polityka(db: Session, user: PortalUser) -> Polityka:
    firma = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    if firma is None:
        return Polityka()
    return Polityka(firma.biometria, firma.biometria_dni, firma.biometria_pin,
                    firma.blokada_aplikacji_minut)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="cmdb-biometria")


def _klucz(klucz_b64: str):
    try:
        klucz = load_der_public_key(base64.b64decode(klucz_b64, validate=True))
    except (ValueError, binascii.Error, TypeError) as blad:
        raise BladBiometrii("nieczytelny klucz publiczny") from blad
    if not isinstance(klucz, ec.EllipticCurvePublicKey) or klucz.curve.name != "secp256r1":
        raise BladBiometrii("wymagany klucz EC P-256")
    return klucz


def zarejestruj(db: Session, user: PortalUser, nazwa: str, klucz_b64: str,
                zastepuje: str | None = None) -> UrzadzenieMobilne:
    if polityka(db, user).biometria == "wylaczona":
        raise BladBiometrii("biometria jest wyłączona w Twojej firmie", "wylaczona")
    _klucz(klucz_b64)
    if zastepuje:
        stare = db.get(UrzadzenieMobilne, zastepuje)
        if stare is not None and stare.user_id == user.id and stare.odlaczono is None:
            stare.odlaczono = utcnow()
            stare.powod_odlaczenia = "nowy klucz po pełnym logowaniu"
    aktywne = db.execute(select(UrzadzenieMobilne).where(
        UrzadzenieMobilne.user_id == user.id, UrzadzenieMobilne.odlaczono.is_(None)
    ).order_by(UrzadzenieMobilne.dodano)).scalars().all()
    for nadmiarowe in aktywne[:max(0, len(aktywne) - MAKS_URZADZEN + 1)]:
        nadmiarowe.odlaczono = utcnow()
        nadmiarowe.powod_odlaczenia = "przekroczony limit urządzeń"
    urzadzenie = UrzadzenieMobilne(user_id=user.id, nazwa=(nazwa or "Telefon").strip()[:200],
                                   klucz_publiczny=klucz_b64, ostatnie_pelne_logowanie=utcnow())
    db.add(urzadzenie)
    db.flush()
    return urzadzenie


def wyzwanie(urzadzenie_id: str) -> str:
    return _serializer().dumps({"d": urzadzenie_id, "t": int(time.time() * 1000),
                                "n": secrets.token_urlsafe(16)})


def zaloguj(db: Session, urzadzenie_id: str, tresc_wyzwania: str,
            podpis_b64: str) -> tuple[UrzadzenieMobilne, PortalUser]:
    """Sprawdza podpis wyzwania i stan konta. BladBiometrii z kodem przy odmowie."""
    try:
        dane = _serializer().loads(tresc_wyzwania, max_age=WAZNOSC_WYZWANIA)
    except SignatureExpired as blad:
        raise BladBiometrii("wyzwanie wygasło - spróbuj ponownie", "wyzwanie") from blad
    except BadSignature as blad:
        raise BladBiometrii("nieprawidłowe wyzwanie", "wyzwanie") from blad
    if not isinstance(dane, dict) or dane.get("d") != urzadzenie_id:
        raise BladBiometrii("wyzwanie wystawione dla innego urządzenia", "wyzwanie")

    urzadzenie = db.get(UrzadzenieMobilne, urzadzenie_id)
    if urzadzenie is None or urzadzenie.odlaczono is not None:
        raise BladBiometrii("to urządzenie zostało odłączone od konta", "odlaczone")
    if urzadzenie.ostatnie_wyzwanie is not None and dane["t"] <= urzadzenie.ostatnie_wyzwanie:
        raise BladBiometrii("to wyzwanie zostało już użyte", "wyzwanie")
    try:
        podpis = base64.b64decode(podpis_b64, validate=True)
        _klucz(urzadzenie.klucz_publiczny).verify(
            podpis, tresc_wyzwania.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, binascii.Error) as blad:
        raise BladBiometrii("podpis się nie zgadza", "podpis") from blad

    user = db.get(PortalUser, urzadzenie.user_id)
    if user is None or not user.is_active:
        raise BladBiometrii("konto jest wyłączone", "konto")
    pol = polityka(db, user)
    if pol.biometria == "wylaczona":
        raise BladBiometrii("biometria jest wyłączona w Twojej firmie", "wylaczona")
    if as_utc(urzadzenie.ostatnie_pelne_logowanie) < utcnow() - timedelta(days=pol.dni):
        raise BladBiometrii(
            f"Minęło {pol.dni} dni od ostatniego logowania hasłem. Zaloguj się ponownie.",
            "pelne_logowanie")
    if user.tenant_id:
        firma = db.get(Tenant, user.tenant_id)
        if firma is None or not firma.is_active:
            raise BladBiometrii("firma jest nieaktywna", "konto")

    urzadzenie.ostatnie_wyzwanie = dane["t"]
    urzadzenie.ostatnio_uzyte = utcnow()
    return urzadzenie, user


def odlacz(urzadzenie: UrzadzenieMobilne, powod: str) -> None:
    if urzadzenie.odlaczono is None:
        urzadzenie.odlaczono = utcnow()
        urzadzenie.powod_odlaczenia = powod[:200]


def urzadzenia_konta(db: Session, user_id: str, z_odlaczonymi: bool = False) -> list:
    zapytanie = select(UrzadzenieMobilne).where(UrzadzenieMobilne.user_id == user_id)
    if not z_odlaczonymi:
        zapytanie = zapytanie.where(UrzadzenieMobilne.odlaczono.is_(None))
    return db.execute(zapytanie.order_by(UrzadzenieMobilne.dodano.desc())).scalars().all()


def odlacz_wszystkie(db: Session, user_id: str, powod: str) -> int:
    """Wylogowanie wszedzie i zmiana hasla odlaczaja tez biometrie -
    inaczej telefon logowalby sie dalej palcem na konto, ktorego wlasciciel
    wlasnie chcial wszystkich wyrzucic."""
    ile = 0
    for urzadzenie in urzadzenia_konta(db, user_id):
        odlacz(urzadzenie, powod)
        ile += 1
    return ile
