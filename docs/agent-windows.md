# Agent na Windows

Od wersji **0.5.9** worker i tray są jednym `cmdb-agent.exe`. Bez argumentów
program startuje cicho do zasobnika; `run` wykonuje raport, a `configure`
otwiera ustawienia. [Skanowanie sieci i aktualizacja](wykrywanie-sieci.md).

## Wymagania

* Windows 7 SP1 / Server 2008 R2 lub nowszy (PowerShell 5.1 jest w systemie
  od Windows 10 / Server 2016; na starszych wystarczy WMF 5.1).
* Uprawnienia administratora — część danych (konta, poprawki, numery seryjne
  dysków) jest niedostępna dla zwykłego użytkownika. Agent działa jako SYSTEM.
* Łączność HTTPS do serwera CMDB. Agent nie wymaga otwierania portów
  przychodzących — inicjuje połączenie sam.
* Python **nie jest wymagany** — agent i ikona są w jednym samodzielnym `.exe`.
* Ikona w zasobniku wymaga sesji graficznej. Na serwerach bez pulpitu instaluj
  z `-NoTray` — sam agent działa wtedy normalnie jako zadanie.

## Budowanie

Domyślny build tworzy jeden `cmdb-agent.exe`. Instalator jest opcjonalny:

```powershell
cd agent\packaging
.\build-agent.ps1 -Installer

# z podpisem kodu — bez niego SmartScreen i część systemów EDR blokują agenta:
.\build-agent.ps1 -Installer -SignCertThumbprint 1A2B3C…
```

| Plik | Rola |
|---|---|
| `cmdb-agent.exe` | bez argumentów: tray; `run`: worker SYSTEM; `configure`: ustawienia |
| `CMDB-Agent-Setup-0.5.10.exe` | opcjonalny instalator jednego programu |

Budowanie instalatora wymaga **Inno Setup 6** (`winget install JRSoftware.InnoSetup`).
Bez niego powstaną same pliki `.exe`, a instalacja przebiegnie skryptem
`install-agent.ps1`.

Zgodność wydania sprawdź na najstarszej wspieranej wersji Windows w swojej flocie.

## Instalacja z kreatora

Uruchom `CMDB-Agent-Setup-0.5.10.exe` i podaj dane otrzymane od administratora:

```
┌─ Połączenie z serwerem CMDB ─────────────────────────────┐
│  Adres serwera (wymagane https):  https://cmdb.firma.pl  │
│  Token rejestracyjny:             cmdb_ent_…             │
└──────────────────────────────────────────────────────────┘
```

Kreator sprawdza oba pola od razu — adres bez `https://` i token bez prefiksu
`cmdb_ent_` nie przepuszczą do następnego kroku. Kolejne strony pozwalają
ustawić częstotliwość raportowania, zbieranie listy procesów, widoczność ikony
w zasobniku i opcjonalny plik CA.

Cała logika instalacji siedzi w `install-agent.ps1` — kreator tylko go wywołuje.
Dzięki temu instalacja z kreatora i wdrożenie masowe przez GPO idą dokładnie tą
samą ścieżką, zamiast rozjeżdżać się w dwóch miejscach.

## Instalacja z wiersza poleceń

```powershell
.\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_… -IntervalHours 4
```

Skrypt:

1. kopiuje pliki do `%ProgramFiles%\CMDB Agent`,
2. zapisuje `%ProgramData%\CMDB\agent.conf` i zawęża dostęp do katalogu
   (`icacls`: tylko SYSTEM i Administratorzy — plik zawiera token),
3. tworzy `%ProgramData%\CMDB\public\` na plik statusu — czytelny dla
   wszystkich użytkowników, bo nie ma w nim żadnych sekretów,
4. rejestruje maszynę w serwerze (wymiana tokenu firmowego na własny),
5. tworzy zadanie **CMDB Agent** działające jako SYSTEM: przy starcie systemu
   (z 3-minutowym opóźnieniem) i co N godzin, z losowym rozrzutem do 10 minut,
6. nadaje grupie Użytkownicy prawo *uruchomienia* zadania (nie zmiany), żeby
   „Synchronizuj teraz" z ikony nie prosiło o hasło administratora,
7. dodaje ikonę w zasobniku do autostartu i wysyła pierwszy raport.

Parametry dodatkowe:

| Parametr | Zastosowanie |
|---|---|
| `-CaBundle C:\pki\firma-ca.pem` | serwer z certyfikatem wewnętrznego CA |
| `-PinSha256 <odcisk>` | przypięcie certyfikatu serwera |
| `-NoProcessList` | rezygnacja ze zbierania listy procesów |
| `-IntervalHours 8` | rzadsze raportowanie (domyślnie 4 h) |
| `-NoTray` | bez ikony w zasobniku (serwery bez pulpitu) |
| `-Silent` | bez komunikatów interaktywnych (tryb dla instalatora) |

## Monitorowanie usług

Obok zadania inwentaryzacji instalator zakłada drugie: **„CMDB Agent Monitor”**
(SYSTEM, przy starcie systemu, bez limitu czasu). Sprawdza ono dostępność usług
i ważność certyfikatów wskazanych w panelu CMDB.

Musi być osobne: inwentaryzacja odpala się raz na kilka godzin i kończy,
a sonda dostępności ma chodzić co minutę — jedno w drugim zmieścić się nie da.

```powershell
Get-ScheduledTask -TaskName "CMDB Agent Monitor" | Get-ScheduledTaskInfo
Get-Content "$env:ProgramData\CMDB\agent.log" -Tail 40 -Wait
```

Cele i ich odstępy przychodzą z serwera przy każdym pobraniu polityki, więc
zmiana w panelu działa bez wchodzenia na maszynę. Bez przypisanych celów
zadanie tylko pyta o politykę i śpi — dlatego zakładamy je zawsze.

Otwarta przerwa i znane odciski certyfikatów przeżywają restart; leżą
w `%ProgramData%\CMDB\monitoring-state.json`. Ręczne uruchomienie:
`cmdb-agent monitor`.

Opis samego monitorowania:
[`monitorowanie-uslug.md`](monitorowanie-uslug.md).

## Ikona w zasobniku

Ikona pokazuje stan kolorem, bez otwierania czegokolwiek:

| Kolor | Znaczenie |
|---|---|
| zielony | ostatnia synchronizacja poprawna |
| pomarańczowy | brak łączności — raporty czekają w buforze |
| czerwony | serwer odrzucił raport (np. wycofany token) |
| szary | agent nieskonfigurowany |

Menu: **Status agenta**, **Synchronizuj teraz**, **Ustawienia**, **Pokaż dziennik**,
**Zakończ**. Okno statusu odświeża się co 5 sekund i pokazuje:

```
Agent inwentaryzacyjny CMDB
Synchronizacja poprawna

Ostatnia poprawna synchronizacja   2026-08-19 20:29:34  (3 min temu)
Ostatnia proba                     -
Nastepna okolo                     2026-08-20 00:29:34
Maszyna                            WS-KSIEGOWOSC-01
Firma                              firma-abc
Serwer                             https://cmdb.firma.pl
```

**Data ostatniej poprawnej synchronizacji jest pokazywana osobno od daty
ostatniej próby.** Ma to znaczenie praktyczne: agent może próbować co godzinę
i za każdym razem dostawać odmowę — wtedy „ostatnia próba: przed chwilą"
sugerowałaby, że wszystko działa, podczas gdy dane w CMDB są sprzed tygodnia.
Data poprawnej synchronizacji przesuwa się **wyłącznie** po potwierdzeniu
przyjęcia raportu przez serwer.

### Podział uprawnień

Ikona działa jako zwykły zalogowany użytkownik i tylko **czyta** plik
`%ProgramData%\CMDB\public\status.json`. Nigdy nie sięga do pliku
z poświadczeniem. Operacje wymagające uprawnień rozwiązywane są tak:

* **Synchronizuj teraz** → uruchomienie zadania harmonogramu, które i tak
  działa jako SYSTEM (i dzięki temu zbierze komplet danych). Gdy się nie uda —
  monit UAC.
* **Ustawienia** → osobny, podniesiony proces `cmdb-agent.exe configure`.

Dzięki temu nic, co chodzi cały czas na pulpicie, nie potrzebuje uprawnień
administratora.

## Zmiana ustawień po instalacji

Ikona w zasobniku → **Ustawienia…** (albo `cmdb-agent.exe configure` z wiersza
poleceń jako administrator). Okno pozwala zmienić adres serwera, token, plik CA
i częstotliwość, a po zapisaniu od razu rejestruje maszynę — nie trzeba czekać
do najbliższego przebiegu zadania, żeby zobaczyć, czy dane są poprawne.

Typowe błędy tłumaczone są na komunikaty, z których wynika, co zrobić:

| Sytuacja | Komunikat |
|---|---|
| certyfikat wewnętrznego CA | „Nie udało się zweryfikować certyfikatu serwera… wskaż plik CA" |
| token wycofany lub obcięty | „Serwer odrzucił token. Sprawdź, czy nie został wycofany…" |
| zapora / zły adres | „Brak łączności z serwerem. Sprawdź adres, połączenie i zaporę" |
| niezgodny odcisk certyfikatu | „…może to oznaczać podszycie się pod serwer" |

## Wdrożenie masowe

Token rejestracyjny jest wspólny dla firmy, więc ten sam wiersz polecenia
działa na wszystkich maszynach.

**GPO** — skrypt startowy komputera, na udziale dostępnym do odczytu dla
`Domain Computers`:

```powershell
\\srv\deploy\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_…
```

Warto poprzedzić sprawdzeniem, czy agent już jest, żeby nie instalować
przy każdym starcie:

```powershell
if (-not (Get-ScheduledTask -TaskName "CMDB Agent" -ErrorAction SilentlyContinue)) {
    & \\srv\deploy\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_…
}
```

**Intune / SCCM** — spakuj `cmdb-agent.exe`
i `install-agent.ps1` (kreator graficzny nie nadaje się do wdrożenia cichego,
bo pyta o dane).

* Polecenie instalacji: `powershell.exe -ExecutionPolicy Bypass -File install-agent.ps1 -ServerUrl … -Token … -Silent`
* Polecenie deinstalacji: `powershell.exe -ExecutionPolicy Bypass -File uninstall-agent.ps1`
* Reguła wykrywania: obecność pliku `%ProgramFiles%\CMDB Agent\cmdb-agent.exe`

Token trafia do konfiguracji pakietu — traktuj ją jak materiał wrażliwy
i ogranicz dostęp do udziału/pakietu.

## Co agent zbiera

| Sekcja | Pola | Źródło |
|---|---|---|
| `hardware.system` | producent, model, numer seryjny, UUID, obudowa, domena, wirtualizacja | `Win32_ComputerSystem`, `Win32_BIOS`, `Win32_ComputerSystemProduct`, `Win32_SystemEnclosure` |
| `hardware.cpu` | model, rdzenie fizyczne i logiczne, taktowanie, gniazda | `Win32_Processor` |
| `hardware.memory` | pojemność łączna, moduły (slot, rozmiar, typ, taktowanie, s/n) | `Win32_PhysicalMemory` |
| `hardware.storage` | dyski fizyczne (model, rozmiar, SSD/HDD, s/n), wolumeny (rozmiar, wolne, zajętość) | `Win32_DiskDrive`, `Get-PhysicalDisk`, `Win32_LogicalDisk` |
| `hardware.firmware` | producent, wersja i data BIOS/UEFI | `Win32_BIOS` |
| `hardware.gpus` | karty graficzne, wersje sterowników | `Win32_VideoController` |
| `os` | nazwa, wersja, build, edycja, architektura, instalacja, ostatni start, język, strefa | `Win32_OperatingSystem`, `Win32_TimeZone` |
| `network.interfaces` | nazwa, MAC, adresy IP, bramy, DNS, DHCP, stan | `Win32_NetworkAdapter(Configuration)` |
| `software.packages` | nazwa, wersja, producent, data instalacji, zasięg (maszyna/użytkownik) | klucze rejestru `Uninstall` |
| `software.services` | nazwa, opis, stan, tryb startu, konto, ścieżka | `Win32_Service` |
| `software.updates` | poprawki i data instalacji | `Win32_QuickFixEngineering` |
| `software.processes` | nazwa, PID, pamięć, ścieżka | `Win32_Process` |
| `users.local_accounts` | konto, opis, SID, stan, blokada, wygasanie hasła, ostatnie logowanie | `Win32_UserAccount`, `Win32_NetworkLoginProfile` |
| `users.administrators` | członkowie grupy administratorów (lokalni i domenowi) | ADSI WinNT, grupa po SID `S-1-5-32-544` |
| `users.groups` | Użytkownicy pulpitu zdalnego, Operatorzy kopii zapasowych | ADSI WinNT po SID |
| `users.sessions` | zalogowani użytkownicy | `Win32_LoggedOnUser` |

### Dwie decyzje warte odnotowania

**Lista programów czytana jest z rejestru, nie przez `Win32_Product`.**
Zapytanie do `Win32_Product` uruchamia operację *reconfigure* na każdym
zainstalowanym pakiecie MSI. Na maszynie z kilkudziesięcioma programami
potrafi to trwać minutami, zasypuje dziennik zdarzeń wpisami MsiInstaller
i realnie potrafi uszkodzić instalację. To najczęstszy błąd w skryptach
inwentarzowych. Klucze `Uninstall` dają te same dane natychmiast i bez
skutków ubocznych.

**Grupa administratorów wyszukiwana jest po SID `S-1-5-32-544`, nie po nazwie.**
Na polskim Windows grupa nazywa się „Administratorzy”. Skrypt szukający
`Administrators` zwróciłby pustą listę — i to po cichu, bez błędu.

## Diagnostyka

### Gdy rejestracja się zawiesza

Zacznij od `doctor` — rozkłada połączenie na etapy i mierzy każdy osobno,
więc od razu widać, który zawodzi. Odpowiada w kilka sekund zamiast czekać
do końca limitów:

```powershell
.\cmdb-agent.exe doctor
.\cmdb-agent.exe --server https://127.0.0.1:8443 --ca-bundle C:\...\server.crt doctor
```

```
  [OK  ] Adres serwera                        0.00 s  127.0.0.1:8443
  [OK  ] Rozwiazanie nazwy                    0.00 s  127.0.0.1 (IPv4)
  [BLAD] Polaczenie TCP 127.0.0.1 (IPv4)      5.01 s  brak odpowiedzi - zapora odrzuca pakiety po cichu

  Wynik: polaczenie NIE dziala.
  Podpowiedz: Brak odpowiedzi na TCP zwykle oznacza regule zapory...
```

To samo jest pod przyciskiem **Sprawdź połączenie** w oknie ustawień (działa
przed zapisaniem konfiguracji) i w menu ikony w zasobniku.

Trzy przyczyny, które to najczęściej wyłapuje:

Wynik ma trzy poziomy: `OK`, `UWAGA` (działa, ale coś kosztuje czas)
i `BŁĄD` (nie działa). O werdykcie decydują wyłącznie błędy — nieudana próba
na jeden z kilku adresów nie jest awarią, bo klient przechodzi do następnego.

| Objaw w `doctor` | Poziom | Przyczyna | Co zrobić |
|---|---|---|---|
| TCP: „brak odpowiedzi" po pełnym limicie | BŁĄD | zapora odrzuca pakiety po cichu | reguła w Zaporze Windows po stronie serwera |
| TCP na IPv6 nieudane, IPv4 udane | UWAGA | `localhost` rozwiązuje się też na `::1`, serwer słucha tylko IPv4 | wpisz adres IPv4 wprost: `https://127.0.0.1:8443` |
| TCP: wszystkie adresy odmawiają | BŁĄD | serwer nie działa lub zły port | sprawdź, czy serwer wystartował |
| TLS: „certyfikat niezaufany" | BŁĄD | certyfikat self-signed | wskaż plik CA (`--ca-bundle` / pole w oknie) |
| TLS: „nie obejmuje nazwy" | BŁĄD | użyta nazwa nie jest w certyfikacie | użyj nazwy lub adresu z certyfikatu |

Przypadek z IPv6 wygląda niegroźnie, ale na Windows odrzucenie połączenia na
`::1` potrafi kosztować ~2 s — i to przy **każdym** żądaniu, bo agent za każdym
razem próbuje najpierw IPv6. `doctor` podaje zmierzone opóźnienie, jeśli
przekracza pół sekundy.

### Pozostałe polecenia

```powershell
cd "$env:ProgramFiles\CMDB Agent"

.\cmdb-agent.exe status                    # stan i data ostatniej poprawnej synchronizacji
.\cmdb-agent.exe status --json             # to samo maszynowo (czyta to ikona w zasobniku)
.\cmdb-agent.exe show --out C:\temp\raport.json   # zbierz bez wysyłki i obejrzyj
.\cmdb-agent.exe run                       # jeden pełny cykl z komunikatami
.\cmdb-agent.exe --log-level DEBUG run     # czasy poszczególnych kroków
.\cmdb-agent.exe configure                 # okno ustawień (wymaga uprawnień administratora)

Get-Content "$env:ProgramData\CMDB\agent.log" -Tail 50
Get-ScheduledTaskInfo -TaskName "CMDB Agent"   # ostatni wynik zadania
```

Przykładowe wyjście `status`:

```
stan                 : brak lacznosci z serwerem
ostatnia udana synchr: 2026-08-19 20:10:00 (7 min temu)
ostatnia proba       : 2026-08-19 20:17:19 (przed chwila)
ostatni blad         : nie udalo sie polaczyc z serwerem po 4 probach
raporty w buforze    : 1
```

Kody wyjścia:

| Kod | Znaczenie | Co zrobić |
|---|---|---|
| 0 | raport wysłany | — |
| 1 | błędna konfiguracja lub nieoczekiwany błąd | sprawdź `agent.conf` i dziennik |
| 2 | serwer odrzucił żądanie (np. 401) | token wycofany albo maszyna usunięta — zarejestruj ponownie |
| 3 | brak łączności | raport trafił do bufora, pójdzie przy następnym cyklu; uruchom `doctor` |
| 4 | niezgodny odcisk certyfikatu | **potencjalny MITM** — sprawdź certyfikat serwera przed dalszymi krokami |

Najczęstsze sytuacje:

* **`CERTIFICATE_VERIFY_FAILED`** — serwer ma certyfikat wewnętrznego CA,
  a stacja mu nie ufa. Dodaj CA do magazynu maszyny albo użyj `-CaBundle`.
* **Pusta lista programów** — agent działa jako zwykły użytkownik.
  Sprawdź, czy zadanie ma konto SYSTEM.
* **Sekcja z błędem w panelu** — pojedynczy kolektor nie miał uprawnień
  lub przekroczył limit czasu. Reszta raportu jest poprawna; treść błędu
  widać na karcie maszyny.
* **Maszyna zdublowana w panelu** — producent wpisał w SMBIOS ten sam UUID
  na wielu maszynach. Agent to wykrywa i schodzi na `MachineGuid`, ale jeśli
  maszyny sklonowano razem z systemem, `MachineGuid` też się powtórzy —
  wtedy trzeba przegenerować go sysprepem.

## Ile trwa rejestracja

Przy sprawnym połączeniu **1–3 sekundy**. Rejestracja nie zbiera całego
raportu — czyta tylko identyfikację maszyny (jedno wywołanie PowerShella
z kilkoma zapytaniami CIM) i wysyła jedno żądanie HTTPS. Pierwszy pełny
raport idzie dopiero potem i zajmuje kilka–kilkanaście sekund, zależnie
od liczby zainstalowanych programów.

Gdy połączenie nie działa, czas zależy od tego, **jak** nie działa:

| Sytuacja | Czas do błędu |
|---|---|
| serwer odmawia połączenia (RST) lub zła nazwa DNS | poniżej sekundy |
| zapora odrzuca pakiety po cichu — rejestracja z okna | ~30 s |
| zapora odrzuca pakiety po cichu — agent w tle | ~4 min 15 s |

Ta różnica jest zamierzona. W tle cierpliwość jest zaletą — agent ma przetrwać
chwilową awarię sieci, więc próbuje 4 razy z limitem 60 s. Przy rejestracji
z okna ktoś czeka przed ekranem, więc budżet jest skrócony do 2 prób po 15 s.
Test `test_interactive_budget_fits_in_process_timeout` pilnuje, żeby limit
okna był większy od budżetu agenta — inaczej okno ubijałoby agenta, zanim ten
zdąży zgłosić zrozumiały błąd.

## Bufor offline

Gdy serwer jest nieosiągalny, raport trafia do
`%ProgramData%\CMDB\spool\` (do 12 najnowszych) i zostaje wysłany przy
najbliższym udanym połączeniu. Dzięki temu laptop pracujący tydzień poza
biurem nie zostawia dziury w historii inwentarza.

## Aktualizacja i deinstalacja

Aktualizacja: uruchom nowy `CMDB-Agent-Setup.exe` albo `install-agent.ps1`
z nowym `.exe`. Skrypt zatrzyma zadanie i podmieni program. Przy aktualizacji
tej samej rejestracji pozostaw token pusty: poświadczenie oraz ustawienia
wykrywania sieci zostaną zachowane. Nowy token oznacza ponowną rejestrację.
Ikona w zasobniku jest zatrzymywana przed podmianą plików — inaczej trzymałaby
otwarty plik `.exe` i aktualizacja by się nie powiodła.

```powershell
.\uninstall-agent.ps1                # usuwa oba zadania i program, zachowuje poświadczenie
.\uninstall-agent.ps1 -RemoveData    # usuwa też poświadczenie i konfigurację
```

Deinstalator usuwa **oba** zadania — inwentaryzację i „CMDB Agent Monitor”.
Zostawienie drugiego oznaczałoby maszynę bez agenta, która nadal próbuje
sondować cele.

Deinstalacja agenta **nie usuwa maszyny z CMDB** — zasób zostaje w bazie
z datą ostatniego kontaktu i po `CMDB_STALE_AFTER_HOURS` pojawia się na
liście maszyn bez kontaktu. Jest to zamierzone: historia inwentarza nie
powinna znikać razem z agentem.

## Historia zbudowanych wersji

Domyślny build zostawia jeden program `agent/dist/cmdb-agent.exe`.
Opcja `-Archive` dodatkowo zapisuje kopię `history/cmdb-agent-0.5.10.exe`;
opcja `-Installer` tworzy osobny, opcjonalny pakiet instalacyjny.
Stare pliki z wcześniejszych buildów nie są automatycznie usuwane.
Build w CI nadaje wspólną wersję `0.6.<numer uruchomienia>+<numer próby>`
agentowi, instalatorowi i paczce Linux. Po testach wydanie z zatwierdzonej gałęzi
jest podpisywane, publikowane i importowane do katalogu CMDB.
[Jednorazowa konfiguracja automatycznych wydań](automatyczne-wydania.md).

## Jeden plik: agent i ikona w zasobniku

`cmdb-agent.exe` bez argumentów uruchamia wyłącznie ikonę tray — bez okna
statusu ani mignięcia konsoli. Polecenie `gui` działa tak samo; `run`, `enroll`,
`status` i `configure` wybierają odpowiedni tryb. Zadanie SYSTEM zawsze używa
`run`, natomiast autostart użytkownika używa `gui`. Druga ikona w tej samej
sesji i instalacji nie jest uruchamiana.

Ikona czyta publiczny status, bez dostępu do poświadczenia. Ustawienia
wymagają podniesienia uprawnień. Aby odczytać wynik CLI w PowerShell, użyj
potoku, np. `& .\cmdb-agent.exe status --json | Out-String`.

### Magazyn i aktualizacje

W trybie automatycznym podpisane wydania trafiają do katalogu bez uploadu;
administrator nadal wybiera wersję dla maszyn i firm w CMDB.
Stopka `entry_mode=unified` i podsystem PE nie są dowodem zaufania.

W starszym trybie ręcznym wgraj tylko `cmdb-agent.exe`, ale najpierw administrator wdrożenia musi
przypiąć SHA-256 przetestowanego pliku, jego wersję i architekturę w konfiguracji
serwera.
Bez przypięcia Windows nie otrzyma aktualizacji ani instalatora sieciowego;
dotyczy to również wydań wcześniej aktywowanych w bazie.
[Procedura dla wersji 0.5.10](zaufane-wydania-windows.md).
Instalacja ręczna setupem pozostaje osobną operacją administratora.

Okno pokazuje wersję bieżącego programu wyłącznie w nagłówku, niezależnie od
publicznego pliku statusu. Nie wyświetla historycznej wersji workera;
przeterminowany sukces jest oznaczany ostrzeżeniem. Wszystkie okna mają ikonę C
zamiast domyślnego pióra Tk. Informacja o skanerze jest widoczna wyłącznie
w ustawieniach otwartych z uprawnieniami administratora, tylko do odczytu.

Przy pierwszym przejściu z dwóch EXE uruchom instalator, który zmieni
autostart i zachowa stary tray jako `.legacy.bak`. Późniejsze aktualizacje
pojedynczego EXE aktualizują oba tryby: działająca ikona wykrywa podmianę
co 30 sekund i uruchamia się ponownie, nadal bez pokazywania okna.
Proces SYSTEM nie uruchamia interfejsu na pulpicie użytkownika.

## Podpisywanie

Niepodpisany plik wywołuje ostrzeżenie SmartScreen przy pierwszym uruchomieniu,
a część systemów EDR potrafi go zablokować. Skrypt budujący przyjmuje gotowy
certyfikat:

```powershell
.\build-agent.ps1 -SignCertThumbprint 1A2B3C... -Installer
```

Podpis jest znakowany czasem (domyślnie serwerem DigiCert), więc pozostaje
ważny także po wygaśnięciu certyfikatu.

### Skąd wziąć certyfikat

| Wariant | Koszt | Co daje |
|---|---|---|
| **Azure Trusted Signing** | ~10 USD/mies. | podpis zaufany publicznie, bez własnego tokenu sprzętowego |
| **Certyfikat OV** | ~200–400 USD/rok | zaufanie publiczne; reputacja SmartScreen buduje się stopniowo |
| **Certyfikat EV** | ~400–700 USD/rok | reputacja SmartScreen od razu, bez okresu docierania |
| **Własne PKI / self-signed** | 0 | działa **tylko** na maszynach, które ufają temu wystawcy |

Od czerwca 2023 klucz prywatny certyfikatu publicznie zaufanego musi leżeć na
sprzęcie spełniającym FIPS 140-2 Level 2 (token USB albo HSM w chmurze) —
plik `.pfx` na dysku już nie wystarczy. Azure Trusted Signing rozwiązuje to
za nas, bo klucz zostaje po stronie usługi; wymaga natomiast zweryfikowania
organizacji, a dla zaufania publicznego również udokumentowanego stażu firmy.

### Wariant wewnętrzny, bez kosztów

Dla floty w jednej organizacji wystarczy własny certyfikat rozgłoszony przez
GPO do magazynu **Zaufani wydawcy**. SmartScreen nadal ostrzeże przy pobraniu
z internetu, ale AppLocker, WDAC i większość systemów EDR przestaną blokować:

```powershell
# raz, na maszynie budującej
$c = New-SelfSignedCertificate -Type CodeSigningCert `
     -Subject "CN=CMDB Agent, O=Twoja Firma" `
     -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5)
$c.Thumbprint
Export-Certificate -Cert $c -FilePath cmdb-agent-ca.cer   # to rozgłaszasz przez GPO
```

Przy agencie pobieranym z **własnego serwera po HTTPS, ze sprawdzeniem
SHA-256**, podpis nie jest jedynym zabezpieczeniem — jest nim także to, że
plik pochodzi z uwierzytelnionego źródła i zgadza się co do bajtu.
