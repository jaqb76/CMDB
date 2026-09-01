"""Rodzaje sprzetu jako slownik i pola wlasciwe dla rodzaju.

Rodzaj przestaje byc stala lista w kodzie: firma dokłada "Projektor" albo
"UPS" bez czekania na wydanie serwera. Kazdy rodzaj niesie przy tym wlasny
zestaw pol, bo monitor opisuje sie przekatna, a przelacznik liczba portow -
jeden wspolny zestaw dla wszystkiego oznaczalby, ze przy monitorze wisi
dwadziescia pustych rubryk, a pusta rubryka przestaje odrozniac
"nie wpisano" od "nie dotyczy".

Podzial na dwie warstwy jest tu istotny:

* **wspolne** - numer seryjny, gwarancja, lokalizacja, dostawca, opiekun.
  Zostaja kolumnami tabeli, bo stoja na nich wykrywanie duplikatow, raport
  gwarancyjny i zestawienia. Musza znaczyc to samo w kazdej firmie.
* **wlasciwe dla rodzaju** - opisane schematem, trzymane w jednej kolumnie
  JSONB przy sprzecie.

Klucz rodzaju jest jego tozsamoscia: leza po nim wpisy sprzetu, a piec z nich
rozpoznaja relacje. Etykiete zmienia sie dowolnie, klucza nigdy.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Asset, SchematSlownika, WpisSlownika, utcnow
from . import schemat as definicje
from . import slowniki, wzorzec
from .scoping import TenantContext

log = logging.getLogger(__name__)

KATEGORIA = "rodzaj"


def _klucz_wpisu(wpis: WpisSlownika) -> str:
    return str((wpis.atrybuty or {}).get("klucz_rodzaju") or "").strip()


def zapewnij_startowe(db: Session, ctx: TenantContext) -> None:
    """Zaklada rodzaje wzorcowe, gdy firma nie ma jeszcze zadnego.

    Klucze musza zgadzac sie z tymi, ktore leza juz przy sprzecie - inaczej
    istniejace maszyny zostalyby bez rozpoznawalnego rodzaju.
    """
    slowniki.schemat(db, ctx, KATEGORIA)
    ile = db.execute(select(func.count()).select_from(WpisSlownika).where(
        WpisSlownika.tenant_id == ctx.tenant_id, WpisSlownika.kategoria == KATEGORIA
    )).scalar_one()
    if ile:
        return
    for klucz, nazwa in wzorzec.RODZAJE_STARTOWE:
        wpis = WpisSlownika(
            tenant_id=ctx.tenant_id, kategoria=KATEGORIA, wartosc=nazwa,
            klucz=nazwa.casefold(),
            atrybuty={"nazwa": nazwa, "klucz_rodzaju": klucz},
            utworzyl="wzorzec",
        )
        try:
            with db.begin_nested():
                db.add(wpis)
        except IntegrityError:
            continue


def wszystkie(db: Session, ctx: TenantContext) -> list[WpisSlownika]:
    zapewnij_startowe(db, ctx)
    return slowniki.wpisy(db, ctx, KATEGORIA)


def etykiety(db: Session, ctx: TenantContext) -> dict[str, str]:
    """Klucz rodzaju -> nazwa widoczna dla czlowieka."""
    return {_klucz_wpisu(w): w.wartosc for w in wszystkie(db, ctx) if _klucz_wpisu(w)}


def wpis_rodzaju(db: Session, ctx: TenantContext, klucz: str) -> WpisSlownika | None:
    if not klucz:
        return None
    return next((w for w in wszystkie(db, ctx) if _klucz_wpisu(w) == klucz), None)


def uzycie(db: Session, ctx: TenantContext, klucz: str) -> int:
    """Ile sprzetu ma ten rodzaj. Rodzaju w uzyciu nie wolno usunac."""
    if not klucz:
        return 0
    return int(db.execute(select(func.count()).select_from(Asset).where(
        Asset.tenant_id == ctx.tenant_id, Asset.typ == klucz
    )).scalar_one())


# --- pola wlasciwe dla rodzaju ----------------------------------------------

def schemat_pol(db: Session, ctx: TenantContext, klucz: str) -> definicje.Schemat:
    """Zestaw pol tego rodzaju; przy pierwszym uzyciu kopia wzorca."""
    if not klucz:
        return definicje.Schemat(kategoria=definicje.KATEGORIA_SPRZETU, pola=[])
    wiersz = db.execute(select(SchematSlownika).where(
        SchematSlownika.tenant_id == ctx.tenant_id,
        SchematSlownika.kategoria == definicje.KATEGORIA_SPRZETU,
        SchematSlownika.rodzaj == klucz,
    )).scalar_one_or_none()
    if wiersz is not None:
        return definicje.Schemat.model_validate(wiersz.definicja)

    swiezy = wzorzec.wzorcowe_pola_rodzaju(klucz)
    try:
        with db.begin_nested():
            db.add(SchematSlownika(
                tenant_id=ctx.tenant_id, kategoria=definicje.KATEGORIA_SPRZETU,
                rodzaj=klucz, definicja=swiezy.model_dump(), zmienil="wzorzec"))
    except IntegrityError:
        pass
    return swiezy


def zapisz_schemat_pol(db: Session, ctx: TenantContext, klucz: str,
                       nowy: definicje.Schemat) -> definicje.Schemat:
    schemat_pol(db, ctx, klucz)          # gwarantuje istnienie wiersza
    wiersz = db.execute(select(SchematSlownika).where(
        SchematSlownika.tenant_id == ctx.tenant_id,
        SchematSlownika.kategoria == definicje.KATEGORIA_SPRZETU,
        SchematSlownika.rodzaj == klucz,
    )).scalar_one()
    poprzedni = definicje.Schemat.model_validate(wiersz.definicja)
    nowy.kategoria = definicje.KATEGORIA_SPRZETU
    nowy.wersja = poprzedni.wersja + 1
    wiersz.definicja = nowy.model_dump()
    wiersz.zmieniony = utcnow()
    wiersz.zmienil = ctx.actor
    return nowy


def uzycie_pola(db: Session, ctx: TenantContext, klucz_rodzaju: str, klucz: str) -> int:
    """Ile sprzetu ma wartosc w tym polu - pola z wartosciami sie nie usuwa."""
    return int(db.execute(select(func.count()).select_from(Asset).where(
        Asset.tenant_id == ctx.tenant_id,
        Asset.typ == klucz_rodzaju,
        Asset.atrybuty[klucz].isnot(None),
    )).scalar_one())


def wyczysc_pole(db: Session, ctx: TenantContext, klucz_rodzaju: str, klucz: str) -> int:
    ile = 0
    for sprzet in db.execute(select(Asset).where(
        Asset.tenant_id == ctx.tenant_id, Asset.typ == klucz_rodzaju
    )).scalars():
        dane = dict(sprzet.atrybuty or {})
        if dane.pop(klucz, None) is not None:
            sprzet.atrybuty = dane
            ile += 1
    return ile


# --- wartosci przy sprzecie -------------------------------------------------

def zapisz_atrybuty(db: Session, ctx: TenantContext, sprzet: Asset, dane: dict) -> dict:
    """Sprawdza i zapisuje pola wlasciwe dla rodzaju tego sprzetu.

    Wartosci pol, ktore do biezacego rodzaju nie naleza, ZOSTAJA nietkniete.
    Zmiana rodzaju ma je ukryc, a nie skasowac - powrot do poprzedniego
    rodzaju ma je odzyskac.
    """
    opis = schemat_pol(db, ctx, sprzet.typ or "")
    sprawdzone = definicje.sprawdz(opis, dane, egzekwuj_wymagane=True)
    zachowane = {k: v for k, v in (sprzet.atrybuty or {}).items()
                 if opis.pole(k) is None}
    sprzet.atrybuty = {**zachowane, **sprawdzone}
    return sprawdzone


def ukryte_przy_zmianie(db: Session, ctx: TenantContext, sprzet: Asset,
                        nowy_rodzaj: str) -> list[str]:
    """Etykiety pol, ktore po zmianie rodzaju znikna z widoku.

    Wartosci zostaja w bazie - ostrzezenie ma powiedziec, ile ich jest,
    zeby zmiana rodzaju nie wygladala na skasowanie danych.
    """
    if not sprzet.atrybuty or (sprzet.typ or "") == nowy_rodzaj:
        return []
    stary = schemat_pol(db, ctx, sprzet.typ or "")
    nowy = schemat_pol(db, ctx, nowy_rodzaj)
    return [pole.etykieta for pole in stary.pola
            if sprzet.atrybuty.get(pole.klucz) not in (None, "", [])
            and nowy.pole(pole.klucz) is None]


def widoczne(db: Session, ctx: TenantContext, sprzet: Asset) -> list[dict[str, str]]:
    """Pola rodzaju z wartosciami - do pokazania na karcie sprzetu."""
    opis = schemat_pol(db, ctx, sprzet.typ or "")
    wynik: list[dict[str, str]] = []
    for pole in opis.pola:
        wartosc = (sprzet.atrybuty or {}).get(pole.klucz)
        if wartosc in (None, "", []):
            continue
        if pole.typ == "logiczna":
            wartosc = "tak" if wartosc else "nie"
        wynik.append({"klucz": pole.klucz, "etykieta": pole.etykieta,
                      "wartosc": str(wartosc)})
    return wynik
