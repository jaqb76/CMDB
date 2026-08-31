"""Schemat slownika firmowego: jakie pola ma wpis i jak sprawdzac wartosci.

Schemat jest DANYMI, nie kolumnami. Kazda firma ma wlasny i moze go zmieniac
bez migracji bazy - inaczej dodanie jednego atrybutu wymagaloby wydania serwera,
a firmy roznia sie dokladnie tym, co chca sledzic.

Rozdzielamy dwie rzeczy, ktore latwo pomylic:

* **typ** mowi, czym wartosc JEST - decyduje o przechowywaniu, sortowaniu
  i porownywaniu; liczba da sie posortowac, data porownac,
* **format** mowi, jak wartosc WYGLADA - decyduje o sprawdzaniu, podpowiedzi
  i kontrolce w formularzu.

Kod pocztowy nie jest osobnym typem, tylko tekstem o formacie. Gdyby kazdy taki
przypadek robic typem, skonczyloby sie na kilkudziesieciu typach roznacych sie
wylacznie wzorcem.

Sprawdzanie MUSI dzialac po stronie serwera; formularz w przegladarce jest
wygoda, a nie zabezpieczeniem. Ten sam schemat jedzie do formularza i wraca
tutaj przy zapisie.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- limity -----------------------------------------------------------------
#
# Bez gornej granicy pierwsze wklejenie kolumn z arkusza zamienia formularz
# w cos, czego nikt nie otworzy. Wartosci z zapasem: wzorzec ma kilkanascie pol.
MAKS_POL = 40
MAKS_OPCJI = 50
MAKS_ETYKIETY = 60
MAKS_TEKSTU = 500
MAKS_NOTATKI = 2000

TYPY = ("tekst", "notatka", "liczba", "data", "logiczna", "wybor", "odwolanie")

# Role wiaza funkcje systemu ze schematem. Funkcja pyta o role, nie o nazwe
# pola - dzieki temu zmiana etykiety na "E-mail serwisu" niczego nie psuje.
# Jedno pole na role: dwa adresy zgloszen to pytanie, na ktore system nie
# umialby odpowiedziec.
ROLE: dict[str, str] = {
    "email_zgloszen": "adres, na ktory ida zgloszenia serwisowe",
    "kanal_zgloszen": "sposob kontaktu z dostawca",
    "telefon_wsparcia": "numer wsparcia technicznego",
    "miasto": "miasto lokalizacji - do zestawien",
    "budynek": "budynek - do zestawien",
    "kierownik": "osoba odpowiedzialna za dzial",
}


# --- formaty ----------------------------------------------------------------

def _norm_kod_pocztowy(wartosc: str) -> str:
    cyfry = re.sub(r"\D", "", wartosc)
    return f"{cyfry[:2]}-{cyfry[2:]}" if len(cyfry) == 5 else wartosc


def _norm_email(wartosc: str) -> str:
    return wartosc.strip().lower()


def _norm_telefon(wartosc: str) -> str:
    """Numer w jednej postaci, zeby dwa zapisy tego samego byly tym samym."""
    czysty = re.sub(r"[^\d+]", "", wartosc)
    if czysty.startswith("00"):
        czysty = "+" + czysty[2:]
    if not czysty.startswith("+") and len(czysty) == 9:
        czysty = "+48" + czysty          # numer krajowy bez prefiksu
    if czysty.startswith("+48") and len(czysty) == 12:
        reszta = czysty[3:]
        return f"+48 {reszta[:2]} {reszta[2:5]} {reszta[5:7]} {reszta[7:]}"
    return czysty


def _norm_url(wartosc: str) -> str:
    czysty = wartosc.strip()
    if czysty and not re.match(r"^https?://", czysty, re.I):
        czysty = "https://" + czysty
    return czysty


def _norm_nip(wartosc: str) -> str:
    return re.sub(r"\D", "", wartosc)


def _suma_nip(cyfry: str) -> bool:
    """Suma kontrolna NIP - sprawdza wiecej niz dlugosc.

    Bez niej "1111111111" przechodzi jako poprawny numer, a to jest wlasnie
    ten rodzaj danych, ktory wpisuje sie, zeby formularz przestal marudzic.
    """
    if len(cyfry) != 10 or not cyfry.isdigit():
        return False
    wagi = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    suma = sum(waga * int(cyfra) for waga, cyfra in zip(wagi, cyfry)) % 11
    return suma != 10 and suma == int(cyfry[9])


FORMATY: dict[str, dict] = {
    "kod_pocztowy": {
        "wzorzec": r"^\d{2}-\d{3}$",
        "normalizuj": _norm_kod_pocztowy,
        "przyklad": "00-950",
        "kontrolka": "text",
        "blad": "kod pocztowy ma postac 00-000",
    },
    "email": {
        "wzorzec": r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$",
        "normalizuj": _norm_email,
        "przyklad": "serwis@dostawca.pl",
        "kontrolka": "email",
        "blad": "to nie wyglada na adres e-mail",
    },
    "telefon": {
        "wzorzec": r"^\+?[\d ]{6,20}$",
        "normalizuj": _norm_telefon,
        "przyklad": "+48 22 579 00 00",
        "kontrolka": "tel",
        "blad": "numer moze zawierac tylko cyfry, spacje i prefiks +",
    },
    "url": {
        "wzorzec": r"^https?://[^\s/]+\.[^\s]*$",
        "normalizuj": _norm_url,
        "przyklad": "https://support.dostawca.pl",
        "kontrolka": "url",
        "blad": "to nie wyglada na adres strony",
    },
    "nip": {
        "wzorzec": r"^\d{10}$",
        "normalizuj": _norm_nip,
        "przyklad": "1234563218",
        "kontrolka": "text",
        "blad": "NIP ma 10 cyfr i poprawna sume kontrolna",
        "dodatkowe": _suma_nip,
    },
    "godziny": {
        "wzorzec": r"^.{1,60}$",
        "normalizuj": lambda w: " ".join(w.split()),
        "przyklad": "pn-pt 8:00-17:00",
        "kontrolka": "text",
        "blad": "opis godzin jest za dlugi",
    },
}


# --- schemat ----------------------------------------------------------------

class Pole(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    # Klucz jest tozsamoscia pola: po nim leza wartosci w istniejacych wpisach.
    # Etykiete zmienia sie dowolnie, klucza nigdy - zmiana klucza to w istocie
    # usuniecie pola i dodanie nowego.
    klucz: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    etykieta: str = Field(min_length=1, max_length=MAKS_ETYKIETY)
    typ: Literal[TYPY] = "tekst"  # type: ignore[valid-type]
    format: str | None = None
    opcje: list[str] = Field(default_factory=list, max_length=MAKS_OPCJI)
    wymagane: bool = False
    rola: str | None = None
    grupa: str = Field(default="Pozostale", max_length=MAKS_ETYKIETY)
    podpowiedz: str | None = Field(default=None, max_length=120)
    cel: str | None = None          # dla typu "odwolanie": osoba albo kategoria
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def spojnosc(self):
        if self.format is not None:
            if self.format not in FORMATY:
                raise ValueError(f"nieznany format: {self.format}")
            if self.typ != "tekst":
                raise ValueError("format dotyczy wylacznie pol tekstowych")
        if self.typ == "wybor" and not self.opcje:
            raise ValueError("pole wyboru wymaga listy opcji")
        if self.typ != "wybor" and self.opcje:
            raise ValueError("opcje ma tylko pole wyboru")
        if self.typ == "odwolanie" and not self.cel:
            raise ValueError("odwolanie wymaga wskazania celu")
        if self.rola is not None and self.rola not in ROLE:
            raise ValueError(f"nieznana rola: {self.rola}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min nie moze byc wieksze od max")
        return self


class Schemat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kategoria: str
    wersja: int = Field(default=1, ge=1)
    pola: list[Pole] = Field(default_factory=list, max_length=MAKS_POL)

    @field_validator("pola")
    @classmethod
    def bez_powtorzen(cls, pola):
        klucze = [pole.klucz for pole in pola]
        if len(set(klucze)) != len(klucze):
            raise ValueError("klucze pol musza byc rozne")
        role = [pole.rola for pole in pola if pole.rola]
        if len(set(role)) != len(role):
            raise ValueError("jedna rola moze byc przypisana tylko do jednego pola")
        return pola

    def pole(self, klucz: str) -> Pole | None:
        return next((p for p in self.pola if p.klucz == klucz), None)

    def wg_roli(self, rola: str) -> Pole | None:
        return next((p for p in self.pola if p.rola == rola), None)

    def grupy(self) -> dict[str, list[Pole]]:
        wynik: dict[str, list[Pole]] = {}
        for pole in self.pola:
            wynik.setdefault(pole.grupa, []).append(pole)
        return wynik


# --- sprawdzanie wartosci ---------------------------------------------------

class BladPola(Exception):
    def __init__(self, bledy: dict[str, str]):
        self.bledy = bledy
        super().__init__("; ".join(f"{k}: {v}" for k, v in bledy.items()))


def _sprawdz_pole(pole: Pole, surowa: Any) -> Any:
    """Zwraca wartosc gotowa do zapisu albo podnosi ValueError z powodem."""
    if isinstance(surowa, str):
        surowa = surowa.strip()
    if surowa in (None, "", []):
        return None

    if pole.typ == "liczba":
        try:
            liczba = float(str(surowa).replace(",", "."))
        except ValueError:
            raise ValueError("wpisz liczbe")
        if pole.min is not None and liczba < pole.min:
            raise ValueError(f"nie mniej niz {pole.min:g}")
        if pole.max is not None and liczba > pole.max:
            raise ValueError(f"nie wiecej niz {pole.max:g}")
        return int(liczba) if liczba.is_integer() else liczba

    if pole.typ == "data":
        try:
            return date.fromisoformat(str(surowa)).isoformat()
        except ValueError:
            raise ValueError("data w postaci RRRR-MM-DD")

    if pole.typ == "logiczna":
        return str(surowa).lower() in {"1", "true", "tak", "on"}

    if pole.typ == "wybor":
        if str(surowa) not in pole.opcje:
            raise ValueError("wybierz wartosc z listy")
        return str(surowa)

    if pole.typ == "odwolanie":
        return str(surowa)[:36]

    tekst = str(surowa)
    if pole.typ == "notatka":
        if len(tekst) > MAKS_NOTATKI:
            raise ValueError(f"najwyzej {MAKS_NOTATKI} znakow")
        return tekst

    if pole.format:
        opis = FORMATY[pole.format]
        tekst = opis["normalizuj"](tekst)
        if not re.match(opis["wzorzec"], tekst):
            raise ValueError(opis["blad"])
        dodatkowe = opis.get("dodatkowe")
        if dodatkowe and not dodatkowe(tekst):
            raise ValueError(opis["blad"])
        return tekst

    if len(tekst) > MAKS_TEKSTU:
        raise ValueError(f"najwyzej {MAKS_TEKSTU} znakow")
    return tekst


def sprawdz(schemat: Schemat, dane: dict, *, egzekwuj_wymagane: bool) -> dict:
    """Sprawdza i normalizuje wartosci wpisu.

    ``egzekwuj_wymagane`` rozstrzyga napiecie miedzy polami obowiazkowymi
    a slownikiem, ktory zapelnia sie sam. Wpis powstajacy mimochodem - przy
    zapisie maszyny - pomija te kontrole i zostaje szkicem; wpis edytowany
    swiadomie w karcie slownika bez wymaganych pol sie nie zapisze. Inaczej
    dodanie maszyny od nieznanego dostawcy zaczynaloby sie od wypelniania
    jego metryczki, a wtedy nikt nie wpisalby niczego.
    """
    wynik: dict[str, Any] = {}
    bledy: dict[str, str] = {}
    for pole in schemat.pola:
        try:
            wartosc = _sprawdz_pole(pole, dane.get(pole.klucz))
        except ValueError as exc:
            bledy[pole.klucz] = str(exc)
            continue
        if wartosc is None:
            if pole.wymagane and egzekwuj_wymagane:
                bledy[pole.klucz] = "pole wymagane"
            continue
        wynik[pole.klucz] = wartosc
    if bledy:
        raise BladPola(bledy)
    return wynik


def braki(schemat: Schemat, atrybuty: dict | None) -> list[str]:
    """Etykiety wymaganych pol, ktorych wpis jeszcze nie ma.

    Kompletnosc pokazujemy, a nie wymuszamy - lista slownika ma odpowiadac na
    pytanie "co jeszcze zostalo do uzupelnienia", zamiast blokowac prace.
    """
    dane = atrybuty or {}
    return [pole.etykieta for pole in schemat.pola
            if pole.wymagane and dane.get(pole.klucz) in (None, "", [])]
