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
i adresów lokalnych), zakłada schemat w PostgreSQL, firmę, konto panelu
i token, po czym startuje serwer z TLS i wypisuje dane do wklejenia w agencie.

Wymaga działającego PostgreSQL — to jedyny wspierany silnik, także lokalnie.
Domyślnie łączy się z `postgresql://cmdb:cmdb@localhost:5432/cmdb_dev`;
własną bazę wskaż zmienną `CMDB_DATABASE_URL`. Jeśli bazy nie ma:

```bash
psql -U postgres -c "CREATE ROLE cmdb LOGIN PASSWORD 'cmdb'"
psql -U postgres -c "CREATE DATABASE cmdb_dev OWNER cmdb"
```
Artefakty lądują w `server/.quickstart/` i są pomijane przez git — zawierają
klucz prywatny, klucz sesji i token.

Certyfikat generowany jest biblioteką `cryptography`, a nie poleceniem
`openssl` — na Windows zwykle go nie ma.

**To nie jest konfiguracja produkcyjna**: certyfikat self-signed, konto
z hasłem wpisanym w skrypcie i baza bez kopii zapasowych. Do produkcji
użyj poniższego docker compose.

## Docker Compose (zalecane)

```bash
cd deploy
cp .env.example .env
```

Uzupełnij `.env` — trzy pierwsze wartości są **wymagane**, bez nich
`docker compose up` przerwie start:

```bash
POSTGRES_PASSWORD=$(openssl rand -base64 32)
CMDB_SECRET_KEY=$(openssl rand -base64 48)
CMDB_PUBLIC_URL=https://cmdb.twoja-firma.pl
```

`CMDB_PUBLIC_URL` trafia do skryptu instalacyjnego wydawanego agentom.
Za nginx serwer widzi adres kontenera, więc nie da się go odgadnąć z żądania —
zły adres oznacza instalację wskazującą w nicość.

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

Dwa wolumeny trwałe: `pgdata` (baza) i `releases` (wgrane wersje agenta).
Bez tego drugiego wersje znikałyby przy każdym odtworzeniu kontenera,
a wskazania wersji aktywnej zostawałyby bez plików.

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

Potrzebne jest jeszcze konto **superadmina** — to ono zarządza wersjami
agentów, danymi o podatnościach i wszystkimi firmami naraz:

```bash
docker compose exec server python -m cmdb_server.cli user-create     --email admin@twoja-firma.pl --superadmin
```

Hasło zostanie zapytane interaktywnie.

### Instalacja agentów

Serwer sam wydaje agenta po HTTPS — na maszynach nie trzeba niczego klonować
ani kopiować:

```bash
# Linux (Ubuntu, Debian, Raspberry Pi) - na maszynie docelowej
curl -fsSL https://cmdb.twoja-firma.pl/download/install.sh   | sudo bash -s -- --token cmdb_ent_...
```

Gotowe polecenia, wraz z wersją Windows, czekają w panelu w zakładce
**Instalacja agenta**. Paczka źródeł dla Linuksa jest budowana przy tworzeniu
obrazu, więc działa od pierwszego uruchomienia.

### Dostęp wychodzący

Serwer potrzebuje wychodzącego HTTPS do pobierania danych o podatnościach
(Debian Security Tracker, baza USN Ubuntu — łącznie ~130 MB przy odświeżeniu).
Bez niego wszystko inne działa, a strona podatności pokazuje stan „nieznany”.

Drugim wyjściem na zewnątrz jest **monitorowanie usług**: serwer nawiązuje
połączenie z każdym skonfigurowanym celem, więc musi go widzieć. Usługa
w segmencie sieci niedostępnym dla serwera będzie raportowana jako
niedostępna — i będzie to prawda z jego punktu widzenia, choć nie z punktu
widzenia jej użytkowników. Lista adresów, pod które serwer świadomie nie
pójdzie, jest w [`monitorowanie-uslug.md`](monitorowanie-uslug.md).

Agenci **nigdy** nie są odpytywani przez serwer — ta łączność jest zawsze
jednostronna, od agenta do serwera.

## Zmienne konfiguracyjne serwera

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_ENV` | `dev` | `prod` włącza twarde wymagania konfiguracyjne |
| `CMDB_DATABASE_URL` | `postgresql+psycopg://cmdb:cmdb@localhost:5432/cmdb` | wyłącznie PostgreSQL — inny silnik serwer odrzuca przy starcie |
| `CMDB_SECRET_KEY` | — | klucz podpisujący sesje, min. 32 znaki |
| `CMDB_REQUIRE_HTTPS` | `false` | wymuszenie HTTPS, HSTS, Secure na ciasteczku |
| `CMDB_SNAPSHOT_RETENTION` | `50` | ile snapshotów na maszynę (0 = bez limitu) |
| `CMDB_STALE_AFTER_HOURS` | `48` | po ilu godzinach maszyna jest „bez kontaktu” |
| `CMDB_REPORT_INTERVAL_SECONDS` | `3600` | odstęp narzucany agentom w odpowiedzi |
| `CMDB_MAX_REPORT_BYTES` | `8388608` | limit rozmiaru raportu |
| `CMDB_MONITORING_ENABLED` | `true` | monitorowanie usług i certyfikatów |
| `CMDB_MONITORING_TICK_SECONDS` | `60` | jak często budzi się pętla sprawdzeń |
| `CMDB_MONITORING_WORKERS` | `8` | ile sprawdzeń naraz |
| `CMDB_MONITORING_MAX_TARGETS` | `200` | limit monitorowanych usług na firmę |
| `CMDB_MONITORING_HISTORY_DAYS` | `30` | retencja pomiarów dostępności |
| `CMDB_MONITORING_ALLOW_LOOPBACK` | `false` | zezwolenie na cele pod adresem pętli zwrotnej |
| `CMDB_LOG_LEVEL` | `INFO` | poziom dziennika |

W trybie `prod` serwer **odmawia startu**, jeśli klucz sesji jest domyślny
lub za krótki albo HTTPS nie jest wymuszony. Adres bazy sprawdzany jest
w każdym trybie: kod używa `JSONB`, blokad doradczych i indeksów GIN, więc na
innym silniku nie działa gorzej, tylko nie działa wcale — lepiej powiedzieć
to przy starcie niż w połowie pracy.

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

Zadania w tle — wysyłka raportów i monitorowanie usług — uzgadniają się
**blokadą doradczą PostgreSQL**, więc kolejne repliki nie powielają sprawdzeń
ani alarmów: proces, który nie dostanie blokady, pomija ten obieg. Nie wymaga
to żadnej dodatkowej konfiguracji, ale znaczy też, że monitorowanie idzie
z **jednej** repliki naraz — przy kilku tysiącach celów zwiększ
`CMDB_MONITORING_WORKERS`, a nie liczbę replik.

## Kopie zapasowe

```bash
# zrzut bazy
docker compose exec -T db pg_dump -U cmdb cmdb | gzip > cmdb-$(date +%F).sql.gz

# odtworzenie
gunzip -c cmdb-2026-08-19.sql.gz | docker compose exec -T db psql -U cmdb -d cmdb
```

```bash
# wgrane wersje agenta (wolumen releases)
docker run --rm -v deploy_releases:/dane -v "$PWD:/kopia" alpine     tar czf /kopia/releases-$(date +%F).tar.gz -C /dane .
```

Poza bazą warto zabezpieczyć `deploy/.env` (klucz sesji — jego utrata
wylogowuje wszystkich), `deploy/certs` oraz wolumen `releases`. Wpisów
o podatnościach kopiować nie trzeba — odtwarza je odświeżenie kanałów.

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

## Limity rozmiaru żądań

nginx stosuje **12 MB** do całego ruchu — raport agenta to kilkaset kB tekstu
wysyłanego gzipem, więc zapas jest spory.

Wyjątkiem jest `/admin/releases`, gdzie limit wynosi **160 MB**: plik agenta
spakowany PyInstallerem ma ~8 MB, a wariant z ikoną w zasobniku ponad 29 MB.
Bez tego wyjątku wgranie ikony kończyło się kodem **413**, zanim żądanie
w ogóle doszło do aplikacji.

Limit jest podniesiony wyłącznie tam. Podniesienie go globalnie pozwoliłoby
wysłać setki megabajtów na dowolny inny adres — aplikacja i tak odrzuciłaby
takie żądanie, ale dopiero po odebraniu go w całości.

Odpowiednikiem po stronie aplikacji są `CMDB_MAX_REPORT_BYTES` (8 MB) oraz
`CMDB_MAX_RELEASE_BYTES` (128 MB). Jeśli podnosisz jeden, podnieś i drugi —
niższy z nich decyduje.

### Zmiana konfiguracji nginx wymaga przeładowania

Plik `nginx/cmdb.conf` jest podmontowany jako wolumen, więc zmiana na dysku
jest widoczna w kontenerze natychmiast — ale **nginx trzyma konfigurację
w pamięci**. `docker compose up -d --build` przebudowuje obraz serwera
i odtwarza jego kontener; kontener `proxy` pozostaje nietknięty, bo nic się
w nim nie zmieniło.

```bash
docker compose exec proxy nginx -t          # najpierw sprawdź składnię
docker compose exec proxy nginx -s reload   # potem przeładuj, bez przerwy w działaniu
```

Gdy chcesz mieć pewność, że kontener widzi nową treść:

```bash
docker compose exec proxy grep -A2 "location /admin/releases" /etc/nginx/conf.d/default.conf
```

Pusty wynik oznacza, że na serwerze nie ma jeszcze aktualnego repozytorium —
wtedy najpierw `git pull`.
