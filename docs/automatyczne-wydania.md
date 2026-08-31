# Automatyczne wydania agenta

Build i import **nie zmieniają** wersji oficjalnej, przypisań firm ani maszyn.
Po pojawieniu się wydania w istniejącym panelu `/admin/wersje` administrator
wybiera maszyny pilotażowe, firmę lub wersję oficjalną dotychczasowymi formularzami.

## Co wykonuje pipeline

1. Każdy przebieg workflow otrzymuje wersję `0.6.<run_number>+<run_attempt>`.
   Przykład: `0.6.42+1`, ponowienie `0.6.42+2`. Numer jest wspólny dla źródeł,
   GUI, CLI, Windows EXE, setupu i paczki Linux. Numery nie są ręcznie dopisywane
   do plików i nie powstają dodatkowe commity wersjonujące. Kolejność jest
   wyświetlana jako wydania; CMDB nadal porównuje dokładną wersję docelową.
2. Uruchamia pełne testy serwera i agenta, testy Windows, budowę EXE/setupu,
   test rzeczywistego workera/tray i podmiany EXE oraz renderowanie GUI.
3. Buduje paczkę Linux i sprawdza wersję/protokół programu z rozpakowanej paczki.
4. Osobny job weryfikuje komplet rzeczywistych artefaktów i protokół manifestu
   jednorazowym **testowym** kluczem. Ten podpis nigdy nie jest publikowany.
5. Tylko `push` do zatwierdzonej gałęzi `claude/os-data-collection-agent-gfz2o8`
   w `jaqb76/CMDB`, po sukcesie wszystkich powyższych jobów, uruchamia publikację.
   PR-y budują paczki do pobrania z Actions, ale nie dostają klucza publikacji.
6. Publikator podpisuje manifest Ed25519, tworzy szkic GitHub Release, dodaje
   EXE, setup, paczkę Linux i manifest, dopiero potem ujawnia kompletne wydanie.
   Nie nadpisuje poprzednich tagów/plików. Awaria zostawia szkic, którego CMDB
   nie importuje; ponowienie workflow ma nowy numer wersji.

Nie uruchamiaj równolegle innego workflow z tym samym schematem numeracji.
Nie usuwaj i nie twórz od nowa workflow, resetując jego licznik. Liczniki mają
limit 65535 ze względu na numery zasobów Windows; przed jego osiągnięciem trzeba
rozpocząć nową serię. Lokalne buildy poza CI zachowują wersję ze źródeł i nie
publikują się automatycznie.

## Konfiguracja jednorazowa — przed scaleniem i pierwszą publikacją

### 1. Klucz wydawcy

Na zaufanej maszynie administracyjnej, z zależnościami serwera, w `server/`:

```bash
python -m cmdb_server.release_keys /bezpieczna/sciezka/nowy-katalog-kluczy
```

Rodzic katalogu musi istnieć. Polecenie odmawia nadpisania istniejącego
katalogu; tworzy pliki z ograniczonymi uprawnieniami i nie wypisuje klucza.
Na Windows dodatkowo sprawdź ACL katalogu; tryb pliku POSIX nie zastępuje ACL.

- `signing-key.txt`: klucz **prywatny**, tylko do GitHub Actions.
- `public-keys.json`: klucz publiczny i jego identyfikator, do konfiguracji CMDB.

Nie dodawaj tych plików do repozytorium. Zachowaj prywatny klucz w firmowym
magazynie sekretów. Nie przesyłaj go w rozmowie ani do produkcyjnego CMDB.

### 2. GitHub

W repozytorium utwórz środowisko **agent-release**:

- dopuść wyłącznie gałąź `claude/os-data-collection-agent-gfz2o8`;
- dodaj sekret środowiska **CMDB_RELEASE_SIGNING_KEY** z `signing-key.txt`;
- chroń gałąź i pliki workflow/publikatora wymaganym przeglądem kodu;
- wymagane uprawnienie publikatora to `contents: write`; inne joby mają odczyt.

Sekret powinien być sekretem środowiska, nie ogólnym sekretem repozytorium
dostępnym dla dowolnego joba. Jeśli plan GitHub nie zapewnia wymaganych ochron
środowiska, zabezpiecz dostęp do klucza równoważnym zewnętrznym magazynem/CI;
nie udostępniaj go buildom z niezatwierdzonego kodu. Brak klucza kończy publikację
czytelnym błędem, bez wydania produkcyjnego. GITHUB_TOKEN zapewnia sam Actions.

### 3. Serwer CMDB

W istniejącym `deploy/.env`:

```dotenv
CMDB_RELEASE_IMPORT_ENABLED=true
CMDB_RELEASE_REPOSITORY=jaqb76/CMDB
CMDB_RELEASE_REF=refs/heads/claude/os-data-collection-agent-gfz2o8
CMDB_RELEASE_PUBLIC_KEYS='<jednoliniowa zawartosc public-keys.json>'
CMDB_RELEASE_GITHUB_TOKEN=<token odczytu prywatnego repozytorium>
CMDB_RELEASE_IMPORT_INTERVAL=300
```

Token: fine-grained PAT ograniczony do tego repozytorium z `Contents: read`.
Serwer potrzebuje wyłącznie odczytu, nie uprawnień do zapisu repozytorium,
Actions, sekretów ani administracji. Token nie trafia do agentów lub panelu.
Dla repozytorium publicznego może być pusty, ale obowiązują niższe limity API.

Przebuduj serwer i odtwórz kontener, zachowując wolumeny oraz dotychczasowy `.env`:

```bash
docker compose config --quiet
docker compose build server
docker compose up -d server
```

Kopie bazy, wolumenu wydań i konfiguracji wykonaj przed wdrożeniem. Nowe tabele
`release_provenance` i `release_import_status` dodaje istniejący `init_db()`.
Stare wydania/przypisania zostają. Dotychczasowe ręczne piny SHA-256 mogą zostać
dla starszych wydań; **nowe podpisane wydania nie wymagają dopisywania pinów**.

## Jak działa import

Serwer sprawdza Releases po starcie i następnie domyślnie co 5 minut. Wiele
procesów Uvicorna uzgadnia import blokadą PostgreSQL i wspólną datą przebiegu.
Sprawdza podpis, repozytorium, zatwierdzoną gałąź/workflow, wersję, nazwy,
rozmiary i SHA-256. Dodatkowo weryfikuje PE oraz strukturę/wersję paczki Linux.
Nigdy nie wykonuje pobranego EXE. Podpis oznacza zaufanie do kontrolowanego CI,
nie dowód, że dowolny podpisany program jest wolny od błędów.

Pobieranie odbywa się przez HTTPS; token nie jest przekazywany przy przekierowaniu
do CDN. Cały przebieg ma budżet 180 s i ograniczenia rozmiaru. Błędy nie blokują
raportowania agentów. Kolejny przebieg ponawia pobieranie. Nowe wydania są
sprawdzane zawsze, starsze stronicowane w kolejnych cyklach; po dłuższej przerwie
uzupełnienie całej historii może potrwać kilka przebiegów.

Komplet plików jest weryfikowany przed zapisaniem dwóch wydań (Windows/Linux)
w jednej transakcji. Kolizja wersji nie nadpisuje istniejącego pliku. Usunięcie
wydania w panelu zostawia znacznik importu, więc nie pojawi się ono ponownie.
Przed usunięciem trzeba zdjąć przypisania zgodnie z dotychczasowymi zasadami.

Przy włączonym importerze upload jest ukryty i zablokowany również po stronie
serwera. Katalog pokazuje stan importu, ostatnią próbę, ostatni sukces i błąd.
Przy wydaniu Windows znajduje się **Pobierz setup** (dla superadministratora).
Samo dodanie do katalogu nie instaluje niczego na stacjach.

## Wdrożenie i wycofanie

Wybierz dotychczasowym formularzem wersję dla kilku maszyn lub firmy testowej.
Po ocenie wyników rozszerz zakres. Ustawienie oficjalnej wersji obejmuje firmy
bez własnego przypisania — importer nigdy nie wykonuje tej operacji.

Przy każdej ofercie/pobraniu ponownie sprawdzane są dowody i bajty. Usunięcie
klucza z `CMDB_RELEASE_PUBLIC_KEYS` i odtworzenie serwera blokuje dystrybucję
wydań nim podpisanych, ale nie odinstalowuje już wdrożonych agentów.
Rotacja: dodaj nowy klucz publiczny, zmień klucz CI, zachowaj poprzedni publiczny
tak długo, jak dopuszczasz dystrybucję starych wydań. Samo wyłączenie importera
zatrzymuje pobieranie nowości, a nie już zatwierdzone aktualizacje.

Manifest Ed25519 **nie jest podpisem Authenticode**. Standardowy setup/EXE
nadal nie mają podpisu wydawcy Windows. Jeśli dodasz firmowe podpisywanie,
wykonuj je przed końcowymi testami, wyliczeniem skrótów i podpisaniem manifestu.

## GUI użytkownika

W głównym oknie jest wyłącznie wersja w nagłówku. Nie ma dodatkowych wierszy
wersji ani sekcji skanowania sieci. Podgląd skanera występuje tylko w oknie
ustawień uruchomionym z uprawnieniami administratora; sterowanie pozostaje
w polityce zasobu w panelu CMDB.

Źródła: [GitHub contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts),
[GitHub release assets](https://docs.github.com/en/rest/releases/assets),
[Ed25519](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/).
