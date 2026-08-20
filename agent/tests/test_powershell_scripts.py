"""Statyczna kontrola skryptow PowerShell osadzonych w kolektorze Windows.

Te testy dzialaja na kazdym systemie - nie uruchamiaja PowerShella, tylko
czytaja kod. Dzieki temu blad skladniowy widoczny wylacznie na Windows
wychodzi juz na Linuksie w CI.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

WINDOWS_COLLECTOR = Path(__file__).resolve().parent.parent / "cmdb_agent" / "collectors" / "windows.py"


def powershell_literals() -> list[tuple[int, str]]:
    """Wyciaga skrypty przekazywane do run_powershell.

    Bierzemy wylacznie argumenty wywolan run_powershell, a nie dowolne
    wieloliniowe napisy - inaczej do zestawu trafia docstring modulu,
    ktory o PowerShellu tylko opowiada.
    """
    tree = ast.parse(WINDOWS_COLLECTOR.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != "run_powershell" or not node.args:
            continue

        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            found.append((argument.lineno, argument.value))
        elif isinstance(argument, ast.JoinedStr):
            # f-string: skladamy same czesci literalne, pola formatowania
            # nie wplywaja na skladnie potokow
            tekst = "".join(
                part.value for part in argument.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            found.append((argument.lineno, tekst))
    return found


def test_scripts_are_found():
    """Gdyby zmienil sie sposob osadzania skryptow, pozostale testy cicho by
    przestaly cokolwiek sprawdzac."""
    assert len(powershell_literals()) >= 8


@pytest.mark.parametrize("lineno,script", powershell_literals())
def test_no_line_starts_with_pipe(lineno: int, script: str):
    """W Windows PowerShell 5.1 nowa linia konczy instrukcje, wiec potok NIE
    moze zaczynac linii - konczy sie to bledem "An empty pipe element is not
    allowed". PowerShell 7 to toleruje, 5.1 nie, a to on jest na kazdym
    Windows. Ten blad wylaczyl kiedys cztery kolektory naraz.
    """
    for offset, line in enumerate(script.split("\n")):
        stripped = line.strip()
        assert not stripped.startswith("|"), (
            f"skrypt z linii {lineno}, wiersz {offset + 1}: potok zaczyna linie "
            f"({stripped[:60]!r}) - przenies '|' na koniec poprzedniej linii"
        )


@pytest.mark.parametrize("lineno,script", powershell_literals())
def test_pipelines_are_balanced(lineno: int, script: str):
    """Linia konczaca sie potokiem musi miec kontynuacje."""
    lines = [ln.strip() for ln in script.split("\n")]
    for offset, line in enumerate(lines):
        if line.endswith("|"):
            reszta = [ln for ln in lines[offset + 1:] if ln]
            assert reszta, (
                f"skrypt z linii {lineno}, wiersz {offset + 1}: potok na koncu "
                "skryptu, brak kontynuacji"
            )


@pytest.mark.parametrize("lineno,script", powershell_literals())
def test_scripts_emit_json(lineno: int, script: str):
    """Kazdy skrypt musi zwrocic JSON - inaczej parser dostanie tekst."""
    assert "ConvertTo-Json" in script, f"skrypt z linii {lineno} nie zwraca JSON"


def test_win32_product_is_never_queried():
    """Win32_Product uruchamia reconfigure kazdego pakietu MSI - potrafi trwac
    minutami, zasypuje dziennik zdarzen i realnie uszkadza instalacje.
    Liste programow czytamy z kluczy rejestru Uninstall.

    Sprawdzamy tresc skryptow, a nie caly plik - w komentarzach ta nazwa
    wystepuje celowo, jako ostrzezenie.
    """
    for lineno, script in powershell_literals():
        assert "Win32_Product" not in script, (
            f"skrypt z linii {lineno} odpytuje Win32_Product"
        )


def test_admin_group_is_resolved_by_sid():
    """Na polskim Windows grupa nazywa sie "Administratorzy" - szukanie po
    nazwie "Administrators" zwrocilo by pusta liste, i to po cichu."""
    kod = WINDOWS_COLLECTOR.read_text(encoding="utf-8")
    assert "S-1-5-32-544" in kod
    assert not re.search(r'WinNT://\./Administrators', kod)
