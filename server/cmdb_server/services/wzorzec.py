"""Wzorcowy schemat slownikow - punkt wyjscia dla nowej firmy.

Firma dostaje KOPIE wzorca i od tej chwili schemat nalezy wylacznie do niej.
Pozniejsze poprawki wzorca dotycza tylko firm zakladanych po nich; nic nie
wraca do juz dzialajacych. Dzieki temu nie ma scalania, wersjonowania ani
podgladu roznic - schemat ma jednego wlasciciela i jedna historie.

Producent nie ma tu slownika swiadomie: zna go agent i czyta wprost ze sprzetu
(kolumna ``manufacturer``). Reczna lista obok automatycznej tylko rozjezdzalaby
sie z prawda. ``dostawca`` to kto inny - firma, u ktorej kupiono i ktora
serwisuje.
"""
from __future__ import annotations

from .schemat import Schemat

WZORCE: dict[str, list[dict]] = {
    "osoba": [
        {"klucz": "imie_nazwisko", "etykieta": "Imie i nazwisko", "typ": "tekst",
         "wymagane": True, "w_etykiecie": True, "grupa": "Tozsamosc"},
        {"klucz": "email", "etykieta": "E-mail", "typ": "tekst", "format": "email",
         "wymagane": True, "grupa": "Kontakt", "podpowiedz": "jan.kowalski@firma.pl"},
        {"klucz": "telefon", "etykieta": "Telefon", "typ": "tekst", "format": "telefon",
         "grupa": "Kontakt"},
        {"klucz": "dzial", "etykieta": "Dzial", "typ": "odwolanie", "cel": "dzial",
         "grupa": "Organizacja"},
        {"klucz": "stanowisko", "etykieta": "Stanowisko", "typ": "tekst",
         "grupa": "Organizacja"},
        {"klucz": "notatki", "etykieta": "Notatki", "typ": "notatka", "grupa": "Pozostale"},
    ],
    "dzial": [
        {"klucz": "nazwa_dzialu", "etykieta": "Dział", "typ": "tekst",
         "wymagane": True, "w_etykiecie": True, "grupa": "Tozsamosc",
         "podpowiedz": "np. Infrastruktura IT"},
        {"klucz": "skrot", "etykieta": "Skrot", "typ": "tekst", "grupa": "Tozsamosc",
         "podpowiedz": "np. IT", "w_etykiecie": True},
        {"klucz": "dzial_nadrzedny", "etykieta": "Dzial nadrzedny", "typ": "odwolanie",
         "cel": "dzial", "grupa": "Struktura"},
        {"klucz": "kierownik", "etykieta": "Kierownik", "typ": "odwolanie",
         "cel": "osoba", "rola": "kierownik", "grupa": "Struktura"},
        {"klucz": "centrum_kosztow", "etykieta": "Centrum kosztow", "typ": "tekst",
         "grupa": "Rozliczenia"},
    ],
    "lokalizacja": [
        {"klucz": "typ_miejsca", "etykieta": "Typ", "typ": "wybor", "wymagane": True,
         "opcje": ["biuro", "serwerownia", "magazyn", "praca zdalna", "inne"],
         "grupa": "Tozsamosc"},
        {"klucz": "ulica", "etykieta": "Ulica i numer", "typ": "tekst", "grupa": "Adres",
         "w_etykiecie": True},
        {"klucz": "kod_pocztowy", "etykieta": "Kod pocztowy", "typ": "tekst",
         "format": "kod_pocztowy", "grupa": "Adres", "podpowiedz": "00-950"},
        {"klucz": "miasto", "etykieta": "Miasto", "typ": "tekst", "rola": "miasto",
         "grupa": "Adres", "wymagane": True, "w_etykiecie": True},
        {"klucz": "kraj", "etykieta": "Kraj", "typ": "tekst", "grupa": "Adres",
         "podpowiedz": "Polska"},
        {"klucz": "budynek", "etykieta": "Budynek", "typ": "tekst", "rola": "budynek",
         "grupa": "Umiejscowienie"},
        {"klucz": "pietro", "etykieta": "Pietro", "typ": "tekst", "grupa": "Umiejscowienie"},
        {"klucz": "dzial", "etykieta": "Dział", "typ": "odwolanie", "cel": "dzial",
         "grupa": "Umiejscowienie", "w_etykiecie": True},
        {"klucz": "osoba_na_miejscu", "etykieta": "Osoba na miejscu", "typ": "odwolanie",
         "cel": "osoba", "grupa": "Kontakt"},
        {"klucz": "telefon", "etykieta": "Telefon", "typ": "tekst", "format": "telefon",
         "grupa": "Kontakt", "podpowiedz": "+48 22 579 00 00"},
    ],
    "dostawca": [
        {"klucz": "nazwa_firmy", "etykieta": "Dostawca", "typ": "tekst",
         "wymagane": True, "w_etykiecie": True, "grupa": "Tozsamosc"},
        {"klucz": "nip", "etykieta": "NIP", "typ": "tekst", "format": "nip",
         "grupa": "Tozsamosc", "podpowiedz": "10 cyfr", "w_etykiecie": True},
        {"klucz": "kanal_zgloszen", "etykieta": "Sposob kontaktu", "typ": "wybor",
         "wymagane": True, "opcje": ["portal", "e-mail", "telefon", "serwis u nas"],
         "rola": "kanal_zgloszen", "grupa": "Zgloszenia"},
        {"klucz": "email_zgloszen", "etykieta": "Adres zgloszen", "typ": "tekst",
         "format": "email", "rola": "email_zgloszen", "grupa": "Zgloszenia",
         "podpowiedz": "serwis@dostawca.pl"},
        {"klucz": "portal_zgloszen", "etykieta": "Portal zgloszen", "typ": "tekst",
         "format": "url", "grupa": "Zgloszenia"},
        {"klucz": "telefon_wsparcia", "etykieta": "Telefon wsparcia", "typ": "tekst",
         "format": "telefon", "rola": "telefon_wsparcia", "grupa": "Zgloszenia"},
        {"klucz": "godziny_wsparcia", "etykieta": "Godziny wsparcia", "typ": "tekst",
         "format": "godziny", "grupa": "Zgloszenia", "podpowiedz": "pn-pt 8:00-17:00"},
        {"klucz": "czas_reakcji_h", "etykieta": "Czas reakcji (godz.)", "typ": "liczba",
         "min": 1, "max": 720, "grupa": "Zgloszenia"},
        {"klucz": "numer_umowy", "etykieta": "Numer umowy", "typ": "tekst", "grupa": "Umowa"},
        {"klucz": "koniec_umowy", "etykieta": "Koniec umowy", "typ": "data", "grupa": "Umowa"},
        {"klucz": "opiekun", "etykieta": "Opiekun handlowy", "typ": "tekst", "grupa": "Umowa"},
        {"klucz": "adres", "etykieta": "Ulica, kod, miasto", "typ": "tekst", "grupa": "Adres"},
    ],
}


def wzorcowy(kategoria: str) -> Schemat:
    """Swieza kopia wzorca. Nigdy nie wspoldzielona - firma dostaje swoja."""
    return Schemat(kategoria=kategoria, wersja=1, pola=WZORCE.get(kategoria, []))
