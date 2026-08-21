"""Porownywanie wersji pakietow wedlug regul dpkg.

Serce dopasowywania podatnosci. Pytanie brzmi zawsze tak samo: czy wersja
zainstalowana jest starsza niz ta, w ktorej luke naprawiono. Zwykle
porownanie tekstowe daje tu zle odpowiedzi, i to w obie strony:

    "1.10" < "1.9"        tekstowo, choc 1.10 jest nowsze
    "1.0~rc1" > "1.0"     tekstowo, choc wersja przedwydawnicza jest starsza

Blad w te strone jest grozniejszy: podatnosc uznana za naprawiona nie pojawi
sie w zadnym raporcie i nikt sie o niej nie dowie.

Reguly sa opisane w Debian Policy 5.6.12. Wersja ma postac:

    [epoka:]wersja-gorna[-rewizja]

Porownuje sie kolejno epoke, wersje gorna i rewizje. W kazdej czesci idziemy
naprzemiennie: fragment nieliczbowy porownywany znak po znaku wedlug
zmodyfikowanego porzadku ASCII, potem fragment liczbowy porownywany jako
liczba. W porzadku znakow tylda jest PRZED wszystkim, takze przed koncem
lancucha - dzieki temu "1.0~rc1" wypada przed "1.0". Litery ida przed
pozostalymi znakami.
"""
from __future__ import annotations

import re

# Wersja Debiana: opcjonalna epoka, wersja gorna, opcjonalna rewizja.
_WZORZEC = re.compile(r"^(?:(\d+):)?([^:]*?)(?:-([^:-]*))?$")


def _ranga(znak: str) -> int:
    """Pozycja znaku w porzadku dpkg.

    Tylda jest przed pusta pozycja, litery przed reszta znakow. Wartosci sa
    dobrane tak, zeby zwykle porownanie liczb dawalo wlasciwa kolejnosc.
    """
    if znak == "~":
        return -1
    if znak.isalpha():
        return ord(znak)
    # Znaki niebedace literami maja byc PO literach.
    return ord(znak) + 256


def _porownaj_czesc(a: str, b: str) -> int:
    """Porownuje jedna czesc wersji (gorna albo rewizje)."""
    i = j = 0
    while i < len(a) or j < len(b):
        # Fragment nieliczbowy - znak po znaku wedlug porzadku dpkg.
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ranga_a = _ranga(a[i]) if i < len(a) and not a[i].isdigit() else 0
            ranga_b = _ranga(b[j]) if j < len(b) and not b[j].isdigit() else 0
            if ranga_a != ranga_b:
                return -1 if ranga_a < ranga_b else 1
            if i < len(a) and not a[i].isdigit():
                i += 1
            if j < len(b) and not b[j].isdigit():
                j += 1

        # Fragment liczbowy - jako liczba, wiec wiodace zera nie maja znaczenia.
        poczatek_a, poczatek_b = i, j
        while i < len(a) and a[i].isdigit():
            i += 1
        while j < len(b) and b[j].isdigit():
            j += 1
        liczba_a = int(a[poczatek_a:i] or "0")
        liczba_b = int(b[poczatek_b:j] or "0")
        if liczba_a != liczba_b:
            return -1 if liczba_a < liczba_b else 1

    return 0


def rozbierz(wersja: str) -> tuple[int, str, str]:
    """Rozklada wersje na epoke, wersje gorna i rewizje."""
    dopasowanie = _WZORZEC.match((wersja or "").strip())
    if dopasowanie is None:
        return (0, (wersja or "").strip(), "")
    epoka, gorna, rewizja = dopasowanie.groups()
    return (int(epoka or 0), gorna or "", rewizja or "")


def porownaj(a: str, b: str) -> int:
    """-1 gdy a < b, 0 gdy rowne, 1 gdy a > b."""
    epoka_a, gorna_a, rewizja_a = rozbierz(a)
    epoka_b, gorna_b, rewizja_b = rozbierz(b)

    if epoka_a != epoka_b:
        return -1 if epoka_a < epoka_b else 1
    wynik = _porownaj_czesc(gorna_a, gorna_b)
    if wynik:
        return wynik
    return _porownaj_czesc(rewizja_a, rewizja_b)


def starsza_niz(zainstalowana: str, naprawiona: str) -> bool:
    """Czy zainstalowana wersja jest starsza niz ta z poprawka.

    To jedyne pytanie, ktore zadajemy przy dopasowywaniu podatnosci: pakiet
    jest podatny dokladnie wtedy, gdy ma wersje starsza niz naprawiona.
    """
    if not zainstalowana or not naprawiona:
        return False
    return porownaj(zainstalowana, naprawiona) < 0
