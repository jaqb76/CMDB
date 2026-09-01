"""Katalog atrybutow, ktore mozna pokazac w tabeli raportu.

Kazda kolumna zna swoje zrodlo, bo od tego zalezy koszt jej policzenia:

* ``asset``  - pole maszyny, dostepne od reki,
* ``raport`` - z ostatniego raportu agenta; wymaga odczytania jego tresci,
* ``cve``    - wymaga zestawienia pakietow z kanalem podatnosci, czyli
  najdrozszej operacji w calym module.

Dzieki temu liczymy wylacznie to, co uzytkownik faktycznie zaznaczyl. Kolumna
z liczba podatnosci przy stu maszynach to sto dopasowan - bez tego rozroznienia
placilibysmy za nia takze wtedy, gdy nikt jej nie zaznaczyl.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import TYPY_SPRZETU


def bajty(wartosc) -> str | None:
    """Rozmiar w postaci czytelnej dla czlowieka."""
    if not wartosc:
        return None
    liczba = float(wartosc)
    for jednostka in ("B", "kB", "MB", "GB", "TB"):
        if liczba < 1024 or jednostka == "TB":
            return f"{liczba:.0f} {jednostka}" if jednostka in ("B", "kB") else f"{liczba:.1f} {jednostka}"
        liczba /= 1024
    return str(wartosc)


def sciezka(dane, *czlony):
    """Wartosc zagniezdzona w raporcie albo None, gdy ktoregos czlonu brak."""
    biezace = dane
    for czlon in czlony:
        if not isinstance(biezace, dict):
            return None
        biezace = biezace.get(czlon)
    return biezace


def _cena(maszyna) -> str | None:
    if maszyna.purchase_price is None:
        return None
    waluta = maszyna.purchase_currency or ""
    return f"{maszyna.purchase_price:.2f} {waluta}".strip()


def _dyski(raport) -> str | None:
    dyski = sciezka(raport, "hardware", "storage", "physical_disks") or []
    suma = sum((d.get("size_bytes") or 0) for d in dyski)
    return bajty(suma)


KOLUMNY: list[dict] = [
    # --- identyfikacja ---
    {"klucz": "hostname", "etykieta": "Nazwa", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].hostname, "domyslna": True},
    {"klucz": "fqdn", "etykieta": "FQDN", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].fqdn},
    {"klucz": "domain", "etykieta": "Domena", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].domain},
    {"klucz": "primary_ip", "etykieta": "Adres IP", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].primary_ip, "domyslna": True},
    {"klucz": "owner", "etykieta": "Opiekun", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["opiekunowie"].get(k["maszyna"].owner_id)},
    {"klucz": "uzytkownik", "etykieta": "Uzytkownik", "grupa": "Identyfikacja",
     "zrodlo": "asset",
     "wartosc": lambda k: k["opiekunowie"].get(k["maszyna"].uzytkownik_id)},
    {"klucz": "lokalizacja", "etykieta": "Lokalizacja", "grupa": "Identyfikacja",
     "zrodlo": "asset", "wartosc": lambda k: k["maszyna"].lokalizacja},
    {"klucz": "typ", "etykieta": "Rodzaj sprzetu", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: TYPY_SPRZETU.get(k["maszyna"].typ, k["maszyna"].typ)},
    {"klucz": "role_label", "etykieta": "Rola", "grupa": "Identyfikacja", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].role_label},

    # --- system ---
    {"klucz": "os_name", "etykieta": "System", "grupa": "System", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].os_name, "domyslna": True},
    {"klucz": "os_version", "etykieta": "Wersja systemu", "grupa": "System", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].os_version},
    {"klucz": "kernel", "etykieta": "Jadro", "grupa": "System", "zrodlo": "raport",
     "wartosc": lambda k: sciezka(k["raport"], "os", "kernel")},
    {"klucz": "arch", "etykieta": "Architektura", "grupa": "System", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].arch},
    {"klucz": "agent_version", "etykieta": "Wersja agenta", "grupa": "System", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].agent_version},
    {"klucz": "last_seen", "etykieta": "Ostatni kontakt", "grupa": "System", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].last_seen, "typ": "data"},

    # --- sprzet ---
    {"klucz": "manufacturer", "etykieta": "Producent", "grupa": "Sprzet", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].manufacturer, "domyslna": True},
    {"klucz": "model", "etykieta": "Model", "grupa": "Sprzet", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].model, "domyslna": True},
    {"klucz": "serial_number", "etykieta": "Numer seryjny", "grupa": "Sprzet", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].serial_number},
    {"klucz": "cpu", "etykieta": "Procesor", "grupa": "Sprzet", "zrodlo": "raport",
     "wartosc": lambda k: sciezka(k["raport"], "hardware", "cpu", "model")},
    {"klucz": "cpu_cores", "etykieta": "Rdzenie", "grupa": "Sprzet", "zrodlo": "raport",
     "wartosc": lambda k: sciezka(k["raport"], "hardware", "cpu", "logical_cores"),
     "typ": "liczba"},
    {"klucz": "memory", "etykieta": "Pamiec", "grupa": "Sprzet", "zrodlo": "raport",
     "wartosc": lambda k: bajty(sciezka(k["raport"], "hardware", "memory", "total_bytes")),
     "domyslna": True},
    {"klucz": "disk", "etykieta": "Dysk", "grupa": "Sprzet", "zrodlo": "raport",
     "wartosc": lambda k: _dyski(k["raport"])},
    {"klucz": "virtualization", "etykieta": "Wirtualizacja", "grupa": "Sprzet",
     "zrodlo": "raport",
     "wartosc": lambda k: sciezka(k["raport"], "hardware", "system", "virtualization")},

    # --- zakup i gwarancja ---
    {"klucz": "vendor", "etykieta": "Dostawca", "grupa": "Zakup", "zrodlo": "asset",
     "wartosc": lambda k: (k["maszyna"].dostawca.wartosc if k["maszyna"].dostawca else None)},
    {"klucz": "purchase_date", "etykieta": "Data zakupu", "grupa": "Zakup", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].purchase_date},
    {"klucz": "warranty_until", "etykieta": "Gwarancja do", "grupa": "Zakup", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].warranty_until, "typ": "gwarancja"},
    {"klucz": "purchase_price", "etykieta": "Cena", "grupa": "Zakup", "zrodlo": "asset",
     "wartosc": lambda k: _cena(k["maszyna"]), "typ": "liczba"},
    {"klucz": "invoice_number", "etykieta": "Faktura", "grupa": "Zakup", "zrodlo": "asset",
     "wartosc": lambda k: k["maszyna"].invoice_number},
    {"klucz": "support_contract", "etykieta": "Umowa wsparcia", "grupa": "Zakup",
     "zrodlo": "asset", "wartosc": lambda k: k["maszyna"].support_contract},

    # --- podatnosci (najdrozsze) ---
    {"klucz": "cve_powazne", "etykieta": "Powazne CVE", "grupa": "Podatnosci", "zrodlo": "cve",
     "wartosc": lambda k: (k["cve"] or {}).get("critical_count"),
     "typ": "liczba", "alarm": True},
    {"klucz": "cve_do_zrobienia", "etykieta": "CVE do zrobienia", "grupa": "Podatnosci",
     "zrodlo": "cve", "wartosc": lambda k: (k["cve"] or {}).get("fixable_count"),
     "typ": "liczba"},
    {"klucz": "cve_bez_poprawki", "etykieta": "CVE bez poprawki", "grupa": "Podatnosci",
     "zrodlo": "cve", "wartosc": lambda k: (k["cve"] or {}).get("open_count"),
     "typ": "liczba"},
    {"klucz": "aktualizacje", "etykieta": "Brakujace aktualizacje", "grupa": "Podatnosci",
     "zrodlo": "raport", "typ": "liczba",
     "wartosc": lambda k: sciezka(k["raport"], "software", "updates_pending", "count")},
]

WG_KLUCZA = {kolumna["klucz"]: kolumna for kolumna in KOLUMNY}
DOMYSLNE = [kolumna["klucz"] for kolumna in KOLUMNY if kolumna.get("domyslna")]


def grupy() -> dict[str, list[dict]]:
    """Kolumny pogrupowane tematycznie - do formularza wyboru."""
    wynik: dict[str, list[dict]] = {}
    for kolumna in KOLUMNY:
        wynik.setdefault(kolumna["grupa"], []).append(kolumna)
    return wynik


def wybrane(zapis) -> list[dict]:
    """Kolumny zapisane przy definicji; przy braku wyboru - zestaw domyslny.

    Nieznane klucze pomijamy, zamiast sie wywracac: definicja mogla powstac
    przy wersji, ktora znala kolumne juz usunieta.
    """
    klucze = zapis if isinstance(zapis, list) and zapis else DOMYSLNE
    return [WG_KLUCZA[klucz] for klucz in klucze if klucz in WG_KLUCZA]


def tabela(db: Session, maszyny: list, kolumny: list[dict],
           ostatni_raport, tenant_id: str) -> list[dict]:
    """Wiersze tabeli dla wybranych kolumn.

    Tresc raportu agenta odczytujemy tylko wtedy, gdy ktoras kolumna jej
    potrzebuje, a podatnosci liczymy wylacznie dla kolumn, ktore o nie prosza.
    """
    from ..models import WpisSlownika
    from . import cve

    zrodla = {kolumna["zrodlo"] for kolumna in kolumny}
    trzeba_raportu = bool(zrodla & {"raport", "cve"})
    trzeba_cve = "cve" in zrodla

    # Mape opiekunow budujemy tylko wtedy, gdy jakas kolumna o nia prosi -
    # przy jednej kolumnie mniej nie ma powodu odpytywac bazy.
    opiekunowie: dict[str, str] = {}
    if any(kolumna["klucz"] in ("owner", "uzytkownik") for kolumna in kolumny):
        opiekunowie = {
            o.id: o.wartosc
            for o in db.execute(select(WpisSlownika).where(
                WpisSlownika.tenant_id == tenant_id,
                WpisSlownika.kategoria == "osoba")).scalars()
        }

    wiersze = []
    for maszyna in maszyny:
        payload = ostatni_raport(db, maszyna.id) if trzeba_raportu else None
        wynik_cve = None
        if trzeba_cve and payload:
            wynik = cve.dopasuj(db, payload)
            wynik_cve = wynik if wynik["status"] == cve.STATUS_OK else None

        kontekst = {
            "maszyna": maszyna,
            "raport": payload or {},
            "cve": wynik_cve,
            "opiekunowie": opiekunowie,
        }
        wiersze.append(
            {
                "maszyna": maszyna,
                "komorki": [
                    {"kolumna": kolumna, "wartosc": kolumna["wartosc"](kontekst)}
                    for kolumna in kolumny
                ],
            }
        )
    return wiersze
