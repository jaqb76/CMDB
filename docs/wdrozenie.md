# Wdrożenie

## Uruchomienie testowe (jedno polecenie)

Do pierwszego kontaktu i testów z agentem:

```bash
cd server
pip install -r requirements-dev.txt
python quickstart.py                 # --port 8443, --host 0.0.0.0
python quickstart.py --print-only    # tylko dane dostępowe, bez startu
```

Skrypt wystawia certyfikat self-signed (dla `localhost`, nazwy maszyny
i adresów lokalnych), zakłada bazę SQLite, firmę, konto panelu i token,
po czym startuje serwer z TLS i wypisuje dane do wklejenia w agencie.
Artefakty lądują w `server/.quickstart/` i są pomijane przez git — zawierają
klucz prywatny, klucz sesji i token.

Certyfikat generowany jest biblioteką `cryptography`, a nie poleceniem
`openssl` — na Windows zwykle go nie ma.

**To nie jest konfiguracja produkcyjna**: SQLite zamiast PostgreSQL,
certyfikat self-signed i konto z hasłem wpisanym w skrypcie. Do produkcji
użyj poniższego docker compose.

## Docker Compose (zalecane)

```bash
cd deploy
cp .env.example .env
```

Uzupełnij `.env`:

```bash
POSTGRES_PASSWORD=$(openssl rand -base64 32)
CMDB_SECRET_KEY=$(openssl rand -base64 48)
```

Umieść certyfikat w `deploy/certs/` jako `fullchain.pem` i `privkey.pem`,
po czym:

```bash
docker compose up -d
docker compose logs -f server
```

Usługi:

| Usługa | Rola | Widoczność |
|---|---|---|
| `proxy` | nginx, zakończenie TLS, przekierowanie z portu 80 | porty 80 i 443 |
| `server` | FastAPI + uvicorn (4 procesy) | tylko sieć kontenerów |
| `db` | PostgreSQL 16, wolumen `pgdata` | tylko sieć kontenerów |

Ani baza, ani serwer aplikacji nie są wystawione poza sieć kontenerów —
ruch z zewnątrz wchodzi wyłącznie przez nginx.

### Pierwsze uruchomienie

```bash
docker compose exec server python -m cmdb_server.cli tenant-create --name "Firma ABC" --slug abc
docker compose exec server python -m cmdb_server.cli user-create \
    --email admin@abc.pl --tenant abc --role admin
docker compose exec server python -m cmdb_server.cli token-issue \
    --tenant abc --name "stacje robocze" --days 365
```

Ostatnie polecenie wypisuje token rejestracyjny — **zapisz go od razu**,
w bazie zostaje wyłącznie skrót. Kolejne tokeny można wydawać w panelu.

## Zmienne konfiguracyjne serwera

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_ENV` | `dev` | `prod` włącza twarde wymagania konfiguracyjne |
| `CMDB_DATABASE_URL` | SQLite | `postgresql+psycopg://user:hasło@host/baza` |
| `CMDB_SECRET_KEY` | — | klucz podpisujący sesje, min. 32 znaki |
| `CMDB_REQUIRE_HTTPS` | `false` | wymuszenie HTTPS, HSTS, Secure na ciasteczku |
| `CMDB_SNAPSHOT_RETENTION` | `50` | ile snapshotów na maszynę (0 = bez limitu) |
| `CMDB_STALE_AFTER_HOURS` | `48` | po ilu godzinach maszyna jest „bez kontaktu” |
| `CMDB_REPORT_INTERVAL_SECONDS` | `3600` | odstęp narzucany agentom w odpowiedzi |
| `CMDB_MAX_REPORT_BYTES` | `8388608` | limit rozmiaru raportu |
| `CMDB_LOG_LEVEL` | `INFO` | poziom dziennika |

W trybie `prod` serwer **odmawia startu**, jeśli klucz sesji jest domyślny
lub za krótki, HTTPS nie jest wymuszony albo baza to SQLite. Błędna
konfiguracja nie przejdzie po cichu.

## TLS

**Let's Encrypt** — najprościej wystawić certbota obok nginx i podmontować
`/etc/letsencrypt` do `deploy/certs`. Nginx ma już przygotowaną lokalizację
`/.well-known/acme-challenge/`.

**Wewnętrzne PKI** — umieść `fullchain.pem` (certyfikat serwera + pośrednie)
i `privkey.pem` w `deploy/certs`. Stacje muszą ufać firmowemu CA albo
dostać go przez `-CaBundle` przy instalacji agenta.

Sprawdzenie po wdrożeniu:

```bash
curl -sS https://cmdb.firma.pl/api/v1/health
openssl s_client -connect cmdb.firma.pl:443 -servername cmdb.firma.pl </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -fingerprint -sha256
```

Ostatnie polecenie podaje też odcisk do ewentualnego przypięcia w agencie.

## Wdrożenie bez Dockera

```bash
useradd --system --create-home cmdb
cd /opt && git clone <repo> cmdb && cd cmdb/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# /etc/systemd/system/cmdb.service
[Unit]
Description=Serwer CMDB
After=network.target postgresql.service

[Service]
User=cmdb
WorkingDirectory=/opt/cmdb/server
EnvironmentFile=/etc/cmdb/cmdb.env
ExecStart=/opt/cmdb/server/.venv/bin/uvicorn cmdb_server.main:app \
          --host 127.0.0.1 --port 8000 --proxy-headers --workers 4
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Plik `/etc/cmdb/cmdb.env` (uprawnienia `0600`, właściciel `cmdb`) zawiera
zmienne z tabeli powyżej. Przed nim postaw nginx z konfiguracją
z `deploy/nginx/cmdb.conf` — **`X-Forwarded-Proto` jest niezbędny**, bez
niego serwer uzna połączenie za nieszyfrowane i odrzuci raporty.

## Skalowanie

Serwer jest bezstanowy — stan trzyma baza — więc skaluje się liczbą procesów
uvicorna i replik kontenera.

Szacunek obciążenia: raport to ok. 100–150 kB JSON-a, po gzipie 10–20 kB.
Tysiąc maszyn raportujących co 4 godziny to ok. 250 żądań na godzinę
i kilkanaście MB ruchu — obciążenie pomijalne.

Miejsce w bazie zależy od liczby **zmian**, nie raportów: dzięki
deduplikacji maszyna bez zmian nie tworzy nowych wierszy. Przy retencji 50
snapshotów górna granica to ok. 5–7 MB na maszynę.

Uwaga przy wielu replikach: throttling nieudanych uwierzytelnień jest
lokalny dla procesu. Przy kilku replikach przenieś licznik do Redis albo
ustaw limit na poziomie nginx (`limit_req`).

## Kopie zapasowe

```bash
# zrzut bazy
docker compose exec -T db pg_dump -U cmdb cmdb | gzip > cmdb-$(date +%F).sql.gz

# odtworzenie
gunzip -c cmdb-2026-08-19.sql.gz | docker compose exec -T db psql -U cmdb -d cmdb
```

Poza bazą warto zabezpieczyć `deploy/.env` (klucz sesji — jego utrata
wylogowuje wszystkich) oraz `deploy/certs`.

## Utrzymanie

| Czynność | Jak |
|---|---|
| Maszyny bez kontaktu | pulpit → kafelek „bez kontaktu”, lub `/assets?state=stale` |
| Maszyny bez opiekuna | pulpit → kafelek „bez przypisanego opiekuna” |
| Rotacja tokenu firmy | wydaj nowy, wdroż na stacjach, wycofaj stary (już zarejestrowani agenci działają dalej — mają własne poświadczenia) |
| Odcięcie maszyny | karta maszyny → Historia → Wycofaj poświadczenie (patrz uwaga w `bezpieczenstwo.md`) |
| Eksport inwentarza | `/export/assets.json` albo zapytania SQL z `architektura.md` |
| Przegląd zdarzeń | zakładka Audyt |

## Testy na silniku produkcyjnym

```bash
createdb cmdb_test
cd server && CMDB_DATABASE_URL=postgresql+psycopg://cmdb:...@localhost/cmdb_test pytest
```

Testy kasują cały schemat przed każdym przypadkiem, więc odmawiają startu
na bazie, której nazwa nie zawiera „test” (chyba że ustawisz
`CMDB_TEST_ALLOW_DESTRUCTIVE=1`). Pomyłkowe wskazanie produkcji nic nie zniszczy.

## Aktualizacja serwera

```bash
cd deploy
git pull
docker compose build server
docker compose up -d server
```

Schemat bazy tworzony jest przy starcie (`create_all`) — brakujące tabele
i indeksy powstają automatycznie, ale **zmiany istniejących kolumn nie są
migrowane**. Przed wdrożeniem wersji zmieniającej model zrób kopię bazy.
Docelowo warto wprowadzić Alembic.
