"""Slowniki firmowe: dzialy, lokalizacje, dostawcy.

Zasada jest odwrotna niz w klasycznym slowniku: nie blokujemy wpisania nowej
wartosci, tylko ja zapamietujemy. Wymuszanie wyboru z listy konczy sie tym, ze
ktos nie znajduje swojego dzialu i zostawia pole puste - a wtedy nie ma ani
porzadku, ani danych. Podpowiedzi z juz uzywanych wartosci daja ten sam
porzadek dobrowolnie: literowka jest widoczna od razu, bo nie ma jej na liscie.

Klucz unikalnosci liczymy z wartosci zlozonej do malych liter i bez podwojnych
spacji, wiec "Magazyn" wpisany drugi raz jako "magazyn " nie zalozy drugiego
wpisu. Wyswietlamy pierwsza wersje, jaka wpisal czlowiek.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import KATEGORIE_SLOWNIKA, WpisSlownika
from .scoping import TenantContext

# Ile znakow miesci kolumna - dluzsze wartosci przycinamy, zamiast wywalac sie
# bledem bazy na formularzu.
MAKS_DLUGOSC = 200


def znormalizuj(wartosc: str | None) -> str:
    return " ".join((wartosc or "").split()).strip()


def _klucz(wartosc: str) -> str:
    return znormalizuj(wartosc).lower()


def sprawdz_kategorie(kategoria: str) -> str:
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise ValueError(f"nieznana kategoria slownika: {kategoria}")
    return kategoria


def zapewnij(db: Session, ctx: TenantContext, kategoria: str, wartosc: str | None) -> str | None:
    """Dopisuje wartosc do slownika, jesli jeszcze jej tam nie ma.

    Zwraca wartosc w postaci, w jakiej nalezy ja zapisac na zasobie - czyli
    tak, jak zapamietal ja slownik. Dzieki temu "magazyn" wpisany na drugiej
    maszynie zapisze sie jako "Magazyn", tak samo jak na pierwszej.

    Nie robi commitu: wywolujacy i tak zapisuje w tej samej transakcji zmiane,
    ktora ta wartosc wywolala. Slownik bez tej zmiany nie ma sensu.
    """
    sprawdz_kategorie(kategoria)
    czysta = znormalizuj(wartosc)[:MAKS_DLUGOSC]
    if not czysta:
        return None

    klucz = _klucz(czysta)
    istniejacy = db.execute(
        select(WpisSlownika).where(
            WpisSlownika.tenant_id == ctx.tenant_id,
            WpisSlownika.kategoria == kategoria,
            WpisSlownika.klucz == klucz,
        )
    ).scalar_one_or_none()
    if istniejacy is not None:
        return istniejacy.wartosc

    wpis = WpisSlownika(
        tenant_id=ctx.tenant_id,
        kategoria=kategoria,
        wartosc=czysta,
        klucz=klucz,
        utworzyl=ctx.actor,
    )
    try:
        # Dwa formularze zapisane rownoczesnie moga wpisac te sama nowa
        # wartosc. Kolizja na UNIQUE nie jest bledem - znaczy tylko, ze ktos
        # byl szybszy, a wynik i tak jest ten sam.
        #
        # Punkt zapisu, a nie zwykly flush: wycofanie calej transakcji
        # skasowaloby takze zmiane, dla ktorej ta wartosc jest zapisywana.
        with db.begin_nested():
            db.add(wpis)
    except IntegrityError:
        istniejacy = db.execute(
            select(WpisSlownika).where(
                WpisSlownika.tenant_id == ctx.tenant_id,
                WpisSlownika.kategoria == kategoria,
                WpisSlownika.klucz == klucz,
            )
        ).scalar_one_or_none()
        return istniejacy.wartosc if istniejacy else czysta
    return wpis.wartosc


def wartosci(db: Session, ctx: TenantContext, kategoria: str) -> list[str]:
    """Posortowane wartosci jednej kategorii - do podpowiedzi w formularzu."""
    sprawdz_kategorie(kategoria)
    return list(
        db.execute(
            select(WpisSlownika.wartosc)
            .where(
                WpisSlownika.tenant_id == ctx.tenant_id,
                WpisSlownika.kategoria == kategoria,
            )
            .order_by(WpisSlownika.wartosc)
        ).scalars()
    )


def podpowiedzi(db: Session, ctx: TenantContext) -> dict[str, list[str]]:
    """Wszystkie kategorie naraz - jednym zapytaniem, bo formularze biora je razem."""
    wynik: dict[str, list[str]] = {kategoria: [] for kategoria in KATEGORIE_SLOWNIKA}
    for wpis in db.execute(
        select(WpisSlownika)
        .where(WpisSlownika.tenant_id == ctx.tenant_id)
        .order_by(WpisSlownika.kategoria, WpisSlownika.wartosc)
    ).scalars():
        wynik.setdefault(wpis.kategoria, []).append(wpis.wartosc)
    return wynik


def wpisy(db: Session, ctx: TenantContext) -> dict[str, list[WpisSlownika]]:
    """Pelne wiersze pogrupowane kategoriami - do strony zarzadzania slownikiem."""
    wynik: dict[str, list[WpisSlownika]] = {kategoria: [] for kategoria in KATEGORIE_SLOWNIKA}
    for wpis in db.execute(
        select(WpisSlownika)
        .where(WpisSlownika.tenant_id == ctx.tenant_id)
        .order_by(WpisSlownika.kategoria, WpisSlownika.wartosc)
    ).scalars():
        wynik.setdefault(wpis.kategoria, []).append(wpis)
    return wynik


def usun(db: Session, ctx: TenantContext, wpis_id: str) -> WpisSlownika | None:
    """Kasuje wpis ze slownika. Zasoby zachowuja wpisana wczesniej wartosc.

    Slownik jest podpowiedzia, a nie kluczem obcym - usuniecie "Magazyn"
    nie moze wyczyscic lokalizacji stu maszynom. Wartosc zniknie z podpowiedzi
    i tyle; wpisana ponownie wroci do slownika.
    """
    wpis = db.execute(
        select(WpisSlownika).where(
            WpisSlownika.id == wpis_id, WpisSlownika.tenant_id == ctx.tenant_id
        )
    ).scalar_one_or_none()
    if wpis is None:
        return None
    db.execute(delete(WpisSlownika).where(WpisSlownika.id == wpis.id))
    return wpis
