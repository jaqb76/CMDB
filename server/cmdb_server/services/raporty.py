"""Raporty wysylane cyklicznie poczta.

Trzy rodzaje, bo trzy rozne pytania:

* **podatnosci** - co wymaga lataniia i jak pilnie,
* **sprzet** - co w ogole mamy i w jakim stanie,
* **gwarancje** - czemu konczy sie wsparcie.

Wykresy sa rysowane paskami z HTML i CSS, a nie obrazkami ani biblioteka
JavaScript. Powod jest prozaiczny: klienty poczty nie uruchamiaja skryptow,
wiekszosc wycina SVG, a obrazek trzeba by dolaczac jako zalacznik i liczyc
na to, ze odbiorca zgodzi sie go wyswietlic. Pasek zbudowany z komorki tabeli
z tlem renderuje sie wszedzie - w Outlooku, Gmailu i w przegladarce.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, InventorySnapshot, Tenant, utcnow
from . import cve

log = logging.getLogger(__name__)

RODZAJE = {
    "podatnosci": "Podatnosci",
    "sprzet": "Inwentaryzacja sprzetu",
    "gwarancje": "Gwarancje i wsparcie",
}

CZESTOTLIWOSCI = {
    "dziennie": timedelta(days=1),
    "tygodniowo": timedelta(days=7),
    "miesiecznie": timedelta(days=30),
}

# Ile dni naprzod uznajemy gwarancje za "konczaca sie".
HORYZONT_GWARANCJI = 90

# Barwy paskow - te same co w panelu, zeby raport nie wygladal jak z innego
# programu. Zapisane wprost, bo w poczcie nie ma zmiennych CSS.
BARWY = ["#2f6fd0", "#4a86dd", "#6ea5f0", "#93bdf5", "#b8d4f9"]
BARWA_ALARM = "#a52222"
BARWA_UWAGA = "#8a6100"


def _procent(czesc: int, calosc: int) -> float:
    return round(czesc * 100 / calosc, 1) if calosc else 0.0


def wykres(pozycje: list[tuple[str, int]], barwa: str | None = None) -> list[dict]:
    """Dane paska: etykieta, wartosc i szerokosc w procentach.

    Szerokosc liczymy wzgledem NAJWIEKSZEJ pozycji, a nie sumy - inaczej przy
    kilkunastu kategoriach wszystkie paski bylyby nieczytelnie krotkie.
    """
    if not pozycje:
        return []
    najwieksza = max(wartosc for _, wartosc in pozycje) or 1
    suma = sum(wartosc for _, wartosc in pozycje)
    return [
        {
            "etykieta": etykieta,
            "wartosc": wartosc,
            "szerokosc": max(2, round(wartosc * 100 / najwieksza)),
            "udzial": _procent(wartosc, suma),
            "barwa": barwa or BARWY[numer % len(BARWY)],
        }
        for numer, (etykieta, wartosc) in enumerate(pozycje)
    ]


def _maszyny(db: Session, tenant_id: str) -> list[Asset]:
    return db.execute(
        select(Asset).where(
            Asset.tenant_id == tenant_id,
            Asset.is_active.is_(True),
            Asset.lifecycle == "aktywny",
        ).order_by(Asset.hostname)
    ).scalars().all()


def _ostatni_raport(db: Session, asset_id: str) -> dict | None:
    return db.execute(
        select(InventorySnapshot.payload)
        .where(InventorySnapshot.asset_id == asset_id)
        .order_by(InventorySnapshot.collected_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# --- sprzet -----------------------------------------------------------------

def dane_sprzet(db: Session, tenant_id: str) -> dict:
    maszyny = _maszyny(db, tenant_id)

    systemy = Counter()
    producenci = Counter()
    wersje_agenta = Counter()
    for maszyna in maszyny:
        systemy[maszyna.os_name or maszyna.os_family or "nieznany"] += 1
        producenci[maszyna.manufacturer or "nieznany"] += 1
        wersje_agenta[maszyna.agent_version or "nieznana"] += 1

    prog = utcnow() - timedelta(hours=48)
    bez_kontaktu = [m for m in maszyny if not m.last_seen or m.last_seen.replace(
        tzinfo=m.last_seen.tzinfo or utcnow().tzinfo) < prog]

    return {
        "liczba": len(maszyny),
        "wykres_systemy": wykres(systemy.most_common(10)),
        "wykres_producenci": wykres(producenci.most_common(8)),
        "wykres_agenci": wykres(sorted(wersje_agenta.items())),
        "bez_kontaktu": len(bez_kontaktu),
        "bez_opiekuna": sum(1 for m in maszyny if not m.owner_id),
        "maszyny": maszyny[:200],
    }


# --- gwarancje --------------------------------------------------------------

def dane_gwarancje(db: Session, tenant_id: str, horyzont: int = HORYZONT_GWARANCJI) -> dict:
    maszyny = _maszyny(db, tenant_id)
    dzisiaj = date.today()
    granica = dzisiaj + timedelta(days=horyzont)

    po_terminie, koncza_sie, na_gwarancji, bez_danych = [], [], [], []
    for maszyna in maszyny:
        if maszyna.warranty_until is None:
            bez_danych.append(maszyna)
        elif maszyna.warranty_until < dzisiaj:
            po_terminie.append(maszyna)
        elif maszyna.warranty_until <= granica:
            koncza_sie.append(maszyna)
        else:
            na_gwarancji.append(maszyna)

    po_terminie.sort(key=lambda m: m.warranty_until, reverse=True)
    koncza_sie.sort(key=lambda m: m.warranty_until)

    return {
        "horyzont": horyzont,
        "dzisiaj": dzisiaj,
        "po_terminie": po_terminie,
        "koncza_sie": koncza_sie,
        "na_gwarancji": len(na_gwarancji),
        # Brak daty to nie to samo co brak gwarancji - znaczy tylko, ze nikt
        # jej nie wpisal. Mieszanie tych dwoch rzeczy dawaloby falszywy obraz.
        "bez_danych": bez_danych,
        "wykres": wykres(
            [
                ("po terminie", len(po_terminie)),
                (f"konczy sie w {horyzont} dni", len(koncza_sie)),
                ("na gwarancji", len(na_gwarancji)),
                ("brak danych", len(bez_danych)),
            ]
        ),
    }


# --- podatnosci -------------------------------------------------------------

def dane_podatnosci(db: Session, tenant_id: str) -> dict:
    maszyny = _maszyny(db, tenant_id)

    wiersze = []
    wagi = Counter()
    laczne = Counter()
    for maszyna in maszyny:
        payload = _ostatni_raport(db, maszyna.id)
        if not payload:
            continue
        wynik = cve.dopasuj(db, payload)
        if wynik["status"] != cve.STATUS_OK:
            wiersze.append({"maszyna": maszyna, "nieznane": wynik["detail"], "wynik": None})
            continue

        laczne["do_zrobienia"] += wynik["fixable_count"]
        laczne["bez_poprawki"] += wynik["open_count"]
        laczne["powazne"] += wynik["critical_count"]
        for pozycja in wynik["entries"]:
            if pozycja["status"] != "resolved":
                continue
            ocena = pozycja.get("base_score")
            if ocena is None:
                wagi["bez oceny"] += 1
            elif ocena >= 9.0:
                wagi["krytyczne (9+)"] += 1
            elif ocena >= 7.0:
                wagi["wysokie (7-9)"] += 1
            elif ocena >= 4.0:
                wagi["srednie (4-7)"] += 1
            else:
                wagi["niskie (<4)"] += 1

        wiersze.append({"maszyna": maszyna, "nieznane": None, "wynik": wynik})

    kolejnosc = ["krytyczne (9+)", "wysokie (7-9)", "srednie (4-7)", "niskie (<4)", "bez oceny"]
    barwy = {"krytyczne (9+)": BARWA_ALARM, "wysokie (7-9)": BARWA_ALARM,
             "srednie (4-7)": BARWA_UWAGA}

    paski = []
    for numer, nazwa in enumerate(kolejnosc):
        if not wagi.get(nazwa):
            continue
        paski.extend(wykres([(nazwa, wagi[nazwa])], barwy.get(nazwa, BARWY[numer % len(BARWY)])))
    najwieksza = max((p["wartosc"] for p in paski), default=1) or 1
    for pasek in paski:
        pasek["szerokosc"] = max(2, round(pasek["wartosc"] * 100 / najwieksza))

    # Maszyny z najwieksza liczba rzeczy do zrobienia - od nich zaczyna sie prace.
    najgorsze = sorted(
        (w for w in wiersze if w["wynik"]),
        key=lambda w: (-w["wynik"]["critical_count"], -w["wynik"]["fixable_count"]),
    )[:15]

    return {
        "liczba_maszyn": len(wiersze),
        "do_zrobienia": laczne["do_zrobienia"],
        "bez_poprawki": laczne["bez_poprawki"],
        "powazne": laczne["powazne"],
        "wykres_wagi": paski,
        "najgorsze": najgorsze,
        "nieznane": [w for w in wiersze if w["nieznane"]],
    }


BUDOWNICZOWIE = {
    "podatnosci": dane_podatnosci,
    "sprzet": dane_sprzet,
    "gwarancje": dane_gwarancje,
}


def zbuduj(db: Session, tenant: Tenant, rodzaj: str, wybor_kolumn=None) -> dict:
    """Dane raportu wraz z naglowkiem wspolnym dla wszystkich rodzajow.

    Tabela szczegolowa jest wspolna dla wszystkich rodzajow - rozni je to,
    co jest nad nia: kafelki i wykresy. Dzieki temu wybor kolumn dziala
    wszedzie tak samo i nie trzeba go definiowac osobno dla kazdego raportu.
    """
    from . import kolumny as katalog

    budowniczy = BUDOWNICZOWIE.get(rodzaj)
    if budowniczy is None:
        raise ValueError(f"nieznany rodzaj raportu: {rodzaj}")

    wybrane = katalog.wybrane(wybor_kolumn)
    return {
        "rodzaj": rodzaj,
        "tytul": RODZAJE[rodzaj],
        "firma": tenant.name,
        "wygenerowano": utcnow(),
        "dane": budowniczy(db, tenant.id),
        "kolumny": wybrane,
        "wiersze": katalog.tabela(
            db, _maszyny(db, tenant.id), wybrane, _ostatni_raport, tenant.id
        ),
    }


def nalezy_wyslac(definicja, teraz=None) -> bool:
    """Czy nadszedl czas na kolejna wysylke tego raportu.

    Raport nigdy niewyslany idzie od razu - inaczej po dodaniu definicji
    trzeba by czekac caly okres, nie wiedzac, czy cokolwiek dziala.
    """
    if not definicja.aktywny:
        return False
    if definicja.ostatnia_wysylka is None:
        return True
    from ..models import as_utc

    odstep = CZESTOTLIWOSCI.get(definicja.czestotliwosc, CZESTOTLIWOSCI["tygodniowo"])
    return (teraz or utcnow()) - as_utc(definicja.ostatnia_wysylka) >= odstep


def adresaci(definicja) -> list[str]:
    """Adresy rozbierane z pola tekstowego - przecinek, srednik albo nowa linia."""
    surowe = (definicja.adresaci or "").replace(";", ",").replace("\n", ",")
    return [a.strip() for a in surowe.split(",") if a.strip() and "@" in a]


# --- renderowanie i wysylka -------------------------------------------------

def renderuj(raport: dict) -> tuple[str, str]:
    """Zwraca (HTML, tekst). Obie wersje ida w tej samej wiadomosci."""
    from ..api.ui import templates

    html = templates.get_template("raport.html").render(raport=raport)
    return html, wersja_tekstowa(raport)


def wersja_tekstowa(raport: dict) -> str:
    """Skrot raportu bez znacznikow.

    Nie jest ozdoba: czesc klientow i filtrow antyspamowych ocenia wiadomosc
    bez alternatywy tekstowej jako podejrzana.
    """
    d = raport["dane"]
    linie = [
        raport["tytul"],
        f"{raport['firma']} - stan na {raport['wygenerowano'].strftime('%Y-%m-%d %H:%M')} UTC",
        "",
    ]
    if raport["rodzaj"] == "podatnosci":
        linie += [
            f"Powazne (CVSS >= 7) z gotowa poprawka: {d['powazne']}",
            f"Wszystkie do zrobienia: {d['do_zrobienia']}",
            f"Znane, bez dostepnej poprawki: {d['bez_poprawki']}",
            "",
            "Maszyny wymagajace uwagi:",
        ]
        linie += [
            f"  {w['maszyna'].hostname}: {w['wynik']['critical_count']} powaznych, "
            f"{w['wynik']['fixable_count']} do zrobienia"
            for w in d["najgorsze"]
        ] or ["  brak"]
    elif raport["rodzaj"] == "sprzet":
        linie += [
            f"Maszyn w ewidencji: {d['liczba']}",
            f"Bez kontaktu ponad 48 h: {d['bez_kontaktu']}",
            f"Bez przypisanego opiekuna: {d['bez_opiekuna']}",
            "",
            "System operacyjny:",
        ]
        linie += [f"  {p['etykieta']}: {p['wartosc']}" for p in d["wykres_systemy"]]
    elif raport["rodzaj"] == "gwarancje":
        linie += [
            f"Gwarancja juz wygasla: {len(d['po_terminie'])}",
            f"Konczy sie w {d['horyzont']} dni: {len(d['koncza_sie'])}",
            f"Objetych wsparciem: {d['na_gwarancji']}",
            f"Bez wpisanej daty: {len(d['bez_danych'])}",
            "",
            "Konczy sie wkrotce:",
        ]
        linie += [
            f"  {m.hostname} ({m.model or 'brak modelu'}): {m.warranty_until}"
            for m in d["koncza_sie"]
        ] or ["  brak"]

    linie += ["", "Raport wygenerowany automatycznie przez CMDB."]
    return "\n".join(linie)


def wyslij_raport(db: Session, definicja) -> None:
    """Buduje i wysyla jeden raport. Wynik zapisuje przy definicji.

    Blad nie jest przemilczany ani rzucany dalej: trafia do definicji, zeby
    administrator zobaczyl w panelu, ze wysylka sie nie udaje. Wyjatek
    przerwalby caly przebieg i pozostale raporty tez by nie poszly.
    """
    from ..models import Tenant
    from . import poczta

    tenant = db.get(Tenant, definicja.tenant_id)
    if tenant is None:
        return

    try:
        odbiorcy = adresaci(definicja)
        if not odbiorcy:
            raise poczta.BladPoczty("brak poprawnych adresow")

        raport = zbuduj(db, tenant, definicja.rodzaj, definicja.kolumny)
        html, tekst = renderuj(raport)
        temat = f"[CMDB] {raport['tytul']} - {tenant.name}"
        poczta.wyslij(db, tenant.id, odbiorcy, temat, html, tekst)
        definicja.ostatni_status = "ok"
        definicja.ostatni_blad = None
    except Exception as exc:  # celowo szeroko - zaden raport nie moze zatrzymac reszty
        log.error("raport %s (%s) nie zostal wyslany: %s", definicja.nazwa, tenant.slug, exc)
        definicja.ostatni_status = "blad"
        definicja.ostatni_blad = str(exc)[:1000]
    finally:
        definicja.ostatnia_wysylka = utcnow()
        db.commit()


def wyslij_zalegle(db: Session) -> dict:
    """Wysyla wszystkie raporty, ktorych termin minal."""
    from ..models import DefinicjaRaportu

    definicje = db.execute(
        select(DefinicjaRaportu).where(DefinicjaRaportu.aktywny.is_(True))
    ).scalars().all()

    wyslane = pominiete = 0
    for definicja in definicje:
        if not nalezy_wyslac(definicja):
            pominiete += 1
            continue
        wyslij_raport(db, definicja)
        wyslane += 1
    return {"wyslane": wyslane, "pominiete": pominiete}
