# Agent: jeden EXE, zasobnik i wykrywanie sieci

## Uruchamianie Windows

`cmdb-agent.exe` bez argumentów uruchamia tylko ikonę, również na nieskonfigurowanym
komputerze. Status i konfigurację otwiera użytkownik z menu ikony; zamknięcie
statusu pozostawia ikonę. Ten sam `cmdb-agent.exe run` służy do pracy
w tle jako SYSTEM. Awaryjne uruchomienie workera z menu używa SW_HIDE;
monit UAC przy operacji wymagającej administratora jest zachowany.

## Włączenie skanera

1. Zaloguj się jako administrator firmy w CMDB.
2. Otwórz kartę wybranego komputera → zakładka **Agent** → **Dodatkowe
   funkcjonalności** → **Skaner sieci** albo wybierz komputer na stronie
   **Wykrywanie sieci**. Dawny adres `/assets/<id>/discovery-policy` przekierowuje
   w to samo miejsce.
3. Włącz moduł wyłącznie dla sieci objętych zgodą administratora. Wybierz
   automatyczne podsieci i/lub zakresy CIDR, np. `192.168.10.0/24`.
4. Zapisz politykę. Agent od wersji 0.5.10 pobierze ją podczas następnego cyklu
   raportowania; zmieniona polityka inicjuje skan w tym cyklu, następne
   skany domyślnie nie częściej niż co 24 godziny. Do chwili pobrania zakładka
   **Agent** pokazuje „czeka na odebranie” — patrz
   [`funkcje-agenta.md`](funkcje-agenta.md).
5. Wyniki są na stronie **Wykrywanie sieci** (`/wykrywanie`).

Główne okno statusu nie pokazuje informacji o skanerze. Jego stan jest widoczny
wyłącznie w lokalnych ustawieniach otwartych z uprawnieniami administratora.
To podgląd: bez przełącznika, edytora zakresów ani przycisku uruchomienia skanu.
Zwykła synchronizacja inwentarza nie omija centralnej polityki ani harmonogramu.
Zapis polityki wymaga administratora firmy/superadministratora, CSRF i zgodnej
rewizji formularza; widzowie mogą ją tylko odczytać. Zmiany trafiają do audytu.

Przed każdym cyklem skanera agent pobiera świeżą politykę przez uwierzytelnione
HTTPS. Odpowiedź wiąże losowy nonce, maszynę i jej zasób; ważność wynosi 60 s.
Brak połączenia, błędna odpowiedź lub brak polityki blokują nowy skan.
Zapisana poprzednio zgoda nie jest używana awaryjnie. Inwentaryzacja pracuje dalej.
Wyłączenie blokuje następne skany, ale nie przerywa już rozpoczętego sondowania
(maksymalny budżet poniżej). Polityka nie obejmuje agenta starszego niż 0.5.10:
najpierw zaktualizuj wybrane komputery; starych lokalnych zgód nie importujemy.

Warto włączyć jeden skaner na lokalizację/podsieć, nie na wszystkich stacjach.
Automatyka czyta rzeczywiste prefiksy aktywnych interfejsów (również wirtualnych
oraz VPN). Nie odkrywa topologii całej firmy ani niedostępnych VLAN-ów. Ręcznie
podane sieci muszą mieć trasę i zgodę zapory z maszyny skanującej.

## Co jest rozpoznawane

- Aktywność TCP, adres IP, nazwa DNS PTR, MAC z lokalnej tablicy sąsiadów.
  Aktywne wpisy ARP (Reachable) pozwalają dostrzec lokalne urządzenia, które
  filtrują wszystkie badane porty TCP. Sam stary wpis ARP nie dowodzi aktywności.
- Porty TCP: 22, 80, 135, 139, 443, 445, 515, 631, 3389, 9100.
- Baner SSH oraz ograniczony odczyt strony głównej HTTP/HTTPS/IPP: nagłówek
  Server i tytuł strony. Mogą ujawnić producenta, model lub rodzinę OS;
  oryginalna obserwacja widoczna jest jako podstawa rozpoznania.
- Podpowiedź: komputer/serwer, drukarka, sprzęt sieciowy albo nierozpoznany;
  poziom pewności typu oraz ostrożna podpowiedź systemu.

Nie ma logowania, prób haseł, WMI/WinRM, wykonywania poleceń na innych hostach,
SNMP community guessing ani wysyłania danych na porty drukowania. Port 9100
jest jedynie otwierany i zamykany, bez wysłania zadania wydruku.

SMB może być Sambą, RDP może być xrdp, a SSH działa także na Windows.
Dokładnej wersji OS, RAM, CPU czy numeru seryjnego nie można zagwarantować
na podstawie takiego skanu. Brak odpowiedzi nie oznacza braku urządzenia;
wyłączone, izolowane lub filtrujące ruch urządzenia mogą być niewidoczne.
MAC zwykle nie jest dostępny przez router. DNS i banery są niezaufanymi
podpowiedziami. Odczyt banera HTTPS dopuszcza certyfikat własny urządzenia,
nie wysyła sekretów i nie podąża za przekierowaniami. Połączenie z CMDB
zachowuje pełną weryfikację TLS i przypięcie certyfikatu.

## Limity i konfiguracja

Limity ustawia administrator w formularzu polityki CMDB. Lokalne klucze
`discovery_*` w `agent.conf`, zmiennych środowiska i nadpisaniach są ignorowane.
Zapis z nowego GUI usuwa stare lokalne ustawienia skanowania.

Dozwolone są wyłącznie prywatne IPv4 RFC1918. IPv6 oraz publiczne zakresy nie
są skanowane. Domyślnie: do 1024 hostów, 8 pracowników, maks. 32 połączenia
TCP/s i 300 s na sondowanie; końcowy odczyt MAC może potrwać dodatkowo do 8 s.
Timeout pojedynczego połączenia wynosi 350 ms. Rozpoznawanie DNS jest osobnym,
przerywalnym procesem z limitem 2 s i respektuje budżet skanu.

Maksymalne konfigurowalne wartości: 4096 hostów, 128 połączeń/s, 900 s,
32 zakresy; odstęp 1 h–7 dni. Zbyt duży zakres jest odrzucany w całości,
a nie cicho przycinany. Przekroczenie czasu daje wynik częściowy z błędem.
Licznik postępu oznacza hosty, dla których wykonano cały zestaw prób TCP.
Wyniki wykrywania mają dodatkowy limit 1 MiB, aby zostawić miejsce na zwykły
raport inwentaryzacyjny. Nadmiar daje wynik częściowy z zaleceniem podziału zakresu.
Niepełne/nieudane skany są widoczne w panelu oraz błędach kolektorów raportu.

Wyniki jadą tym samym uwierzytelnionym raportem HTTPS co inwentaryzacja.
Przy braku łączności trafiają do istniejącego bufora agenta. Nie zmieniają
odcisku konfiguracji skanera ani nie powodują dodatkowych snapshotów tylko
z powodu kolejnego skanu. Wyłączenie modułu zatrzymuje następne skany;
nie usuwa wcześniejszych obserwacji ani już zbuforowanych raportów.

## Ewidencja i izolacja

Lista jest oddzielona od zasobów. Administrator zatwierdza nazwę i typ,
tworząc zasób ręczny, albo wskazuje ID istniejącego zasobu. Dopasowania IP/DNS
są tylko sugestiami. Agentowe dane istniejącego zasobu nigdy nie są nadpisywane.
Opiekuna i pozostałe dane uzupełnia się na karcie zasobu. Odczyt jest dostępny
również dla kont viewer; zapis wymaga prawa edycji i CSRF, trafia do audytu.

Klucz obserwacji to skaner + IP. Identyczne adresy w różnych lokalizacjach
lub od różnych skanerów nie są automatycznie scalane. Powtórzenie zatwierdzenia
nie tworzy kolejnego zasobu. Zmiana wykrytego MAC lub DNS usuwa powiązanie
obserwacji i wymaga ponownej weryfikacji (stary zasób pozostaje). Bez stabilnego
MAC/DNS nie da się wiarygodnie wykryć wszystkich zmian dzierżaw DHCP.
Brak urządzenia w kolejnym skanie nie usuwa zasobu ani nie odświeża daty jego
obserwacji. Starszy lub ponowiony raport nie cofa wyników.

## Aktualizacja i sprawdzenie

Najpierw zaktualizuj serwer: nowe tabele `discovery_scanners` i
`discovery_devices` oraz `discovery_policies` tworzy istniejące `init_db()`.
Następnie zainstaluj agenta 0.5.10 i ustaw politykę centralnie.
[Zaufanie do binariów i wymagany katalog SHA-256](zaufane-wydania-windows.md). Dotychczasowe binaria w repozytorium nie są automatycznie zmieniane.
Workflow testów buduje **CMDB-Agent-Windows-0.5.10** jako artefakt GitHub Actions:
jeden `cmdb-agent.exe` (worker + tray) oraz opcjonalny instalator
`CMDB-Agent-Setup-0.5.10.exe`. Jest to build bez podpisu
Authenticode; wydanie produkcyjne należy podpisać firmowym certyfikatem.
Przejście z dwóch plików na jeden wykonaj instalatorem: zmienia autostart,
zatrzymuje starą ikonę i zachowuje stary plik jako `.legacy.bak`. Kolejne
aktualizacje pojedynczego EXE są wykrywane przez tray co 30 s: ikona uruchamia
się ponownie w tej samej sesji użytkownika, nadal bez widocznego okna.
Zadanie SYSTEM używa `run`, nigdy nie uruchamia interfejsu graficznego.
Mutex zapobiega uruchomieniu dwóch ikon z tej samej instalacji w jednej sesji.

Na własnym Windows: `agent/packaging/build-agent.ps1` tworzy jeden plik programu.
`-Installer` dodaje opcjonalny instalator; `-Archive` dodaje kopię wersjonowaną
w podkatalogu `history`. Stare pliki z wcześniejszych buildów pozostają nietknięte;
użyj nowego `-OutputDir`, aby otrzymać czysty katalog wynikowy.
CLI zachowuje stdout/stderr i kody wyjścia przy przekierowaniu potoków.
W interaktywnym PowerShellu użyj `& .\cmdb-agent.exe --version | Out-String`,
aby zaczekać na program typu windowed i odebrać jego wynik.
Instalator otrzymuje wersję z `cmdb_agent/__init__.py`, a nie stałą 0.1.0.
Testy używają atrap sieci; CI dodatkowo sprawdza pojedynczy EXE na Windows:
CLI, błędy i kody wyjścia, pracę równoległą workera i tray, blokadę drugiej ikony,
cichy start oraz restart ikony po podmianie EXE.
Nie wykonujemy testowego skanu sieci użytkownika ani sieci runnera.

Referencje poleceń odczytu Windows:
[Get-NetIPAddress](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netipaddress),
[Get-NetNeighbor](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netneighbor).
