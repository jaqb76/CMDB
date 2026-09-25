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

## Dane dopisywane ręcznie

Agent zna maszynę od środka, ale nie wie, gdzie ona stoi, kto przy niej
siedzi ani od kogo została kupiona. Te informacje wpisuje człowiek:

| Pole | Gdzie | Uwagi |
|---|---|---|
| Opiekun | strona sprzętu | osoba odpowiedzialna za sprzęt |
| Użytkownik komputera | strona sprzętu | osoba, która przy nim pracuje — zwykle ktoś inny niż opiekun |
| Lokalizacja | strona sprzętu | np. „Serwerownia A", „pokój 214" |
| Dział | lista osób | przypisany do osoby, nie do maszyny |
| Dostawca, faktura, gwarancja | zakładka „Zakup i gwarancja" | podstawa raportu o wygasającym wsparciu |

**Sprzęt bez agenta** (drukarki, switche, monitory, telefony) dodaje się
ręcznie przyciskiem *Dodaj sprzęt ręcznie* na liście. Taki wpis ma rodzaj,
lokalizację, opiekuna i dane zakupowe, ale nie ma raportu — i dlatego nigdy
nie trafia na listę „bez kontaktu": urządzenie bez agenta nie odezwie się
nigdy, więc alarm byłby stały i fałszywy. Wpis ręczny wolno usunąć; maszynę
z agentem można tylko wycofać, bo niesie historię raportów.

**Słowniki** (dział, lokalizacja, dostawca) zapełniają się same: każda nowa
wartość wpisana w formularzu od razu do nich trafia i od następnego razu
podpowiada się na liście. Wpisania własnej wartości nic nie blokuje —
wymuszanie wyboru z listy kończy się pustym polem, gdy ktoś nie znajdzie
swojego działu. Porównanie ignoruje wielkość liter i nadmiarowe spacje, więc
„magazyn" nie zakłada drugiego wpisu obok „Magazyn". Zawartość słowników
przegląda się na stronie *Słowniki*.

## Konta i uprawnienia

| Konto | Zakres | Zapis |
|---|---|---|
| `viewer` | jedna firma | nie |
| `admin` | jedna firma | tak |
| audytor globalny | wszystkie firmy | **nie** |
| superadmin | wszystkie firmy + panel `/admin` | tak |

Audytora globalnego zakłada superadmin w `/admin/firmy`. Przełącza się on
między firmami tak samo jak superadmin, ale nie dostaje żadnego formularza
zapisu — prawo do zapisu powstaje w jednym miejscu w kodzie i temu kontu nie
jest przyznawane nigdzie.

Hasło zmienia się pod `/konto`, zawsze po podaniu dotychczasowego — również
administrator. Superadmin może ustawić hasło dowolnemu kontu w `/admin/firmy`;
to jedyne wyjście z sytuacji, w której administrator firmy zapomniał swojego.
Hasła trzymane są jako skróty **Argon2id** i nie da się ich odczytać.

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

Najprościej prosto z serwera — w PowerShellu jako administrator:

```powershell
iwr https://cmdb.firma.pl/download/install.ps1 -OutFile install.ps1
.\install.ps1 -Token cmdb_ent_...
```

Skrypt pobiera agenta, sprawdza jego skrót SHA-256 i zakłada zadanie
harmonogramu. Wymaga wcześniejszego wgrania wersji dla Windows w panelu —
plik dla Windows, w odróżnieniu od paczki dla Linuksa, nie jest wbudowany
w serwer.

Wariant z kreatorem graficznym:

```powershell
# na maszynie budującej (raz) — powstaje CMDB-Agent-Setup-0.5.10.exe
.\agent\packaging\build-agent.ps1 -Installer

# na maszynie docelowej — kreator pyta o adres serwera i token
CMDB-Agent-Setup-0.5.10.exe

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

## Monitorowanie usług i certyfikatów SSL

Ewidencja odpowiada na pytanie „co mamy”. Osobny moduł odpowiada na dwa inne:
**czy to działa** i **do kiedy ważny jest certyfikat**. Cel podaje się adresem
IP albo nazwą hosta — i **nie musi mieć wpisu w ewidencji**: certyfikat domeny
u zewnętrznego dostawcy albo adres na load balancerze też jest usługą, którą
ktoś musi pilnować.

**Sonduje agent na wskazanej maszynie — i tylko agent.** Usługa żyje w sieci
klienta, za NAT-em i firewallem; serwer CMDB tej sieci zwykle nie widzi,
a gdyby widział sieci wszystkich firm naraz, byłby jednym miejscem, z którego
da się zajrzeć do każdej z nich. Agent już tam stoi i mierzy to, co zobaczy
użytkownik usługi. Serwer wydaje politykę, przyjmuje wyniki, rozbiera
certyfikat, ocenia stan i powiadamia — nie ma trybu, w którym sonduje sam.

Cel bez czynnej maszyny sprawdzającej nie jest więc monitorowany wcale
i dostaje w panelu znacznik **„nikt nie sprawdza”** wraz z przyczyną. Usunięcie
maszyny nie kasuje przy tym celu ani historii jego awarii — to problem do
naprawienia, a nie dane do wyrzucenia.

```
   maszyna z agentem                        serwer CMDB
  ┌────────────────────┐   polityka      ┌───────────────────────┐
  │ cmdb-agent monitor │ <────────────── │ co sprawdzać i jak     │
  │  • sonda co 60 s   │                 │ często                 │
  │  • cert co 24 h    │   raport        │                        │
  │  • składa przerwy  │ ──────────────> │ ocena stanu, progi,    │
  └─────────┬──────────┘   co 15 min     │ powiadomienia          │
            ▼              + awarie od   └───────────────────────┘
   usługa w sieci klienta   razu
```

**Dwa rytmy.** Sonda idzie we własnym odstępie każdego celu — „czy odpowiada”
ma sens co minutę, „do kiedy ważny certyfikat” raz na dobę. Raport idzie co
15 minut i **nie zawiera sond**, tylko ich podsumowanie („15 sond, 15 udanych”)
oraz to, czego brakowało: „12:34:34 – 12:40:22, connection refused”. Doba
monitorowania co minutę to **96 wierszy zamiast 1440**. Początek i koniec
potwierdzonej awarii idą natychmiast, osobnym małym żądaniem — alarm, który
czeka kwadrans, przestaje być alarmem.

| Protokół | Co sprawdza |
|---|---|
| `tcp` | port przyjmuje połączenia |
| `tls` | połączenie szyfrowane **i certyfikat** — także dla SMTPS, IMAPS, LDAPS |
| `http` | kod odpowiedzi serwera WWW |
| `https` | kod odpowiedzi **i certyfikat** |

**Dostępność i certyfikat oceniamy osobno**, bo osobno się psują: usługa
potrafi odpowiadać z certyfikatem wygasającym jutro i potrafi milczeć
z certyfikatem ważnym rok. Stan celu na liście to gorsza z dwóch ocen, ale
powiadomienia o nich idą niezależnie. Procent dostępności liczy się z podsumowań
agenta; brak sond pokazujemy jako „brak danych”, a nie 100%. Gdy agent
przestaje raportować, cel dostaje znacznik **„brak raportów”** — to nie jest
awaria usługi ani jej sprawność, tylko brak wiedzy.

Wiadomość idzie **przy zmianie stanu i tylko przy zmianie**: przy potwierdzonej
awarii (jedna zgubiona odpowiedź to jeszcze nie awaria), przy powrocie usługi
i przy każdym przekroczonym progu ważności certyfikatu — 30 dni, 7 dni, po
terminie. Odnowiony certyfikat zeruje progi i przynosi potwierdzenie, że
odnowienie zadziałało. Wysyłka przez serwer SMTP tej firmy, ten sam co raporty.

Certyfikat rozbiera serwer — agent chodzi na samej bibliotece standardowej,
w której nie ma parsera X.509. Agent liczy odcisk SHA-256 i przysyła **cały
certyfikat tylko przy zmianie odcisku**, czyli po odnowieniu.

Na maszynie klienta monitorowanie to **osobna usługa** obok inwentaryzacji
(`cmdb-agent-monitor.service` / zadanie „CMDB Agent Monitor”): inwentaryzacja
odpala się raz na kilka godzin i kończy, a sonda ma chodzić co minutę.
Instalatory zakładają ją zawsze — bez przypisanych celów tylko pyta o politykę
i śpi.

Oprócz alarmów w chwili awarii dostępny jest cykliczny raport pocztą
**Dostępność usług i certyfikaty** — podsumowanie okresu dla kogoś, kto nie
siedzi przy alarmach.

Szczegóły, kontrakt raportu i lista adresów, pod które agent świadomie nie
pójdzie: [`docs/monitorowanie-uslug.md`](docs/monitorowanie-uslug.md).

## Helpdesk — zgłoszenia klientów

Klienci piszą maile pod **jeden adres**, a system zakłada z nich zgłoszenia.
O tym, czyje jest zgłoszenie, decyduje domena nadawcy; firma nadaje numer
(`BON-123`, `KLE-88`), a numer wraca w temacie maila, więc odpowiedź klienta
trafia do swojej sprawy, a nie zakłada drugiej.

| Element | Jak działa |
|---|---|
| Odbiór | IMAP, jedna skrzynka, pętla co 2 minuty; wiadomości nie są kasowane |
| Rozpoznanie | wątek (`In-Reply-To`, `References`, numer w temacie) → domena → firma |
| Z telefonu | technik zakłada zgłoszenie w imieniu klienta; dalej idzie tą samą drogą co mail |
| Nierozpoznane | poczta z obcej domeny czeka na decyzję operatora; nikt nie dostaje odpowiedzi |
| Wątek | wiadomości, odpowiedzi, komentarze wewnętrzne i zdarzenia w jednej rozmowie |
| Status | idzie za pocztą: odpowiedź do klienta → „Oczekuje", jego odpowiedź → „W trakcie" |
| Czyja piłka | znacznik na belce liczy zgłoszenia czekające na naszą odpowiedź; kasuje go tylko wysłana wiadomość |
| Zamknięcie | wysyła klientowi wiadomość o zakończeniu, z podsumowaniem technika |
| Czas pracy | każdy technik dopisuje swoje minuty; pomyłkę prostuje wpis na minus (`-15`) |
| Zgłaszający | adres z maila wiąże zgłoszenie z osobą z kartoteki — telefon, lokalizacja, jej sprzęt |
| Raporty | podsumowanie wszystkich techników za miesiąc plus rozbicia per firma i per technik, eksport CSV i XLSX |
| Sprzęt | zgłoszenie wiąże się z maszyną z CMDB — to jest jej karta napraw |

Technik nie należy do żadnej firmy: obsługuje te, które przypisze mu
superadmin, i w każdej z nich pracuje z prawami jej operatora — łącznie
z wdrożeniem innej wersji agenta, bo „agent nie wysyła danych" też bywa
zgłoszeniem.

Szczegóły: [`docs/helpdesk.md`](docs/helpdesk.md).

## Baza wiedzy

Procedury, runbooki i opisy rozwiązań w układzie znanym z Confluence —
przestrzenie i drzewo stron — ale **powiązane z maszynami z CMDB**. Artykuł
wskazuje, czego dotyczy: listą systemów (nazwy hostów, FQDN, aplikacji) albo
warunkami na nazwę hosta (`db0*`), system operacyjny, oprogramowanie
z ostatniego raportu, rodzaj sprzętu, lokalizację czy etykietę. **Wystarczy
jeden pasujący warunek**, a artykuł sam pojawia się na karcie maszyny
w zakładce „Wiedza”, z powodem dopasowania.

| Element | Jak działa |
|---|---|
| Treść | Markdown z panelami (`:::uwaga`) i polami `{hostname}`, `{ip}`… wypełnianymi danymi maszyny, z której otwarto artykuł |
| Historia | wersja tylko przy realnej zmianie, z listą zmienionych pól; porównanie i przywrócenie treści |
| Wyszukiwanie | pełnotekstowe po tytule, streszczeniu, tagach, systemach i treści, bez względu na polskie znaki |
| Lista zasobów | liczba artykułów przy nazwie maszyny — jedno zapytanie dla całej listy |
| Kosz | usunięty artykuł zostaje z historią i załącznikami, można go przywrócić |
| Przegląd | właściciel i termin przeglądu; po terminie artykuł trafia na listę „Do przeglądu” |
| Nazwy → linki | `GET /wiedza/linki?nazwa=…` — które z podanych nazw mają artykuł (dla innych modułów) |

Przestrzenie należą do firmy; technik widzi tylko firmy, do których ma
dostęp. Konto tylko do odczytu dostaje 403 na każdej trasie zapisu.

Szczegóły: [`docs/baza-wiedzy.md`](docs/baza-wiedzy.md).

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
| [`docs/podatnosci.md`](docs/podatnosci.md) | badanie CVE, dane dystrybucji, porównywanie wersji dpkg |
| [`docs/raporty.md`](docs/raporty.md) | raporty pocztą, SMTP per firma, harmonogram, dane gwarancji |
| [`docs/funkcje-agenta.md`](docs/funkcje-agenta.md) | zakładka „Agent”: funkcje podstawowe, dodatkowe funkcjonalności per agent, „czeka na odebranie” |
| [`docs/nutanix.md`](docs/nutanix.md) | odczyt Nutanix Prism Central przez agenta: klastry, hosty, VM, łączenie z agentami po UUID |
| [`docs/vmware.md`](docs/vmware.md) | odczyt VMware vCenter przez agenta: klastry, hosty ESXi, VM — ta sama ścieżka co Nutanix |
| [`docs/monitorowanie-uslug.md`](docs/monitorowanie-uslug.md) | monitorowanie dostępności usług i ważności certyfikatów SSL, progi, powiadomienia |
| [`docs/helpdesk.md`](docs/helpdesk.md) | zgłoszenia z maili, rozpoznawanie wątków, czas pracy, raporty, dostępy techników |
| [`docs/baza-wiedzy.md`](docs/baza-wiedzy.md) | przestrzenie i artykuły, dopasowanie do maszyn, historia wersji, trasa „nazwy → linki” |
| [`docs/kopie-zapasowe.md`](docs/kopie-zapasowe.md) | co jest stanem instalacji, nocne zadanie, przywracanie z panelu, przenosiny |
| [`docs/dwie-instancje.md`](docs/dwie-instancje.md) | produkcja i development na jednym serwerze, wspólny nginx, oznaczenie instancji |
| [`docs/wdrozenie.md`](docs/wdrozenie.md) | docker compose, TLS, kopie zapasowe, utrzymanie |

## Testy

Testy serwera chodzą na PostgreSQL — tym samym silniku co produkcja. Baza
testowa zakłada się raz:

```bash
psql -U postgres -c "CREATE ROLE cmdb LOGIN PASSWORD 'cmdb'"
psql -U postgres -c "CREATE DATABASE cmdb_test OWNER cmdb"
```

```bash
cd server && pytest              # API agentów, izolacja firm, panel, migracje
cd agent  && pytest              # konfiguracja, stan, status, diagnostyka, kolektory

# inne dane dostępowe niż domyślne cmdb:cmdb@localhost:5432/cmdb_test
CMDB_DATABASE_URL=postgresql+psycopg://user:hasło@host/cmdb_test pytest
```

Testy kasują schemat przed każdym przypadkiem, więc odmawiają startu na bazie
bez „test" w nazwie — przypadkowe wskazanie produkcji nic nie zniszczy.

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

## Aktualizacja: bezpieczeństwo i zarządzanie zasobami

Sesje z unieważnianiem, blokada ponownej rejestracji agenta, bieżący pełny
odczyt niezależny od historii, relacje VM/host/klaster/aplikacja oraz widok
jakości danych opisano w [instrukcji aktualizacji](docs/przeglad-poprawki.md).

### Jeden EXE i wykrywanie urządzeń w sieci (agent 0.5.10)

Nowe wydania z CI otrzymują automatyczny numer `0.6.<run>+<attempt>`.
Po jednorazowej konfiguracji podpisu i dostępu do repozytorium trafiają
automatycznie do katalogu CMDB, bez uploadu i bez zmiany przypisań maszyn.
[Automatyczne wydania — konfiguracja i wdrożenie](docs/automatyczne-wydania.md).

Jeden `cmdb-agent.exe` zawiera worker i tray. Bez argumentów uruchamia się
do zasobnika bez otwierania statusu; `run` wykonuje pracę w tle. Opcjonalny
moduł wykrywania włącza administrator centralnie w CMDB, na karcie zasobu.
Główne okno pokazuje wersję tylko w nagłówku i nie pokazuje skanera.
Podgląd modułu jest dostępny wyłącznie administratorowi w ustawieniach.
Okna używają znaku C. Podpowiedzi IP,
MAC, DNS, typu sprzętu i OS trafiają do **Wykrywanie sieci** w panelu;
elementy zatwierdza się do ewidencji po sprawdzeniu.
[Konfiguracja, limity i aktualizacja](docs/wykrywanie-sieci.md).
Dystrybucja Windows wymaga zaufanego skrótu gotowego buildu w konfiguracji
serwera — sama stopka `unified` nie daje uprawnień do samoaktualizacji.
[Przygotowanie zaufanych wydań](docs/zaufane-wydania-windows.md).

## Licencja

Projekt jest udostępniony na licencji
[PolyForm Noncommercial 1.0.0](LICENSE.md). To **nie jest** licencja open
source w rozumieniu OSI — kod jest publiczny do wglądu, ale prawo do używania
jest ograniczone.

**Bezpłatnie wolno** (wyłącznie w celach niekomercyjnych):

* używać prywatnie — do nauki, testów, badań, projektów hobbystycznych,
  bez przewidywanego zastosowania komercyjnego,
* używać w organizacjach niekomercyjnych: fundacjach i organizacjach
  charytatywnych, szkołach i uczelniach, publicznych instytucjach badawczych,
  ochrony zdrowia i bezpieczeństwa, ochrony środowiska oraz w instytucjach
  rządowych,
* modyfikować i rozpowszechniać kod w tych samych celach, zawsze razem
  z treścią licencji i linią `Required Notice`.

**Bez osobnej, płatnej licencji nie wolno** używać projektu w żadnej firmie
ani działalności zarobkowej — dotyczy to także użytku wyłącznie
wewnętrznego (inwentaryzacja maszyn własnej firmy), świadczenia usług
klientom (np. przez firmę IT lub MSP), udostępniania jako usługi (SaaS)
oraz wbudowywania w produkty komercyjne. Jednoosobowa działalność
gospodarcza również jest firmą.

Licencję komercyjną można uzyskać od autora — kontakt przez
[profil GitHub](https://github.com/jaqb76).

Oprogramowanie jest dostarczane „tak jak jest”, bez żadnych gwarancji.
Wiążący jest angielski tekst w [LICENSE.md](LICENSE.md); powyższy opis ma
charakter informacyjny.
