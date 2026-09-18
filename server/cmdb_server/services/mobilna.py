"""Plik aplikacji Android wydawany przez portal.

Telefon technika nie ma jak pobrac APK z prywatnego repozytorium na GitHubie:
wymagaloby to konta z dostepem do repozytorium, ktorego technik miec nie musi.
Plik wydaje wiec ten sam serwer, z ktorym aplikacja i tak rozmawia, a na
stronie instalacji stoi kod QR - zeby nie przepisywac dlugiego adresu z ekranu
na telefon.

APK lezy w PODKATALOGU katalogu wydan agenta. Ten katalog jest juz wolumenem
w docker compose i wchodzi do kopii zapasowej, wiec plik przezywa odtworzenie
kontenera. Wlasny katalog wymagalby dopisania wolumenu przy kazdym wdrozeniu -
i po cichu ginalby wszedzie tam, gdzie nikt by tego nie zrobil.

Wersji nie da sie odczytac z samego pliku: numer siedzi w skompilowanym
AndroidManifest.xml, ktorego rozbieranie byloby osobna biblioteka. Bierzemy go
wiec z nazwy pliku wydania ("CMDB-Mobile-0.5.0-debug.apk") albo z pola w
formularzu i traktujemy jako opis, a nie jako fakt wyciagniety z bajtow.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from ..models import utcnow

log = logging.getLogger(__name__)

NAZWA_PLIKU = "cmdb-mobile.apk"
NAZWA_OPISU = "opis.json"

# Numer wersji z nazwy pliku: "CMDB-Mobile-0.5.0-debug.apk" -> "0.5.0".
WZORZEC_WERSJI = re.compile(r"(\d+(?:\.\d+){1,3})")


class BladAplikacji(RuntimeError):
    """Pliku nie da sie przyjac jako aplikacji Android."""


@dataclass(frozen=True)
class Aplikacja:
    """Opis wgranego APK - tyle, ile portal pokazuje przy kodzie QR."""

    wersja: str
    rozmiar: int
    sha256: str
    wgral: str | None
    wgrano: datetime

    @property
    def nazwa_pobrania(self) -> str:
        """Nazwa, pod ktora plik zapisze sie na telefonie."""
        return f"CMDB-Mobile-{self.wersja}.apk"


def katalog() -> Path:
    sciezka = Path(get_settings().release_dir) / "mobilna"
    sciezka.mkdir(parents=True, exist_ok=True)
    return sciezka


def plik() -> Path:
    return katalog() / NAZWA_PLIKU


def wersja_z_nazwy(nazwa: str | None) -> str:
    dopasowanie = WZORZEC_WERSJI.search(nazwa or "")
    return dopasowanie.group(1) if dopasowanie else ""


def sprawdz_apk(sciezka: Path) -> None:
    """Czy to naprawde aplikacja Android.

    Sprawdzamy zawartosc, a nie rozszerzenie: plik z niewlasciwa trescia
    zaladowalby sie na strone z kodem QR i telefon odmowilby instalacji bez
    slowa wyjasnienia. Lepiej odmowic tutaj, przy wgrywaniu.
    """
    if not zipfile.is_zipfile(sciezka):
        raise BladAplikacji("to nie jest plik APK (APK jest archiwum ZIP)")
    try:
        with zipfile.ZipFile(sciezka) as archiwum:
            nazwy = set(archiwum.namelist())
    except zipfile.BadZipFile as blad:
        raise BladAplikacji(f"uszkodzone archiwum APK: {blad}") from blad
    if "AndroidManifest.xml" not in nazwy:
        raise BladAplikacji("w archiwum nie ma AndroidManifest.xml - to nie jest APK")
    if not any(nazwa.startswith("classes") and nazwa.endswith(".dex") for nazwa in nazwy):
        raise BladAplikacji("w archiwum nie ma kodu aplikacji (classes.dex)")


def opis() -> Aplikacja | None:
    """Opis wgranego pliku albo None, gdy portal nie ma czego wydawac.

    Opis bez pliku jest traktowany jak brak aplikacji: po odtworzeniu kontenera
    bez wolumenu zostaje sam JSON, a kod QR prowadzacy do nieistniejacego pliku
    jest gorszy niz jego brak.
    """
    sciezka = plik()
    dane_opisu = katalog() / NAZWA_OPISU
    if not sciezka.is_file() or not dane_opisu.is_file():
        return None
    try:
        zapis = json.loads(dane_opisu.read_text(encoding="utf-8"))
        return Aplikacja(
            wersja=str(zapis["wersja"]),
            rozmiar=sciezka.stat().st_size,
            sha256=str(zapis["sha256"]),
            wgral=zapis.get("wgral"),
            wgrano=datetime.fromisoformat(zapis["wgrano"]),
        )
    except (OSError, ValueError, KeyError) as blad:
        log.warning("nie moge odczytac opisu aplikacji mobilnej: %s", blad)
        return None


def przyjmij(tymczasowy: Path, *, wersja: str, sha256: str, wgral: str | None) -> Aplikacja:
    """Wstawia wgrany plik na miejsce poprzedniego i zapisuje jego opis.

    Podmiana idzie przez plik tymczasowy w tym samym katalogu, wiec albo mamy
    stary komplet, albo nowy - nigdy polowy pliku pod adresem, ktory wlasnie
    ktos skanuje kodem QR.
    """
    sprawdz_apk(tymczasowy)
    docelowy = plik()
    shutil.move(str(tymczasowy), docelowy)
    aplikacja = Aplikacja(
        wersja=wersja,
        rozmiar=docelowy.stat().st_size,
        sha256=sha256,
        wgral=wgral,
        wgrano=utcnow(),
    )
    (katalog() / NAZWA_OPISU).write_text(
        json.dumps(
            {
                "wersja": aplikacja.wersja,
                "sha256": aplikacja.sha256,
                "wgral": aplikacja.wgral,
                "wgrano": aplikacja.wgrano.isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return aplikacja


def usun() -> bool:
    """Zdejmuje aplikacje z portalu. Zwraca, czy bylo co zdejmowac."""
    bylo = plik().is_file()
    plik().unlink(missing_ok=True)
    (katalog() / NAZWA_OPISU).unlink(missing_ok=True)
    return bylo
