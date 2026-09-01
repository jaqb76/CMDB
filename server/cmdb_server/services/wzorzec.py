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
    "rodzaj": [
        {"klucz": "nazwa", "etykieta": "Nazwa rodzaju", "typ": "tekst",
         "wymagane": True, "w_etykiecie": True, "grupa": "Tozsamosc"},
        # Klucz jest tozsamoscia rodzaju: po nim leza wpisy sprzetu i po nim
        # rozpoznaja go relacje. Etykiete zmienia sie dowolnie, klucza nigdy.
        {"klucz": "klucz_rodzaju", "etykieta": "Klucz", "typ": "tekst",
         "wymagane": True, "grupa": "Tozsamosc",
         "podpowiedz": "male litery bez spacji, np. projektor"},
        {"klucz": "opis", "etykieta": "Do czego sluzy", "typ": "notatka",
         "grupa": "Tozsamosc"},
    ],
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


# Rodzaje sprzetu, ktore firma dostaje na start. Klucze musza zgadzac sie
# z TYPY_SPRZETU, bo to one leza przy istniejacym sprzecie.
RODZAJE_STARTOWE: list[tuple[str, str]] = [
    ("komputer", "Komputer / serwer"),
    ("siec", "Sprzet sieciowy"),
    ("drukarka", "Drukarka / skaner"),
    ("monitor", "Monitor"),
    ("telefon", "Telefon / tablet"),
    ("vm", "Maszyna wirtualna"),
    ("host", "Host wirtualizacji"),
    ("klaster", "Klaster"),
    ("aplikacja", "Aplikacja"),
    ("inne", "Inne"),
]

# Klucze, na ktorych stoi kod: relacje sprawdzaja po nich, czy zwiazek ma sens,
# a wykrywanie sieci klasyfikuje znaleziska. Zmiana albo usuniecie takiego
# rodzaju wylaczyloby funkcje bez zadnego komunikatu.
KLUCZE_CHRONIONE = frozenset({"komputer", "vm", "host", "klaster", "aplikacja"})

# Zestawy pol wlasciwych dla rodzaju. Sa punktem wyjscia, nie ograniczeniem -
# administrator dokłada i usuwa je tak samo jak w kazdym innym schemacie.
POLA_RODZAJU: dict[str, list[dict]] = {
    "monitor": [
        {"klucz": "przekatna_cale", "etykieta": "Przekatna (cale)", "typ": "liczba",
         "min": 5, "max": 120, "grupa": "Parametry"},
        {"klucz": "rozdzielczosc", "etykieta": "Rozdzielczosc", "typ": "tekst",
         "grupa": "Parametry", "podpowiedz": "2560x1440"},
        {"klucz": "zlacza", "etykieta": "Zlacza", "typ": "tekst",
         "grupa": "Parametry", "podpowiedz": "HDMI, DisplayPort"},
        {"klucz": "matryca", "etykieta": "Matryca", "typ": "wybor",
         "opcje": ["IPS", "VA", "TN", "OLED", "nieznana"], "grupa": "Parametry"},
    ],
    "siec": [
        {"klucz": "liczba_portow", "etykieta": "Liczba portow", "typ": "liczba",
         "min": 1, "max": 1024, "grupa": "Parametry"},
        {"klucz": "poe", "etykieta": "Zasilanie PoE", "typ": "logiczna", "grupa": "Parametry"},
        {"klucz": "predkosc_portow", "etykieta": "Predkosc portow", "typ": "wybor",
         "opcje": ["100 Mb/s", "1 Gb/s", "2,5 Gb/s", "10 Gb/s"], "grupa": "Parametry"},
        {"klucz": "wersja_firmware", "etykieta": "Wersja firmware", "typ": "tekst",
         "grupa": "Utrzymanie"},
        {"klucz": "adres_zarzadzania", "etykieta": "Adres zarzadzania", "typ": "tekst",
         "format": "url", "grupa": "Utrzymanie"},
    ],
    "drukarka": [
        {"klucz": "licznik_wydrukow", "etykieta": "Licznik wydrukow", "typ": "liczba",
         "min": 0, "grupa": "Eksploatacja"},
        {"klucz": "rodzaj_tonera", "etykieta": "Rodzaj tonera", "typ": "tekst",
         "grupa": "Eksploatacja"},
        {"klucz": "kolor", "etykieta": "Druk w kolorze", "typ": "logiczna",
         "grupa": "Parametry"},
        {"klucz": "dupleks", "etykieta": "Druk dwustronny", "typ": "logiczna",
         "grupa": "Parametry"},
    ],
    "telefon": [
        {"klucz": "imei", "etykieta": "IMEI", "typ": "tekst", "grupa": "Tozsamosc"},
        {"klucz": "numer", "etykieta": "Numer telefonu", "typ": "tekst",
         "format": "telefon", "grupa": "Abonament"},
        {"klucz": "operator", "etykieta": "Operator", "typ": "tekst", "grupa": "Abonament"},
        {"klucz": "koniec_umowy", "etykieta": "Koniec umowy", "typ": "data",
         "grupa": "Abonament"},
    ],
}


def wzorcowe_pola_rodzaju(klucz: str) -> Schemat:
    """Zestaw pol dla rodzaju sprzetu; pusty, gdy nie mamy dla niego wzorca."""
    return Schemat(kategoria="sprzet", wersja=1, pola=POLA_RODZAJU.get(klucz, []))
