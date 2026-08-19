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

## Szybki start (środowisko deweloperskie)

```bash
# 1. Serwer
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export CMDB_SECRET_KEY="$(python -m cmdb_server.cli gen-secret)"
python -m cmdb_server.cli init-db
python -m cmdb_server.cli tenant-create --name "Firma ABC" --slug abc
python -m cmdb_server.cli user-create --email admin@abc.pl --tenant abc --role admin
python -m cmdb_server.cli token-issue --tenant abc --name "stacje robocze"

uvicorn cmdb_server.main:app --reload          # http://127.0.0.1:8000
```

```bash
# 2. Agent (na maszynie do zinwentaryzowania)
cd agent
python -m cmdb_agent.main show                 # podgląd raportu, bez wysyłki
python -m cmdb_agent.main --server https://cmdb.firma.pl --token cmdb_ent_... enroll
python -m cmdb_agent.main run
```

W trybie deweloperskim serwer działa po HTTP. **Agent wysyła dane wyłącznie
po HTTPS** — do testów lokalnych użyj certyfikatu i parametru `--ca-bundle`
(opis w [`docs/wdrozenie.md`](docs/wdrozenie.md)).

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
| [`docs/wdrozenie.md`](docs/wdrozenie.md) | docker compose, TLS, kopie zapasowe, utrzymanie |

## Testy

```bash
cd server && pytest              # 28 testów: API agentów, izolacja firm, panel, gzip
cd agent  && pytest              # 42 testy: konfiguracja, stan, status, kolektory, transport

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
