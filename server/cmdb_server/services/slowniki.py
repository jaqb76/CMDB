"""Slowniki firmowe: dzialy, lokalizacje, dostawcy.

Wpis slownika jest RZECZA, a nie slowem: maszyna wskazuje go odwolaniem, wiec
wszystko, co przy nim zapisano - telefon, adres, kanal zgloszen - jest w jej
zasiegu. Kopia napisu, ktora byla tu wczesniej, nie mogla niesc niczego.

Zasada pozostaje odwrotna niz w klasycznym slowniku: nie blokujemy wpisania
nowej wartosci, tylko ja zapamietujemy. Wymuszanie wyboru z listy konczy sie
tym, ze ktos nie znajduje swojego dzialu i zostawia pole puste - a wtedy nie ma
ani porzadku, ani danych.

Klucz unikalnosci liczymy z nazwy zlozonej do malych liter i bez podwojnych
spacji, wiec "Magazyn" wpisany drugi raz jako "magazyn " nie zalozy drugiego
wpisu. Wyswietlamy pierwsza wersje, jaka wpisal czlowiek.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import KATEGORIE_SLOWNIKA, Asset, Owner, SchematSlownika, WpisSlownika, utcnow
from . import schemat as definicje
from . import wzorzec
from .scoping import TenantContext

MAKS_DLUGOSC = 200

# Co wskazuje wpis danej kategorii. Dzial nalezy do OSOBY, nie do maszyny:
# jedna osoba pracuje w dziale, a sprzet ma opiekuna - dzial wynika stad.
WLASCICIEL = {
    "dzial": ("Owner", "dzial_id"),
    "lokalizacja": ("Asset", "lokalizacja_id"),
    "dostawca": ("Asset", "dostawca_id"),
}


def znormalizuj(wartosc: str | None) -> str:
    return " ".join((wartosc or "").split()).strip()


def _klucz(wartosc: str) -> str:
    return znormalizuj(wartosc).lower()


def sprawdz_kategorie(kategoria: str) -> str:
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise ValueError(f"nieznana kategoria slownika: {kategoria}")
    return kategoria


# --- schemat ----------------------------------------------------------------

def schemat(db: Session, ctx: TenantContext, kategoria: str) -> definicje.Schemat:
    """Schemat tej firmy; przy pierwszym uzyciu kopia wzorca.

    Kopia, nie wspoldzielenie: od tej chwili schemat nalezy do firmy i pozniejsze
    poprawki wzorca juz do niej nie wracaja.
    """
    sprawdz_kategorie(kategoria)
    wiersz = db.execute(
        select(SchematSlownika).where(
            SchematSlownika.tenant_id == ctx.tenant_id,
            SchematSlownika.kategoria == kategoria,
        )
    ).scalar_one_or_none()
    if wiersz is not None:
        return definicje.Schemat.model_validate(wiersz.definicja)

    swiezy = wzorzec.wzorcowy(kategoria)
    try:
        with db.begin_nested():
            db.add(SchematSlownika(tenant_id=ctx.tenant_id, kategoria=kategoria,
                                   definicja=swiezy.model_dump(), zmienil="wzorzec"))
    except IntegrityError:
        # Dwa rownolegle zadania moga zalozyc schemat naraz - wynik jest ten sam.
        pass
    return swiezy


def zapisz_schemat(db: Session, ctx: TenantContext, kategoria: str,
                   nowy: definicje.Schemat) -> definicje.Schemat:
    """Podmienia schemat firmy. Wywolujacy sprawdza uprawnienia."""
    sprawdz_kategorie(kategoria)
    schemat(db, ctx, kategoria)          # gwarantuje istnienie wiersza
    wiersz = db.execute(
        select(SchematSlownika).where(
            SchematSlownika.tenant_id == ctx.tenant_id,
            SchematSlownika.kategoria == kategoria,
        )
    ).scalar_one()
    poprzedni = definicje.Schemat.model_validate(wiersz.definicja)
    nowy.wersja = poprzedni.wersja + 1
    nowy.kategoria = kategoria
    wiersz.definicja = nowy.model_dump()
    wiersz.zmieniony = utcnow()
    wiersz.zmienil = ctx.actor
    return nowy


def uzycie_pola(db: Session, ctx: TenantContext, kategoria: str, klucz: str) -> int:
    """Ile wpisow ma wartosc w tym polu.

    Pole z wartosciami sie nie usuwa - trzeba je najpierw jawnie wyczyscic.
    Ta liczba jest tresc komunikatu, ktory to tlumaczy.
    """
    return int(db.execute(
        select(func.count()).select_from(WpisSlownika).where(
            WpisSlownika.tenant_id == ctx.tenant_id,
            WpisSlownika.kategoria == kategoria,
            WpisSlownika.atrybuty[klucz].isnot(None),
        )
    ).scalar_one())


def wyczysc_pole(db: Session, ctx: TenantContext, kategoria: str, klucz: str) -> int:
    """Usuwa wartosci jednego pola ze wszystkich wpisow. Zwraca ile."""
    ile = 0
    for wpis in db.execute(
        select(WpisSlownika).where(
            WpisSlownika.tenant_id == ctx.tenant_id,
            WpisSlownika.kategoria == kategoria,
        )
    ).scalars():
        dane = dict(wpis.atrybuty or {})
        if dane.pop(klucz, None) is not None:
            wpis.atrybuty = dane
            ile += 1
    return ile


# --- wpisy ------------------------------------------------------------------

def zapewnij(db: Session, ctx: TenantContext, kategoria: str,
             wartosc: str | None) -> WpisSlownika | None:
    """Wpis o tej nazwie; tworzy szkic, gdy jeszcze go nie ma.

    To sciezka "mimochodem" - z formularza maszyny. Pola wymagane sa tu
    pomijane celowo: gdyby blokowaly, dodanie maszyny od nieznanego dostawcy
    zaczynaloby sie od wypelniania jego metryczki i nikt nie wpisalby niczego.
    """
    sprawdz_kategorie(kategoria)
    czysta = znormalizuj(wartosc)[:MAKS_DLUGOSC]
    if not czysta:
        return None

    klucz = _klucz(czysta)
    istniejacy = _wg_klucza(db, ctx, kategoria, klucz)
    if istniejacy is not None:
        return istniejacy

    wpis = WpisSlownika(tenant_id=ctx.tenant_id, kategoria=kategoria, wartosc=czysta,
                        klucz=klucz, atrybuty={}, utworzyl=ctx.actor)
    try:
        # Punkt zapisu, a nie zwykly flush: wycofanie calej transakcji
        # skasowaloby takze zmiane, dla ktorej ta wartosc powstaje.
        with db.begin_nested():
            db.add(wpis)
    except IntegrityError:
        return _wg_klucza(db, ctx, kategoria, klucz)
    return wpis


def _wg_klucza(db: Session, ctx: TenantContext, kategoria: str, klucz: str):
    return db.execute(
        select(WpisSlownika).where(
            WpisSlownika.tenant_id == ctx.tenant_id,
            WpisSlownika.kategoria == kategoria,
            WpisSlownika.klucz == klucz,
        )
    ).scalar_one_or_none()


def wpis(db: Session, ctx: TenantContext, wpis_id: str) -> WpisSlownika | None:
    return db.execute(
        select(WpisSlownika).where(
            WpisSlownika.id == wpis_id, WpisSlownika.tenant_id == ctx.tenant_id
        )
    ).scalar_one_or_none()


def zapisz_wpis(db: Session, ctx: TenantContext, cel: WpisSlownika, nazwa: str,
                dane: dict) -> WpisSlownika:
    """Swiadoma edycja w karcie slownika - tu pola wymagane obowiazuja."""
    czysta = znormalizuj(nazwa)[:MAKS_DLUGOSC]
    if not czysta:
        raise definicje.BladPola({"nazwa": "nazwa nie moze byc pusta"})
    klucz = _klucz(czysta)
    kolizja = _wg_klucza(db, ctx, cel.kategoria, klucz)
    if kolizja is not None and kolizja.id != cel.id:
        raise definicje.BladPola({"nazwa": "taka nazwa juz istnieje w slowniku"})

    opis = schemat(db, ctx, cel.kategoria)
    cel.atrybuty = definicje.sprawdz(opis, dane, egzekwuj_wymagane=True)
    cel.wartosc = czysta
    cel.klucz = klucz
    return cel


def wpisy(db: Session, ctx: TenantContext, kategoria: str | None = None) -> list[WpisSlownika]:
    zapytanie = select(WpisSlownika).where(WpisSlownika.tenant_id == ctx.tenant_id)
    if kategoria:
        zapytanie = zapytanie.where(WpisSlownika.kategoria == sprawdz_kategorie(kategoria))
    return list(db.execute(zapytanie.order_by(WpisSlownika.kategoria,
                                              WpisSlownika.wartosc)).scalars())


def podpowiedzi(db: Session, ctx: TenantContext) -> dict[str, list[WpisSlownika]]:
    """Wszystkie kategorie naraz - formularze biora je razem."""
    wynik: dict[str, list[WpisSlownika]] = {k: [] for k in KATEGORIE_SLOWNIKA}
    for pozycja in wpisy(db, ctx):
        wynik.setdefault(pozycja.kategoria, []).append(pozycja)
    return wynik


def uzycie_wpisu(db: Session, ctx: TenantContext, pozycja: WpisSlownika) -> int:
    """Ile rekordow wskazuje ten wpis - maszyn albo osob, zaleznie od kategorii."""
    nazwa, pole = WLASCICIEL[pozycja.kategoria]
    model = {"Owner": Owner, "Asset": Asset}[nazwa]
    return int(db.execute(
        select(func.count()).select_from(model).where(
            model.tenant_id == ctx.tenant_id, getattr(model, pole) == pozycja.id
        )
    ).scalar_one())


def usun(db: Session, ctx: TenantContext, wpis_id: str) -> WpisSlownika | None:
    """Kasuje wpis. Maszyny traca odwolanie (ON DELETE SET NULL).

    Odwrotnie niz przed przebudowa: wpis nie jest juz sama podpowiedzia, wiec
    jego usuniecie faktycznie odpina go od maszyn. Wywolujacy pokazuje, ilu
    maszyn to dotyczy, zanim spyta o potwierdzenie.
    """
    pozycja = wpis(db, ctx, wpis_id)
    if pozycja is None:
        return None
    db.execute(delete(WpisSlownika).where(WpisSlownika.id == pozycja.id))
    return pozycja


def wg_roli(db: Session, ctx: TenantContext, kategoria: str, pozycja: WpisSlownika | None,
            rola: str) -> str | None:
    """Wartosc pola pelniacego wskazana role.

    Funkcje pytaja o ROLE, nie o nazwe pola: dzieki temu zmiana etykiety na
    "E-mail serwisu" niczego nie psuje. Gdy roli nie pelni zadne pole, funkcja
    dostaje None i mowi o tym wprost, zamiast dzialac po cichu blednie.
    """
    if pozycja is None:
        return None
    pole = schemat(db, ctx, kategoria).wg_roli(rola)
    if pole is None:
        return None
    wartosc = (pozycja.atrybuty or {}).get(pole.klucz)
    return str(wartosc) if wartosc not in (None, "") else None
