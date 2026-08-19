# Agent na Windows

## Wymagania

* Windows 7 SP1 / Server 2008 R2 lub nowszy (PowerShell 5.1 jest w systemie
  od Windows 10 / Server 2016; na starszych wystarczy WMF 5.1).
* Uprawnienia administratora — część danych (konta, poprawki, numery seryjne
  dysków) jest niedostępna dla zwykłego użytkownika. Agent działa jako SYSTEM.
* Łączność HTTPS do serwera CMDB. Agent nie wymaga otwierania portów
  przychodzących — inicjuje połączenie sam.
* Python **nie jest wymagany** — agent i ikona w zasobniku to samodzielne pliki `.exe`.
* Ikona w zasobniku wymaga sesji graficznej. Na serwerach bez pulpitu instaluj
  z `-NoTray` — sam agent działa wtedy normalnie jako zadanie.

## Budowanie

Agent powstaje jako dwa samodzielne pliki `.exe` plus instalator:

```powershell
cd agent\packaging
.\build-agent.ps1 -Installer

# z podpisem kodu — bez niego SmartScreen i część systemów EDR blokują agenta:
.\build-agent.ps1 -Installer -SignCertThumbprint 1A2B3C…
```

| Plik | Rola |
|---|---|
| `cmdb-agent.exe` | agent zbierający dane, uruchamiany przez zadanie jako SYSTEM |
| `cmdb-agent-tray.exe` | ikona w zasobniku: status i ustawienia, działa jako zalogowany użytkownik |
| `CMDB-Agent-Setup-0.1.0.exe` | instalator graficzny |

Budowanie instalatora wymaga **Inno Setup 6** (`winget install JRSoftware.InnoSetup`).
Bez niego powstaną same pliki `.exe`, a instalacja przebiegnie skryptem
`install-agent.ps1`.

Buduj na najstarszej wersji Windows w flocie — plik zbudowany na Windows 11
działa na starszych, ale nie odwrotnie.

## Instalacja z kreatora

Uruchom `CMDB-Agent-Setup-0.1.0.exe` i podaj dane otrzymane od administratora:

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

**Intune / SCCM** — spakuj `cmdb-agent.exe`, `cmdb-agent-tray.exe`
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
| 3 | brak łączności | raport trafił do bufora, pójdzie przy następnym cyklu |
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

## Bufor offline

Gdy serwer jest nieosiągalny, raport trafia do
`%ProgramData%\CMDB\spool\` (do 12 najnowszych) i zostaje wysłany przy
najbliższym udanym połączeniu. Dzięki temu laptop pracujący tydzień poza
biurem nie zostawia dziury w historii inwentarza.

## Aktualizacja i deinstalacja

Aktualizacja: uruchom nowy `CMDB-Agent-Setup.exe` albo `install-agent.ps1`
z nowym `.exe`. Skrypt zatrzyma zadanie, podmieni pliki i zarejestruje maszynę
na nowo (poprzednie poświadczenie zostanie unieważnione po stronie serwera).
Ikona w zasobniku jest zatrzymywana przed podmianą plików — inaczej trzymałaby
otwarty plik `.exe` i aktualizacja by się nie powiodła.

```powershell
.\uninstall-agent.ps1                # usuwa zadanie i program, zachowuje poświadczenie
.\uninstall-agent.ps1 -RemoveData    # usuwa też poświadczenie i konfigurację
```

Deinstalacja agenta **nie usuwa maszyny z CMDB** — zasób zostaje w bazie
z datą ostatniego kontaktu i po `CMDB_STALE_AFTER_HOURS` pojawia się na
liście maszyn bez kontaktu. Jest to zamierzone: historia inwentarza nie
powinna znikać razem z agentem.
