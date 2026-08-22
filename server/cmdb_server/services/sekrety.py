"""Szyfrowanie sekretow, ktore trzeba odzyskac w postaci jawnej.

Tokeny i hasla uzytkownikow trzymamy jako skroty - nigdy ich nie odczytujemy,
tylko porownujemy. Haslo SMTP jest inne: serwer musi podac je przy kazdym
polaczeniu, wiec skrot nie wystarcza i potrzebne jest szyfrowanie odwracalne.

Klucz wyprowadzamy z CMDB_SECRET_KEY, ktory i tak musi byc ustawiony
i chroniony - dodatkowy sekret oznaczalby drugie miejsce do zgubienia.
Konsekwencja: zmiana CMDB_SECRET_KEY uniewaznia zapisane hasla SMTP i trzeba
je wpisac ponownie. To ten sam kompromis co przy ciasteczkach sesji.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings

log = logging.getLogger(__name__)

# Etykieta oddziela klucz do hasel SMTP od innych zastosowan tego samego
# sekretu - ten sam material wejsciowy nie powinien dawac tego samego klucza
# w dwoch roznych miejscach.
ETYKIETA = b"cmdb-smtp-v1"


def _klucz() -> bytes:
    surowy = hashlib.pbkdf2_hmac(
        "sha256", get_settings().secret_key.encode("utf-8"), ETYKIETA, 200_000, dklen=32
    )
    return base64.urlsafe_b64encode(surowy)


def zaszyfruj(jawne: str) -> str:
    return Fernet(_klucz()).encrypt(jawne.encode("utf-8")).decode("ascii")


def odszyfruj(zaszyfrowane: str | None) -> str | None:
    """Haslo w postaci jawnej albo None, gdy nie da sie go odczytac.

    Nieudane odszyfrowanie znaczy zwykle, ze zmienil sie CMDB_SECRET_KEY.
    Zwracamy None zamiast rzucac wyjatkiem, bo to nie jest awaria serwera -
    to sytuacja, w ktorej administrator musi wpisac haslo ponownie.
    """
    if not zaszyfrowane:
        return None
    try:
        return Fernet(_klucz()).decrypt(zaszyfrowane.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        log.warning("nie moge odszyfrowac zapisanego hasla SMTP: %s", type(exc).__name__)
        return None
