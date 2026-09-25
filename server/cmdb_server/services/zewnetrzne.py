"""Logowanie kontem Google, Microsoft albo GitHub.

Konto u zewnetrznego dostawcy mowi tylko, KIM ktos jest - nie, w jakiej
firmie pracuje ani co mu wolno. Dlatego samo zalogowanie przez Google nie
daje zadnego dostepu: konto zewnetrzne dziala wylacznie wtedy, gdy zostalo
wczesniej powiazane z kontem CMDB - z zaproszenia albo przez zalogowana osobe.
Uprawnienia zawsze nadaje CMDB.

Przebieg (authorization code + PKCE):
  1. ``adres_autoryzacji`` - przekierowanie do dostawcy ze ``state``,
     ``nonce`` i ``code_challenge`` zapamietanymi w podpisanym ciasteczku,
  2. dostawca wraca z kodem, ``tozsamosc_z_kodu`` wymienia go na tokeny
     i zwraca ``Tozsamosc(dostawca, sub, email)``.

id_token od Google i Microsoft dostajemy bezposrednio z endpointu tokenow
przez TLS, wiec - zgodnie z OpenID Connect Core 3.1.3.7 - weryfikacja TLS
zastepuje sprawdzanie podpisu. Pozostale pola (iss, aud, exp, nonce)
sprawdzamy zawsze. GitHub nie ma OIDC: pytamy jego API o /user i /user/emails.

Zapytania HTTP ida przez ``zapytanie_http`` - testy podstawiaja tam atrape.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..config import get_settings

log = logging.getLogger(__name__)

LIMIT_SEKUND = 10


@dataclass(frozen=True)
class Dostawca:
    klucz: str
    nazwa: str
    autoryzacja: str
    tokeny: str
    zakres: str
    oidc: bool


DOSTAWCY = {
    "google": Dostawca(
        "google", "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email profile", True,
    ),
    "microsoft": Dostawca(
        "microsoft", "Microsoft",
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "openid email profile", True,
    ),
    "github": Dostawca(
        "github", "GitHub",
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email", False,
    ),
}

WYDAWCY_GOOGLE = {"https://accounts.google.com", "accounts.google.com"}
WYDAWCA_MICROSOFT = re.compile(r"^https://login\.microsoftonline\.com/[0-9a-f-]{36}/v2\.0$")


class BladZewnetrzny(RuntimeError):
    """Dostawca odmowil albo zwrocil cos, czemu nie ufamy."""


@dataclass(frozen=True)
class Tozsamosc:
    dostawca: str
    sub: str
    email: str | None
    nazwa: str | None


def _poswiadczenia(klucz: str) -> tuple[str, str]:
    ustawienia = get_settings()
    identyfikator = getattr(ustawienia, f"{klucz}_client_id", "") or ""
    sekret = getattr(ustawienia, f"{klucz}_client_secret", None)
    return identyfikator.strip(), (sekret.get_secret_value() if sekret else "").strip()


def wlaczeni() -> list[Dostawca]:
    return [d for k, d in DOSTAWCY.items() if all(_poswiadczenia(k))]


def dostawca(klucz: str) -> Dostawca:
    d = DOSTAWCY.get(klucz)
    if d is None or not all(_poswiadczenia(klucz)):
        raise BladZewnetrzny("ten sposób logowania nie jest włączony na tym serwerze")
    return d


# --- krok 1: przekierowanie -------------------------------------------------

def _b64(dane: bytes) -> str:
    return base64.urlsafe_b64encode(dane).rstrip(b"=").decode("ascii")


def nowy_stan(klucz: str, cel: str, **dodatkowe) -> dict:
    """Dane do ciasteczka: state, nonce, weryfikator PKCE i cel logowania."""
    return {"dostawca": klucz, "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24), "weryfikator": secrets.token_urlsafe(48),
            "cel": cel, **dodatkowe}


def adres_autoryzacji(stan: dict, adres_zwrotny: str, podpowiedz: str | None = None) -> str:
    d = dostawca(stan["dostawca"])
    identyfikator, _ = _poswiadczenia(d.klucz)
    parametry = {
        "client_id": identyfikator,
        "redirect_uri": adres_zwrotny,
        "response_type": "code",
        "scope": d.zakres,
        "state": stan["state"],
        "code_challenge": _b64(hashlib.sha256(stan["weryfikator"].encode()).digest()),
        "code_challenge_method": "S256",
    }
    if d.oidc:
        parametry["nonce"] = stan["nonce"]
        # Za kazdym razem wybor konta - wspolny komputer nie loguje po cichu
        # ostatniej osoby.
        parametry["prompt"] = "select_account"
    if podpowiedz:
        parametry["login_hint"] = podpowiedz
    return d.autoryzacja + "?" + urllib.parse.urlencode(parametry)


# --- krok 2: kod -> tozsamosc -----------------------------------------------

def _zapytanie_domyslne(metoda: str, adres: str, dane: dict | None = None,
                        naglowki: dict | None = None) -> dict | list:
    tresc = urllib.parse.urlencode(dane).encode() if dane is not None else None
    zadanie = urllib.request.Request(adres, data=tresc, method=metoda, headers={
        "Accept": "application/json", "User-Agent": "CMDB", **(naglowki or {})})
    try:
        with urllib.request.urlopen(zadanie, timeout=LIMIT_SEKUND) as odp:
            return json.loads(odp.read().decode("utf-8"))
    except urllib.error.HTTPError as blad:
        try:
            szczegoly = json.loads(blad.read().decode("utf-8"))
        except ValueError:
            szczegoly = {}
        raise BladZewnetrzny(
            f"{blad.code}: {szczegoly.get('error_description') or szczegoly.get('error') or blad.reason}"
        ) from blad
    except (urllib.error.URLError, TimeoutError, ValueError) as blad:
        raise BladZewnetrzny(f"brak odpowiedzi dostawcy: {blad}") from blad


# Podmieniane w testach.
zapytanie_http = _zapytanie_domyslne


def _zapytaj(*args, **kwargs):
    return zapytanie_http(*args, **kwargs)


def _payload_jwt(token: str) -> dict:
    try:
        _, srodek, _ = token.split(".")
        return json.loads(base64.urlsafe_b64decode(srodek + "=" * (-len(srodek) % 4)))
    except (ValueError, json.JSONDecodeError) as blad:
        raise BladZewnetrzny("nieczytelny id_token") from blad


def _sprawdz_id_token(d: Dostawca, token: str, nonce: str) -> dict:
    identyfikator, _ = _poswiadczenia(d.klucz)
    dane = _payload_jwt(token)
    aud = dane.get("aud")
    if identyfikator not in (aud if isinstance(aud, list) else [aud]):
        raise BladZewnetrzny("id_token wystawiony dla innej aplikacji")
    if int(dane.get("exp", 0)) < time.time() - 60:
        raise BladZewnetrzny("id_token wygasł")
    if not secrets.compare_digest(str(dane.get("nonce", "")), nonce):
        raise BladZewnetrzny("id_token nie pasuje do tego logowania")
    iss = str(dane.get("iss", ""))
    if d.klucz == "google" and iss not in WYDAWCY_GOOGLE:
        raise BladZewnetrzny("nieznany wystawca tokenu")
    if d.klucz == "microsoft" and not WYDAWCA_MICROSOFT.match(iss):
        raise BladZewnetrzny("nieznany wystawca tokenu")
    if not dane.get("sub"):
        raise BladZewnetrzny("token bez identyfikatora osoby")
    return dane


def tozsamosc_z_kodu(stan: dict, kod: str, adres_zwrotny: str) -> Tozsamosc:
    d = dostawca(stan["dostawca"])
    identyfikator, sekret = _poswiadczenia(d.klucz)
    tokeny = _zapytaj("POST", d.tokeny, {
        "grant_type": "authorization_code", "code": kod, "redirect_uri": adres_zwrotny,
        "client_id": identyfikator, "client_secret": sekret,
        "code_verifier": stan["weryfikator"],
    })
    if not isinstance(tokeny, dict) or tokeny.get("error"):
        raise BladZewnetrzny(str((tokeny or {}).get("error_description") or "odmowa wymiany kodu"))

    if d.oidc:
        if not tokeny.get("id_token"):
            raise BladZewnetrzny("dostawca nie zwrócił id_token")
        dane = _sprawdz_id_token(d, tokeny["id_token"], stan["nonce"])
        email = dane.get("email") or dane.get("preferred_username")
        if d.klucz == "google" and dane.get("email_verified") is False:
            email = None
        # Microsoft: sub jest staly dla pary (osoba, aplikacja) - dokladnie tego
        # potrzebujemy. Dodajemy tid, zeby ten sam sub w dwoch dzierzawach nie
        # mogl sie pomylic.
        sub = str(dane["sub"]) if d.klucz != "microsoft" else f"{dane.get('tid', '')}:{dane['sub']}"
        return Tozsamosc(d.klucz, sub, (email or "").lower() or None, dane.get("name"))

    dostep = tokeny.get("access_token")
    if not dostep:
        raise BladZewnetrzny("dostawca nie zwrócił tokenu dostępu")
    naglowki = {"Authorization": f"Bearer {dostep}", "Accept": "application/vnd.github+json"}
    osoba = _zapytaj("GET", "https://api.github.com/user", None, naglowki)
    if not isinstance(osoba, dict) or not osoba.get("id"):
        raise BladZewnetrzny("GitHub nie zwrócił konta")
    email = None
    adresy = _zapytaj("GET", "https://api.github.com/user/emails", None, naglowki)
    if isinstance(adresy, list):
        glowny = next((a for a in adresy if a.get("primary") and a.get("verified")), None)
        email = glowny.get("email") if glowny else None
    return Tozsamosc("github", str(osoba["id"]), (email or "").lower() or None,
                     osoba.get("name") or osoba.get("login"))
