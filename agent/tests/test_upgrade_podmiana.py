"""Podmiana pliku agenta na Windows, gdy ktos trzyma poprzednia kopie.

Zgloszenie z maszyny Windows:

    ERROR cmdb_agent.upgrade: nie udalo sie podmienic pliku agenta:
    [WinError 5] Odmowa dostepu: 'C:\\Program Files\\CMDB Agent\\cmdb-agent.exe.stara'

Windows nie pozwala USUNAC pliku, z ktorego dziala proces - pozwala go tylko
PRZEMIANOWAC. Podmiana odkladala wiec stara wersje pod jedna, stala nazwa
".stara" i kasowala poprzednia kopie przed kolejna aktualizacja.

Dzialalo to dopoki nic tej kopii nie trzymalo. Monitorowanie chodzi jednak
CIAGLE i z tego samego pliku: po pierwszej aktualizacji jego proces zostaje
przy obrazie ".stara" i nie konczy sie sam. Kasowanie musialo zaczac
zawodzic - a maszyna, ktorej raz sie to przytrafilo, nie miala juz jak
zaktualizowac sie nigdy.
"""
from __future__ import annotations

import subprocess
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cmdb_agent import proces, upgrade, uslugi


@pytest.fixture
def instalacja(tmp_path):
    """Katalog programu z dzialajacym agentem i pobrana nowa wersja."""
    biezacy = tmp_path / "cmdb-agent.exe"
    biezacy.write_text("wersja stara", encoding="utf-8")
    nowy = tmp_path / "cmdb-agent.exe.nowa"
    nowy.write_text("wersja nowa", encoding="utf-8")
    return biezacy, nowy


def test_podmiana_nie_rusza_zajetej_kopii(instalacja, monkeypatch):
    """Sedno usterki.

    Kopia sprzed poprzedniej aktualizacji zostaje na dysku, bo trzyma ja
    dzialajace monitorowanie. Podmiana ma ja OMINAC, a nie probowac skasowac -
    proba konczyla sie odmowa dostepu i przerywala cala aktualizacje.
    """
    biezacy, nowy = instalacja
    zajeta = biezacy.with_name(biezacy.name + upgrade.ROZSZERZENIE_STAREJ)
    zajeta.write_text("wersja sprzed poprzedniej aktualizacji", encoding="utf-8")

    # Windows odmawia skasowania pliku, z ktorego dziala proces. Linux na to
    # pozwala, wiec blokade trzeba tu odtworzyc - inaczej test przeszedlby
    # takze na kodzie sprzed poprawki, na ktorym maszyna klienta stanela.
    prawdziwy_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == zajeta.name:
            raise PermissionError(f"[WinError 5] Odmowa dostepu: '{self}'")
        return prawdziwy_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    stara = upgrade._podmien(biezacy, nowy)

    assert biezacy.read_text(encoding="utf-8") == "wersja nowa"
    assert stara.read_text(encoding="utf-8") == "wersja stara"
    assert stara != zajeta
    # Nietkniete: kasowanie tego pliku bylo cala usterka.
    assert zajeta.read_text(encoding="utf-8") == "wersja sprzed poprzedniej aktualizacji"


def test_kazda_podmiana_dostaje_wolna_nazwe(instalacja, monkeypatch):
    """Dwie aktualizacje w tej samej sekundzie nie moga na siebie wejsc."""
    biezacy, _ = instalacja
    zegar = types.SimpleNamespace(
        now=lambda strefa=None: datetime(2026, 9, 9, 23, 50, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(upgrade, "datetime", zegar)

    pierwsza = upgrade.sciezka_starej(biezacy)
    pierwsza.write_text("zajete", encoding="utf-8")
    druga = upgrade.sciezka_starej(biezacy)

    assert pierwsza.name == "cmdb-agent.exe.stara-20260909-235005"
    assert druga != pierwsza and not druga.exists()


def test_wycofanie_gdy_nowa_wersja_nie_wchodzi(instalacja, monkeypatch):
    """Nieudane wstawienie nowej wersji ma zostawic maszyne z dzialajaca stara.

    Pada tu WYLACZNIE drugie przemianowanie - to, ktore wstawia nowy plik
    w miejsce starego. Pierwsze juz sie udalo, wiec bez wycofania katalog
    programu zostalby bez pliku agenta w ogole.
    """
    biezacy, nowy = instalacja
    prawdziwy_rename = Path.rename

    def rename(self, cel):
        if self.name == nowy.name:
            raise OSError("zajete przez inny proces")
        return prawdziwy_rename(self, cel)

    monkeypatch.setattr(Path, "rename", rename)

    with pytest.raises(OSError):
        upgrade._podmien(biezacy, nowy)

    monkeypatch.undo()
    assert biezacy.read_text(encoding="utf-8") == "wersja stara"
    assert not list(biezacy.parent.glob(biezacy.name + upgrade.ROZSZERZENIE_STAREJ + "*"))


def test_sprzatanie_usuwa_wszystkie_kopie_takze_stara_nazwe(instalacja):
    """Wzorzec musi lapac tez nazwe sprzed poprawki - takie pliki juz leza."""
    biezacy, _ = instalacja
    dawna = biezacy.with_name(biezacy.name + ".stara")
    nowsza = biezacy.with_name(biezacy.name + ".stara-20260909-235005")
    for plik in (dawna, nowsza):
        plik.write_text("x", encoding="utf-8")

    upgrade.posprzataj_poprzednia(biezacy)

    assert not dawna.exists() and not nowsza.exists()
    assert biezacy.exists()


def test_zajeta_kopia_nie_przerywa_sprzatania(instalacja, monkeypatch):
    """Kopia trzymana przez monitorowanie zostaje na kolejny raz.

    To nie jest blad ani powod, zeby przerwac - to normalny stan miedzy
    podmiana a restartem procesu, ktory z tego pliku dziala.
    """
    biezacy, _ = instalacja
    zajeta = biezacy.with_name(biezacy.name + ".stara-20260909-235005")
    wolna = biezacy.with_name(biezacy.name + ".stara-20260908-101010")
    for plik in (zajeta, wolna):
        plik.write_text("x", encoding="utf-8")

    prawdziwy_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == zajeta.name:
            raise PermissionError("[WinError 5] Odmowa dostepu")
        return prawdziwy_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    upgrade.posprzataj_poprzednia(biezacy)   # nie moze rzucic

    assert zajeta.exists()
    assert not wolna.exists()


# --- restart monitorowania po aktualizacji -----------------------------------
#
# Podmiana dotyczy PLIKU. Dzialajacy proces zostaje przy obrazie, z ktorego
# wystartowal, wiec monitorowanie pracowaloby na starym kodzie az do restartu
# maszyny - i do tego czasu trzymaloby odlozona kopie.


def _wynik(kod=0, blad=b""):
    return subprocess.CompletedProcess(args=[], returncode=kod, stdout=b"", stderr=blad)


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(uslugi.sys, "platform", "linux")
    wywolania = []
    monkeypatch.setattr(proces, "uruchom",
                        lambda polecenie, **k: (wywolania.append(polecenie), _wynik())[1])
    return wywolania


def test_restart_podnosi_dzialajace_monitorowanie(monkeypatch, monitor):
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "dziala"})
    assert uslugi.zrestartuj_monitor()
    assert monitor == [["systemctl", "try-restart", uslugi.USLUGA_LINUX]]


@pytest.mark.parametrize("stan", ["zatrzymana", "brak", "nieznany"])
def test_restart_nie_rusza_tego_co_nie_dziala(monkeypatch, monitor, stan):
    """Aktualizacja programu nie jest powodem, zeby cofac decyzje administratora.

    "nieznany" tez zostawiamy w spokoju: nie wiemy, czy jest co restartowac,
    a start uslugi swiadomie wylaczonej byloby gorsze niz jej brak.
    """
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": stan})
    assert uslugi.zrestartuj_monitor() is None
    assert monitor == []


def test_nieudany_restart_nie_psuje_aktualizacji(monkeypatch):
    """Plik jest juz podmieniony i sprawdzony - blad restartu niczego nie cofa.

    Monitorowanie wstanie samo: jednostka systemd ma Restart=always, a zadanie
    Windows RestartCount.
    """
    monkeypatch.setattr(uslugi.sys, "platform", "linux")
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "dziala"})
    monkeypatch.setattr(proces, "uruchom", lambda *a, **k: _wynik(1, b"brak uprawnien"))
    assert uslugi.zrestartuj_monitor() is None

    monkeypatch.setattr(proces, "uruchom",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("brak systemctl")))
    assert uslugi.zrestartuj_monitor() is None


def test_restart_na_windows_uzywa_zadania_a_nie_uslugi(monkeypatch):
    monkeypatch.setattr(uslugi.sys, "platform", "win32")
    monkeypatch.setattr(uslugi, "stan_uslugi_monitora", lambda: {"stan": "dziala"})
    monkeypatch.setitem(
        __import__("sys").modules, "cmdb_agent.collectors.windows",
        type("M", (), {"powershell_executable": staticmethod(lambda: "powershell.exe")}))
    zapamietane = {}
    monkeypatch.setattr(proces, "uruchom",
                        lambda polecenie, **k: (zapamietane.update(cmd=polecenie), _wynik())[1])

    assert uslugi.zrestartuj_monitor()
    polecenie = " ".join(zapamietane["cmd"])
    assert "Stop-ScheduledTask" in polecenie and "Start-ScheduledTask" in polecenie
    assert uslugi.ZADANIE_WINDOWS in polecenie
    # Konwencja repozytorium - patrz test_powershell_invocation.
    assert "-NoProfile" in zapamietane["cmd"] and "-NonInteractive" in zapamietane["cmd"]
    assert "-EncodedCommand" not in polecenie and "-ExecutionPolicy" not in polecenie
