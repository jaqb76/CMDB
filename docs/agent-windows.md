# Agent na Windows

## Wymagania

* Windows 7 SP1 / Server 2008 R2 lub nowszy (PowerShell 5.1 jest w systemie
  od Windows 10 / Server 2016; na starszych wystarczy WMF 5.1).
* Uprawnienia administratora — część danych (konta, poprawki, numery seryjne
  dysków) jest niedostępna dla zwykłego użytkownika. Agent działa jako SYSTEM.
* Łączność HTTPS do serwera CMDB. Agent nie wymaga otwierania portów
  przychodzących — inicjuje połączenie sam.
* Python **nie jest wymagany** — zalecana postać to jeden plik `.exe`.

## Instalacja

```powershell
# 1. Zbuduj agenta (raz, na maszynie budującej)
.\agent\packaging\build-agent.ps1
# opcjonalnie z podpisem — bez niego SmartScreen i część EDR blokują uruchomienie:
.\agent\packaging\build-agent.ps1 -SignCertThumbprint 1A2B3C…

# 2. Zainstaluj na maszynie docelowej (PowerShell jako administrator)
.\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_… -IntervalHours 4
```

Instalator:

1. kopiuje `cmdb-agent.exe` do `%ProgramFiles%\CMDB Agent`,
2. zapisuje `%ProgramData%\CMDB\agent.conf` i zawęża dostęp do katalogu
   (`icacls`: tylko SYSTEM i Administratorzy — plik zawiera token),
3. rejestruje maszynę w serwerze (wymiana tokenu firmowego na własny),
4. tworzy zadanie **CMDB Agent** działające jako SYSTEM: przy starcie systemu
   (z 3-minutowym opóźnieniem) i co N godzin, z losowym rozrzutem do 10 minut,
5. wysyła pierwszy raport i wypisuje stan.

Parametry dodatkowe:

| Parametr | Zastosowanie |
|---|---|
| `-CaBundle C:\pki\firma-ca.pem` | serwer z certyfikatem wewnętrznego CA |
| `-PinSha256 <odcisk>` | przypięcie certyfikatu serwera |
| `-NoProcessList` | rezygnacja ze zbierania listy procesów |
| `-IntervalHours 8` | rzadsze raportowanie (domyślnie 4 h) |

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

**Intune / SCCM** — spakuj `cmdb-agent.exe` i `install-agent.ps1`.

* Polecenie instalacji: `powershell.exe -ExecutionPolicy Bypass -File install-agent.ps1 -ServerUrl … -Token …`
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

.\cmdb-agent.exe status                    # czy zarejestrowany, kiedy ostatni raport
.\cmdb-agent.exe show --out C:\temp\raport.json   # zbierz bez wysyłki i obejrzyj
.\cmdb-agent.exe run                       # jeden pełny cykl z komunikatami
.\cmdb-agent.exe --log-level DEBUG run     # czasy poszczególnych kroków

Get-Content "$env:ProgramData\CMDB\agent.log" -Tail 50
Get-ScheduledTaskInfo -TaskName "CMDB Agent"   # ostatni wynik zadania
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

Aktualizacja: uruchom `install-agent.ps1` ponownie z nowym `.exe`.
Skrypt zatrzyma zadanie, podmieni plik i zarejestruje maszynę na nowo
(poprzednie poświadczenie zostanie unieważnione po stronie serwera).

```powershell
.\uninstall-agent.ps1                # usuwa zadanie i program, zachowuje poświadczenie
.\uninstall-agent.ps1 -RemoveData    # usuwa też poświadczenie i konfigurację
```

Deinstalacja agenta **nie usuwa maszyny z CMDB** — zasób zostaje w bazie
z datą ostatniego kontaktu i po `CMDB_STALE_AFTER_HOURS` pojawia się na
liście maszyn bez kontaktu. Jest to zamierzone: historia inwentarza nie
powinna znikać razem z agentem.
