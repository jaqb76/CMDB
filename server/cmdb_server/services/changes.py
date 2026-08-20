"""Wykrywanie zmian miedzy kolejnymi raportami maszyny.

To jest wlasciwy powod, dla ktorego stawia sie CMDB: nie "co jest na
maszynie", tylko "co sie na niej zmienilo i kiedy". Sam spis sprzetu daje
kazdy skrypt inwentaryzacyjny.

Porownujemy wylacznie rzeczy, ktorych zmiana cos znaczy. Lista procesow
i zalogowanych sesji jest z definicji zmienna przy kazdym odczycie i
zasypalaby historie szumem - te sekcje sa pomijane tak samo, jak przy
deduplikacji raportow.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# Ile zmian jednego rodzaju zapisujemy przy jednym raporcie. Pierwsza
# instalacja agenta na maszynie z setkami programow nie ma sensu rozpisywac
# na setki wpisow - liczy sie, ze maszyna pojawila sie w systemie.
LIMIT_NA_SEKCJE = 60


def _pobierz(payload: dict, sciezka: str) -> Any:
    """Wartosc spod sciezki 'a.b.c', albo None."""
    biezacy: Any = payload
    for czesc in sciezka.split("."):
        if not isinstance(biezacy, dict):
            return None
        biezacy = biezacy.get(czesc)
    return biezacy


def _tekst(wartosc: Any) -> str | None:
    if wartosc is None:
        return None
    return str(wartosc)[:400]


# --- opis tego, co porownujemy ---------------------------------------------
#
# Kazda pozycja to lista obiektow rozpoznawanych po kluczu. Zmiana pola
# z "sledzone" liczy sie jako modyfikacja; pojawienie sie i znikniecie
# obiektu - jako dodanie i usuniecie.

LISTY: list[dict] = [
    {
        "path": "software.packages",
        "category": "software",
        "klucz": lambda p: (p.get("name") or "").lower(),
        "etykieta": lambda p: f"{p.get('name')} {p.get('version') or ''}".strip(),
        "sledzone": ["version"],
    },
    {
        "path": "software.services",
        "category": "software",
        "klucz": lambda p: (p.get("name") or "").lower(),
        "etykieta": lambda p: p.get("display_name") or p.get("name") or "",
        "sledzone": ["start_mode", "account"],
    },
    {
        "path": "software.updates",
        "category": "software",
        "klucz": lambda p: (p.get("id") or "").upper(),
        "etykieta": lambda p: p.get("id") or "",
        "sledzone": [],
    },
    {
        "path": "users.local_accounts",
        "category": "users",
        "klucz": lambda p: (p.get("name") or "").lower(),
        "etykieta": lambda p: p.get("name") or "",
        "sledzone": ["enabled", "locked"],
    },
    {
        "path": "users.administrators",
        "category": "users",
        "klucz": lambda p: (p.get("name") or "").lower(),
        "etykieta": lambda p: p.get("name") or "",
        "sledzone": [],
    },
    {
        "path": "hardware.memory.modules",
        "category": "hardware",
        "klucz": lambda p: (p.get("slot") or p.get("serial_number") or "").lower(),
        "etykieta": lambda p: f"{p.get('slot') or '?'} — {_gb(p.get('capacity_bytes'))}",
        "sledzone": ["capacity_bytes", "speed_mhz"],
    },
    {
        "path": "hardware.storage.physical_disks",
        "category": "hardware",
        "klucz": lambda p: (p.get("serial_number") or p.get("model") or "").lower(),
        "etykieta": lambda p: f"{p.get('model') or '?'} — {_gb(p.get('size_bytes'))}",
        "sledzone": ["size_bytes", "status"],
    },
    {
        "path": "network.interfaces",
        "category": "network",
        "klucz": lambda p: (p.get("mac_address") or p.get("name") or "").lower(),
        "etykieta": lambda p: p.get("name") or p.get("mac_address") or "",
        "sledzone": ["ip_addresses", "dhcp_enabled"],
    },
]

# Pojedyncze wartosci, ktorych zmiana jest istotna sama w sobie.
SKALARY: list[tuple[str, str, str]] = [
    ("os.name", "os", "system operacyjny"),
    ("os.version", "os", "wersja systemu"),
    ("os.build", "os", "numer kompilacji systemu"),
    ("hardware.cpu.model", "hardware", "procesor"),
    ("hardware.cpu.logical_cores", "hardware", "liczba rdzeni logicznych"),
    ("hardware.memory.total_bytes", "hardware", "pamiec RAM"),
    ("hardware.system.manufacturer", "hardware", "producent"),
    ("hardware.system.model", "hardware", "model"),
    ("hardware.system.serial_number", "hardware", "numer seryjny"),
    ("hardware.firmware.version", "hardware", "wersja BIOS/UEFI"),
    ("agent.version", "os", "wersja agenta"),
]


def _gb(bajty: Any) -> str:
    try:
        return f"{int(bajty) / 1024 ** 3:.0f} GB"
    except (TypeError, ValueError):
        return "?"


def _wartosc_pola(obiekt: dict, pole: str) -> str | None:
    wartosc = obiekt.get(pole)
    if isinstance(wartosc, list):
        return ", ".join(str(x) for x in wartosc) or None
    if pole.endswith("_bytes"):
        return _gb(wartosc)
    return _tekst(wartosc)


def _porownaj_liste(stary: dict, nowy: dict, opis: dict) -> Iterable[dict]:
    poprzednie = _pobierz(stary, opis["path"]) or []
    biezace = _pobierz(nowy, opis["path"]) or []
    if not isinstance(poprzednie, list) or not isinstance(biezace, list):
        return

    klucz: Callable = opis["klucz"]
    etykieta: Callable = opis["etykieta"]

    mapa_stara = {klucz(p): p for p in poprzednie if isinstance(p, dict) and klucz(p)}
    mapa_nowa = {klucz(p): p for p in biezace if isinstance(p, dict) and klucz(p)}

    for k in mapa_nowa.keys() - mapa_stara.keys():
        yield {
            "category": opis["category"],
            "action": "dodano",
            "path": opis["path"],
            "label": etykieta(mapa_nowa[k])[:400],
            "old_value": None,
            "new_value": None,
        }

    for k in mapa_stara.keys() - mapa_nowa.keys():
        yield {
            "category": opis["category"],
            "action": "usunieto",
            "path": opis["path"],
            "label": etykieta(mapa_stara[k])[:400],
            "old_value": None,
            "new_value": None,
        }

    for k in mapa_stara.keys() & mapa_nowa.keys():
        for pole in opis["sledzone"]:
            stara = _wartosc_pola(mapa_stara[k], pole)
            nowa = _wartosc_pola(mapa_nowa[k], pole)
            if stara != nowa:
                yield {
                    "category": opis["category"],
                    "action": "zmieniono",
                    "path": f"{opis['path']}.{pole}",
                    "label": f"{etykieta(mapa_nowa[k])}: {pole}"[:400],
                    "old_value": stara,
                    "new_value": nowa,
                }


def _porownaj_skalary(stary: dict, nowy: dict) -> Iterable[dict]:
    for sciezka, kategoria, etykieta in SKALARY:
        stara = _pobierz(stary, sciezka)
        nowa = _pobierz(nowy, sciezka)
        if sciezka.endswith("_bytes"):
            stara_tekst, nowa_tekst = _gb(stara), _gb(nowa)
        else:
            stara_tekst, nowa_tekst = _tekst(stara), _tekst(nowa)
        if stara_tekst == nowa_tekst:
            continue
        # Pojawienie sie wartosci tam, gdzie jej nie bylo, to nie jest zmiana
        # wartosci - to uzupelnienie danych przez nowsza wersje agenta.
        akcja = "zmieniono" if stara_tekst and nowa_tekst else "dodano" if nowa_tekst else "usunieto"
        yield {
            "category": kategoria,
            "action": akcja,
            "path": sciezka,
            "label": etykieta,
            "old_value": stara_tekst,
            "new_value": nowa_tekst,
        }


def wykryj_zmiany(poprzedni: dict | None, biezacy: dict) -> list[dict]:
    """Zwraca liste zmian miedzy dwoma raportami.

    Brak poprzedniego raportu (pierwszy raport maszyny) daje pusta liste -
    rozpisywanie kilkuset zainstalowanych programow na osobne wpisy
    "dodano" nie niesie informacji; liczy sie, ze maszyna pojawila sie
    w systemie, a to widac po first_seen.
    """
    if not poprzedni:
        return []

    zmiany: list[dict] = []
    for opis in LISTY:
        z_sekcji = list(_porownaj_liste(poprzedni, biezacy, opis))
        if len(z_sekcji) > LIMIT_NA_SEKCJE:
            log.info(
                "sekcja %s zmienila sie w %d miejscach - zapisuje %d",
                opis["path"], len(z_sekcji), LIMIT_NA_SEKCJE,
            )
            z_sekcji = z_sekcji[:LIMIT_NA_SEKCJE]
        zmiany.extend(z_sekcji)

    zmiany.extend(_porownaj_skalary(poprzedni, biezacy))
    return zmiany
