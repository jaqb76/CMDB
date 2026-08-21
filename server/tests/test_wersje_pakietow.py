"""Porownywanie wersji pakietow wedlug regul dpkg.

Przypadki pochodza z Debian Policy 5.6.12 i z zestawu testow samego dpkg.
Blad tutaj jest cichy w najgorszy sposob: podatnosc uznana za naprawiona nie
pojawi sie w zadnym raporcie i nikt sie o niej nie dowie.
"""
from __future__ import annotations

import pytest
from cmdb_server.services.wersje_pakietow import porownaj, rozbierz, starsza_niz


@pytest.mark.parametrize(
    "mniejsza, wieksza",
    [
        # Podstawy.
        ("1.0", "1.1"),
        ("1.0", "2.0"),
        ("1.0-1", "1.0-2"),
        ("1.0", "1.0-1"),
        # Czlony liczbowe porownujemy jako liczby, nie jako tekst.
        # Tekstowo "1.10" < "1.9", co jest odwrotnoscia prawdy.
        ("1.9", "1.10"),
        ("1.2", "1.11"),
        ("2.4.9", "2.4.10"),
        # Wiodace zera nie maja znaczenia.
        ("1.007", "1.8"),
        # Tylda jest przed wszystkim, takze przed koncem lancucha - dzieki
        # temu wersje przedwydawnicze wypadaja przed wydaniem koncowym.
        ("1.0~rc1", "1.0"),
        ("1.0~rc1", "1.0~rc2"),
        ("1.0~", "1.0"),
        ("1.0~beta", "1.0~rc1"),
        # Epoka bije wszystko.
        ("2.0", "1:1.0"),
        ("1:1.0", "2:0.1"),
        # Litery ida przed pozostalymi znakami.
        ("1.0a", "1.0+"),
        # Prawdziwe wersje z Debiana i Ubuntu.
        ("3.0.11-1~deb12u2", "3.0.11-1~deb12u3"),
        ("3.0.2-0ubuntu1.10", "3.0.2-0ubuntu1.15"),
        ("1:8.9p1-3", "1:8.9p1-3ubuntu0.6"),
        ("22.08.8-6", "22.08.8-6+deb12u1"),
    ],
)
def test_kolejnosc_wersji(mniejsza, wieksza):
    assert porownaj(mniejsza, wieksza) == -1, f"{mniejsza} powinna byc starsza niz {wieksza}"
    assert porownaj(wieksza, mniejsza) == 1, f"{wieksza} powinna byc nowsza niz {mniejsza}"


@pytest.mark.parametrize(
    "a, b",
    [
        ("1.0", "1.0"),
        ("1.0-1", "1.0-1"),
        ("0:1.0", "1.0"),          # brak epoki znaczy epoke zero
        ("1.007", "1.7"),          # wiodace zera bez znaczenia
        ("2.4.10", "2.4.10"),
    ],
)
def test_wersje_rowne(a, b):
    assert porownaj(a, b) == 0, f"{a} i {b} powinny byc rowne"


def test_rozbior_wersji():
    assert rozbierz("1:2.3.4-5") == (1, "2.3.4", "5")
    assert rozbierz("2.3.4") == (0, "2.3.4", "")
    assert rozbierz("2.3.4-5") == (0, "2.3.4", "5")


def test_rewizja_to_wszystko_po_ostatnim_mysliku():
    """Wersja gorna moze zawierac mysliki - rewizja jest ostatnim czlonem."""
    assert rozbierz("1.0-2-3") == (0, "1.0-2", "3")


# --- pytanie, ktore faktycznie zadajemy -------------------------------------

def test_pakiet_starszy_niz_poprawka_jest_podatny():
    assert starsza_niz("3.0.11-1~deb12u2", "3.0.11-1~deb12u3") is True


def test_pakiet_z_poprawka_nie_jest_podatny():
    assert starsza_niz("3.0.11-1~deb12u3", "3.0.11-1~deb12u3") is False


def test_pakiet_nowszy_niz_poprawka_nie_jest_podatny():
    assert starsza_niz("3.0.11-1~deb12u4", "3.0.11-1~deb12u3") is False


def test_backport_nie_jest_uznawany_za_podatny():
    """Sedno calego problemu: dystrybucje lataja wstecznie, nie zmieniajac
    numeru glownego. Wersja 22.08.8-6+deb12u1 zawiera poprawke, ktorej numer
    upstream 22.08.8 nie zdradza."""
    assert starsza_niz("22.08.8-6+deb12u1", "22.08.8-6+deb12u1") is False
    assert starsza_niz("22.08.8-6", "22.08.8-6+deb12u1") is True


def test_brak_wersji_nie_jest_podatnoscia():
    """Nieznana wersja to powod do milczenia, nie do alarmu."""
    assert starsza_niz("", "1.0") is False
    assert starsza_niz("1.0", "") is False
    assert starsza_niz(None, "1.0") is False
