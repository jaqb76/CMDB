# Produkcja i development na jednym serwerze

Dwie instancje pod dwiema nazwami: `cmdb.newcenter.pl` i `cmdb-dev.newcenter.pl`.
Osobne bazy, osobne wolumeny, osobne wersje kodu — jeden nginx z przodu.

## Dlaczego jeden nginx

Porty 80 i 443 może trzymać tylko jeden proces. Stos produkcyjny uruchamia
`proxy` i to on jest jedynym wejściem z zewnątrz; instancja testowa nie ma
własnego nginx i nie publikuje portów — dołącza się do sieci wejściowej
produkcji i jest tam widoczna pod aliasem `cmdb-dev`.

```
nginx (stos produkcyjny, porty 80/443)
 ├── cmdb.newcenter.pl      → cmdb-prod:8000
 └── cmdb-dev.newcenter.pl  → cmdb-dev:8000
```

Alias jest stały i niezależny od nazwy projektu compose — inaczej konfiguracja
nginx zmieniałaby się razem z nazwą katalogu, w którym stoi instancja.

## Dwa katalogi, nie jeden

```
~/CMDB        → gałąź główna, produkcja
~/CMDB-dev    → gałąź robocza, development
```

Osobne katalogi, bo cały sens instancji testowej polega na tym, że stoi na innym
commicie. Jeden katalog z dwoma plikami `.env` tego nie da.

## Uruchomienie

**Produkcja** — bez zmian, tak jak do tej pory:

```bash
cd ~/CMDB && ./deploy/wdroz.sh
```

**Instancja testowa** — własny katalog, własny `.env`, własna nazwa projektu:

```bash
cd ~/CMDB-dev
git checkout <gałąź robocza>
CMDB_WERSJA="$(git log -1 --format='%h (%cs)')" \
  docker compose -p cmdb-dev -f deploy/docker-compose.dev.yml up -d --build
```

W `.env` instancji testowej ustaw co najmniej:

```
POSTGRES_PASSWORD=<inne niż na produkcji>
CMDB_SECRET_KEY=<inny niż na produkcji, chyba że chcesz odtwarzać jej kopie>
CMDB_PUBLIC_URL=https://cmdb-dev.newcenter.pl
CMDB_INSTANCJA=DEVELOPMENT
CMDB_INSTANCJA_BARWA=bursztyn
```

`CMDB_SECRET_KEY` to wybór: **ten sam** co na produkcji pozwala odtworzyć jej
kopię razem z działającą skrzynką (której i tak nie chcesz — patrz niżej),
**inny** izoluje instancje całkowicie, ale po odtworzeniu kopii hasła skrzynki
trzeba wpisać ponownie.

Na koniec domontuj drugi vhost do kontenera proxy produkcji — w
`deploy/docker-compose.yml`, w usłudze `proxy`:

```yaml
      - ./nginx/cmdb-dev.conf:/etc/nginx/conf.d/cmdb-dev.conf:ro
```

i `docker compose restart proxy`. W DNS dopisz `cmdb-dev` na ten sam adres.
Certyfikat: albo osobny z Let's Encrypt, albo jeden SAN na obie nazwy.

## Po czym poznać, że to nie produkcja

Pasek u góry **każdej** strony — łącznie z ekranem logowania i ekranami
administracji — plus prefiks w tytule karty przeglądarki: `[DEVELOPMENT]
Zgłoszenia — CMDB`. Właściwą kartę znajduje się po tytule, nie po kolorze,
więc prefiks jest tu ważniejszy od paska.

Sterują tym dwie zmienne: `CMDB_INSTANCJA` (dowolny tekst — obsłuży też
instalację zapasową i szkoleniową) i `CMDB_INSTANCJA_BARWA` (`bursztyn`,
`czerwony`, `fiolet`, `zielony`). Barwa jest nazwą z zamkniętej listy, a nie
dowolnym kolorem, bo trafia do CSS jako **klasa** — polityka bezpieczeństwa
aplikacji (`style-src 'self'`) nie przepuszcza stylów wstawianych w atrybut.
Literówka w nazwie barwy wywala serwer przy starcie, zamiast po cichu dawać
instancję bez oznaczenia.

**Przy `CMDB_ENV=dev` oznaczenie włącza się samo**, nawet gdy `CMDB_INSTANCJA`
zostanie puste. Zapomnieć można w obie strony, ale tylko jedna boli: instancja
testowa wyglądająca jak produkcja.

## Czego na devie nie wolno włączać

`docker-compose.dev.yml` ma to wpisane na sztywno i **tak ma zostać**:

- **`CMDB_MONITORING_ENABLED=false`** — inaczej dev wydawałby agentom drugą,
  konkurencyjną politykę monitorowania.
- **`CMDB_RELEASE_IMPORT_ENABLED=false`** — dev nie ma pobierać wydań.
- **skrzynka helpdesku wyłączona.** To najważniejsze. Dev z odtworzoną kopią
  produkcji ma te same hasła do skrzynki i tę samą pętlę IMAP: pobrałby maila
  klienta, oznaczył jako przeczytany, założył zgłoszenie u siebie — a produkcja
  tego maila **już by nie zobaczyła**. Zgłoszenie znika bez śladu. Do tego
  rozsyłałby potwierdzenia i odpowiedzi pod prawdziwe adresy.

Dlatego przywracanie kopii na instancji z `CMDB_ENV=dev` **samo** wyłącza
skrzynkę i czyści hasła IMAP/SMTP. Nie jest to wybór przy przywracaniu, bo wybór
da się przeoczyć raz — a jeden raz wystarczy.

## Dev stoi bez dodatkowej zapory

Decyzja świadoma: broni go samo logowanie, tak samo jak produkcji. Nazwa jest
w publicznym DNS, więc skanery ją znajdą. Skoro tak, na start warto dać devowi
okrojone dane, a pełną kopię produkcji odtwarzać tylko wtedy, gdy jest po co.

Gdyby to się kiedyś zmieniło, wystarczy kilka linijek w `cmdb-dev.conf`:
`auth_basic` z plikiem haseł albo `allow`/`deny` z listą adresów.
