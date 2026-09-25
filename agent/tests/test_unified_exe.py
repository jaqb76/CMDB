"""Dispatch and lifecycle tests; no real network traffic."""
from unittest.mock import Mock

import pytest

from cmdb_agent import main as commands, windows_entry
from cmdb_agent.config import AgentConfig
from cmdb_agent.gui.tray_app import TrayApp


@pytest.mark.parametrize("args,expected", [([], ["gui"]), (["run"], ["run"]),
    (["--version"], ["--version"]), (["configure"], ["configure"]),
    (["--config", "C:/agent.conf", "status", "--json"], ["--config", "C:/agent.conf", "status", "--json"])])
def test_one_exe_dispatch(monkeypatch, args, expected):
    monkeypatch.setattr(windows_entry.sys, "platform", "win32")
    monkeypatch.setattr(windows_entry, "restore_output", Mock())
    main = Mock(return_value=7)
    monkeypatch.setattr(commands, "main", main)
    assert windows_entry.main(args) == 7
    main.assert_called_once_with(expected)


def test_linux_entry_keeps_cli_semantics(monkeypatch):
    monkeypatch.setattr(windows_entry.sys, "platform", "linux")
    main = Mock(return_value=2)
    monkeypatch.setattr(commands, "main", main)
    assert windows_entry.main([]) == 2
    main.assert_called_once_with([])


def test_output_restored_without_opening_console(monkeypatch):
    output = Mock()
    monkeypatch.setattr(windows_entry.sys, "platform", "win32")
    monkeypatch.setattr(windows_entry.sys, "stdout", None)
    monkeypatch.setattr(windows_entry.sys, "stderr", None)
    inherited = Mock(return_value=output)
    monkeypatch.setattr(windows_entry, "_inherited_output", inherited)
    windows_entry.restore_output()
    assert windows_entry.sys.stdout is output and windows_entry.sys.stderr is output
    assert [c.args[0] for c in inherited.call_args_list] == [-11, -12]


def test_istniejacy_strumien_nie_wywraca_sie_na_polskich_znakach(monkeypatch):
    """Instalator czyta wyjscie "status" potokiem - Python sam otwiera wtedy
    stdout w cp1252 "strict" i "ł" konczylo sie oknem z wyjatkiem."""
    import io

    bufor = io.BytesIO()
    strumien = io.TextIOWrapper(bufor, encoding="cp1252", errors="strict", newline="\n")
    monkeypatch.setattr(windows_entry.sys, "platform", "win32")
    monkeypatch.setattr(windows_entry.sys, "stdout", strumien)
    monkeypatch.setattr(windows_entry.sys, "stderr", strumien)
    windows_entry.restore_output()
    strumien.write("monitorowanie usług: brak celów · żółw\n")
    strumien.flush()
    assert bufor.getvalue().decode("cp1252") == "monitorowanie uslug: brak celów · zólw\n"


def test_tray_restarts_after_binary_replacement(monkeypatch):
    app = TrayApp.__new__(TrayApp)
    app._executable_signature = (10, 100)
    app.restart_requested = False
    app.root = Mock()
    app.quit = Mock()
    monkeypatch.setattr(app, "_file_signature", lambda: (20, 200))
    app._refresh_icon()
    assert app.restart_requested
    app.quit.assert_called_once()
    app.root.after.assert_not_called()


def test_temporary_missing_binary_during_rename_does_not_restart(monkeypatch):
    from cmdb_agent.gui import tray_app
    app = TrayApp.__new__(TrayApp)
    app._executable_signature = (10, 100)
    app.restart_requested = False
    app.root, app.config, app.icon = Mock(), AgentConfig(), None
    monkeypatch.setattr(app, "_file_signature", lambda: None)
    monkeypatch.setattr(tray_app.status_module, "read", Mock())
    app._refresh_icon()
    assert not app.restart_requested
    app.root.after.assert_called_once()


# --- polskie znaki w oknie konsoli -------------------------------------------
#
# Windowed EXE dostaje stdout None i odtwarza go na surowym uchwycie, wiec
# omija WriteConsoleW. Bajty czyta wtedy sama konsola, wedlug swojej strony
# kodowej - a my wysylalismy UTF-8 niezaleznie od tego, co ona potrafi.


def test_strona_kodowa_konsoli_wyznacza_kodek():
    """Polski Windows stoi zwykle na stronie 852 i tam ma wszystkie ogonki."""
    assert windows_entry.kodowanie_dla_strony(852) == "cp852"
    assert windows_entry.kodowanie_dla_strony(1250) == "cp1250"


def test_nieznana_strona_kodowa_to_utf8():
    """Zero znaczy "proces bez konsoli", 65001 to UTF-8 pod inna nazwa.

    Zadne z nich nie moze wywrocic startu agenta - bez stdout nie ma jak
    zglosic bledu, wiec awaryjne UTF-8 jest jedynym sensownym wyjsciem.
    """
    assert windows_entry.kodowanie_dla_strony(0) == "utf-8"
    assert windows_entry.kodowanie_dla_strony(65001) == "utf-8"
    assert windows_entry.kodowanie_dla_strony(999999) == "utf-8"


def test_ogonki_przezywaja_strone_852():
    """Sedno zgloszenia: to sa znaki, ktore uzytkownik widzial rozsypane."""
    tekst = "Działa · sprawdza 1 z 2 usł. · żółw"
    wynik = tekst.encode("cp852", errors=windows_entry.NAZWA_BLEDU).decode("cp852")
    assert "Działa" in wynik and "usł." in wynik and "żółw" in wynik


def test_znak_spoza_strony_kodowej_dostaje_odpowiednik_ascii():
    """Kropka srodkowa nie istnieje w 852. Domyslne "?" nie niesie niczego,
    a mysnik oddziela czlony statusu dokladnie tak samo."""
    assert "Dziala - dwa".encode("cp852").decode("cp852") == "Dziala - dwa"
    wynik = "Dziala · dwa".encode("cp852", errors=windows_entry.NAZWA_BLEDU).decode("cp852")
    assert wynik == "Dziala - dwa"
    assert "?" not in wynik


def test_nieznany_znak_nadal_nie_wywraca_wypisu():
    """Tablica zamiennikow jest krotka i ma taka zostac. Wszystko spoza niej
    ma zejsc do "?", bo wyjatek w trakcie wypisu status znaczy brak statusu."""
    wynik = "próba 中".encode("cp852", errors=windows_entry.NAZWA_BLEDU).decode("cp852")
    assert wynik == "próba ?"


def test_polska_litera_spoza_strony_traci_tylko_ogonek():
    wynik = "Łódź, ąę".encode("cp1252", errors=windows_entry.NAZWA_BLEDU).decode("cp1252")
    assert wynik == "Lódz, ae"
