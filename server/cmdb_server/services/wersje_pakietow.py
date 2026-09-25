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


# --- jadro ------------------------------------------------------------------

# Nazwa wydania jadra z uname: "7.0.0-1011-aws" albo "6.8.0-51-generic".
# Interesuje nas czlon ABI, czyli "7.0.0-1011" - to on rosnie z kazda
# aktualizacja jadra i to jego podaja wersje pakietow.
_ABI_JADRA = re.compile(r"^(\d+\.\d+\.\d+-\d+)")


def abi_jadra(wydanie: str | None) -> str | None:
    """Czlon ABI z nazwy wydania jadra albo z wersji pakietu.

    "7.0.0-1011-aws"  -> "7.0.0-1011"
    "7.0.0-1008.8"    -> "7.0.0-1008"

    Wersja pakietu jadra ma na koncu numer kompilacji ("-1008.8"), ktorego
    uname nie podaje. Porownanie pelnych wersji byloby wiec porownywaniem
    dwoch roznych rzeczy - do rozstrzygniecia, czy dziala nowsze jadro,
    wystarczy i musi wystarczyc sam czlon ABI.
    """
    if not wydanie:
        return None
    dopasowanie = _ABI_JADRA.match(wydanie.strip())
    return dopasowanie.group(1) if dopasowanie else None


def dzialajace_jadro_starsze(uruchomione: str | None, naprawione: str | None) -> bool | None:
    """Czy DZIALAJACE jadro jest starsze niz to z poprawka.

    Zwraca None, gdy nie da sie tego ustalic - wtedy zostaje zwykle
    porownanie wersji pakietu, bo lepiej zglosic za duzo niz przemilczec.
    """
    a, b = abi_jadra(uruchomione), abi_jadra(naprawione)
    if a is None or b is None:
        return None
    return porownaj(a, b) < 0


# --- RPM (Red Hat i pochodne) -----------------------------------------------
#
# RPM porownuje wersje inaczej niz dpkg i pomylenie jednych regul z drugimi
# daje zle odpowiedzi na granicach: w RPM znaki nie bedace litera ani cyfra
# sa tylko separatorami ("1.0_1" == "1.0.1"), segment liczbowy jest zawsze
# nowszy niz literowy, a po tyldzie jest daszek ("1.0^git1" jest PO "1.0",
# ale przed "1.0.1"). Implementacja odwzorowuje rpmvercmp() z rpm 4.x.
#
# Wersja RPM ma postac [epoka:]wersja-wydanie ("1:3.0.7-27.el9_4").

def _segmenty_rpm(a: str, b: str) -> int:
    """rpmvercmp: porownanie jednej czesci (wersji albo wydania)."""
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and not a[i].isalnum() and a[i] not in "~^":
            i += 1
        while j < len(b) and not b[j].isalnum() and b[j] not in "~^":
            j += 1

        # Tylda sortuje przed wszystkim, takze przed koncem napisu.
        if (i < len(a) and a[i] == "~") or (j < len(b) and b[j] == "~"):
            if not (i < len(a) and a[i] == "~"):
                return 1
            if not (j < len(b) and b[j] == "~"):
                return -1
            i += 1
            j += 1
            continue

        # Daszek sortuje przed wszystkim OPROCZ konca napisu.
        if (i < len(a) and a[i] == "^") or (j < len(b) and b[j] == "^"):
            if i >= len(a):
                return -1
            if j >= len(b):
                return 1
            if a[i] != "^":
                return 1
            if b[j] != "^":
                return -1
            i += 1
            j += 1
            continue

        if not (i < len(a) and j < len(b)):
            break

        poczatek_a, poczatek_b = i, j
        if a[i].isdigit():
            liczbowy = True
            while i < len(a) and a[i].isdigit():
                i += 1
            while j < len(b) and b[j].isdigit():
                j += 1
        else:
            liczbowy = False
            while i < len(a) and a[i].isalpha():
                i += 1
            while j < len(b) and b[j].isalpha():
                j += 1

        seg_a, seg_b = a[poczatek_a:i], b[poczatek_b:j]
        if not seg_b:
            # Rozne rodzaje segmentow: liczbowy jest zawsze nowszy.
            return 1 if liczbowy else -1
        if liczbowy:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return -1 if len(seg_a) < len(seg_b) else 1
        if seg_a != seg_b:
            return -1 if seg_a < seg_b else 1

    if i >= len(a) and j >= len(b):
        return 0
    return -1 if i >= len(a) else 1


def rozbierz_rpm(wersja: str) -> tuple[int | None, str, str]:
    """Rozklada wersje RPM na epoke, wersje i wydanie.

    Epoka None znaczy "nie podano" - to cos innego niz 0, patrz starsza_niz_rpm.
    """
    tekst = (wersja or "").strip()
    epoka: int | None = None
    if ":" in tekst:
        przed, tekst = tekst.split(":", 1)
        epoka = int(przed) if przed.isdigit() else 0
    wersja_gorna, _, wydanie = tekst.rpartition("-")
    if not wersja_gorna:
        return epoka, wydanie, ""
    return epoka, wersja_gorna, wydanie


def porownaj_rpm(a: str, b: str, *, pomin_epoke: bool = False) -> int:
    """-1 gdy a < b, 0 gdy rowne, 1 gdy a > b - wedlug regul RPM."""
    epoka_a, wersja_a, wydanie_a = rozbierz_rpm(a)
    epoka_b, wersja_b, wydanie_b = rozbierz_rpm(b)
    if not pomin_epoke:
        epoka_a, epoka_b = epoka_a or 0, epoka_b or 0
        if epoka_a != epoka_b:
            return -1 if epoka_a < epoka_b else 1
    wynik = _segmenty_rpm(wersja_a, wersja_b)
    if wynik:
        return wynik
    return _segmenty_rpm(wydanie_a, wydanie_b)


def starsza_niz_rpm(zainstalowana: str, naprawiona: str) -> bool:
    """Czy zainstalowany pakiet RPM jest starszy niz ten z poprawka.

    Nowy agent podaje epoke zawsze ("0:1.2-3") w polu "evr", starsze - nigdy. Brak
    dwukropka znaczy wiec "epoka nieznana", a nie "epoka 0". Porownanie
    nieznanej epoki z "1:..." z danych Red Hata uznaloby kazdy pakiet z epoka
    za podatny - to setki falszywych alarmow. Epoka w obrebie jednego wydania
    RHEL praktycznie sie nie zmienia, wiec gdy jej nie znamy, porownujemy
    sama wersje i wydanie.
    """
    if not zainstalowana or not naprawiona:
        return False
    nieznana_epoka = ":" not in zainstalowana
    return porownaj_rpm(zainstalowana, naprawiona, pomin_epoke=nieznana_epoka) < 0


# Architektura na koncu nazwy wydania jadra RHEL: "5.14.0-427.13.1.el9_4.x86_64".
_ARCH_JADRA_RPM = re.compile(r"\.(x86_64|aarch64|ppc64le|s390x|i686|noarch)$")


def dzialajace_jadro_starsze_rpm(uruchomione: str | None, naprawione: str | None) -> bool | None:
    """Odpowiednik dzialajace_jadro_starsze() dla RHEL.

    uname podaje "5.14.0-427.13.1.el9_4.x86_64", czyli wersje i wydanie
    pakietu jadra z doklejona architektura - po jej odcieciu da sie porownac
    wprost z wersja z poprawka.
    """
    if not uruchomione or not naprawione:
        return None
    bez_arch = _ARCH_JADRA_RPM.sub("", uruchomione.strip())
    # Warianty jadra: "...el9_4.x86_64+debug", "...el9_4.x86_64+rt".
    bez_arch = _ARCH_JADRA_RPM.sub("", bez_arch.split("+", 1)[0])
    if "-" not in bez_arch:
        return None
    return porownaj_rpm(bez_arch, naprawione, pomin_epoke=True) < 0
