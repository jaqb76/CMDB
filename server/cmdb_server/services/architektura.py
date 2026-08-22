"""Rozpoznawanie architektury procesora pliku wykonywalnego.

Rodzina systemu nie wystarcza. ELF dla x86-64 i ELF dla ARM64 to oba "linux",
a plik zbudowany dla jednej architektury nie uruchomi sie na drugiej. Bez tego
rozroznienia wersja oficjalna zbudowana na serwerze x86 trafialaby na kazdego
Raspberry Pi w flocie jako plik nie do uruchomienia.

Architekture czytamy z naglowka pliku, a nie z deklaracji wgrywajacego -
naglowek jest faktem, deklaracja bywa pomylka.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Nazwy sprowadzone do jednej postaci. platform.machine() zwraca rozne
# warianty tego samego: "AMD64" na Windows, "x86_64" na Linuksie.
NORMALIZACJA = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64",
    "armv6l": "arm", "armv7l": "arm", "armv8l": "arm", "arm": "arm",
    "i386": "x86", "i486": "x86", "i586": "x86", "i686": "x86", "x86": "x86",
}

ETYKIETY = {
    "x86_64": "x86-64 (64-bit Intel/AMD)",
    "aarch64": "ARM64 (Raspberry Pi 4/5, serwery ARM)",
    "arm": "ARM 32-bit (starsze Raspberry Pi)",
    "x86": "x86 (32-bit Intel/AMD)",
    # Paczka zrodel nie jest zbudowana pod zadna architekture - agent stoi na
    # samej bibliotece standardowej, wiec dziala wszedzie tam, gdzie jest Python.
    "zrodla": "zrodla (kazda architektura)",
}

# Wartosci pola e_machine w naglowku ELF.
ELF_MASZYNY = {0x03: "x86", 0x28: "arm", 0x3E: "x86_64", 0xB7: "aarch64"}
# Wartosci pola Machine w naglowku COFF (Windows PE).
PE_MASZYNY = {0x014C: "x86", 0x8664: "x86_64", 0xAA64: "aarch64", 0x01C4: "arm"}


def normalizuj(nazwa: str | None) -> str | None:
    """Sprowadza nazwe architektury do jednej postaci."""
    if not nazwa:
        return None
    return NORMALIZACJA.get(nazwa.strip().lower())


def etykieta(arch: str | None) -> str:
    return ETYKIETY.get(arch or "", arch or "nieznana")


def wykryj_z_pliku(sciezka: Path) -> str | None:
    """Architektura odczytana z naglowka pliku wykonywalnego."""
    try:
        with sciezka.open("rb") as plik:
            naglowek = plik.read(64)
            if naglowek[:4] == bytes.fromhex("7f") + b"ELF":
                return _z_elf(naglowek)
            if naglowek[:2] == b"MZ":
                return _z_pe(plik, naglowek)
    except OSError as exc:
        log.warning("nie moge odczytac naglowka %s: %s", sciezka, exc)
    return None


def _z_elf(naglowek: bytes) -> str | None:
    if len(naglowek) < 20:
        return None
    # Bajt 5 okresla kolejnosc bajtow: 1 = little endian, 2 = big endian.
    kolejnosc = "little" if naglowek[5] == 1 else "big"
    e_machine = int.from_bytes(naglowek[18:20], kolejnosc)
    return ELF_MASZYNY.get(e_machine)


def _z_pe(plik, naglowek: bytes) -> str | None:
    if len(naglowek) < 0x40:
        return None
    # Pod adresem 0x3C lezy przesuniecie naglowka PE.
    przesuniecie = int.from_bytes(naglowek[0x3C:0x40], "little")
    try:
        plik.seek(przesuniecie)
        sygnatura = plik.read(6)
    except OSError:
        return None
    if len(sygnatura) < 6 or sygnatura[:4] != b"PE" + bytes(2):
        return None
    return PE_MASZYNY.get(int.from_bytes(sygnatura[4:6], "little"))


# --- rodzaj pliku Windows ---------------------------------------------------

# Wariant z ikona w zasobniku nie jest osobna architektura, ale musi byc
# odrozniony od agenta: to dwa rozne pliki tej samej wersji, a magazyn wydan
# rozroznia wpisy po trojce wersja + system + architektura.
ARCH_TRAY = "tray"

# Pole Subsystem z naglowka opcjonalnego PE. 2 = program okienkowy,
# 3 = program konsolowy. Agent chodzi jako zadanie SYSTEM i nigdy nie rysuje
# okien, wiec jest konsolowy; ikona w zasobniku jest okienkowa.
PE_GUI = 2
PE_KONSOLA = 3

# Przesuniecie pola Subsystem: sygnatura PE (4) + naglowek COFF (20) + 68.
# Takie samo dla PE32 i PE32+.
_PRZESUNIECIE_PODSYSTEMU = 4 + 20 + 68


def podsystem_pe(sciezka: Path) -> int | None:
    """Rodzaj programu Windows odczytany z naglowka pliku.

    Czytamy naglowek, a nie polegamy na nazwie pliku ani na deklaracji
    wgrywajacego: nazwe da sie zmienic, a wgrywajacy moze sie pomylic.
    """
    try:
        with sciezka.open("rb") as plik:
            naglowek = plik.read(0x40)
            if len(naglowek) < 0x40 or naglowek[:2] != b"MZ":
                return None
            przesuniecie = int.from_bytes(naglowek[0x3C:0x40], "little")
            plik.seek(przesuniecie)
            if plik.read(4) != b"PE" + bytes(2):
                return None
            plik.seek(przesuniecie + _PRZESUNIECIE_PODSYSTEMU)
            pole = plik.read(2)
    except OSError as exc:
        log.warning("nie moge odczytac podsystemu %s: %s", sciezka, exc)
        return None
    if len(pole) < 2:
        return None
    return int.from_bytes(pole, "little")


def czy_okienkowy(sciezka: Path) -> bool:
    """Czy plik jest wariantem z interfejsem (ikona w zasobniku)."""
    return podsystem_pe(sciezka) == PE_GUI
