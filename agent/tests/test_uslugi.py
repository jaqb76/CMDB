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
