"""Poswiadczenia: tokeny agentow, hasla panelu, sesje i CSRF.

Tokeny maja postac:  cmdb_<typ>_<prefix>_<sekret>
  * prefix - jawny, sluzy wylacznie do wyszukania wiersza w bazie (indeks),
  * sekret - 256 bitow entropii; w bazie trzymamy tylko SHA-256 calego tokenu.
Poniewaz sekret jest losowy i ma pelna entropie, SHA-256 wystarcza (nie ma
czego zgadywac slownikiem) - w odroznieniu od hasel, gdzie uzywamy Argon2id.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

TOKEN_PREFIX_LEN = 12
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class GeneratedToken:
    """Wynik wygenerowania tokenu - wartosc jawna widoczna tylko raz."""

    plaintext: str
    prefix: str
    token_hash: str


def _random_prefix() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_PREFIX_LEN))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(kind: str) -> GeneratedToken:
    """kind: 'ent' (enrollment, per firma) albo 'agt' (per agent)."""
    prefix = _random_prefix()
    secret = secrets.token_urlsafe(32)
    plaintext = f"cmdb_{kind}_{prefix}_{secret}"
    return GeneratedToken(plaintext=plaintext, prefix=prefix, token_hash=hash_token(plaintext))


def parse_token(token: str) -> tuple[str, str] | None:
    """Zwraca (kind, prefix) albo None gdy format jest niepoprawny."""
    parts = token.split("_")
    if len(parts) < 4 or parts[0] != "cmdb":
        return None
    kind, prefix = parts[1], parts[2]
    if kind not in {"ent", "agt"} or len(prefix) != TOKEN_PREFIX_LEN:
        return None
    return kind, prefix


def verify_token(token: str, expected_hash: str) -> bool:
    """Porownanie w czasie stalym."""
    return hmac.compare_digest(hash_token(token), expected_hash)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


# --- sesje panelu WWW -------------------------------------------------------

def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=salt)


def sign_session(data: dict) -> str:
    return _serializer("cmdb-session").dumps(data)


def load_session(raw: str) -> dict | None:
    try:
        return _serializer("cmdb-session").loads(raw, max_age=get_settings().session_max_age)
    except (BadSignature, SignatureExpired):
        return None


# Jak dlugo wazny jest klucz z kodu QR. Tyle, zeby zdazyc zeskanowac kod
# i zaczac pobieranie - a nie tyle, zeby wklejony komus adres dzialal jutro.
KLUCZ_POBRANIA_MAX_AGE = 30 * 60


def sign_download_key(user_id: str, session_version: int) -> str:
    """Klucz jednorazowego pobrania wstawiany w adres kodu QR.

    Telefon, ktory skanuje kod, nie ma sesji portalu, a przekierowanie na
    logowanie i tak nie wrocilo by pod ten adres - wiec zamiast sesji adres
    niesie podpisany, krotko wazny klucz wystawiony osobie, ktora ten kod
    ogladala. Zmiana hasla podbija ``session_version`` i uniewaznia klucz
    razem z sesjami tego konta.
    """
    return _serializer("cmdb-pobranie").dumps({"uid": user_id, "sv": session_version})


def load_download_key(raw: str) -> dict | None:
    try:
        dane = _serializer("cmdb-pobranie").loads(raw, max_age=KLUCZ_POBRANIA_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return dane if isinstance(dane, dict) and dane.get("uid") else None


def issue_csrf_token(session_id: str) -> str:
    return _serializer("cmdb-csrf").dumps(session_id)


def check_csrf_token(raw: str, session_id: str, max_age: int = 86400) -> bool:
    try:
        value = _serializer("cmdb-csrf").loads(raw, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(value), session_id)
