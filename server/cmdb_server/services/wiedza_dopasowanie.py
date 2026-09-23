"""Ktorych maszyn dotyczy artykul - i ktore artykuly dotycza maszyny.

Artykul wiaze sie z maszyna na dwa sposoby, a wystarczy JEDEN:

* systemy - nazwy wpisane w artykule (jak w poprzednim systemie); nazwa rowna
  nazwie hosta albo FQDN maszyny (bez wzgledu na wielkosc liter) wiaze artykul
  z ta maszyna,
* warunki "Dotyczy" - nazwa hosta wg wzorca, system operacyjny,
  oprogramowanie z ostatniego raportu, rodzaj sprzetu, lokalizacja, etykieta
  zasobu. Maszyna pasuje, gdy spelnia KTORYKOLWIEK warunek.

Cale dopasowanie to jedno zapytanie SQL, a warunki sa w nim DANYMI (kolumna
``dotyczy``), a nie skladanym tekstem zapytania. Dzieki temu:

* lista zasobow liczy artykuly wszystkich maszyn jednym zapytaniem, bez
  wzgledu na liczbe artykulow,
* karta maszyny, strona artykulu, podglad w edytorze i trasa "nazwy -> linki"
  korzystaja z tej samej logiki - nie ma dwoch implementacji, ktore moglyby
  sie rozjechac,
* wartosc wpisana przez uzytkownika nie dotyka tekstu zapytania; wzorzec LIKE
  liczymy przy zapisie i przekazujemy jako dana.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

# Pola warunku -> etykieta w panelu. "nazwa" nie jest polem warunku, tylko
# oznacza dopasowanie przez liste systemow artykulu.
POLA_DOTYCZY: dict[str, str] = {
    "host": "Nazwa hosta",
    "os": "System operacyjny",
    "oprogramowanie": "Oprogramowanie",
    "rodzaj": "Rodzaj sprzętu",
    "lokalizacja": "Lokalizacja",
    "etykieta": "Etykieta zasobu",
}
ETYKIETY_POWODOW = {**POLA_DOTYCZY, "nazwa": "System"}

# Pola porownywane z CALA wartoscia (z mozliwoscia * i ?). Pozostale szukaja
# fragmentu: "Windows Server 2019" pasuje do "Microsoft Windows Server 2019
# Standard".
_CALA_WARTOSC = {"host", "etykieta"}

MAKS_WARUNKOW = 20
MAKS_DLUGOSC_WARTOSCI = 200


def wzorzec(pole: str, wartosc: str) -> str:
    """Wzorzec LIKE (male litery) dla wartosci warunku.

    ``*`` i ``?`` to jedyne znaki specjalne dla uzytkownika - ``%`` i ``_``
    wpisane w nazwie traktujemy doslownie.
    """
    wartosc = " ".join(wartosc.split()).lower()
    if pole == "rodzaj":
        return wartosc
    wynik = (wartosc.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
             .replace("*", "%").replace("?", "_"))
    return wynik if pole in _CALA_WARTOSC else f"%{wynik}%"


def normalizuj_warunki(surowe: list[dict]) -> list[dict]:
    """Warunki z formularza -> postac zapisywana w artykule.

    Pusta wartosc jest pomijana: przy regule "wystarczy jeden warunek" pusty
    warunek pasowalby do wszystkiego, a to prawie na pewno pomylka.
    """
    wynik: list[dict] = []
    widziane: set[tuple[str, str]] = set()
    for warunek in surowe:
        pole = str(warunek.get("pole") or "").strip()
        wartosc = " ".join(str(warunek.get("wartosc") or "").split())
        if not wartosc:
            continue
        if pole not in POLA_DOTYCZY:
            raise ValueError(f"nieznane pole warunku: {pole}")
        if len(wartosc) > MAKS_DLUGOSC_WARTOSCI:
            raise ValueError(f"wartość warunku jest dłuższa niż {MAKS_DLUGOSC_WARTOSCI} znaków")
        if wartosc.strip("*? ") == "":
            raise ValueError("warunek złożony z samych * i ? pasowałby do każdej maszyny")
        klucz = (pole, wartosc.lower())
        if klucz in widziane:
            continue
        widziane.add(klucz)
        wynik.append({"pole": pole, "wartosc": wartosc, "wzorzec": wzorzec(pole, wartosc)})
    if len(wynik) > MAKS_WARUNKOW:
        raise ValueError(f"artykuł może mieć najwyżej {MAKS_WARUNKOW} warunków")
    return wynik


# Rozpakowanie tablicy JSON, ktora nie musi byc tablica. jsonb_array_elements
# na obiekcie albo NULL-u rzuca bledem - raport agenta nie jest pod nasza
# kontrola, a jedna dziwna maszyna nie moze wywrocic listy zasobow.
def _tablica(wyrazenie: str) -> str:
    return f"CASE WHEN jsonb_typeof({wyrazenie}) = 'array' THEN {wyrazenie} ELSE '[]'::jsonb END"


_DOPASOWANIE_WARUNKU = f"""
    (w.pole = 'host' AND (lower(s.hostname) LIKE w.wzorzec ESCAPE '\\'
                          OR lower(coalesce(s.fqdn, '')) LIKE w.wzorzec ESCAPE '\\'))
 OR (w.pole = 'os' AND lower(concat_ws(' ', s.os_family, s.os_name, s.os_version))
                        LIKE w.wzorzec ESCAPE '\\')
 OR (w.pole = 'rodzaj' AND lower(s.typ) = w.wzorzec)
 OR (w.pole = 'lokalizacja' AND EXISTS (
        SELECT 1 FROM slowniki l
        WHERE l.id = s.lokalizacja_id AND lower(l.wartosc) LIKE w.wzorzec ESCAPE '\\'))
 OR (w.pole = 'etykieta' AND EXISTS (
        SELECT 1 FROM jsonb_array_elements_text({_tablica('s.tags')}) e
        WHERE lower(e) LIKE w.wzorzec ESCAPE '\\'))
 OR (w.pole = 'oprogramowanie' AND EXISTS (
        SELECT 1 FROM asset_current_reports r,
             jsonb_array_elements({_tablica("r.payload -> 'software' -> 'packages'")}) p
        WHERE r.asset_id = s.id AND lower(p ->> 'name') LIKE w.wzorzec ESCAPE '\\'))
"""


def _sql_trafien(*, podglad: bool, artykul: bool, maszyny: bool, nazwy: bool) -> str:
    """CTE ``trafienia(artykul_id, asset_id, powod)``.

    ``podglad`` - warunki i systemy z parametrow (edytor, przed zapisem),
    w przeciwnym razie z zapisanych artykulow firmy.
    """
    if podglad:
        warunki = f"""
            SELECT 'podglad'::text AS artykul_id, w.value ->> 'pole' AS pole,
                   w.value ->> 'wartosc' AS wartosc, w.value ->> 'wzorzec' AS wzorzec
            FROM jsonb_array_elements({_tablica('CAST(:dotyczy AS jsonb)')}) w"""
        systemy = """
            SELECT 'podglad'::text AS artykul_id, n.klucz, n.klucz AS wartosc
            FROM unnest(CAST(:systemy AS text[])) n(klucz)"""
    else:
        filtr = "AND a.id = :artykul_id" if artykul else ""
        warunki = f"""
            SELECT a.id AS artykul_id, w.value ->> 'pole' AS pole,
                   w.value ->> 'wartosc' AS wartosc, w.value ->> 'wzorzec' AS wzorzec
            FROM wiedza_artykuly a, jsonb_array_elements({_tablica('a.dotyczy')}) w
            WHERE a.tenant_id = :tenant_id AND a.usuniety_o IS NULL {filtr}"""
        systemy = f"""
            SELECT a.id AS artykul_id, sl.klucz, sl.wartosc
            FROM wiedza_artykuly a
            JOIN wiedza_artykuly_slownik x ON x.artykul_id = a.id
            JOIN wiedza_slownik sl ON sl.id = x.wpis_id AND sl.rodzaj = 'system'
            WHERE a.tenant_id = :tenant_id AND a.usuniety_o IS NULL {filtr}"""

    filtr_maszyn = ""
    if maszyny:
        filtr_maszyn += " AND s.id = ANY(CAST(:asset_ids AS text[]))"
    if nazwy:
        filtr_maszyn += (" AND (lower(s.hostname) = ANY(CAST(:nazwy AS text[]))"
                         " OR lower(s.fqdn) = ANY(CAST(:nazwy AS text[])))")

    return f"""
    WITH warunki AS ({warunki}),
    systemy AS ({systemy}),
    trafienia AS (
        SELECT w.artykul_id, s.id AS asset_id, w.pole || ':' || w.wartosc AS powod
        FROM warunki w
        JOIN assets s ON s.tenant_id = :tenant_id {filtr_maszyn}
        WHERE {_DOPASOWANIE_WARUNKU}
        UNION ALL
        SELECT y.artykul_id, s.id AS asset_id, 'nazwa:' || y.wartosc AS powod
        FROM systemy y
        JOIN assets s ON s.tenant_id = :tenant_id {filtr_maszyn}
             AND (lower(s.hostname) = y.klucz OR lower(s.fqdn) = y.klucz)
    )"""


@dataclass
class Trafienie:
    artykul_id: str
    asset_id: str
    powody: list[tuple[str, str]] = field(default_factory=list)   # (pole, wartosc)


def _powody(surowe: list[str]) -> list[tuple[str, str]]:
    wynik = []
    for powod in sorted(set(surowe)):
        pole, _, wartosc = powod.partition(":")
        wynik.append((pole, wartosc))
    return wynik


def trafienia(
    db: Session,
    tenant_id: str,
    *,
    artykul_id: str | None = None,
    asset_ids: list[str] | None = None,
) -> list[Trafienie]:
    """Pary (artykul, maszyna) z powodami - jedno zapytanie."""
    sql = _sql_trafien(podglad=False, artykul=artykul_id is not None,
                       maszyny=asset_ids is not None, nazwy=False)
    wiersze = db.execute(text(sql + """
        SELECT artykul_id, asset_id, array_agg(powod) FROM trafienia
        GROUP BY artykul_id, asset_id"""), {
        "tenant_id": tenant_id, "artykul_id": artykul_id, "asset_ids": asset_ids or [],
    }).all()
    return [Trafienie(a, s, _powody(p)) for a, s, p in wiersze]


def liczniki(db: Session, tenant_id: str, asset_ids: list[str] | None = None) -> dict[str, int]:
    """Ile artykulow dotyczy kazdej maszyny - jednym zapytaniem dla calej listy."""
    sql = _sql_trafien(podglad=False, artykul=False, maszyny=asset_ids is not None, nazwy=False)
    return dict(db.execute(text(sql + """
        SELECT asset_id, count(DISTINCT artykul_id) FROM trafienia GROUP BY asset_id"""), {
        "tenant_id": tenant_id, "asset_ids": asset_ids or [],
    }).all())


def podglad(db: Session, tenant_id: str, warunki: list[dict], systemy: list[str],
            limit: int = 50) -> tuple[int, list[tuple[str, str, list[tuple[str, str]]]]]:
    """Maszyny pasujace do NIEZAPISANYCH warunkow - licznik w edytorze.

    Zwraca (liczba, [(asset_id, hostname, powody)]) - ta sama logika co
    po zapisie, wiec podglad nie obiecuje niczego, czego artykul nie zrobi.
    """
    sql = _sql_trafien(podglad=True, artykul=False, maszyny=False, nazwy=False)
    wiersze = db.execute(text(sql + """
        SELECT t.asset_id, s.hostname, array_agg(t.powod)
        FROM trafienia t JOIN assets s ON s.id = t.asset_id
        GROUP BY t.asset_id, s.hostname ORDER BY lower(s.hostname)"""), {
        "tenant_id": tenant_id,
        "dotyczy": json.dumps(warunki),
        "systemy": [" ".join(s.split()).lower() for s in systemy if s.strip()],
    }).all()
    return len(wiersze), [(a, h, _powody(p)) for a, h, p in wiersze[:limit]]


def nazwy_z_artykulami(db: Session, tenant_id: str, nazwy: list[str]) -> set[str]:
    """Ktore z podanych nazw (hostow, FQDN, systemow) maja artykul - jedno zapytanie.

    Kontrakt znany z poprzedniego systemu: modul z lista obiektow wysyla ich
    nazwy i dostaje z powrotem tylko te, przy ktorych warto pokazac ikone.
    Nazwa ma artykul, gdy pasuje do niej maszyna objeta artykulem albo gdy
    jest wprost jednym z systemow artykulu (takze bez maszyny w CMDB).
    """
    klucze = list(dict.fromkeys(" ".join(n.split()).lower() for n in nazwy if n and n.strip()))
    if not klucze:
        return set()
    sql = _sql_trafien(podglad=False, artykul=False, maszyny=False, nazwy=True)
    wiersze = db.execute(text(sql + """
        SELECT n.klucz FROM unnest(CAST(:nazwy AS text[])) n(klucz)
        WHERE EXISTS (
                SELECT 1 FROM trafienia t JOIN assets s ON s.id = t.asset_id
                WHERE lower(s.hostname) = n.klucz OR lower(s.fqdn) = n.klucz)
           OR EXISTS (SELECT 1 FROM systemy y WHERE y.klucz = n.klucz)"""), {
        "tenant_id": tenant_id, "nazwy": klucze,
    }).scalars().all()
    return set(wiersze)
