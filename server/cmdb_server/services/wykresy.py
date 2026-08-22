"""Wykresy kolowe (pierscienie) dla widoku raportu w panelu.

Dlaczego osobno od wersji pocztowej: wiadomosc i strona maja rozne
ograniczenia. Klient poczty wycina sekcje <style> i nie renderuje SVG, wiec
w mailu zostaja tabele z tlem komorki. Przegladarka nie ma zadnego z tych
ograniczen - upieranie sie przy wspolnym rysunku obniza jakosc obu.

Pierscien rysujemy jednym okregiem na segment, sterujac stroke-dasharray:
widoczny jest wylacznie kawalek o dlugosci udzialu, reszta obwodu zostaje
pusta. Nie wymaga to liczenia luku ani atrybutu "d", wiec nie ma tu miejsca
na blad zaokraglenia, ktory rozjezdza rysunek.

Wszystko powstaje po stronie serwera, bo polityka bezpieczenstwa nie
dopuszcza skryptow spoza serwisu ani skryptow wpisanych w strone.
"""
from __future__ import annotations

from math import pi

# Barwy odrozniaja od siebie sasiednie segmenty, a nie niosa znaczenia.
# Dobrane tak, zeby zachowac czytelnosc na jasnym i na ciemnym tle - stad
# srednie nasycenie zamiast pasteli, ktore na ciemnym tle sie zlewaja.
PALETA = [
    "#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7",
    "#06b6d4", "#ec4899", "#84cc16", "#6366f1", "#f97316",
]

PROMIEN = 60.0
GRUBOSC = 26.0
BOK = 160.0

# Wiecej segmentow niz barw w palecie oznacza powtorki, a wykres kolowy
# z kilkunastoma kawalkami i tak przestaje byc czytelny. Nadmiar laczymy.
MAKS_SEGMENTOW = len(PALETA) - 1
POZOSTALE = "pozostale"


def _skrot(pozycje: list[tuple[str, int]]) -> list[tuple[str, int]]:
    if len(pozycje) <= MAKS_SEGMENTOW + 1:
        return pozycje
    uszeregowane = sorted(pozycje, key=lambda p: p[1], reverse=True)
    czolo = uszeregowane[:MAKS_SEGMENTOW]
    reszta = sum(wartosc for _, wartosc in uszeregowane[MAKS_SEGMENTOW:])
    return czolo + [(POZOSTALE, reszta)]


def pierscien(pozycje: list[tuple[str, int]]) -> dict:
    """Dane gotowe do wstawienia w SVG i w legende.

    Zwraca None-podobny pusty wynik, gdy nie ma czego rysowac - pusty
    pierscien wprowadzalby w blad, sugerujac, ze wszystkie wartosci sa zerowe.
    """
    pozycje = [(etykieta, wartosc) for etykieta, wartosc in pozycje if wartosc]
    suma = sum(wartosc for _, wartosc in pozycje)
    if not suma:
        return {"segmenty": [], "suma": 0, "obwod": 0.0, "promien": PROMIEN,
                "grubosc": GRUBOSC, "bok": BOK, "srodek": BOK / 2}

    pozycje = _skrot(pozycje)
    obwod = 2 * pi * PROMIEN
    segmenty = []
    narastajaco = 0.0

    for numer, (etykieta, wartosc) in enumerate(pozycje):
        udzial = wartosc * 100 / suma
        dlugosc = obwod * wartosc / suma
        segmenty.append(
            {
                "etykieta": etykieta,
                "wartosc": wartosc,
                "udzial": round(udzial, 1),
                "barwa": PALETA[numer % len(PALETA)],
                # Widoczny kawalek, potem przerwa do konca obwodu.
                "dasharray": f"{dlugosc:.3f} {obwod - dlugosc:.3f}",
                # Ujemne przesuniecie odsuwa segment od poczatku okregu;
                # obrot calej grupy o -90 stopni zaczyna wykres u gory.
                "dashoffset": f"{-narastajaco:.3f}",
            }
        )
        narastajaco += dlugosc

    return {
        "segmenty": segmenty,
        "suma": suma,
        "obwod": round(obwod, 3),
        "promien": PROMIEN,
        "grubosc": GRUBOSC,
        "bok": BOK,
        "srodek": BOK / 2,
    }


# Przy podziale wedlug wagi barwa niesie znaczenie, wiec nie moze byc brana
# z ogolnej palety. W wersji pocztowej krytyczne i wysokie maja ten sam
# czerwony - na pierscieniu byly by nieodroznialne, wiec rozdzielamy odcienie.
PALETA_WAGI = {
    "krytyczne (9+)": "#b91c1c",
    "wysokie (7-9)": "#ef4444",
    "srednie (4-7)": "#f59e0b",
    "niskie (<4)": "#3b82f6",
    "bez oceny": "#94a3b8",
}


def z_paskow(paski: list[dict], barwy: dict[str, str] | None = None) -> dict:
    """Pierscien z danych przygotowanych dla wersji pocztowej.

    Zrodlo liczb pozostaje jedno - strona i wiadomosc pokazuja te same
    wartosci, roznia sie wylacznie sposobem narysowania.
    """
    wynik = pierscien([(p["etykieta"], p["wartosc"]) for p in paski])
    if barwy:
        for segment in wynik["segmenty"]:
            segment["barwa"] = barwy.get(segment["etykieta"], segment["barwa"])
    return wynik
