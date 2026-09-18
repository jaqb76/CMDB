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

Gotowy APK leży w wydaniach repozytorium: otwórz
[Releases](https://github.com/jaqb76/CMDB/releases), znajdź wydanie
`CMDB Mobile <wersja>` i pobierz plik `CMDB-Mobile-<wersja>-debug.apk`.
Wydanie powstaje automatycznie po wejściu zmian aplikacji do gałęzi głównej;
ten sam numer wersji zbudowany ponownie podmienia plik w tym samym wydaniu.

Plik idzie do wydania, a nie do artefaktów przebiegu, bo artefakty mają limit
miejsca na koncie — po kilkunastu buildach wysyłka kończyła się błędem
„Artifact storage quota has been hit”, czyli APK był zbudowany, ale nie było
jak go pobrać.

### Rozdanie aplikacji technikom (kod QR w portalu)

Repozytorium jest prywatne, więc telefon technika nie pobierze APK z wydania
bez konta na GitHubie. Portal wydaje więc ten sam plik samodzielnie:

1. Pobierz `CMDB-Mobile-<wersja>-debug.apk` z wydania.
2. W portalu, w administracji → **Wersje agentów** → **Aplikacja Android**,
   wgraj plik. Zawartość jest sprawdzana — plik, który nie jest APK, nie
   przejdzie. Numer wersji odczytujemy z nazwy pliku (w samym APK siedzi
   w skompilowanym manifeście).
3. Zakładka **Instalacja agenta** pokazuje od tej chwili kod QR. Technik
   skanuje go aparatem telefonu i pobiera aplikację.

Adres w kodzie QR niesie podpisany klucz wystawiony osobie, która ten kod
ogląda: jest ważny 30 minut i unieważnia go zmiana hasła tego konta. To nie
jest publiczny odnośnik do pliku — telefon nie musi mieć sesji portalu, ale
adres przepisany komuś innemu przestaje działać po pół godzinie.

APK leży w podkatalogu katalogu wydań agenta (`releases/mobilna`), czyli na
wolumenie, który już jest w `docker compose` i wchodzi do kopii zapasowej —
plik przeżywa odtworzenie kontenera.
Backend musi zawierać drugi etap API — samo zainstalowanie APK nie aktualizuje serwera.
Po wdrożeniu backendu zaloguj się kontem portalu. Telefon musi mieć dostęp sieciowy
oraz ufać certyfikatowi HTTPS serwera. To nadal wydanie testowe, nie publikacja w sklepie.

Od wersji 0.5.1 wszystkie wydania są podpisane tym samym kluczem debugowym
(`android/keystore/cmdb-debug.keystore`), więc instalują się na wierzch
poprzedniej wersji. Przejście z wersji zbudowanej **wcześniej** wymaga jednego
odinstalowania — tamte APK miały losowy podpis z przebiegu CI i Android odmawia
aktualizacji („konflikt z istniejącym pakietem”). Odinstalowanie kasuje zapisaną
sesję, więc po instalacji trzeba zalogować się ponownie.

### Test ręczny przed szerszym wdrożeniem

1. Konto administratora: utwórz wpis, sprawdź odwołania, edytuj i usuń go.
2. Wywołaj błąd walidacji i sprawdź zachowanie wpisanych danych.
3. Zmień przypisania maszyny i porównaj je z portalem.
4. Wyszukaj maszynę spoza pierwszej strony wyników, zmień filtr i załaduj kolejną stronę.
5. Utwórz raport, zmień kolumny i częstotliwość. Wysyłaj wyłącznie na świadomie wskazane adresy.
6. Konto viewer/audytora: sprawdź brak możliwości zapisu. Konto globalne: wybierz firmę.
7. Przełącz każdy motyw, zamknij aplikację i uruchom ponownie.


## Wydanie 0.4.0 — wygląd według makiet

Aplikacja została przerysowana tak, żeby odpowiadała projektowi graficznemu.
Zakres API i uprawnienia się nie zmieniły — to jest zmiana wyglądu, nie funkcji.

- Logowanie: wyśrodkowany znak firmowy, etykiety nad polami, pełnej szerokości
  przycisk „Zaloguj”. Formularz da się przewinąć, gdy klawiatura zasłoni ekran.
- Pulpit: kafelki liczbowe na jasnym tle z ikoną i podpisem, pierścień
  „Dystrybucja systemów operacyjnych” z udziałem procentowym wpisanym w wycinek
  i legendą „62% (154)”, a „Ostatni kontakt” jako jedna karta z wierszami.
- Maszyny: karty z logo systemu (Windows, pingwin, malina, przełącznik),
  adresem IP, opiekunem i czasem ostatniego kontaktu w kolorze świeżości.
- Dolny pasek ma trzy pozycje — Pulpit, Maszyny, Więcej. Słowniki, historia
  zmian, raporty, zmiana firmy, motyw i wylogowanie są pod „Więcej”.
- Czasy są pokazywane względnie („18 min temu”, „3 dni temu”) zamiast surowym
  znacznikiem ISO z serwera.
- Motyw jasny prowadzi granatem, ciemny jasnym błękitem; ikony pasków systemowych
  dostrajają się do wybranego motywu.

Kropka i kolor przy „Ostatni kontakt” mówią o świeżości danych (do godziny,
do doby, powyżej doby). Kafelek „Bez kontaktu” liczy serwer według ustawień
firmy — te dwie liczby nie muszą się zgadzać i nie są tym samym.

## Wydanie 0.5.0 — helpdesk w telefonie

Zakładka „Helpdesk” pokazuje zgłoszenia wszystkich firm, które obsługuje
zalogowane konto — tak samo jak panel WWW i niezależnie od firmy wybranej
w aplikacji. Konto bez dostępu do helpdesku nie widzi tej zakładki wcale.

- Lista zgłoszeń z wyszukiwaniem po numerze, temacie i adresie, licznikami
  „Otwarte / Moje / Czekają” oraz filtrami (otwarte, moje, czekające,
  nieprzypisane, zamknięte). Znacznik na pasku liczy sprawy, w których
  ostatnie słowo należy do klienta.
- Karta zgłoszenia: status, rodzaj, powiązany sprzęt, kartoteka zgłaszającego,
  przypisany technik, czas pracy i załączniki. Zmiana statusu, przypisanie,
  dopisanie czasu i podpięcie sprzętu działają z telefonu.
- Rozmowa: cały wątek w jednym miejscu — wiadomości klienta, odpowiedzi
  i notatki wewnętrzne (oznaczone kłódką, nie wychodzą na zewnątrz).
  Odpowiedź przestawia zgłoszenie na „Oczekuje”, chyba że technik to wyłączy.
- Nowe zgłoszenie z telefonu: firma, zgłaszający, temat, rodzaj sprawy,
  urządzenie, opis, zdjęcie z aparatu lub plik z dysku. Klient może dostać
  numer mailem, jeżeli skrzynka helpdesku jest włączona.
- Załączniki: pobranie i otwarcie w aplikacji telefonu, a doklejone do
  odpowiedzi wychodzą razem z mailem do klienta.

### Czego nie ma i dlaczego

Projekt graficzny pokazuje priorytet zgłoszenia i licznik SLA. CMDB nie ma
w modelu danych ani jednego, ani drugiego — zgłoszenie ma status, rodzaj
i historię wiadomości. Zamiast priorytetu aplikacja pokazuje rodzaj sprawy
(incydent, prośba, inne), a w miejscu licznika SLA to, co system naprawdę wie:
od kiedy sprawa czeka na NASZĄ odpowiedź. Wymyślony licznik wyglądałby jak
zobowiązanie, którego nikt nie podjął.

## Wydanie 0.5.1 — aktualizacja bez odinstalowania

- Wszystkie wydania są podpisane jednym kluczem debugowym z repozytorium, więc
  kolejny APK instaluje się na wierzch poprzedniego. Wcześniej każdy przebieg
  CI generował własny klucz i Android odmawiał aktualizacji.
- Technik helpdesku widzi w aplikacji firmy, które mu nadano. Wcześniej konto
  bez własnej firmy (a tak wygląda konto technika) utykało na ekranie „Wybierz
  firmę — brak dostępnych firm”, mimo nadanego dostępu.
- Plik, którego telefon nie potrafi odczytać, przerywa wysyłkę z komunikatem
  zamiast wypaść po cichu z załączników.

### O kluczu debugowym

Klucz leży w repozytorium i **celowo nie jest tajny** — ma hasło domyślne dla
Androida (`android`). Służy wyłącznie do budowania wersji testowych: nie da się
nim opublikować aplikacji w sklepie ani dostać do serwera CMDB. Cena jest taka,
że ktoś z dostępem do repozytorium może zbudować APK, które Android przyjmie
jako aktualizację tej aplikacji — żeby to wykorzystać, musiałby i tak namówić
kogoś na instalację swojego pliku.

Jeśli ta cena jest za wysoka, klucz można trzymać jako sekret repozytorium
(`base64` w `secrets`) i odtwarzać go w workflow przed budowaniem — wtedy nie
ma go w historii gita, ale wydania nadal mają ten sam podpis.

## Błąd 429 podczas logowania

Po przekroczeniu limitu błędnych haseł portal blokuje konto i adres źródłowy.
Aplikacja 0.2.2 pokazuje czas do ponownej próby. Administrator może odblokować
logowanie z katalogu `deploy` poleceniem:

```bash
docker compose exec server cmdb-admin odblokuj --wszystko
```

Po odblokowaniu przed kolejną próbą sprawdź hasło przez portal WWW. Sam restart
kontenera nie usuwa blokady, ponieważ jest zapisana w PostgreSQL.
