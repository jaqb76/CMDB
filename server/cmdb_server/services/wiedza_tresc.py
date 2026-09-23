"""Tresc artykulow bazy wiedzy: Markdown -> HTML, pola maszyny, wyszukiwanie.

Tresc trzymamy jako Markdown, a nie HTML z edytora. Powody:

* bezpieczenstwo - renderujemy z WYLACZONYM surowym HTML, wiec ``<script>``
  wpisany w tresc zawsze wychodzi jako tekst; HTML z edytora trzeba by czyscic
  przy kazdym zapisie i latwo cos przepuscic,
* historia - porownanie dwoch wersji Markdownu to porownanie linii tekstu,
* wyszukiwanie - do indeksu trafia tekst bez znacznikow.

Rozszerzenia wzorowane na Confluence:

* panele - blok miedzy ``:::uwaga Tytul`` a ``:::`` (info, uwaga, stop, ok),
* pola maszyny - ``{hostname}``, ``{fqdn}``, ``{ip}``, ``{system}``,
  ``{lokalizacja}``; artykul otwarty w kontekscie maszyny podstawia jej dane.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

# html=False: znaczniki wpisane w tresc ida na strone jako tekst. Walidacja
# odnosnikow markdown-it odrzuca javascript:, vbscript: i data: (poza
# obrazkami), wiec z tresci nie da sie tez zrobic klikalnego skryptu.
_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

PANELE: dict[str, str] = {
    "info": "Informacja",
    "uwaga": "Uwaga",
    "stop": "Stop",
    "ok": "Gotowe",
}

POLA_MASZYNY: dict[str, str] = {
    "hostname": "nazwa hosta",
    "fqdn": "pełna nazwa (FQDN)",
    "ip": "adres IP",
    "system": "system operacyjny",
    "lokalizacja": "lokalizacja",
}

_POCZATEK_PANELU = re.compile(r"^:::[ \t]*(" + "|".join(PANELE) + r")\b[ \t]*(.*)$")
_KONIEC_PANELU = re.compile(r"^:::[ \t]*$")
_PLOT = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_POLE = re.compile(r"\{(" + "|".join(POLA_MASZYNY) + r")\}")
_ZNACZNIK = re.compile(r"(<[^>]*>)")


def _podziel(tresc: str) -> list[tuple[str, str, str]]:
    """Dzieli tresc na odcinki: ("md", "", tekst) i ("panel", rodzaj, tytul+tekst).

    Panel dziala tylko na najwyzszym poziomie dokumentu, poza blokami kodu -
    ``:::`` wewnatrz przykladu w bloku kodu zostaje przykladem.
    """
    odcinki: list[tuple[str, str, str]] = []
    bufor: list[str] = []
    panel: tuple[str, str] | None = None
    wnetrze: list[str] = []
    plot: str | None = None

    def zamknij_bufor() -> None:
        if bufor:
            odcinki.append(("md", "", "\n".join(bufor)))
            bufor.clear()

    for linia in tresc.replace("\r\n", "\n").split("\n"):
        dopasowanie_plotu = _PLOT.match(linia)
        if dopasowanie_plotu:
            znak = dopasowanie_plotu.group(1)[0]
            if plot is None:
                plot = znak
            elif plot == znak:
                plot = None
        if plot is None and panel is None:
            poczatek = _POCZATEK_PANELU.match(linia)
            if poczatek:
                zamknij_bufor()
                panel = (poczatek.group(1), poczatek.group(2).strip())
                continue
        if plot is None and panel is not None and _KONIEC_PANELU.match(linia):
            odcinki.append(("panel", panel[0], panel[1] + "\x00" + "\n".join(wnetrze)))
            panel, wnetrze = None, []
            continue
        (wnetrze if panel is not None else bufor).append(linia)

    if panel is not None:
        # Niezamkniety panel obejmuje reszte tresci - lepsze niz zgubienie jej.
        odcinki.append(("panel", panel[0], panel[1] + "\x00" + "\n".join(wnetrze)))
    zamknij_bufor()
    return odcinki


def _pola(html: str, kontekst: dict[str, str] | None) -> str:
    """Podstawia pola maszyny w TEKSCIE strony, nigdy wewnatrz znacznikow.

    Wartosci pochodza z raportu agenta, wiec sa escapowane jak kazdy inny
    tekst od uzytkownika.
    """
    def zamien(dopasowanie: re.Match) -> str:
        klucz = dopasowanie.group(1)
        wartosc = (kontekst or {}).get(klucz)
        if wartosc:
            return (f'<span class="kb-pole kb-pole-wypelnione" title="{POLA_MASZYNY[klucz]}">'
                    f"{escape(wartosc)}</span>")
        return (f'<span class="kb-pole" title="{POLA_MASZYNY[klucz]} — uzupełnia się, '
                f'gdy artykuł otworzysz z karty maszyny">{{{klucz}}}</span>')

    kawalki = _ZNACZNIK.split(html)
    return "".join(k if k.startswith("<") else _POLE.sub(zamien, k) for k in kawalki)


def renderuj(tresc: str, kontekst: dict[str, str] | None = None) -> Markup:
    """HTML artykulu, gotowy do wstawienia w szablon."""
    czesci: list[str] = []
    for rodzaj, podtyp, tekst in _podziel(tresc or ""):
        if rodzaj == "md":
            czesci.append(_md.render(tekst))
            continue
        tytul, _, wnetrze = tekst.partition("\x00")
        czesci.append(
            f'<div class="kb-panel kb-panel-{podtyp}">'
            f'<div class="kb-panel-tytul">{escape(tytul or PANELE[podtyp])}</div>'
            f"{_md.render(wnetrze)}</div>"
        )
    return Markup(_pola("".join(czesci), kontekst))


def kontekst_maszyny(asset, payload: dict | None = None) -> dict[str, str]:
    """Wartosci pol {hostname} itd. dla wskazanej maszyny."""
    os_info = (payload or {}).get("os") or {}
    system = os_info.get("name") or " ".join(
        x for x in (asset.os_name, asset.os_version) if x
    )
    return {
        "hostname": asset.hostname or "",
        "fqdn": asset.fqdn or "",
        "ip": asset.primary_ip or "",
        "system": system or "",
        "lokalizacja": asset.lokalizacja.wartosc if asset.lokalizacja else "",
    }


# --- wyszukiwanie ------------------------------------------------------------

@lru_cache(maxsize=4096)
def _znak_bez_ogonka(znak: str) -> str:
    """Jeden znak -> jeden znak: bez ogonkow i malymi literami.

    Dlugosc tekstu musi zostac taka sama, bo po pozycjach w tekscie
    znormalizowanym wycinamy fragment z oryginalu (podswietlenie wyniku).
    """
    if znak in "łŁ":
        return "l"
    rozlozony = unicodedata.normalize("NFKD", znak)
    podstawa = rozlozony[0] if rozlozony else znak
    mala = podstawa.lower()
    return mala if len(mala) == 1 else podstawa


def bez_ogonkow(tekst: str) -> str:
    return "".join(_znak_bez_ogonka(z) for z in tekst or "")


def do_indeksu(tekst: str) -> str:
    """Tekst dla to_tsvector('simple', ...): bez ogonkow i bez interpunkcji.

    Interpunkcje zamieniamy na spacje po obu stronach (indeks i zapytanie),
    inaczej parser Postgresa traktuje "db02.example.local" jako jeden wyraz,
    a zapytanie "db02" nie trafialoby w nic.
    """
    return re.sub(r"[^\w]+", " ", bez_ogonkow(tekst))


def slowa_zapytania(zapytanie: str) -> list[str]:
    """Slowa zapytania, bezpieczne do zlozenia w tsquery (same litery i cyfry)."""
    slowa = [s[:40] for s in re.findall(r"\w+", bez_ogonkow(zapytanie or ""))]
    return list(dict.fromkeys(s for s in slowa if s))[:8]


def tsquery(zapytanie: str) -> str | None:
    """Zapytanie "wszystkie slowa, kazde jako poczatek wyrazu" albo None."""
    slowa = slowa_zapytania(zapytanie)
    return " & ".join(f"{s}:*" for s in slowa) if slowa else None


_ZNACZNIKI_MD = [
    (re.compile(r"^[ \t]*:::[ \t]*\w*", re.M), ""),       # panele
    (re.compile(r"^[ \t]*(#{1,6}|>|[-*+]|\d+[.)])[ \t]+", re.M), ""),   # naglowki, listy
    (re.compile(r"^[ \t]*(`{3,}|~{3,}).*$", re.M), ""),    # ploty blokow kodu
    (re.compile(r"^[ \t]*\|?[ \t:|-]+\|[ \t:|-]*$", re.M), ""),   # linia pod naglowkiem tabeli
    (re.compile(r"\*\*|__|`|\|"), " "),
]


def bez_znacznikow(tekst: str) -> str:
    """Tresc Markdown jako zwykly tekst - do fragmentu w wynikach wyszukiwania."""
    for wzor, zamiana in _ZNACZNIKI_MD:
        tekst = wzor.sub(zamiana, tekst or "")
    return tekst


def fragment(tekst: str, zapytanie: str, szerokosc: int = 90) -> Markup:
    """Fragment tresci wokol pierwszego trafienia, z podswietlonymi slowami."""
    tekst = re.sub(r"\s+", " ", bez_znacznikow(tekst)).strip()
    slowa = slowa_zapytania(zapytanie)
    znormalizowany = bez_ogonkow(tekst)
    pozycje = [znormalizowany.find(s) for s in slowa if znormalizowany.find(s) >= 0]
    if not pozycje:
        return Markup(escape(tekst[: 2 * szerokosc] + ("…" if len(tekst) > 2 * szerokosc else "")))
    start = max(0, min(pozycje) - szerokosc)
    koniec = min(len(tekst), min(pozycje) + szerokosc)
    wycinek, wycinek_norm = tekst[start:koniec], znormalizowany[start:koniec]

    trafienia: list[tuple[int, int]] = []
    for slowo in slowa:
        for m in re.finditer(re.escape(slowo), wycinek_norm):
            trafienia.append((m.start(), m.end()))
    trafienia.sort()
    wynik, ostatni = [], 0
    for poczatek, koniec_trafienia in trafienia:
        if poczatek < ostatni:
            continue
        wynik.append(str(escape(wycinek[ostatni:poczatek])))
        wynik.append(f"<mark>{escape(wycinek[poczatek:koniec_trafienia])}</mark>")
        ostatni = koniec_trafienia
    wynik.append(str(escape(wycinek[ostatni:])))
    return Markup(("…" if start else "") + "".join(wynik) + ("…" if koniec < len(tekst) else ""))
