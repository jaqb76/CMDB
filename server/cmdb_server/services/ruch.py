"""Statystyki ruchu portalu: liczba zapytan, bledy i czasy odpowiedzi.

Liczymy w pamieci procesu, a do bazy trafia suma z pol minuty. Zapis przy
kazdym zapytaniu podwajalby prace bazy przy kazdym raporcie agenta - czyli
spowalnial dokladnie to, co mamy mierzyc.

Czego to NIE jest: dziennika dostepu. Nie zapisujemy adresow, uzytkownikow
ani sciezek - wylacznie liczby w kilku grupach. Pelny dziennik ma nginx.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import StatystykaRuchu, utcnow

log = logging.getLogger(__name__)

# Gorne granice przedzialow czasu odpowiedzi (ms) i ich kolumny.
PROGI_MS = (50, 100, 250, 500, 1000, 2500)
KOLUMNY = ("do_50", "do_100", "do_250", "do_500", "do_1000", "do_2500", "powyzej")

CO_ILE_SEKUND = 30
DNI_HISTORII = 30

# Kolejnosc ma znaczenie: pierwsze pasujace wygrywa.
GRUPY = (
    ("/api/v1/mobile", "mobilna"),
    ("/api/", "agenty"),
    ("/static/", "statyczne"),
    ("/admin", "administracja"),
    ("/login", "logowanie"),
    ("/logowanie", "logowanie"),
)
NAZWY_GRUP = {
    "portal": "Portal firmowy",
    "agenty": "API agentów",
    "mobilna": "Aplikacja mobilna",
    "administracja": "Administracja",
    "logowanie": "Logowanie",
    "statyczne": "Pliki statyczne",
}

_licznik: dict[tuple[datetime, str], dict] = {}
_blokada = threading.Lock()


def grupa(sciezka: str) -> str:
    for poczatek, nazwa in GRUPY:
        if sciezka.startswith(poczatek):
            return nazwa
    return "portal"


def _przedzial(ms: float) -> str:
    for prog, kolumna in zip(PROGI_MS, KOLUMNY):
        if ms <= prog:
            return kolumna
    return KOLUMNY[-1]


def zanotuj(sciezka: str, status: int, ms: float, chwila: datetime | None = None) -> None:
    """Jedno zapytanie. Wywolywane z middleware - musi byc tanie."""
    minuta = (chwila or utcnow()).replace(second=0, microsecond=0)
    klucz = (minuta, grupa(sciezka))
    with _blokada:
        wpis = _licznik.get(klucz)
        if wpis is None:
            wpis = _licznik[klucz] = {"liczba": 0, "bledy_4xx": 0, "bledy_5xx": 0,
                                      "suma_ms": 0.0, "maks_ms": 0.0,
                                      **{k: 0 for k in KOLUMNY}}
        wpis["liczba"] += 1
        if 400 <= status < 500:
            wpis["bledy_4xx"] += 1
        elif status >= 500:
            wpis["bledy_5xx"] += 1
        wpis["suma_ms"] += ms
        wpis["maks_ms"] = max(wpis["maks_ms"], ms)
        wpis[_przedzial(ms)] += 1


def zrzuc(db: Session) -> int:
    """Dopisuje zebrane liczby do bazy. Zwraca liczbe zapisanych wierszy."""
    with _blokada:
        porcja = dict(_licznik)
        _licznik.clear()
    if not porcja:
        return 0
    tabela = StatystykaRuchu.__table__
    for (minuta, nazwa), wpis in porcja.items():
        zapytanie = insert(tabela).values(minuta=minuta, grupa=nazwa, **wpis)
        dodaj = {k: tabela.c[k] + zapytanie.excluded[k]
                 for k in ("liczba", "bledy_4xx", "bledy_5xx", "suma_ms", *KOLUMNY)}
        dodaj["maks_ms"] = func.greatest(tabela.c.maks_ms, zapytanie.excluded.maks_ms)
        db.execute(zapytanie.on_conflict_do_update(
            index_elements=[tabela.c.minuta, tabela.c.grupa], set_=dodaj))
    db.commit()
    return len(porcja)


def sprzataj(db: Session) -> None:
    db.execute(delete(StatystykaRuchu).where(
        StatystykaRuchu.minuta < utcnow() - timedelta(days=DNI_HISTORII)))
    db.commit()


def petla(zatrzymaj: threading.Event) -> None:
    """Watek procesu roboczego: zapis co CO_ILE_SEKUND i raz na godzine sprzatanie."""
    from ..db import SessionLocal

    ostatnie_sprzatanie = utcnow()
    while not zatrzymaj.wait(CO_ILE_SEKUND):
        try:
            with SessionLocal() as db:
                zrzuc(db)
                if utcnow() - ostatnie_sprzatanie > timedelta(hours=1):
                    sprzataj(db)
                    ostatnie_sprzatanie = utcnow()
        except Exception:  # noqa: BLE001 - statystyki nie moga polozyc portalu
            log.warning("nie zapisalem statystyk ruchu", exc_info=True)
    # Przy zamykaniu procesu zapisujemy resztke, zeby restart nie gubil minuty.
    try:
        with SessionLocal() as db:
            zrzuc(db)
    except Exception:  # noqa: BLE001
        log.warning("nie zapisalem statystyk ruchu przy zamykaniu", exc_info=True)


def percentyl(przedzialy: dict[str, int], ulamek: float) -> float | None:
    """Gorna granica przedzialu, w ktorym wypada percentyl (np. 0.95)."""
    razem = sum(przedzialy.get(k, 0) for k in KOLUMNY)
    if not razem:
        return None
    prog_liczby = razem * ulamek
    narastajaco = 0
    for kolumna, granica in zip(KOLUMNY, (*PROGI_MS, None)):
        narastajaco += przedzialy.get(kolumna, 0)
        if narastajaco >= prog_liczby:
            return float(granica) if granica is not None else float(PROGI_MS[-1] * 2)
    return float(PROGI_MS[-1] * 2)
