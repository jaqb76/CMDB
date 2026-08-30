# Agent 0.5.8: zasobnik i wykrywanie sieci

## Uruchamianie Windows

`cmdb-agent-tray.exe` uruchamia tylko ikonę, również na nieskonfigurowanym
komputerze. Status i konfigurację otwiera użytkownik z menu ikony; zamknięcie
statusu pozostawia ikonę. Worker `cmdb-agent.exe run` nadal służy do pracy
w tle jako SYSTEM. Awaryjne uruchomienie workera z menu używa SW_HIDE;
monit UAC przy operacji wymagającej administratora jest zachowany.

## Włączenie skanera

1. Na wybranym komputerze: ikona → **Ustawienia** (administrator).
2. Zaznacz **Włącz skanowanie sieci** tylko dla sieci, do których masz zgodę.
3. Pozostaw automatyczne wykrywanie lokalnych podsieci albo odznacz je i podaj
   zakresy, np. `192.168.10.0/24, 10.20.30.0/24`. Ręczne zakresy są dodawane do
   automatycznych, gdy obie opcje są włączone.
4. Zapisz. Zarejestrowany agent nie wymaga ponownego tokenu. Wybierz
   **Synchronizuj teraz**; kolejne skany wykonują się podczas raportowania,
   domyślnie nie częściej niż co 24 godziny.
5. W panelu CMDB otwórz **Wykrywanie sieci** (`/wykrywanie`).

Warto włączyć jeden skaner na lokalizację/podsieć, nie na wszystkich stacjach.
Automatyka czyta rzeczywiste prefiksy aktywnych interfejsów (również wirtualnych
oraz VPN). Nie odkrywa topologii całej firmy ani niedostępnych VLAN-ów. Ręcznie
podane sieci muszą mieć trasę i zgodę zapory z maszyny skanującej.

## Co jest rozpoznawane

- Aktywność TCP, adres IP, nazwa DNS PTR, MAC z lokalnej tablicy sąsiadów.
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

Przykładowy fragment `agent.conf`:

```json
{
  "discovery_enabled": true,
  "discovery_auto_subnets": false,
  "discovery_cidrs": ["192.168.10.0/24"],
  "discovery_interval_seconds": 86400,
  "discovery_max_hosts": 1024,
  "discovery_rate": 32,
  "discovery_budget_seconds": 300
}
```

Dozwolone są wyłącznie prywatne IPv4 RFC1918. IPv6 oraz publiczne zakresy nie
są skanowane. Domyślnie: do 1024 hostów, 8 pracowników, maks. 32 połączenia
TCP/s i 300 s na sondowanie; końcowy odczyt MAC może potrwać dodatkowo do 8 s.
Timeout pojedynczego połączenia wynosi 350 ms. Rozpoznawanie DNS jest osobnym,
przerywalnym procesem z limitem 2 s i respektuje budżet skanu.

Maksymalne konfigurowalne wartości: 4096 hostów, 128 połączeń/s, 900 s,
32 zakresy; odstęp 1 h–7 dni. Zbyt duży zakres jest odrzucany w całości,
a nie cicho przycinany. Przekroczenie czasu daje wynik częściowy z błędem.
Licznik postępu oznacza hosty, dla których wykonano cały zestaw prób TCP.
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
`discovery_devices` tworzy istniejące `init_db()`. Następnie zainstaluj agenta
0.5.8. Dotychczasowe binaria w repozytorium nie są automatycznie zmieniane.
Workflow testów buduje **CMDB-Agent-Windows-0.5.8** jako artefakt GitHub Actions:
worker, tray oraz `CMDB-Agent-Setup-0.5.8.exe`. Jest to build bez podpisu
Authenticode; wydanie produkcyjne należy podpisać firmowym certyfikatem.
Instalator aktualizuje oba pliki, podczas gdy dotychczasowa aktualizacja samego
workera nie zmienia działającej ikony tray. Zamknij starą ikonę przed instalacją.

Na własnym Windows: `agent/packaging/build-agent.ps1 -Installer`.
Instalator otrzymuje wersję z `cmdb_agent/__init__.py`, a nie stałą 0.1.0.
Testy używają atrap sieci; CI dodatkowo uruchamia zbudowany tray na Windows
bez konfiguracji i sprawdza, że proces pozostaje aktywny bez widocznego okna.
Nie wykonujemy testowego skanu sieci użytkownika ani sieci runnera.

Referencje poleceń odczytu Windows:
[Get-NetIPAddress](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netipaddress),
[Get-NetNeighbor](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netneighbor).
