# CMDB — inwentaryzacja maszyn z agentów

System zbiera dane o sprzęcie, zainstalowanym i uruchomionym oprogramowaniu
oraz kontach użytkowników z maszyn w firmie, przesyła je szyfrowanym
połączeniem na serwer, przechowuje w bazie o strukturze JSON i pokazuje
w panelu WWW. Każda maszyna może mieć przypisanego opiekuna.

System jest **wielofirmowy od podstaw** — każda firma widzi wyłącznie własne
maszyny, a agent posługuje się tokenem przypisanym do konkretnej firmy.

```
   maszyna Windows                 serwer CMDB                     panel WWW
  ┌──────────────────┐          ┌─────────────────────┐        ┌──────────────┐
  │ cmdb-agent.exe   │  HTTPS   │ FastAPI             │        │ lista maszyn │
  │ • sprzęt         │ ───────► │ • token → firma     │ ─────► │ karta maszyny│
  │ • oprogramowanie │  TLS 1.2+│ • snapshot JSONB    │        │ opiekunowie  │
  │ • użytkownicy    │  Bearer  │ • deduplikacja      │        │ tokeny       │
  └──────────────────┘          └──────────┬──────────┘        └──────────────┘
                                           │
                                   PostgreSQL (JSONB + GIN)
```

## Co system zbiera

| Obszar | Dane |
|---|---|
| Sprzęt | producent, model, numer seryjny, obudowa, CPU (model, rdzenie, taktowanie), moduły RAM (slot, pojemność, typ, s/n), dyski fizyczne i wolumeny, BIOS/UEFI, karty graficzne, wykrywanie wirtualizacji |
| System | nazwa, wersja, build, edycja, architektura, data instalacji, ostatni start, język, strefa czasowa |
| Sieć | interfejsy, MAC, adresy IP, bramy, serwery DNS, DHCP |
| Oprogramowanie | zainstalowane pakiety (nazwa, wersja, producent, data), usługi (stan, tryb startu, konto), zainstalowane poprawki, procesy w chwili raportu |
| Użytkownicy | konta lokalne (stan, blokada, ostatnie logowanie, SID), członkowie grupy administratorów, grupy wrażliwe, sesje |

Pełny raport trafia do bazy jako JSON — widoki w panelu są tylko jego
prezentacją, więc dołożenie nowego pola nie wymaga migracji bazy.

## Szybki start — jedno polecenie

```bash
cd server
pip install -r requirements-dev.txt
python quickstart.py
```

Skrypt wystawia certyfikat, zakłada bazę, firmę, konto panelu i token, po czym
startuje serwer i wypisuje gotowe dane do wklejenia w agencie:

```
  Adres serwera : https://localhost:8443
  Token         : cmdb_ent_…
  Certyfikat CA : …\server\.quickstart\server.crt

  PANEL WWW     : https://localhost:8443/
  login         : admin@moja-firma.pl
  haslo         : cmdb-haslo-testowe-2026
```

Agent wysyła dane **wyłącznie po HTTPS**, więc serwer startuje z TLS nawet
w trybie testowym. Certyfikat jest self-signed, dlatego agent musi dostać go
jawnie — wskaż wypisany plik w polu „Certyfikat CA" okna ustawień albo podaj
`--ca-bundle`. Uruchomienie jest idempotentne: kolejne starty używają
istniejącego certyfikatu i tokenu.

Domyślnie serwer nasłuchuje na `0.0.0.0`, więc agent z innej maszyny w sieci
lokalnej też się połączy (skrypt wypisuje adres LAN). Do produkcji użyj
[`deploy/docker-compose.yml`](deploy/docker-compose.yml) — PostgreSQL, nginx
i certyfikat publicznego urzędu.

<details>
<summary>Ręczna konfiguracja krok po kroku</summary>

```bash
cd server
export CMDB_SECRET_KEY="$(python -m cmdb_server.cli gen-secret)"
python -m cmdb_server.cli init-db
python -m cmdb_server.cli tenant-create --name "Firma ABC" --slug abc
python -m cmdb_server.cli user-create --email admin@abc.pl --tenant abc --role admin
python -m cmdb_server.cli token-issue --tenant abc --name "stacje robocze"
uvicorn cmdb_server.main:app --reload
```

```bash
cd agent
python -m cmdb_agent.main show                 # podgląd raportu, bez wysyłki
python -m cmdb_agent.main --server https://cmdb.firma.pl --token cmdb_ent_... enroll
python -m cmdb_agent.main run
```

</details>

## Wdrożenie produkcyjne

```bash
cd deploy
cp .env.example .env          # uzupełnij hasło bazy i CMDB_SECRET_KEY
# umieść fullchain.pem i privkey.pem w deploy/certs/
docker compose up -d
docker compose exec server python -m cmdb_server.cli tenant-create --name "Firma ABC" --slug abc
```

Szczegóły: [`docs/wdrozenie.md`](docs/wdrozenie.md).

## Instalacja agenta na Windows

```powershell
# na maszynie budującej (raz) — powstaje CMDB-Agent-Setup-0.1.0.exe
.\agent\packaging\build-agent.ps1 -Installer

# na maszynie docelowej — kreator pyta o adres serwera i token
CMDB-Agent-Setup-0.1.0.exe

# albo bez kreatora, np. przez GPO
.\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_... -Silent
```

Instalacja kopiuje pliki, zapisuje konfigurację z ograniczonymi uprawnieniami,
rejestruje maszynę i tworzy zadanie harmonogramu działające jako SYSTEM.

W zasobniku pojawia się ikona pokazująca kolorem stan agenta, a w oknie statusu
— **datę ostatniej poprawnej synchronizacji** (osobno od daty ostatniej próby,
żeby seria nieudanych prób nie wyglądała jak działający agent). Z tego samego
menu zmienia się adres serwera i token oraz wymusza synchronizację.

Ikona działa jako zwykły użytkownik i tylko czyta plik statusu — nigdy nie
sięga do poświadczenia agenta. Szczegóły i wdrożenie masowe (GPO, Intune, SCCM):
[`docs/agent-windows.md`](docs/agent-windows.md).

## Instalacja agenta na Linuksie (w tym Raspberry Pi)

Jedno polecenie na maszynie docelowej — bez klonowania repozytorium i bez
budowania czegokolwiek:

```bash
curl -fsSL https://cmdb.firma.pl/download/install.sh \
  | sudo bash -s -- --token cmdb_ent_...
```

Serwer wydaje źródła agenta po HTTPS (za tokenem firmowym), skrypt sprawdza ich
skrót SHA-256 i uruchamia instalator. Gotowe polecenie czeka w panelu, w
zakładce **Instalacja agenta**.

Skrypt zakłada usługę i timer systemd (agent jest jednorazowy, nie rezydentny),
rejestruje maszynę i wysyła pierwszy raport. Na Raspberry Pi dane o modelu i
numerze seryjnym pochodzą z `/proc/device-tree` i `/proc/cpuinfo`, bo DMI tam
nie istnieje.

Wydanie agenta opisane jest parą **system + architektura**, a architekturę
serwer odczytuje z nagłówka pliku — dzięki temu build dla x86-64 nigdy nie
trafi na ARM. Szczegóły: [`docs/agent-linux.md`](docs/agent-linux.md).

## Wielofirmowość i tokeny

Rejestracja agenta jest dwustopniowa:

1. Firma dostaje **token rejestracyjny** (`cmdb_ent_...`) — wspólny dla firmy,
   używany wyłącznie przy pierwszym uruchomieniu agenta.
2. Serwer wymienia go na **poświadczenie maszyny** (`cmdb_agt_...`), którym
   agent posługuje się przy każdym kolejnym raporcie.

Dzięki temu wyciek z jednej stacji nie kompromituje całej firmy, a pojedynczą
maszynę można odciąć bez wymiany tokenu u pozostałych. W bazie przechowywane
są wyłącznie skróty SHA-256 — wartość jawna pokazywana jest raz, przy wydaniu.

Przynależność do firmy wynika **zawsze z tokenu**, nigdy z treści żądania —
agent nie jest w stanie zaraportować maszyny do cudzej firmy.

## Dokumentacja

| Dokument | Zawartość |
|---|---|
| [`docs/architektura.md`](docs/architektura.md) | model danych, przepływ raportu, deduplikacja, dodawanie kolejnych systemów |
| [`docs/bezpieczenstwo.md`](docs/bezpieczenstwo.md) | model zagrożeń, tokeny, TLS, izolacja firm, dane wrażliwe |
| [`docs/agent-windows.md`](docs/agent-windows.md) | co i jak agent zbiera, instalacja, wdrożenie masowe, diagnostyka |
| [`docs/agent-linux.md`](docs/agent-linux.md) | instalacja na Ubuntu/Raspberry Pi, systemd, architektury procesorów |
| [`docs/aktualizacje.md`](docs/aktualizacje.md) | brakujące poprawki, źródła danych per system, czego to nie jest |
| [`docs/wdrozenie.md`](docs/wdrozenie.md) | docker compose, TLS, kopie zapasowe, utrzymanie |

## Testy

```bash
cd server && pytest              # 28 testów: API agentów, izolacja firm, panel, gzip
cd agent  && pytest              # 55 testow: konfiguracja, stan, status, diagnostyka, kolektory

# Testy kasują schemat przed każdym przypadkiem, więc odmawiają startu na bazie
# bez "test" w nazwie — przypadkowe wskazanie produkcji nic nie zniszczy.

# ten sam zestaw na silniku produkcyjnym (PostgreSQL/JSONB)
CMDB_DATABASE_URL=postgresql+psycopg://cmdb:...@localhost/cmdb_test pytest
```

## Struktura repozytorium

```
server/     serwer FastAPI: API agentów, panel WWW, model danych, CLI administracyjne
agent/      agent (biblioteka standardowa Pythona), kolektory per system, skrypty instalacyjne
deploy/     docker compose, obraz serwera, konfiguracja nginx z TLS
docs/       dokumentacja
```

## Stan i dalsze kroki

Zaimplementowane i przetestowane end-to-end: agent → HTTPS → serwer →
PostgreSQL/JSONB → panel. Kolektor Windows jest kompletny; kolektor Linux
działa i służy jako wzorzec dla kolejnych systemów.

Naturalne kolejne kroki: migracje Alembic zamiast `create_all`, porównywanie
snapshotów w panelu (co dokładnie się zmieniło), powiadomienia o maszynach bez
kontaktu, import opiekunów z Active Directory, kolektor macOS.
