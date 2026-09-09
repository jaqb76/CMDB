"""Czy usluga monitorowania istnieje w systemie.

Sedno tych testow: rozroznienie "nie zainstalowano" od "zainstalowano, ale nie
chodzi" i od "nie wiem". Trzecie jest tu rownie wazne jak dwa pierwsze -
falszywe "nie zainstalowano" wyslaloby kogos do instalatora bez powodu,
a instalator na dzialajacej maszynie to niepotrzebne ryzyko.
"""
from __future__ import annotations

import subprocess

import pytest

from cmdb_agent import proces, uslugi


def _wynik(kod: int = 0, tekst: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=kod, stdout=tekst.encode("utf-8"), stderr=b"")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(uslugi.sys, "platform", "linux")
    monkeypatch.setattr(uslugi.os.path, "isdir", lambda sciezka: True)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(uslugi.sys, "platform", "win32")
    monkeypatch.setitem(
        __import__("sys").modules, "cmdb_agent.collectors.windows",
        type("M", (), {"powershell_executable": staticmethod(lambda: "powershell.exe")}),
    )


def test_linux_bez_jednostki(monkeypatch, linux):
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(
        tekst="LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"))
    assert uslugi.stan_uslugi_monitora()["stan"] == "brak"


def test_linux_jednostka_dziala(monkeypatch, linux):
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(
        tekst="LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"))
    assert uslugi.stan_uslugi_monitora()["stan"] == "dziala"


def test_linux_jednostka_wylaczona_mowi_o_restarcie(monkeypatch, linux):
    """Rozne rzeczy: nie chodzi TERAZ i nie wstanie PO RESTARCIE."""
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(
        tekst="LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"))
    wynik = uslugi.stan_uslugi_monitora()
    assert wynik["stan"] == "zatrzymana"
    assert "disabled" in wynik["szczegol"]


def test_windows_bez_zadania(monkeypatch, windows):
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(tekst="BRAK\n"))
    assert uslugi.stan_uslugi_monitora()["stan"] == "brak"


def test_windows_zadanie_dziala(monkeypatch, windows):
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(tekst="Running\n"))
    assert uslugi.stan_uslugi_monitora()["stan"] == "dziala"


def test_windows_zarejestrowane_ale_nieuruchomione(monkeypatch, windows):
    """"Ready" znaczy: czeka na wyzwalacz. Wyzwalaczem jest start systemu, wiec
    po instalacji bez restartu zadanie potrafi tak tkwic i nic nie sprawdzac."""
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(tekst="Ready\n"))
    wynik = uslugi.stan_uslugi_monitora()
    assert wynik["stan"] == "zatrzymana"
    assert "nie uruchomione" in wynik["szczegol"]


def test_windows_nie_parsujemy_tlumaczonego_wydruku(monkeypatch, windows):
    """Pytamy o nazwe ze stanu wyliczeniowego, nie o tekst w jezyku systemu.

    Gdyby ktos wrocil do parsowania schtasks, ten test przypomni dlaczego:
    "Gotowy" na polskim Windows nie jest tym samym napisem co "Ready".
    """
    zapamietane = {}
    monkeypatch.setattr(proces, "uruchom",
                        lambda polecenie, **k: (zapamietane.update(cmd=polecenie),
                                                _wynik(tekst="Ready\n"))[1])
    uslugi.stan_uslugi_monitora()
    polecenie = zapamietane["cmd"]
    assert "schtasks" not in " ".join(polecenie).lower()
    assert "-NoProfile" in polecenie and "-NonInteractive" in polecenie
    # Konwencja repozytorium dla PowerShella - patrz test_powershell_invocation.
    assert "-EncodedCommand" not in polecenie
    assert "-ExecutionPolicy" not in polecenie


@pytest.mark.parametrize("awaria", [
    lambda *a, **k: _wynik(kod=1, tekst=""),
    lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 10)),
    lambda *a, **k: (_ for _ in ()).throw(OSError("brak systemctl")),
])
def test_nieudane_zapytanie_to_nie_jest_brak_uslugi(monkeypatch, linux, awaria):
    """Najwazniejszy z tych testow.

    "Nie wiem" musi zostac "nie wiem". Gdyby nieudane zapytanie zwracalo "brak",
    agent odsylalby do instalatora maszyny, na ktorej usluga chodzi poprawnie -
    a tylko zabraklo uprawnien, zeby o nia zapytac.
    """
    monkeypatch.setattr(proces, "uruchom", awaria)
    assert uslugi.stan_uslugi_monitora()["stan"] == "nieznany"


def test_podpowiedz_zawsze_cos_mowi():
    """Kazdy stan ma konkretny nastepny krok - takze "nieznany"."""
    for stan in ("brak", "zatrzymana", "dziala", "nieznany"):
        assert uslugi.podpowiedz(stan), stan


# --- naprawa po aktualizacji -------------------------------------------------
#
# Aktualizacja podmienia plik programu i nie zaklada jednostek systemowych.
# Maszyna, ktora dostala monitorowanie przez samoaktualizacje, nigdy nie
# dostala jego uslugi - i bez tej naprawy nie miala jak z tego wyjsc.

import types
from pathlib import Path


def _config(tmp_path):
    return types.SimpleNamespace(data_dir=tmp_path)


@pytest.fixture
def naprawa(monkeypatch, linux):
    """Maszyna z instalacja, na ktorej brakuje samego monitorowania."""
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "brak", "szczegol": ""})
    monkeypatch.setattr(uslugi, "stan_inwentaryzacji", lambda: {"stan": "dziala", "szczegol": ""})
    zalozone = {}

    def zaloz(konfiguracja, katalog_danych):
        zalozone["ok"] = (konfiguracja, katalog_danych)
        return "zalozone"

    monkeypatch.setattr(uslugi, "_zaloz_linux", zaloz)
    return zalozone


def test_zaklada_brakujaca_usluge(tmp_path, naprawa):
    opis = uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/etc/cmdb-agent/agent.conf")
    assert opis and "ok" in naprawa
    assert (tmp_path / uslugi.ZNACZNIK_ZALOZENIA).is_file()


def test_zaklada_tylko_raz(tmp_path, naprawa):
    """Administrator, ktory usunal usluge swiadomie, ma miec spokoj.

    Agent doklada brakujaca usluge raz. Gdyby robil to co godzine, walczylby
    z decyzja czlowieka, a status i tak powie, ze monitorowania nie ma.
    """
    uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/etc/cmdb-agent/agent.conf")
    naprawa.clear()
    assert uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/etc/cmdb-agent/agent.conf") is None
    assert not naprawa


def test_nie_stawia_uslug_na_maszynie_bez_instalatora(tmp_path, monkeypatch, linux):
    """Dokladamy siostrzana usluge do INSTALACJI, nie stawiamy pierwszej.

    Bez tego warunku agent uruchomiony z kopii repozytorium zakladalby
    jednostki systemowe na maszynie programisty.
    """
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "brak", "szczegol": ""})
    monkeypatch.setattr(uslugi, "stan_inwentaryzacji", lambda: {"stan": "brak", "szczegol": ""})
    monkeypatch.setattr(uslugi, "_zaloz_linux", lambda *a: pytest.fail("nie wolno tu zakladac"))
    assert uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/x") is None


def test_nie_rusza_dzialajacej_uslugi(tmp_path, monkeypatch, linux):
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "dziala", "szczegol": ""})
    monkeypatch.setattr(uslugi, "_zaloz_linux", lambda *a: pytest.fail("nie ma czego naprawiac"))
    assert uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/x") is None


def test_blad_zakladania_nie_przerywa_inwentaryzacji(tmp_path, monkeypatch, linux):
    """Naprawa jest poboczna. Raport o sprzecie ma pojsc mimo jej niepowodzenia."""
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "brak", "szczegol": ""})
    monkeypatch.setattr(uslugi, "stan_inwentaryzacji", lambda: {"stan": "dziala", "szczegol": ""})
    monkeypatch.setattr(uslugi, "_zaloz_linux",
                        lambda *a: (_ for _ in ()).throw(OSError("brak uprawnien")))
    assert uslugi.zapewnij_usluge_monitora(_config(tmp_path), "/x") is None
    # Znacznika nie ma, wiec przy nastepnym przebiegu sprobujemy ponownie.
    assert not (tmp_path / uslugi.ZNACZNIK_ZALOZENIA).exists()


def test_jednostka_nie_rozjechala_sie_z_instalatorem():
    """Jedno zachowanie opisane w dwoch jezykach musi byc pilnowane.

    Instalator pisze te jednostke w bashu, agent w Pythonie. Gdy ktos zmieni
    jedno miejsce, drugie ma o tym uslyszec od testu, a nie od maszyny klienta.
    """
    instalator = (Path(__file__).resolve().parent.parent
                  / "packaging" / "install-agent.sh").read_text(encoding="utf-8")
    for dyrektywa in ("Type=simple", "KillSignal=SIGTERM", "TimeoutStopSec=30",
                      "Restart=always", "RestartSec=30", "NoNewPrivileges=true",
                      "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
                      "WantedBy=multi-user.target"):
        assert dyrektywa in uslugi.JEDNOSTKA_LINUX, dyrektywa
        assert dyrektywa in instalator, dyrektywa
    assert "monitor" in uslugi.JEDNOSTKA_LINUX
    # ReadWritePaths ma obejmowac katalog danych - tam laduje monitoring.json.
    assert "ReadWritePaths={katalog_danych}" in uslugi.JEDNOSTKA_LINUX
