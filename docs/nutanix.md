# Nutanix Prism Central

CMDB odczytuje z Nutanix Prism Central klastry, hosty i maszyny wirtualne
i wpisuje je do ewidencji razem z zależnościami:

```
klaster  ← host_cluster ─  host  ← vm_host ─  maszyna wirtualna
```

## Kto się z kim łączy

Z Prism łączy się **agent** na wybranej maszynie, nie serwer CMDB. Serwer nie
musi mieć trasy do Prism, a zasada „połączenia zawsze od agenta do serwera”
zostaje nienaruszona:

1. agent co 5 minut pobiera z CMDB konfigurację (adres, konto, hasło),
2. co ustawiony odstęp czyta Prism Central przez **API v4** (tylko `GET`),
3. odsyła do CMDB spłaszczony wynik, a serwer wpisuje go do ewidencji.

Odczyt działa w usłudze monitorowania agenta (`cmdb-agent-monitor` na
Linuksie, zadanie „CMDB Agent Monitor” na Windows). Ta usługa działa bez
przerwy, dlatego zlecony z panelu test połączenia wraca w ciągu kilku minut.

Wymagania:

- Prism Central **pc.2024.3 lub nowszy** (API v4: `clustermgmt/v4.0`, `vmm/v4.0`),
- konto w Prism z rolą **Viewer** — odczyt niczego w klastrze nie zmienia,
- maszyna z agentem, która widzi `https://<prism>:9440`,
- certyfikat Prism zaufany na tej maszynie (firmowe CA dodane do systemu)
  albo wklejony w konfiguracji. **Weryfikacji certyfikatu nie da się wyłączyć.**

Sprawdzenie z maszyny, na której ma działać odczyt:

```bash
curl -s https://prism.firma.pl:9440/ -o /dev/null -w '%{http_code}\n'
```

Dowolny kod HTTP oznacza, że połączenie działa. Błąd certyfikatu oznacza,
że trzeba dodać CA — patrz „Instalacja agenta” w `wdrozenie.md`.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **Nutanix Prism Central**:

1. adres (`prism.firma.pl` wystarczy, port 9440 dopisze się sam),
2. użytkownik i hasło konta Viewer,
3. **Zapisz**, potem **Testuj połączenie**. Wynik testu pojawi się w tym
   samym miejscu po najbliższym sprawdzeniu konfiguracji przez agenta,
4. zaznacz **Odczytuj Prism Central z tej maszyny** i zapisz.

Test działa także przy wyłączonym odczycie, więc ustawienia można sprawdzić,
zanim cokolwiek trafi do ewidencji.

## Co trafia do ewidencji

| Z Prism | W CMDB |
|---|---|
| klaster (bez samego Prism Central) | zasób rodzaju „Klaster”: wersja AOS, hypervisor, liczba hostów |
| host | zasób rodzaju „Host wirtualizacji”: model, numer seryjny, CPU, RAM, AHV, IP, IPMI + relacja do klastra |
| maszyna wirtualna | zasób rodzaju „Maszyna wirtualna”: stan, vCPU, RAM, dyski, karty sieciowe, NGT, system wg NGT + relacja do hosta |

Wpisy z odczytu mają źródło **„nutanix”**: nie są wpisami ręcznymi i nie
podlegają ocenie „bez kontaktu”. Opiekuna, lokalizację, uwagi i dane zakupowe
można im nadać jak każdemu innemu wpisowi — kolejny odczyt tych pól nie rusza.

Wszystko widać w menu **Wirtualizacja**: drzewo klaster → host → VM z kolumną
„Agent CMDB”, która pokazuje maszyny wirtualne bez inwentaryzacji
oprogramowania. Karta każdej maszyny ma sekcję **Wirtualizacja**.

## Mapa relacji

**Relacje i zależności → Pokaż na mapie** (albo „Pokaż na mapie relacji” na karcie
zasobu). Są dwa widoki tych samych danych:

- **Widok: grupy** (domyślny) pokazuje całą infrastrukturę naraz. Każdy host to
  zwarta grupa: blok hosta, a pod nim jego maszyny w kolumnach. Grupy układają
  się w rzędy pod klastrem, a szerokość rzędów dobiera się do proporcji okna
  i przelicza przy każdej zmianie rozmiaru. Linie prowadzą jak w schemacie
  organizacyjnym, wyłącznie wewnątrz grupy, więc **nie przecinają się**.
  Kolor paska bloku to stan maszyny: z agentem CMDB, bez agenta, wyłączona
  (według Nutanixa) albo agent milczy (dłużej niż próg „bez kontaktu” firmy).
  Z daleka widać same kolory, a po przybliżeniu nazwy. Kółko myszy przybliża,
  przeciąganie przesuwa widok, kliknięcie hosta najeżdża na jego grupę i liczy
  zasięg awarii, dwuklik zwija hosta. Wyszukiwarka i filtry stanu działają na
  całej mapie. Aplikacje są domyślnie ukryte, bo jedna aplikacja na kilku
  hostach to jedyne źródło przecięć linii.
- **Widok: ścieżka** pokazuje jeden zasób w kolumnach klaster → host → VM/serwer
  → aplikacja, z przełącznikiem kierunku: co na nim stoi (↓), na czym stoi (↑)
  albo jedno i drugie.

Hosty bez relacji do klastra trafiają do grupy „Bez klastra”, a maszyny bez
hosta do grupy „Bez hosta”. Adres zawiera wybrany zasób, więc widok da się
komuś wysłać. Mapa tylko pokazuje: relacje dodaje się i usuwa w tabeli.

## Maszyna wirtualna z agentem: jedna karta, nie dwie

Na AHV identyfikator VM w Prism jest tym samym UUID, który agent odczytuje
z BIOS-u maszyny (`hardware.system.uuid`, zwykle także jako numer seryjny).
Po nim odczyt łączy VM z kartą agenta: dane z hypervisora dopisują się do
niej, zamiast zakładać drugi wpis. Dopasowanie uwzględnia też odwróconą
kolejność bajtów w trzech pierwszych polach UUID, bo tak odczytują go
niektóre systemy.

Jeśli agenta zainstalowano na maszynie, która była już w ewidencji z samego
odczytu, dawny wpis zostaje **wycofany** z adnotacją „połączona z maszyną
z agentem: …”, a nie skasowany, bo mógł już mieć opiekuna albo uwagi.

## Zmiany i znikanie

| Sytuacja | Co robi CMDB |
|---|---|
| VM przeniesiona na inny host | przepina relację `vm_host`, wpis w historii zmian karty |
| VM usunięta z Prism | wpis „nutanix” wycofany z powodem „zniknęła z Prism Central”; historia zostaje |
| VM wróciła | wycofanie cofnięte — ale tylko to, które zrobił odczyt, nie decyzja człowieka |
| maszyna z agentem zniknęła z Prism | tylko zdjęte relacje; maszyna z agentem nigdy nie jest wycofywana przez odczyt |
| nieudany odczyt (hasło, sieć, certyfikat) | **nic** nie jest wycofywane; błąd widać w podzakładce i na stronie Wirtualizacja |
| odczyt zrobiony na starej konfiguracji | pomijany — np. po zmianie adresu Prism stary wynik niczego nie wycofa |

Znikanie ocenia tylko ten agent, który obiekt wcześniej widział. Drugi agent
czytający inny Prism nie wycofa cudzych maszyn tylko dlatego, że ich nie widzi.

## Bezpieczeństwo

- Hasło jest w bazie zaszyfrowane (tym samym mechanizmem co hasła skrzynek
  pocztowych, klucz z `CMDB_SECRET_KEY`), nie ma go w audycie ani w dzienniku.
- Dostaje je wyłącznie agent, któremu włączono funkcję, w odpowiedzi ważnej
  60 sekund i związanej z jednorazową wartością (`Cache-Control: no-store`).
- Agent trzyma hasło tylko w pamięci, na czas jednego odczytu.
- Agent nie podąża za przekierowaniami HTTP z Prism — nagłówek z hasłem nie
  może trafić na inny host.
- Zmiana konfiguracji i zlecenie testu są audytowane (`nutanix.config_changed`,
  `nutanix.test_requested`).

## Diagnostyka na maszynie

```bash
cmdb-agent status          # wiersze "odczyt Nutanix" i "blad odczytu Nutanix"
journalctl -u cmdb-agent-monitor -n 50 | grep -i nutanix
```

| Komunikat | Znaczenie |
|---|---|
| `HTTP 401` | zły użytkownik lub hasło |
| `HTTP 403` | konto bez uprawnień — nadaj rolę Viewer |
| `HTTP 404` | to nie jest Prism Central z API v4 (np. Prism Element albo starsza wersja) |
| `certyfikat Prism nie jest zaufany` | dodaj firmowe CA do systemu albo wklej je w konfiguracji |
| `brak połączenia` | firewall / DNS / zły port |
