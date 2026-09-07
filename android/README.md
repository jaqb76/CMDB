# CMDB Mobile

Natywna aplikacja Android dla portalu CMDB. Nie osadza portalu w WebView.

## Wymagania

- Android Studio z JDK 17
- Android SDK 35
- serwer CMDB z API `/api/v1/mobile`
- publiczny adres serwera z prawidlowym certyfikatem HTTPS

## Uruchomienie

1. Otworz katalog `android` w Android Studio.
2. Poczekaj na synchronizacje Gradle.
3. Uruchom konfiguracje `app` na emulatorze lub telefonie z Androidem 8+.
4. Na ekranie logowania wpisz pelny adres portalu, np. `https://cmdb.example.pl`.

Adres HTTP jest celowo odrzucany. Token sesji jest przechowywany przy uzyciu
Android Keystore, a kopie zapasowe aplikacji sa wylaczone, aby poswiadczenie
nie trafilo do backupu konta Google.

## Zakres API

- logowanie tym samym kontem co portal,
- izolacja firm i tryb globalnego audytora/superadmina,
- pulpit,
- maszyny i karty urzadzen,
- osoby, przypisania i slowniki,
- historia zmian i audyt,
- definicje raportow oraz wysylka na zadanie.

Drugi etap dodaje wyszukiwanie maszyn, edycje przypisan oraz komplet
slownikow. Formularze osob, dzialow, lokalizacji i dostawcow sa budowane
dynamicznie ze schematu firmy, wiec zmiana pol w portalu nie wymaga wydania
nowej wersji aplikacji.

Uprawnienia `viewer`, `admin`, `superadmin` i `global_viewer` sa wyliczane po
stronie serwera. Ukrycie przycisku w aplikacji nie jest mechanizmem ochrony;
kazdy zapis jest ponownie autoryzowany i audytowany przez API.

## Wydanie 0.2.0 — drugi etap

- Dodawanie, edycja i usuwanie wpisów wszystkich słowników według schematu firmy.
- Obsługa grup, wymaganych pól, odwołań, tekstu, notatek, liczb, dat i wartości logicznych.
- Edycja opiekuna, użytkownika, lokalizacji, roli i miejsca z karty maszyny.
- Wyszukiwanie po nazwie, FQDN, IP i numerze seryjnym w całej bazie, filtr systemu
  i braku opiekuna oraz kolejne strony wyników.
- Tworzenie, edycja, usuwanie i wysyłka raportów; rodzaj, częstotliwość i kolumny
  pochodzą z katalogu backendu. Wysyłka do dostawców pozostaje jawną opcją.
- Jasny, ciemny i systemowy motyw. Przycisk w nagłówku przełącza te ustawienia;
  wybór zostaje zapisany na telefonie.
- Konta globalne wybierają firmę po logowaniu; audytor pozostaje tylko do odczytu.
- Formularz zamyka się po udanym zapisie; błąd walidacji nie usuwa wprowadzonych danych.

### Instalacja testowa

W GitHub Actions otwórz najnowszy udany przebieg `CMDB Android build`, pobierz
artefakt `CMDB-Mobile-debug-<run_id>`, rozpakuj i zainstaluj `app-debug.apk`.
Backend musi zawierać drugi etap API — samo zainstalowanie APK nie aktualizuje serwera.
Po wdrożeniu backendu zaloguj się kontem portalu. Telefon musi mieć dostęp sieciowy
oraz ufać certyfikatowi HTTPS serwera. To nadal wydanie testowe, nie publikacja w sklepie.

Buildy debug z różnych uruchomień CI mogą mieć różne podpisy. Jeżeli Android odmówi
aktualizacji, odinstaluj poprzedni APK (usunie lokalną sesję) i zainstaluj nowy.

### Test ręczny przed szerszym wdrożeniem

1. Konto administratora: utwórz wpis, sprawdź odwołania, edytuj i usuń go.
2. Wywołaj błąd walidacji i sprawdź zachowanie wpisanych danych.
3. Zmień przypisania maszyny i porównaj je z portalem.
4. Wyszukaj maszynę spoza pierwszej strony wyników, zmień filtr i załaduj kolejną stronę.
5. Utwórz raport, zmień kolumny i częstotliwość. Wysyłaj wyłącznie na świadomie wskazane adresy.
6. Konto viewer/audytora: sprawdź brak możliwości zapisu. Konto globalne: wybierz firmę.
7. Przełącz każdy motyw, zamknij aplikację i uruchom ponownie.
