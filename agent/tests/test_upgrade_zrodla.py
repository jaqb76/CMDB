"""Aktualizacja agenta zainstalowanego ze zrodel.

Na Linuksie agent nie jest pojedynczym plikiem, tylko katalogiem - PyInstaller
nie kompiluje na inna architekture, wiec jedna paczka zrodel obsluguje i
Raspberry Pi, i serwer x86. Aktualizacja to wiec podmiana katalogu, a nie pliku.

Dwie rzeczy, ktore musza dzialac bezwzglednie: agent nie moze podmienic
katalogu, ktory nie jest instalacja (np. czyjejs kopii repozytorium), i nie
moze probowac instalowac paczki zrodel tam, gdzie dziala plik wykonywalny.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from cmdb_agent import upgrade


def _paczka(cel: Path, wersja: str = "9.9.9", zepsuta: bool = False) -> Path:
    """Buduje paczke zrodel z dzialajacym (albo nie) pakietem agenta."""
    korzen = cel / "budowa" / upgrade.KATALOG_W_ARCHIWUM
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / "packaging").mkdir(parents=True)

    tresc = f'__version__ = "{wersja}"\n'
    if zepsuta:
        tresc += "to nie jest poprawny Python (\n"
    (korzen / "cmdb_agent" / "__init__.py").write_text(tresc, encoding="utf-8")
    (korzen / "cmdb_agent" / "main.py").write_text(
        "from . import __version__\n"
        "import sys\n"
        "print(f'cmdb-agent {__version__}')\n",
        encoding="utf-8",
    )
    (korzen / "packaging" / "install-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    archiwum = cel / "paczka.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        tar.add(korzen, arcname=upgrade.KATALOG_W_ARCHIWUM)
    return archiwum


# --- ochrona katalogu roboczego ---------------------------------------------

def test_bez_znacznika_agent_nie_rusza_katalogu(tmp_path, monkeypatch):
    """Sedno zabezpieczenia: bez znacznika to nie jest instalacja, tylko
    czyjas kopia repozytorium - podmiana byla by utrata pracy."""
    korzen = tmp_path / "repozytorium"
    (korzen / "cmdb_agent").mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))

    assert upgrade.katalog_instalacji() is None


def test_ze_znacznikiem_katalog_jest_instalacja(tmp_path, monkeypatch):
    korzen = tmp_path / "opt-cmdb-agent"
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / upgrade.ZNACZNIK_INSTALACJI).write_text("2026-08-21", encoding="utf-8")
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))

    assert upgrade.katalog_instalacji() == korzen.resolve()


def test_agent_w_pliku_wykonywalnym_nie_jest_instalacja_ze_zrodel(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "frozen", True, raising=False)
    assert upgrade.katalog_instalacji() is None


# --- rozpakowanie -----------------------------------------------------------

def test_rozpakowanie_daje_pakiet_agenta(tmp_path):
    archiwum = _paczka(tmp_path)
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")
    assert (rozpakowany / "cmdb_agent" / "__init__.py").is_file()


def test_archiwum_ze_sciezka_wychodzaca_poza_katalog_jest_odrzucane(tmp_path):
    """Sciezka z ".." pozwolilaby nadpisac dowolny plik na maszynie."""
    archiwum = tmp_path / "zlosliwa.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        dane = b"cokolwiek"
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = len(dane)
        tar.addfile(info, io.BytesIO(dane))

    with pytest.raises(upgrade.UpgradeError, match="podejrzana sciezke"):
        upgrade._rozpakuj(archiwum, tmp_path / "cel")


def test_archiwum_bez_pakietu_agenta_jest_odrzucane(tmp_path):
    archiwum = tmp_path / "obca.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        dane = b"x"
        info = tarfile.TarInfo(f"{upgrade.KATALOG_W_ARCHIWUM}/cokolwiek.txt")
        info.size = len(dane)
        tar.addfile(info, io.BytesIO(dane))

    with pytest.raises(upgrade.UpgradeError, match="nie zawiera pakietu agenta"):
        upgrade._rozpakuj(archiwum, tmp_path / "cel")


# --- test dymny -------------------------------------------------------------

def test_dzialajaca_wersja_przechodzi_test(tmp_path):
    archiwum = _paczka(tmp_path, wersja="9.9.9")
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, opis = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert dziala, opis
    assert "9.9.9" in opis


def test_zepsuta_wersja_nie_przechodzi_testu(tmp_path):
    """Blad skladni wyszedlby dopiero przy nastepnym raporcie, gdy nie byloby
    juz do czego wracac."""
    archiwum = _paczka(tmp_path, wersja="9.9.9", zepsuta=True)
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, _ = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert not dziala


def test_inna_wersja_niz_obiecana_nie_przechodzi(tmp_path):
    archiwum = _paczka(tmp_path, wersja="1.0.0")
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, opis = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert not dziala
    assert "oczekiwano" in opis


# --- podmiana i wycofanie ---------------------------------------------------

def _instalacja(tmp_path: Path) -> Path:
    korzen = tmp_path / "opt"
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / "cmdb_agent" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (korzen / "cmdb_agent" / "stary-plik.py").write_text("# stara wersja\n", encoding="utf-8")
    (korzen / upgrade.ZNACZNIK_INSTALACJI).write_text("2026-08-21", encoding="utf-8")
    return korzen


def test_podmiana_zostawia_poprzednia_wersje_do_wycofania(tmp_path):
    korzen = _instalacja(tmp_path)
    archiwum = _paczka(tmp_path, wersja="9.9.9")
    nowy = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    zapasowy = upgrade._podmien_katalog(korzen, nowy)

    assert '9.9.9' in (korzen / "cmdb_agent" / "__init__.py").read_text(encoding="utf-8")
    assert (zapasowy / "stary-plik.py").is_file(), "poprzednia wersja musi zostac do wycofania"


def test_instalator_z_paczki_trafia_na_miejsce(tmp_path):
    """Pozwala odtworzyc usluge bez pobierania czegokolwiek."""
    korzen = _instalacja(tmp_path)
    nowy = upgrade._rozpakuj(_paczka(tmp_path), tmp_path / "cel")

    upgrade._podmien_katalog(korzen, nowy)
    assert (korzen / "packaging" / "install-agent.sh").is_file()


# --- zgodnosc rodzaju wydania -----------------------------------------------

def test_pobieranie_odrzuca_archiwum_ktore_nie_jest_archiwum(tmp_path, monkeypatch):
    import hashlib

    cel = tmp_path / "pobrane"

    class KlientAtrapa:
        def download(self, sciezka, token, plik, limit):
            dane = b"to nie jest gzip"
            plik.write_bytes(dane)
            return hashlib.sha256(dane).hexdigest()

    oferta = {
        "version": "9.9.9",
        "kind": upgrade.RODZAJ_ZRODLA,
        "sha256": hashlib.sha256(b"to nie jest gzip").hexdigest(),
    }
    with pytest.raises(upgrade.UpgradeError, match="nie jest archiwum"):
        upgrade._pobierz_i_sprawdz(KlientAtrapa(), "token", oferta, cel)
