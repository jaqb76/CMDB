"""Kody QR wstawiane wprost w strone portalu.

Kod jest rysowany jako SVG w kodzie strony, a nie jako osobny obrazek: nie ma
wtedy drugiego zapytania do serwera, nie trzeba pilnowac buforowania i kod
skaluje sie na ekranie telefonu bez rozmycia. Waga to okolo dwoch kilobajtow.

Biblioteka segno jest czystym Pythonem bez zaleznosci - portal pracuje
u klientow w sieciach bez dostepu do internetu, wiec generowanie kodu po
stronie przegladarki (skrypt z CDN) odpadalo.
"""
from __future__ import annotations

import segno

# Poziom korekcji bledow. "M" (15%) wystarcza kodowi ogladanemu na ekranie
# albo wydrukowanemu na kartce; wyzszy zageszcza obraz, a im gestszy kod, tym
# gorzej skanuje go starszy telefon z zabrudzonym obiektywem.
KOREKCJA = "m"

# Pusty pas wokol kodu w modulach. Specyfikacja wymaga co najmniej czterech.
MARGINES = 4


def svg(adres: str, *, skala: int = 5, ciemny: str = "#0B3C6E") -> str:
    """Kod QR z adresem jako gotowy fragment SVG.

    Jasne pola zostaja biale takze w ciemnym motywie: czytnik szuka kontrastu
    miedzy ciemnym a jasnym modulem, a kod narysowany na granatowym tle strony
    czesc telefonow po prostu pomija.

    Rozmiar ustala CSS (``.apk-kod svg``), nie atrybuty: SVG ma sam
    ``viewBox``, wiec skaluje sie do pudelka. Ze sztywnymi width/height
    i paddingiem z CSS (``box-sizing: border-box``) przegladarka ucinala prawy
    i dolny brzeg kodu, a takiego kodu zaden telefon nie odczytal.

    Margines to 4 moduly - minimum ze specyfikacji QR. Przy 2 czesc czytnikow
    nie znajdowala kodu, a kazde przyciecie od razu zjadalo jego krawedz.
    """
    kod = segno.make(adres, error=KOREKCJA)
    return kod.svg_inline(scale=skala, border=MARGINES, dark=ciemny, light="#ffffff",
                          omitsize=True)
