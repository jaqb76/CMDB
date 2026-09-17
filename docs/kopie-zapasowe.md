# Kopie zapasowe i przywracanie

## Co jest stanem instalacji

Baza to nie wszystko. Instalacja to cztery rzeczy, z których trzy nie leżą
w Postgresie:

| Co | Gdzie | W archiwum? |
|---|---|---|
| Baza | wolumen `pgdata` | tak (`baza.dump`) |
| Wgrane wersje agenta | wolumen `releases` | tak |
| Załączniki zgłoszeń | wolumen `helpdesk` | tak |
| `.env` i `deploy/certs/` | katalog repozytorium | **nie** — celowo |

**`.env` nie jest w archiwum i to jest decyzja, nie przeoczenie.** Kluczem
`CMDB_SECRET_KEY` odszyfrowuje się hasła skrzynki helpdesku zapisane w bazie,
więc klucz w tym samym pliku co zrzut zamieniłby kopię zapasową w komplet do
odczytania wszystkiego. Przy przenosinach na inny serwer skopiuj klucz osobnym
kanałem.

Po odtworzeniu na serwerze z **innym** kluczem baza wstanie bez jednego błędu,
a skrzynka po prostu zamilknie. Dlatego przy starcie sprawdzamy, czy da się
odczytać zapisane hasło, i mówimy wprost, gdy się nie da.

## Kolejność w środku archiwum

Najpierw baza, potem pliki — i to nie jest dowolne. Załącznik wgrany pomiędzy
jednym a drugim krokiem zostaje sierotą bez wiersza w bazie, co nikomu nie
przeszkadza. W odwrotnej kolejności mielibyśmy wiersz bez pliku, czyli zepsuty
link w wątku zgłoszenia.

## Codziennie o 3:00

Wpis w cronie **na hoście** (nie w kontenerze — kontener nie ma crona):

```cron
0 3 * * * cd /home/user/CMDB && docker compose -f deploy/docker-compose.yml \
  exec -T server python -m cmdb_server.cli kopia --nocna >> /var/log/cmdb-kopie.log 2>&1
```

`--nocna` znaczy: w niedzielę kopia tygodniowa, w pozostałe dni dzienna, a po
wszystkim rotacja. Trzymamy **7 dziennych i 4 tygodniowe** — razem miesiąc
wstecz bez trzymania trzydziestu plików. Limity zmieniasz w `.env`
(`CMDB_KOPIE_DZIENNYCH`, `CMDB_KOPIE_TYGODNIOWYCH`).

Kopie **ręczne nie rotują się nigdy**. Skoro ktoś zrobił ją świadomie przed
ryzykowną zmianą, nocne zadanie nie ma prawa jej sprzątnąć.

Przed każdą kopią sprawdzamy wolne miejsce. Mniej niż dwa ostatnie zrzuty —
kopia się nie zaczyna i zgłasza dlaczego, zamiast zapchać dysk pod działającą
bazą.

## Z wiersza poleceń

```bash
docker compose exec server python -m cmdb_server.cli kopia          # ręczna
docker compose exec server python -m cmdb_server.cli kopia --nocna  # jak cron
docker compose exec server python -m cmdb_server.cli kopie          # lista
docker compose exec server python -m cmdb_server.cli przywroc <plik>
```

## Z panelu

**Administracja → Kopie zapasowe**, wyłącznie dla superadmina: archiwum zawiera
dane wszystkich firm naraz.

- **Zrób kopię teraz** — ta sama funkcja, którą woła cron.
- **Pobierz** — plik idzie strumieniem do przeglądarki. Pobranie kopii to
  wyniesienie całej bazy poza serwer, więc audytuje się tak samo jak jej
  przywrócenie.
- **Sprawdź** — odtwarza kopię do bazy pomocniczej, liczy firmy, maszyny
  i zgłoszenia, po czym bazę kasuje. Działającej instalacji nie rusza i nie
  podmienia załączników. Kopia, której nikt nigdy nie odtworzył, to nie kopia,
  tylko plik.

## Przywracanie wymaga restartu

To nie jest ograniczenie do obejścia, tylko warunek wykonalności. Uvicorn chodzi
w czterech workerach, każdy trzyma połączenia do bazy, a w tle kręcą się pętla
IMAP, harmonogram i import wydań. `pg_restore --clean` musi usunąć tabele, które
te sesje trzymają otwarte — zablokuje się albo urwie w połowie. Podmiana przez
zmianę nazwy bazy też nie przejdzie: nie da się przemianować bazy, do której
ktoś jest podłączony, a podłączeni jesteśmy my.

Dlatego droga jest trzyetapowa:

1. **Panel uzbraja.** Sprawdza manifest archiwum, odkłada je na wolumen kopii
   i zapisuje znacznik `do-odtworzenia.json`. Bazy nie rusza.
2. **Człowiek restartuje**: `docker compose restart server`.
3. **Wejście kontenera odtwarza** (`cmdb_server.wejscie`) — przed startem
   uvicorna, gdy żadne połączenie aplikacji jeszcze nie istnieje.

Nieudane odtworzenie **zostawia znacznik**: powtórzy się przy kolejnym starcie
albo zostanie odwołane świadomie z panelu. Ciche skasowanie go wyglądałoby jak
sukces. Błąd odtwarzania nie zatrzymuje startu serwera — instalacja działająca
na starych danych z czytelnym błędem w dzienniku jest lepsza od instalacji,
która nie wstaje.

## Wgrywanie archiwum — co to naprawdę znaczy

Zrzut Postgresa nie jest biernym plikiem z danymi. Przy odtwarzaniu wykonuje SQL
jako właściciel bazy. „Wgraj plik i przywróć” znaczy więc: kto przejmie sesję
superadmina, ten wykonuje dowolny SQL. Stąd trzy rzeczy:

- **hasło pytane ponownie** przy samym przywracaniu (nie przy pobieraniu kopii),
- **wpis do audytu przed operacją**, a dodatkowo do dziennika kontenera — audyt
  w bazie za chwilę zostanie zastąpiony odtwarzaną zawartością i ślad po tej
  decyzji by zniknął,
- **manifest** sprawdzany przed przyjęciem. To nie chroni przed złym zamiarem —
  chroni przed plikiem sprzed roku, archiwum z innej instalacji i uciętym
  transferem.

Formularz przyjmuje do `CMDB_MAX_KOPIA_BYTES` (domyślnie 512 MB); nginx ma
odpowiadający wyjątek na `/admin/kopie/przywroc`. Przy większej bazie skopiuj
archiwum przez `scp` i użyj CLI — bez limitu i bez przeglądarki. To jest też
właściwa droga przy przenosinach na inny serwer.

## Przenosiny na inny serwer

1. `docker compose exec server python -m cmdb_server.cli kopia`
2. Pobierz archiwum z panelu albo `docker compose cp`.
3. Na nowym serwerze postaw stos z **tym samym `CMDB_SECRET_KEY`** (osobnym
   kanałem!) i tym samym `POSTGRES_PASSWORD`.
4. `python -m cmdb_server.cli przywroc <archiwum>` i restart.
5. Certyfikaty z `deploy/certs/` skopiuj osobno — też nie ma ich w archiwum.
