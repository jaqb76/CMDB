"""Baza wiedzy: przestrzenie, artykuly, wersje, slowniki, zalaczniki.

Kazda funkcja dostaje kontekst firmy i filtruje po ``tenant_id`` - nie ma
tu sciezki, ktora siegnelaby po artykul z cudzej firmy.

Historia jest liczona pole po polu. Zapis, ktory niczego nie zmienia, nie
tworzy wersji; zmiana jednego pola tworzy wersje z tym jednym polem. Tresc
jest wyjatkiem tylko w sposobie przechowywania: zamiast pary old/new calego
tekstu (jak w poprzednim systemie) zapisujemy raz tekst sprzed zmiany.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    KATEGORIE_WIEDZY,
    SLOWNIK_WIEDZY_SYSTEM,
    SLOWNIK_WIEDZY_TAG,
    PortalUser,
    WiedzaArtykul,
    WiedzaArtykulSlownik,
    WiedzaPrzestrzen,
    WiedzaSlownik,
    WiedzaWersja,
    WiedzaZalacznik,
    utcnow,
)
from . import wiedza_dopasowanie as dopasowanie
from . import wiedza_tresc as tresc_mod
from .scoping import TenantContext

log = logging.getLogger(__name__)

# Limity pol. Poprzedni system nie mial zadnych - kategoria, priorytet czy
# tytul mogly byc czymkolwiek.
MAKS_TYTUL = 200
MAKS_STRESZCZENIE = 500
MAKS_TRESC = 200_000
MAKS_NOTATKI = 5_000
MAKS_TAG = 50
MAKS_SYSTEM = 100
MAKS_ELEMENTOW = 30
PRZEGLADY = (0, 3, 6, 12)

# Etykiety pol w historii, w kolejnosci wyswietlania.
ETYKIETY_POL: dict[str, str] = {
    "tytul": "tytuł",
    "streszczenie": "streszczenie",
    "tresc": "treść",
    "kategoria": "kategoria",
    "przestrzen": "przestrzeń",
    "rodzic": "strona nadrzędna",
    "tagi": "tagi",
    "systemy": "systemy",
    "dotyczy": "dotyczy",
    "notatki_zalacznikow": "opis załączników",
    "notatki_diagramu": "diagram",
    "wlasciciel": "właściciel",
    "przeglad_co_mies": "przegląd co (mies.)",
    "przeglad_do": "przegląd do",
    "na_dyzur": "na dyżur",
    "zalaczniki": "załączniki",
}


def klucz(wartosc: str) -> str:
    """Postac do porownan: male litery, pojedyncze spacje."""
    return " ".join((wartosc or "").split()).lower()


def _plus_miesiace(dzien: date, miesiace: int) -> date:
    rok, miesiac = divmod(dzien.month - 1 + miesiace, 12)
    rok, miesiac = dzien.year + rok, miesiac + 1
    return date(rok, miesiac, min(dzien.day, calendar.monthrange(rok, miesiac)[1]))


# --- dane z formularza --------------------------------------------------------

@dataclass
class DaneArtykulu:
    tytul: str
    streszczenie: str
    tresc: str
    kategoria: str
    przestrzen_id: str
    rodzic_id: str | None = None
    tagi: list[str] = field(default_factory=list)
    systemy: list[str] = field(default_factory=list)
    dotyczy: list[dict] = field(default_factory=list)
    notatki_zalacznikow: str | None = None
    notatki_diagramu: str | None = None
    wlasciciel: str | None = None
    przeglad_co_mies: int = 6
    na_dyzur: bool = False


def lista_nazw(surowe: str | list[str] | None, maks_dlugosc: int, opis: str) -> list[str]:
    """Tagi albo systemy z pola formularza: po przecinku lub w osobnych polach."""
    if surowe is None:
        return []
    kawalki = surowe if isinstance(surowe, list) else re.split(r"[,;\n]", surowe)
    wynik: list[str] = []
    widziane: set[str] = set()
    for kawalek in kawalki:
        wartosc = " ".join(str(kawalek).split())
        if not wartosc or klucz(wartosc) in widziane:
            continue
        if len(wartosc) > maks_dlugosc:
            raise ValueError(f"{opis} „{wartosc[:30]}…” jest dłuższy niż {maks_dlugosc} znaków")
        widziane.add(klucz(wartosc))
        wynik.append(wartosc)
    if len(wynik) > MAKS_ELEMENTOW:
        raise ValueError(f"najwyżej {MAKS_ELEMENTOW} pozycji w polu „{opis}”")
    return wynik


def sprawdz_dane(pola: dict) -> DaneArtykulu:
    """Walidacja formularza artykulu. Rzuca ValueError z opisem dla czlowieka.

    Tworzenie i edycja przechodza przez te sama funkcje - pole, ktorego nie da
    sie zapisac przy tworzeniu, nie wejdzie tylnymi drzwiami przy edycji
    (i odwrotnie: w poprzednim systemie srodowisko gubilo sie wlasnie przy
    tworzeniu, bo obie sciezki czytaly formularz inaczej).
    """
    tytul = " ".join(str(pola.get("tytul") or "").split())
    if not tytul:
        raise ValueError("tytuł jest wymagany")
    if len(tytul) > MAKS_TYTUL:
        raise ValueError(f"tytuł może mieć najwyżej {MAKS_TYTUL} znaków")
    streszczenie = " ".join(str(pola.get("streszczenie") or "").split())
    if not streszczenie:
        raise ValueError("streszczenie jest wymagane - pokazuje się na liście i w wynikach")
    if len(streszczenie) > MAKS_STRESZCZENIE:
        raise ValueError(f"streszczenie może mieć najwyżej {MAKS_STRESZCZENIE} znaków")
    tresc = str(pola.get("tresc") or "").replace("\r\n", "\n").strip("\n")
    if not tresc.strip():
        raise ValueError("treść artykułu jest wymagana")
    if len(tresc) > MAKS_TRESC:
        raise ValueError(f"treść może mieć najwyżej {MAKS_TRESC} znaków")
    kategoria = str(pola.get("kategoria") or "")
    if kategoria not in KATEGORIE_WIEDZY:
        raise ValueError("wybierz kategorię z listy")
    przestrzen_id = str(pola.get("przestrzen_id") or "").strip()
    if not przestrzen_id:
        raise ValueError("wybierz przestrzeń")
    try:
        przeglad = int(pola.get("przeglad_co_mies") or 0)
    except (TypeError, ValueError):
        przeglad = -1
    if przeglad not in PRZEGLADY:
        raise ValueError("nieprawidłowy okres przeglądu")

    notatki = {}
    for nazwa in ("notatki_zalacznikow", "notatki_diagramu"):
        wartosc = str(pola.get(nazwa) or "").strip()
        if len(wartosc) > MAKS_NOTATKI:
            raise ValueError(f"pole „{ETYKIETY_POL[nazwa]}” może mieć najwyżej {MAKS_NOTATKI} znaków")
        notatki[nazwa] = wartosc or None

    wlasciciel = str(pola.get("wlasciciel") or "").strip()[:255] or None
    return DaneArtykulu(
        tytul=tytul, streszczenie=streszczenie, tresc=tresc, kategoria=kategoria,
        przestrzen_id=przestrzen_id,
        rodzic_id=str(pola.get("rodzic_id") or "").strip() or None,
        tagi=lista_nazw(pola.get("tagi"), MAKS_TAG, "tag"),
        systemy=lista_nazw(pola.get("systemy"), MAKS_SYSTEM, "system"),
        dotyczy=dopasowanie.normalizuj_warunki(pola.get("dotyczy") or []),
        wlasciciel=wlasciciel, przeglad_co_mies=przeglad,
        na_dyzur=bool(pola.get("na_dyzur")),
        **notatki,
    )


# --- przestrzenie --------------------------------------------------------------

def przestrzenie(db: Session, ctx: TenantContext) -> list[WiedzaPrzestrzen]:
    return list(db.execute(
        select(WiedzaPrzestrzen).where(WiedzaPrzestrzen.tenant_id == ctx.tenant_id)
        .order_by(WiedzaPrzestrzen.kolejnosc, WiedzaPrzestrzen.nazwa)
    ).scalars())


def przestrzen(db: Session, ctx: TenantContext, przestrzen_id: str) -> WiedzaPrzestrzen | None:
    return db.execute(select(WiedzaPrzestrzen).where(
        WiedzaPrzestrzen.id == przestrzen_id, WiedzaPrzestrzen.tenant_id == ctx.tenant_id,
    )).scalar_one_or_none()


def _skrot(nazwa: str) -> str:
    litery = [s[0] for s in re.findall(r"\w+", nazwa)]
    return ("".join(litery[:3]) if len(litery) > 1 else nazwa[:3]).upper()


def zapisz_przestrzen(db: Session, ctx: TenantContext, nazwa: str, opis: str = "",
                      skrot: str = "", istniejaca: WiedzaPrzestrzen | None = None) -> WiedzaPrzestrzen:
    nazwa = " ".join((nazwa or "").split())
    if not nazwa:
        raise ValueError("nazwa przestrzeni jest wymagana")
    if len(nazwa) > 120:
        raise ValueError("nazwa przestrzeni może mieć najwyżej 120 znaków")
    zajeta = db.execute(select(WiedzaPrzestrzen.id).where(
        WiedzaPrzestrzen.tenant_id == ctx.tenant_id, WiedzaPrzestrzen.klucz == klucz(nazwa),
    )).scalar_one_or_none()
    if zajeta and (istniejaca is None or zajeta != istniejaca.id):
        raise ValueError(f"przestrzeń „{nazwa}” już istnieje")
    cel = istniejaca or WiedzaPrzestrzen(tenant_id=ctx.tenant_id, utworzyl=ctx.actor)
    cel.nazwa, cel.klucz = nazwa, klucz(nazwa)
    cel.opis = (opis or "").strip()[:300] or None
    cel.skrot = ("".join((skrot or "").split())[:4] or _skrot(nazwa)).upper()
    if istniejaca is None:
        db.add(cel)
    db.flush()
    return cel


def usun_przestrzen(db: Session, ctx: TenantContext, cel: WiedzaPrzestrzen) -> None:
    """Pusta przestrzen znika. Z artykulami - nie, takze z usunietymi w koszu."""
    liczba = db.execute(select(func.count(WiedzaArtykul.id)).where(
        WiedzaArtykul.przestrzen_id == cel.id)).scalar_one()
    if liczba:
        raise ValueError("przestrzeń ma artykuły (także w koszu) - przenieś je albo usuń najpierw")
    db.delete(cel)


def zapewnij_przestrzen(db: Session, ctx: TenantContext) -> WiedzaPrzestrzen:
    """Pierwsza przestrzen firmy - zeby pierwszy artykul nie wymagal konfiguracji."""
    istniejace = przestrzenie(db, ctx)
    return istniejace[0] if istniejace else zapisz_przestrzen(db, ctx, "Ogólna", "Artykuły bez własnej przestrzeni")


# --- odczyt artykulow ------------------------------------------------------------

def _zapytanie(ctx: TenantContext, usuniete: bool = False):
    zapytanie = (select(WiedzaArtykul)
                 .where(WiedzaArtykul.tenant_id == ctx.tenant_id)
                 .options(selectinload(WiedzaArtykul.slownik), selectinload(WiedzaArtykul.przestrzen)))
    if usuniete:
        return zapytanie.where(WiedzaArtykul.usuniety_o.is_not(None))
    return zapytanie.where(WiedzaArtykul.usuniety_o.is_(None))


def artykul(db: Session, ctx: TenantContext, artykul_id: str, *,
            takze_usuniety: bool = False, blokuj: bool = False) -> WiedzaArtykul | None:
    zapytanie = (select(WiedzaArtykul)
                 .where(WiedzaArtykul.id == artykul_id, WiedzaArtykul.tenant_id == ctx.tenant_id)
                 .options(selectinload(WiedzaArtykul.slownik)))
    if not takze_usuniety:
        zapytanie = zapytanie.where(WiedzaArtykul.usuniety_o.is_(None))
    if blokuj:
        zapytanie = zapytanie.with_for_update(of=WiedzaArtykul)
    return db.execute(zapytanie).scalar_one_or_none()


def artykuly(db: Session, ctx: TenantContext, *, ids: list[str] | None = None,
             przestrzen_id: str | None = None, usuniete: bool = False,
             na_dyzur: bool | None = None, limit: int | None = None,
             kolejnosc: str = "tytul") -> list[WiedzaArtykul]:
    zapytanie = _zapytanie(ctx, usuniete)
    if ids is not None:
        zapytanie = zapytanie.where(WiedzaArtykul.id.in_(ids))
    if przestrzen_id:
        zapytanie = zapytanie.where(WiedzaArtykul.przestrzen_id == przestrzen_id)
    if na_dyzur is not None:
        zapytanie = zapytanie.where(WiedzaArtykul.na_dyzur.is_(na_dyzur))
    if kolejnosc == "zmiana":
        zapytanie = zapytanie.order_by(WiedzaArtykul.zmieniono.desc())
    elif kolejnosc == "usuniecie":
        zapytanie = zapytanie.order_by(WiedzaArtykul.usuniety_o.desc())
    else:
        zapytanie = zapytanie.order_by(WiedzaArtykul.kolejnosc, func.lower(WiedzaArtykul.tytul))
    if limit:
        zapytanie = zapytanie.limit(limit)
    return list(db.execute(zapytanie).scalars())


def do_przegladu(db: Session, ctx: TenantContext, limit: int = 50) -> list[WiedzaArtykul]:
    return list(db.execute(
        _zapytanie(ctx).where(WiedzaArtykul.przeglad_co_mies > 0,
                              WiedzaArtykul.przeglad_do < date.today())
        .order_by(WiedzaArtykul.przeglad_do).limit(limit)
    ).scalars())


@dataclass
class Wezel:
    artykul: WiedzaArtykul
    dzieci: list["Wezel"] = field(default_factory=list)


def drzewo(lista: list[WiedzaArtykul]) -> list[Wezel]:
    """Drzewo stron przestrzeni. Strona, ktorej rodzic zniknal (kosz, inna
    przestrzen), staje sie korzeniem - nie moze przepasc z nawigacji."""
    wezly = {a.id: Wezel(a) for a in lista}
    korzenie: list[Wezel] = []
    for a in lista:
        rodzic = wezly.get(a.rodzic_id) if a.rodzic_id else None
        (rodzic.dzieci if rodzic else korzenie).append(wezly[a.id])
    return korzenie


def potomkowie(db: Session, ctx: TenantContext, artykul_id: str) -> set[str]:
    wiersze = db.execute(text("""
        WITH RECURSIVE d AS (
            SELECT id FROM wiedza_artykuly WHERE rodzic_id = :id AND tenant_id = :t
            UNION
            SELECT a.id FROM wiedza_artykuly a JOIN d ON a.rodzic_id = d.id WHERE a.tenant_id = :t
        ) SELECT id FROM d"""), {"id": artykul_id, "t": ctx.tenant_id}).scalars().all()
    return set(wiersze)


def sciezka(db: Session, ctx: TenantContext, art: WiedzaArtykul) -> list[WiedzaArtykul]:
    """Przodkowie artykulu od korzenia - do okruszkow."""
    wynik: list[WiedzaArtykul] = []
    widziane = {art.id}
    biezacy = art.rodzic_id
    while biezacy and biezacy not in widziane and len(wynik) < 20:
        widziane.add(biezacy)
        rodzic = artykul(db, ctx, biezacy)
        if rodzic is None or rodzic.przestrzen_id != art.przestrzen_id:
            break
        wynik.insert(0, rodzic)
        biezacy = rodzic.rodzic_id
    return wynik


# --- slownik tagow i systemow ------------------------------------------------------

def _wpisy_slownika(db: Session, ctx: TenantContext, rodzaj: str, wartosci: list[str]) -> list[WiedzaSlownik]:
    """Wpisy dla podanych nazw; brakujace dopisuje (slownik tylko rosnie)."""
    if not wartosci:
        return []
    db.execute(
        insert(WiedzaSlownik)
        .values([{"id": str(uuid.uuid4()), "tenant_id": ctx.tenant_id, "rodzaj": rodzaj,
                  "wartosc": w, "klucz": klucz(w)} for w in wartosci])
        .on_conflict_do_nothing(constraint="uq_wiedza_slownik")
    )
    wpisy = db.execute(select(WiedzaSlownik).where(
        WiedzaSlownik.tenant_id == ctx.tenant_id, WiedzaSlownik.rodzaj == rodzaj,
        WiedzaSlownik.klucz.in_([klucz(w) for w in wartosci]),
    )).scalars().all()
    kolejnosc = {klucz(w): i for i, w in enumerate(wartosci)}
    return sorted(wpisy, key=lambda w: kolejnosc[w.klucz])


def podpowiedzi(db: Session, ctx: TenantContext, rodzaj: str, fraza: str = "", limit: int = 15) -> list[str]:
    """Podpowiedzi tagow/systemow: fragment nazwy, alfabetycznie, najwyzej 15."""
    zapytanie = select(WiedzaSlownik.wartosc).where(
        WiedzaSlownik.tenant_id == ctx.tenant_id, WiedzaSlownik.rodzaj == rodzaj)
    fraza = klucz(fraza)
    if fraza:
        wzor = fraza.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        zapytanie = zapytanie.where(WiedzaSlownik.klucz.like(f"%{wzor}%", escape="\\"))
    return list(db.execute(zapytanie.order_by(WiedzaSlownik.klucz).limit(limit)).scalars())


# --- zapis ------------------------------------------------------------------------------

def _stan(art: WiedzaArtykul, nazwy_przestrzeni: dict[str, str], tytuly: dict[str, str]) -> dict:
    """Stan pol porownywanych przy zapisie. Wartosci czytelne dla czlowieka,
    bo trafiaja wprost do historii."""
    return {
        "tytul": art.tytul,
        "streszczenie": art.streszczenie,
        "kategoria": art.kategoria,
        "przestrzen": nazwy_przestrzeni.get(art.przestrzen_id, art.przestrzen_id),
        "rodzic": tytuly.get(art.rodzic_id) if art.rodzic_id else None,
        "tagi": art.tagi,
        "systemy": art.systemy,
        "dotyczy": [{"pole": w["pole"], "wartosc": w["wartosc"]} for w in (art.dotyczy or [])],
        "notatki_zalacznikow": art.notatki_zalacznikow,
        "notatki_diagramu": art.notatki_diagramu,
        "wlasciciel": art.wlasciciel,
        "przeglad_co_mies": art.przeglad_co_mies,
        "na_dyzur": art.na_dyzur,
    }


def _sprawdz_polozenie(db: Session, ctx: TenantContext, dane: DaneArtykulu,
                       art: WiedzaArtykul | None) -> None:
    if przestrzen(db, ctx, dane.przestrzen_id) is None:
        raise ValueError("nie znaleziono przestrzeni w tej firmie")
    if not dane.rodzic_id:
        return
    rodzic = artykul(db, ctx, dane.rodzic_id)
    if rodzic is None:
        raise ValueError("nie znaleziono strony nadrzędnej w tej firmie")
    if rodzic.przestrzen_id != dane.przestrzen_id:
        raise ValueError("strona nadrzędna musi być w tej samej przestrzeni")
    if art is not None and (rodzic.id == art.id or rodzic.id in potomkowie(db, ctx, art.id)):
        raise ValueError("strona nie może być podrzędna wobec samej siebie ani swojej podstrony")


def _indeksuj(db: Session, art: WiedzaArtykul) -> None:
    """Przelicza wektor wyszukiwania: tytul najwazniejszy, potem streszczenie,
    tagi i systemy, na koncu tresc."""
    db.execute(
        update(WiedzaArtykul).where(WiedzaArtykul.id == art.id).values(szukaj=text(
            "setweight(to_tsvector('simple', :a), 'A') || "
            "setweight(to_tsvector('simple', :b), 'B') || "
            "setweight(to_tsvector('simple', :c), 'C')"
        ).bindparams(
            a=tresc_mod.do_indeksu(art.tytul),
            b=tresc_mod.do_indeksu(" ".join([art.streszczenie, *art.tagi, *art.systemy])),
            c=tresc_mod.do_indeksu(" ".join(filter(None, [
                art.tresc, art.notatki_zalacznikow, art.notatki_diagramu]))),
        ))
    )


def _dopisz_wersje(db: Session, ctx: TenantContext, art: WiedzaArtykul, operacja: str,
                   zmiany: dict, opis: str | None = None, tresc_przed: str | None = None) -> WiedzaWersja:
    wersja = WiedzaWersja(
        artykul_id=art.id, tenant_id=ctx.tenant_id, wersja=art.wersja, kto=ctx.actor,
        operacja=operacja, zmiany=zmiany, opis_zmiany=(opis or "").strip()[:300] or None,
        tresc_przed=tresc_przed,
    )
    db.add(wersja)
    return wersja


def zapisz(db: Session, ctx: TenantContext, dane: DaneArtykulu,
           art: WiedzaArtykul | None = None, opis_zmiany: str = "") -> tuple[WiedzaArtykul, WiedzaWersja | None]:
    """Tworzy artykul albo zapisuje zmiany. Zwraca (artykul, nowa wersja albo None).

    None znaczy "nic sie nie zmienilo" - wtedy nie powstaje wersja i nie
    zmienia sie data modyfikacji.
    """
    _sprawdz_polozenie(db, ctx, dane, art)
    nazwy = {p.id: p.nazwa for p in przestrzenie(db, ctx)}
    tytuly = dict(db.execute(select(WiedzaArtykul.id, WiedzaArtykul.tytul).where(
        WiedzaArtykul.tenant_id == ctx.tenant_id)).all())

    nowy = art is None
    if nowy:
        art = WiedzaArtykul(tenant_id=ctx.tenant_id, utworzyl=ctx.actor, wersja=1)
        przed, tresc_przed = None, None
    else:
        przed, tresc_przed = _stan(art, nazwy, tytuly), art.tresc

    przenosiny = not nowy and art.przestrzen_id != dane.przestrzen_id
    for pole in ("tytul", "streszczenie", "tresc", "kategoria", "przestrzen_id", "rodzic_id",
                 "notatki_zalacznikow", "notatki_diagramu", "przeglad_co_mies", "na_dyzur"):
        setattr(art, pole, getattr(dane, pole))
    art.dotyczy = dane.dotyczy
    art.wlasciciel = dane.wlasciciel or (art.wlasciciel if not nowy else ctx.actor)
    art.slownik = (_wpisy_slownika(db, ctx, SLOWNIK_WIEDZY_TAG, dane.tagi)
                   + _wpisy_slownika(db, ctx, SLOWNIK_WIEDZY_SYSTEM, dane.systemy))

    if nowy:
        art.przeglad_do = (_plus_miesiace(date.today(), art.przeglad_co_mies)
                           if art.przeglad_co_mies else None)
        db.add(art)
        db.flush()
        wersja = _dopisz_wersje(db, ctx, art, "utworzenie", {}, opis_zmiany or "Utworzenie artykułu")
        _indeksuj(db, art)
        return art, wersja

    po = _stan(art, nazwy, tytuly)
    zmiany = {pole: {"old": przed[pole], "new": po[pole]} for pole in po if przed[pole] != po[pole]}
    tresc_zmieniona = tresc_przed != art.tresc
    if tresc_zmieniona:
        zmiany["tresc"] = {"znakow_przed": len(tresc_przed), "znakow_po": len(art.tresc)}
    if "przeglad_co_mies" in zmiany:
        stary = art.przeglad_do
        art.przeglad_do = (_plus_miesiace(date.today(), art.przeglad_co_mies)
                           if art.przeglad_co_mies else None)
        if stary != art.przeglad_do:
            zmiany["przeglad_do"] = {"old": _data(stary), "new": _data(art.przeglad_do)}
    if not zmiany:
        # Autoflush jest wylaczony, a obiekty nie roznia sie od bazy -
        # nic nie trafia do zapisu.
        return art, None

    if przenosiny:
        # Podstrony jada razem ze strona - inaczej zostalyby w starej
        # przestrzeni ze wskazaniem na rodzica w nowej.
        dzieci = potomkowie(db, ctx, art.id)
        if dzieci:
            db.execute(update(WiedzaArtykul).where(WiedzaArtykul.id.in_(dzieci))
                       .values(przestrzen_id=art.przestrzen_id))

    art.wersja += 1
    art.zmieniono, art.zmienil = utcnow(), ctx.actor
    db.flush()
    wersja = _dopisz_wersje(db, ctx, art, "zmiana", zmiany, opis_zmiany,
                            tresc_przed if tresc_zmieniona else None)
    _indeksuj(db, art)
    return art, wersja


def _data(dzien: date | None) -> str | None:
    return dzien.isoformat() if dzien else None


def oznacz_przejrzany(db: Session, ctx: TenantContext, art: WiedzaArtykul) -> WiedzaWersja | None:
    """Potwierdzenie, ze artykul jest aktualny - przesuwa termin przegladu."""
    if not art.przeglad_co_mies:
        return None
    nowy_termin = _plus_miesiace(date.today(), art.przeglad_co_mies)
    if nowy_termin == art.przeglad_do:
        return None
    zmiany = {"przeglad_do": {"old": _data(art.przeglad_do), "new": _data(nowy_termin)}}
    art.przeglad_do = nowy_termin
    art.wersja += 1
    art.zmieniono, art.zmienil = utcnow(), ctx.actor
    db.flush()
    return _dopisz_wersje(db, ctx, art, "zmiana", zmiany, "Przegląd: artykuł aktualny")


def usun(db: Session, ctx: TenantContext, art: WiedzaArtykul) -> WiedzaWersja:
    """Usuniecie miekkie: artykul trafia do kosza razem z historia i plikami."""
    art.usuniety_o, art.usunal = utcnow(), ctx.actor
    art.wersja += 1
    db.flush()
    return _dopisz_wersje(db, ctx, art, "usuniecie", {}, "Artykuł przeniesiony do kosza")


def przywroc(db: Session, ctx: TenantContext, art: WiedzaArtykul) -> WiedzaWersja:
    art.usuniety_o, art.usunal = None, None
    art.wersja += 1
    art.zmieniono, art.zmienil = utcnow(), ctx.actor
    db.flush()
    return _dopisz_wersje(db, ctx, art, "przywrocenie", {}, "Artykuł przywrócony z kosza")


# --- historia --------------------------------------------------------------------------

def wersje(db: Session, art: WiedzaArtykul) -> list[WiedzaWersja]:
    return list(db.execute(select(WiedzaWersja).where(
        WiedzaWersja.artykul_id == art.id, WiedzaWersja.tenant_id == art.tenant_id,
    ).order_by(WiedzaWersja.wersja.desc())).scalars())


def tresc_w_wersji(db: Session, art: WiedzaArtykul, numer: int) -> str:
    """Tresc artykulu po zapisaniu wersji ``numer``.

    Kazda zmiana tresci zapisuje tekst SPRZED zmiany, wiec tresc wersji N to
    ``tresc_przed`` najblizszej pozniejszej wersji, ktora zmienila tresc -
    albo biezaca tresc, gdy takiej nie ma. Jedno zapytanie, bez odtwarzania
    artykulu krok po kroku.
    """
    if numer >= art.wersja:
        return art.tresc
    pozniejsza = db.execute(
        select(WiedzaWersja.tresc_przed).where(
            WiedzaWersja.artykul_id == art.id,
            WiedzaWersja.wersja > numer,
            WiedzaWersja.tresc_przed.is_not(None),
        ).order_by(WiedzaWersja.wersja).limit(1)
    ).scalar_one_or_none()
    return art.tresc if pozniejsza is None else pozniejsza


def przywroc_tresc(db: Session, ctx: TenantContext, art: WiedzaArtykul,
                   numer: int) -> WiedzaWersja | None:
    """Przywraca tresc z wersji ``numer`` jako NOWA wersje - historii nie nadpisujemy."""
    dane = dane_z_artykulu(art)
    dane.tresc = tresc_w_wersji(db, art, numer)
    _, wersja = zapisz(db, ctx, dane, art, f"Przywrócono treść wersji {numer}")
    return wersja


def dane_z_artykulu(art: WiedzaArtykul) -> DaneArtykulu:
    return DaneArtykulu(
        tytul=art.tytul, streszczenie=art.streszczenie, tresc=art.tresc,
        kategoria=art.kategoria, przestrzen_id=art.przestrzen_id, rodzic_id=art.rodzic_id,
        tagi=art.tagi, systemy=art.systemy, dotyczy=list(art.dotyczy or []),
        notatki_zalacznikow=art.notatki_zalacznikow, notatki_diagramu=art.notatki_diagramu,
        wlasciciel=art.wlasciciel, przeglad_co_mies=art.przeglad_co_mies, na_dyzur=art.na_dyzur,
    )


# --- zalaczniki --------------------------------------------------------------------------

def katalog_zalacznikow() -> Path:
    return Path(get_settings().wiedza_dir) / "zalaczniki"


def limit_zalacznika() -> int:
    return get_settings().wiedza_zalacznik_mb * 1024 * 1024


_DOZWOLONE_ROZSZERZENIE = re.compile(r"^[a-z0-9]{1,8}$")


def _rozszerzenie(nazwa: str, typ_mime: str | None) -> str:
    """Rozszerzenie pliku na dysku - tylko litery i cyfry, nazwa od uzytkownika
    nie dotyka systemu plikow."""
    kandydat = Path(nazwa).suffix.lstrip(".").lower() or (
        (mimetypes.guess_extension(typ_mime or "") or "").lstrip("."))
    return f".{kandydat}" if _DOZWOLONE_ROZSZERZENIE.fullmatch(kandydat) else ".bin"


def zalaczniki(db: Session, art: WiedzaArtykul) -> list[WiedzaZalacznik]:
    return list(db.execute(select(WiedzaZalacznik).where(
        WiedzaZalacznik.artykul_id == art.id, WiedzaZalacznik.tenant_id == art.tenant_id,
    ).order_by(WiedzaZalacznik.dodano, WiedzaZalacznik.nazwa)).scalars())


def zalacznik(db: Session, ctx: TenantContext, zalacznik_id: str) -> WiedzaZalacznik | None:
    return db.execute(select(WiedzaZalacznik).where(
        WiedzaZalacznik.id == zalacznik_id, WiedzaZalacznik.tenant_id == ctx.tenant_id,
    )).scalar_one_or_none()


def plik_zalacznika(zal: WiedzaZalacznik) -> Path | None:
    """Sciezka na dysku - pilnujemy, zeby nie wyszla poza katalog zalacznikow."""
    korzen = katalog_zalacznikow().resolve()
    sciezka_pliku = (korzen / zal.sciezka).resolve()
    if korzen not in sciezka_pliku.parents:
        return None
    return sciezka_pliku


@dataclass
class Plik:
    nazwa: str
    typ_mime: str | None
    dane: bytes


def dodaj_zalaczniki(db: Session, ctx: TenantContext, art: WiedzaArtykul,
                     pliki: list[Plik]) -> WiedzaWersja | None:
    """Zapisuje pliki na dysk i opisuje je w bazie - jedna wersja na wysylke."""
    pliki = [p for p in pliki if p.nazwa and p.dane]
    if not pliki:
        return None
    for plik in pliki:
        if len(plik.dane) > limit_zalacznika():
            raise ValueError(
                f"plik „{plik.nazwa[:60]}” przekracza {get_settings().wiedza_zalacznik_mb} MB")
    przed = [z.nazwa for z in zalaczniki(db, art)]
    katalog = katalog_zalacznikow() / art.id
    katalog.mkdir(parents=True, exist_ok=True)
    for plik in pliki:
        nazwa = Path(plik.nazwa.replace("\\", "/")).name.strip()[:255] or "plik"
        wzgledna = f"{art.id}/{uuid.uuid4().hex}{_rozszerzenie(nazwa, plik.typ_mime)}"
        (katalog_zalacznikow() / wzgledna).write_bytes(plik.dane)
        db.add(WiedzaZalacznik(
            tenant_id=ctx.tenant_id, artykul_id=art.id, nazwa=nazwa,
            typ_mime=(plik.typ_mime or "")[:120] or None, rozmiar=len(plik.dane),
            sha256=hashlib.sha256(plik.dane).hexdigest(), sciezka=wzgledna, dodal=ctx.actor,
        ))
    db.flush()
    po = [z.nazwa for z in zalaczniki(db, art)]
    art.wersja += 1
    art.zmieniono, art.zmienil = utcnow(), ctx.actor
    return _dopisz_wersje(db, ctx, art, "zmiana", {"zalaczniki": {"old": przed, "new": po}},
                          "Dodano: " + ", ".join(p.nazwa for p in pliki)[:280])


def usun_zalacznik(db: Session, ctx: TenantContext, art: WiedzaArtykul,
                   zal: WiedzaZalacznik) -> Path | None:
    """Usuwa zalacznik z bazy i dopisuje wersje. Zwraca plik do skasowania.

    Plik kasuje wywolujacy DOPIERO po zatwierdzeniu transakcji: skasowany
    przed commitem, ktory sie nie powiodl, zostawilby wiersz bez pliku.
    Odwrotna kolejnosc zostawia w najgorszym razie sierote na dysku.
    """
    przed = [z.nazwa for z in zalaczniki(db, art)]
    plik = plik_zalacznika(zal)
    db.delete(zal)
    db.flush()
    po = [z.nazwa for z in zalaczniki(db, art)]
    art.wersja += 1
    art.zmieniono, art.zmienil = utcnow(), ctx.actor
    _dopisz_wersje(db, ctx, art, "zmiana", {"zalaczniki": {"old": przed, "new": po}},
                   f"Usunięto: {zal.nazwa}"[:300])
    return plik


# --- wyszukiwanie i lista --------------------------------------------------------------

@dataclass
class Filtr:
    q: str = ""
    kategoria: str = ""
    tag: str = ""
    system: str = ""
    przestrzen_id: str = ""
    asset_id: str = ""


def szukaj(db: Session, ctx: TenantContext, filtr: Filtr, limit: int = 300) -> list[WiedzaArtykul]:
    """Lista artykulow z filtrami. Tekst szuka w tytule, streszczeniu,
    tagach, systemach i tresci - bez wzgledu na polskie znaki."""
    zapytanie = _zapytanie(ctx)
    zapytanie_ts = tresc_mod.tsquery(filtr.q)
    if filtr.q and zapytanie_ts is None:
        return []
    if zapytanie_ts:
        warunek = func.to_tsquery("simple", zapytanie_ts)
        zapytanie = zapytanie.where(WiedzaArtykul.szukaj.op("@@")(warunek)).order_by(
            func.ts_rank(WiedzaArtykul.szukaj, warunek).desc(), WiedzaArtykul.zmieniono.desc())
    else:
        zapytanie = zapytanie.order_by(func.lower(WiedzaArtykul.tytul))
    if filtr.kategoria in KATEGORIE_WIEDZY:
        zapytanie = zapytanie.where(WiedzaArtykul.kategoria == filtr.kategoria)
    if filtr.przestrzen_id:
        zapytanie = zapytanie.where(WiedzaArtykul.przestrzen_id == filtr.przestrzen_id)
    if filtr.tag:
        zapytanie = zapytanie.where(WiedzaArtykul.id.in_(_z_wpisem(SLOWNIK_WIEDZY_TAG, filtr.tag)))
    if filtr.system:
        # System jak w poprzednim systemie: artykuly z ta nazwa na liscie
        # systemow ORAZ artykuly, ktorych warunki obejmuja maszyne o tej nazwie.
        z_regul = [t.artykul_id for t in dopasowanie.trafienia(
            db, ctx.tenant_id, asset_ids=_maszyny_o_nazwie(db, ctx, filtr.system))]
        zapytanie = zapytanie.where(or_(
            WiedzaArtykul.id.in_(_z_wpisem(SLOWNIK_WIEDZY_SYSTEM, filtr.system)),
            WiedzaArtykul.id.in_(z_regul),
        ))
    if filtr.asset_id:
        zapytanie = zapytanie.where(WiedzaArtykul.id.in_(
            [t.artykul_id for t in dopasowanie.trafienia(db, ctx.tenant_id, asset_ids=[filtr.asset_id])]))
    return list(db.execute(zapytanie.limit(limit)).scalars())


def _z_wpisem(rodzaj: str, nazwa: str):
    return (select(WiedzaArtykulSlownik.artykul_id)
            .join(WiedzaSlownik, WiedzaSlownik.id == WiedzaArtykulSlownik.wpis_id)
            .where(WiedzaSlownik.rodzaj == rodzaj, WiedzaSlownik.klucz == klucz(nazwa)))


def _maszyny_o_nazwie(db: Session, ctx: TenantContext, nazwa: str) -> list[str]:
    from ..models import Asset

    k = klucz(nazwa)
    return list(db.execute(select(Asset.id).where(
        Asset.tenant_id == ctx.tenant_id,
        or_(func.lower(Asset.hostname) == k, func.lower(Asset.fqdn) == k),
    )).scalars())


def wlasciciele(db: Session, ctx: TenantContext) -> list[str]:
    """Konta, ktore moga byc wlascicielem artykulu: konta firmy piszace."""
    return list(db.execute(select(PortalUser.email).where(
        PortalUser.tenant_id == ctx.tenant_id, PortalUser.is_active.is_(True),
    ).order_by(PortalUser.email)).scalars())
