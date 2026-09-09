# Monitorowanie usług i certyfikatów SSL

Ewidencja odpowiada na pytanie „co mamy”. Ten moduł odpowiada na dwa inne:
**czy to działa** i **do kiedy ważny jest certyfikat**. Cel podaje się adresem
IP albo nazwą hosta — nie musi mieć wpisu w ewidencji.

## Sonduje agent, nie serwer

To jest decyzja, z której wynika cała reszta.

Usługa żyje w sieci klienta, za NAT-em i firewallem. Serwer CMDB tej sieci
zwykle nie widzi — a gdyby widział sieci wszystkich firm naraz, byłby jednym
miejscem, z którego da się zajrzeć do każdej z nich. Agent już tam stoi
i mierzy dokładnie to, co zobaczy użytkownik usługi.

```
   maszyna z agentem                        serwer CMDB
  ┌────────────────────┐                 ┌──────────────────────┐
  │ cmdb-agent monitor │ ── polityka ──> │ co sprawdzać,        │
  │  • sonda co 60 s   │ <─────────────  │ jak często           │
  │  • cert co 24 h    │                 │                      │
  │  • składa przerwy  │ ── raport ────> │ ocena stanu, progi,  │
  └─────────┬──────────┘   co 15 min     │ parsowanie certyfikatu│
            │              + awarie od   │ powiadomienia        │
            ▼              razu          └──────────────────────┘
   usługa w sieci klienta
```

Podział pracy wychodzi z tego sam:

| Kto | Co robi |
|---|---|
| **agent** | pobiera politykę, wykonuje sondy, składa przerwy, raportuje |
| **serwer** | wydaje politykę, przyjmuje wyniki, rozbiera certyfikat, ocenia stan, powiadamia |

**Agent jest jedynym wykonawcą.** Nie ma trybu, w którym sonduje serwer, i nie
ma go świadomie: taki tryb znaczyłby jedno miejsce z wglądem w sieci wszystkich
firm, a przy okazji drugą implementację sondy — bo agent chodzi na samej
bibliotece standardowej i kodu serwera importować nie może, a dwie
implementacje tego samego pomiaru rozjeżdżają się po cichu. W
`services/monitoring.py` nie ma ani jednego gniazda i pilnuje tego test.

Certyfikat rozbiera serwer, bo agent chodzi na samej bibliotece standardowej,
w której nie ma parsera X.509. Agent liczy odcisk SHA-256 (`hashlib`
wystarczy) i przysyła **cały certyfikat tylko wtedy, gdy odcisk się zmienił** —
certyfikat ważny rok nie ma po co jechać przez sieć co dobę.

## Dwa rytmy

Sonda i raport to nie to samo, i mylenie ich jest najczęstszym nieporozumieniem
przy tym module.

* **Sonda** idzie we własnym odstępie każdego celu. „Czy odpowiada” ma sens co
  minutę; „do kiedy ważny certyfikat” zmienia odpowiedź raz na kilka miesięcy
  i pytanie o to co minutę jest wyłącznie hałasem. Stąd dwa osobne ustawienia:
  odstęp sondy (domyślnie 60 s) i odstęp certyfikatu (domyślnie doba).
* **Raport** idzie co 15 minut i **nie zawiera sond**. Zawiera ich
  podsumowanie — „od 12:30 do 12:45, 15 sond, 15 udanych” — oraz to, czego
  brakowało: „12:34:34 – 12:40:22, connection refused”.

Doba monitorowania co minutę to **96 wierszy zamiast 1440** na cel.

Wyjątkiem od kwadransa są **zdarzenia**: początek i koniec potwierdzonej awarii
idą natychmiast, osobnym małym żądaniem. Redukcja ruchu dotyczy potwierdzeń, że
wszystko działa — alarm, który czeka kwadrans, przestaje być alarmem.

## Co agent wysyła

Jeden wpis na cel, w jednym raporcie:

```json
{
  "id": "…",
  "okno":   {"od": "12:30:00Z", "do": "12:45:00Z", "sond": 15, "udanych": 9},
  "przerwy": [{"od": "12:34:34Z", "do": "12:40:22Z",
               "blad": "connection refused", "sond": 6}],
  "ostatnia_sonda": {"kiedy": "…", "dostepna": true, "potwierdzona_awaria": false,
                     "czas_ms": 12, "kod": 200, "adres": "10.0.0.5"},
  "tls": {"odcisk": "…", "zaufany": true, "blad": null, "pem": null}
}
```

**Okno mówi także, że agent w ogóle monitorował.** Bez tego nie da się odróżnić
„usługa działała” od „nikt nie patrzył” — a to dwie zupełnie różne rzeczy,
które w procencie dostępności wyglądałyby identycznie.

**Przerwa trwająca ma puste `do`** i zostaje domknięta kolejnym raportem.
Rozpoznajemy ją po `(cel, początek)`, bo początek przerwy jest jej tożsamością;
wstawienie drugiego wiersza rozbiłoby jedną awarię na dwie.

Przerwa zaczyna się przy **pierwszym** błędzie, nie przy potwierdzeniu —
inaczej z jej opisu znikałaby ta część, w której usługa już nie działała,
a agent jeszcze nie miał pewności. Kończy się na **ostatniej nieudanej**
sondzie, nie na pierwszej udanej: między nimi usługi nie było.

## Cztery sposoby sprawdzenia

| Protokół | Co sprawdza | Kiedy używać |
|---|---|---|
| `tcp` | port przyjmuje połączenia | usługi bez TLS i bez HTTP: SQL, SMB, DNS po TCP |
| `tls` | połączenie szyfrowane **i certyfikat** | SMTPS, IMAPS, LDAPS — wszystko, co nie mówi po HTTP |
| `http` | kod odpowiedzi serwera WWW | usługi wewnętrzne bez szyfrowania |
| `https` | kod odpowiedzi **i certyfikat** | strony i API |

Otwarty port nie znaczy, że aplikacja za nim żyje — dlatego przy HTTP(S)
pytamy dodatkowo o kod odpowiedzi. Domyślnie akceptujemy każdy kod poniżej
400; można wskazać konkretny, gdy poprawną odpowiedzią jest np. `401`.

Żądanie idzie **bez podążania za przekierowaniem** — przekierowanie prowadzi
pod adres, którego nikt nie sprawdził. Treść odpowiedzi nie jest czytana:
pytanie brzmi „czy aplikacja odpowiada”, a nie „co odpowiada”.

## Dostępność i certyfikat to dwie osobne oceny

Usługa potrafi odpowiadać z certyfikatem wygasającym jutro i potrafi milczeć
z certyfikatem ważnym rok. Zlanie tych dwóch rzeczy w jeden stan kończy się
tym, że jedna sprawa przykrywa drugą — a powiadomienia o nich trafiają do kogo
innego i w innym czasie.

* **Stan celu** widoczny na liście to *gorsza* z dwóch ocen.
* **Procent dostępności** liczy się wyłącznie z dostępności — certyfikat,
  który kończy się za trzy tygodnie, nie obniża go ani o promil.
* **Powiadomienia** idą osobno dla każdej ze spraw.

Stan `nieznany` nie jest tym samym co `ok`. Cel dopiero dodany albo wyłączony
nie został jeszcze zmierzony. Z tego samego powodu brak sond w oknie czasu
pokazujemy jako „brak danych”, a nie 100%.

## Cisza agenta

Gdy agent przestaje przysyłać raporty, cel dostaje znacznik **„brak raportów”**
(po dwóch pominiętych okresach — jeden zdarza się przy restarcie maszyny).

To nie jest awaria usługi ani jej sprawność — to **brak wiedzy**, i naprawia
się go gdzie indziej: przy agencie, a nie przy usłudze. Pokazanie w takiej
sytuacji ostatniego znanego stanu jako bieżącego byłoby twierdzeniem o czymś,
czego nikt od godziny nie mierzył.

## Kiedy przychodzi wiadomość

Zapisujemy stan, **o którym powiadomiliśmy**, a nie sam fakt wysyłki. Dzięki
temu wiadomość idzie przy każdej zmianie i tylko przy zmianie: awaria trwająca
tydzień nie zamienia się w tysiąc wiadomości, a jej koniec zostaje zauważony.

**Dostępność**

* usługa nie odpowiada przez ustawioną liczbę prób → *Usługa niedostępna*,
* usługa znowu odpowiada → *Usługa działa*.

Licznik kolejnych prób prowadzi **agent** — to on zna każdą sondę; serwer widzi
wyłącznie podsumowania. Jedna zgubiona odpowiedź zdarza się w każdej sieci,
więc pierwszy błąd daje tylko stan „ostrzeżenie” i nie wysyła niczego.

**Certyfikat**

* przekroczony próg ostrzeżenia (domyślnie 30 dni) → *Certyfikat wygasa*,
* przekroczony próg alarmu (domyślnie 7 dni) → *Certyfikat wygasa*,
* po terminie → *Certyfikat wygasł*,
* łańcuch przestał się weryfikować → *Certyfikat niezaufany*,
* problem ustąpił albo certyfikat odnowiono → *Certyfikat w porządku*.

Same progi to za mało, a sam stan to za mało: w „ostrzeżeniu” certyfikat stoi
trzy tygodnie, więc bez drabinki progów nikt nie usłyszałby o nim ponownie
przed samym końcem. Dlatego pilnujemy obu rzeczy naraz.

Odnowiony certyfikat ma inny odcisk — to zeruje progi, więc przed następnym
końcem ważności ostrzeżenie przyjdzie ponownie. Przychodzi też potwierdzenie,
że odnowienie zadziałało.

**Wysyłka, która się nie udała, nie przesuwa znacznika.** Powiadomienie, które
nie doszło, nie jest powiadomieniem, więc próba zostanie powtórzona przy
kolejnym raporcie, a przyczyna widnieje na stronie celu.

Wiadomości idą przez **serwer SMTP tej firmy**, ustawiony na stronie
*Raporty → Poczta wychodząca* — tak samo jak raporty i z tego samego powodu.

## Certyfikat własnego urzędu firmy

Usługi wewnętrzne mają zwykle certyfikat urzędu, którego agent nie zna.
Weryfikacja łańcucha się wtedy nie powiedzie — i jest to stan normalny, a nie
awaria. Wyłącz przy takim celu *Wymagaj zaufanego łańcucha*.

**Data ważności jest sprawdzana zawsze**, niezależnie od tego ustawienia.
Odcisk agent liczy z surowych bajtów połączenia, a nie ze słownika
`getpeercert()` — tamten jest pusty, gdy połączenie zestawiono bez weryfikacji,
czyli dokładnie wtedy, gdy certyfikat interesuje nas najbardziej.

Gdy weryfikacja jest włączona, a łańcuch jej nie przechodzi, agent robi drugie
połączenie bez weryfikacji, żeby mimo wszystko zobaczyć certyfikat — pod ten
sam adres co za pierwszym razem. Kosztuje to dodatkowe połączenie tylko wtedy,
gdy naprawdę coś jest nie tak.

## Monitorowanie po adresie IP

Serwer obsługujący kilka domen bez wskazówki poda certyfikat pierwszej
z brzegu. Pole **nazwa w certyfikacie** agent wysyła w SNI i to ją sprawdza —
wypełnij je, gdy cel podano adresem liczbowym.

## Wybór maszyny sprawdzającej

Każdy cel ma **wykonawcę**: aktywną maszynę z agentem. To ona nawiąże
połączenie, więc wybierz taką, która widzi usługę tak, jak widzą ją
użytkownicy — monitorowanie z tej samej maszyny, na której usługa stoi,
odpowiada na słabsze pytanie niż monitorowanie z sieci obok.

Wyłączenie albo wycofanie maszyny gasi monitorowanie jej celów; widać to na
karcie zasobu w sekcji **„Ta maszyna sprawdza”**. Cele trzeba wtedy przepisać
innemu agentowi.

### Cel bez maszyny sprawdzającej

Skoro agent jest jedynym wykonawcą, cel bez czynnego agenta **nie jest
sprawdzany wcale**. Dzieje się to, gdy maszyna zostanie wycofana, wyłączona
albo usunięta. Taki cel dostaje w panelu znacznik **„nikt nie sprawdza”** wraz
z przyczyną, liczy się osobno w podsumowaniu i trafia do raportu.

Osobno od „braku raportów”, bo to inna usterka i naprawia się ją inaczej: tam
agent przestał się odzywać, tu nikomu tego nie zlecono. Znacznik pojawia się
**od razu**, a nie po dwóch pominiętych okresach — nic się nie psuło, więc nie
ma na co czekać.

Usunięcie maszyny **nie kasuje celu ani historii jego awarii** (klucz obcy jest
`SET NULL`, nie `CASCADE`). Cel bez wykonawcy to widoczny problem do
naprawienia — przepisz go innej maszynie — a nie dane do wyrzucenia.

Zmiana wykonawcy **zeruje stan i historię powiadomień** — to wtedy inny pomiar,
choć pod tą samą nazwą. Historia przerw zostaje: opisuje to, co było naprawdę.

## Zlecenie sprawdzenia

Przycisk *Zleć sprawdzenie* jest **zleceniem, nie odpowiedzią**. Panel nie
sonduje, więc wynik przyjdzie dopiero, gdy agent po niego sięgnie — w praktyce
w ciągu minuty. Udawanie natychmiastowej odpowiedzi byłoby najgorszą z opcji:
ktoś zobaczyłby „sprawdzono” i stały stan sprzed zmiany, którą właśnie
wprowadził.

Znacznik gasi się sam: gdy przyjdzie wynik nowszy niż zlecenie, przestaje być
wydawany w polityce.

## Adresy, pod które agent nie pójdzie

Cel wpisuje administrator firmy, a połączenie nawiązuje agent:

| Adres | Dlaczego |
|---|---|
| `169.254.0.0/16`, `fe80::/10` | pod `169.254.169.254` odpowiada usługa metadanych chmury, a agent bywa maszyną wirtualną u dostawcy |
| `0.0.0.0`, `::` | adres nieokreślony |
| multicast, adresy zarezerwowane | nie są usługą |

Agent sprawdza adres **po rozwiązaniu nazwy** i łączy się dokładnie z tym, co
sprawdził — inaczej między sprawdzeniem a połączeniem odpowiedź DNS mogłaby się
zmienić. Adres IPv4 zapisany jako IPv6 (`::ffff:169.254.169.254`) to ten sam
adres i podlega tej samej odmowie.

Adres pętli zwrotnej agent przyjmuje (usługa na tej samej maszynie to zwyczajny
cel), ale **serwer odrzuca go przy zapisie** — `127.0.0.1` w panelu znaczy
„maszyna agenta”, a nie to, co zwykle ma na myśli wpisujący. Zezwala na to
`CMDB_MONITORING_ALLOW_LOOPBACK=true`.

## Usługa na maszynie klienta

Monitorowanie to **osobna, długo żyjąca usługa** obok inwentaryzacji:

| System | Jednostka | Uruchamiana |
|---|---|---|
| Linux | `cmdb-agent-monitor.service` | zawsze, `Restart=always` |
| Windows | zadanie „CMDB Agent Monitor” | przy starcie systemu, SYSTEM |

Musi taka być: inwentaryzacja odpala się z harmonogramu raz na kilka godzin
i kończy, a sonda dostępności ma chodzić co minutę — jedno w drugim zmieścić
się nie da. Bez przypisanych celów usługa tylko pyta serwer o politykę i śpi,
więc instalator zakłada ją zawsze.

Zatrzymanie usługi **dokańcza raport**: agent łapie `SIGTERM` i wysyła ostatnie
podsumowanie, zanim zakończy pracę — inaczej kwadrans pomiarów przepadałby przy
każdym restarcie. Otwarta przerwa i znane odciski przeżywają restart, bo agent
zapisuje je w `monitoring-state.json`.

Ręcznie: `cmdb-agent monitor` (wymaga zarejestrowanego agenta).

## Sprawdzenie z poziomu maszyny

Panel pokazuje, co przyszło od agenta. Nie odpowiada natomiast na pytanie
„dlaczego nic nie przychodzi" — cel bez wyników wygląda tam tak samo, gdy
usługa monitorowania nie wstała, gdy nikt nie przypisał tej maszynie celów
i gdy sonda nie ma jak wyjść z sieci. Rozstrzyga to `cmdb-agent status`
uruchomiony **na maszynie sprawdzającej**:

```
monitorowanie uslug  : Działa · sprawdza 2 usł.
ostatnia sonda       : 2026-09-09 07:40:59 (przed chwilą)
ostatni raport       : 2026-09-09 07:35:19 (6 min temu)

  Portal firmowy
      https intranet.firma.pl:443  co 60 s
      odpowiada w 43 ms (HTTP 200)
      sprawdzona przed chwilą, udanych 6/6 w bieżącym okresie
  Baza danych
      tcp 10.0.10.15:5432  co 60 s
      NIE ODPOWIADA - connection refused
      przerwa trwa od 2026-09-09 07:37:19
```

Pięć stanów, które trzeba od siebie odróżnić, bo naprawia się je gdzie indziej:

| Co pokazuje status | Co to znaczy | Gdzie naprawiać |
|---|---|---|
| `Nie uruchomiono na tej maszynie` | usługa monitorowania nigdy nie wystartowała | na maszynie — patrz niżej |
| `Proces monitorowania nie odpowiada` | usługa stanęła albo się zawiesiła | dziennik agenta, restart usługi |
| `Działa — panel CMDB nie przypisał…` | usługa żyje, ale nie ma czego sprawdzać | panel: wybór maszyny sprawdzającej |
| `Działa, ale ostatnia wymiana…` | sonda chodzi, raport nie dochodzi do serwera | łączność agent → serwer |
| `Działa · sprawdza N usł.` | wszystko na miejscu | — |

Pojedyncza nieudana sonda pokazuje się jako `nieudana sonda (czeka na
potwierdzenie)`, a nie jako awaria: przerwa zaczyna się przy pierwszym błędzie,
ale awarią staje się dopiero po `liczba_prob` próbach. Jedna zgubiona odpowiedź
zdarza się w każdej sieci.

To samo widać w oknie ikony w zasobniku, w wierszu „Monitorowanie usług".

### Usługa monitorowania nie wystartowała

Najczęstsza przyczyna: agent był **aktualizowany w miejscu**. Aktualizacja
podmienia program, ale nie zakłada jednostek systemowych — maszyna z agentem
sprzed wydania z monitorowaniem nie dostanie `cmdb-agent-monitor.service`
z samej aktualizacji.

```bash
# Linux
systemctl enable --now cmdb-agent-monitor
systemctl status cmdb-agent-monitor

# Windows (PowerShell jako administrator)
schtasks /Run /TN "CMDB Agent Monitor"
```

Gdy jednostki nie ma w ogóle, uruchom ponownie instalator agenta — założy ją
obok istniejącej inwentaryzacji i nie ruszy rejestracji maszyny.

Status jest publikowany w `public/monitoring.json` w katalogu danych agenta.
Plik jest czytelny dla każdego zalogowanego użytkownika (czyta go ikona
w zasobniku), więc **nie trafiają tam certyfikaty ani token agenta** — tylko to,
co jest sprawdzane i z jakim skutkiem. Świeżość tego pliku jest zarazem dowodem,
że pętla żyje: status starszy niż dwie minuty agent czyta jako „proces nie
odpowiada".

## Ustawienia serwera

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_MONITORING_ENABLED` | `true` | wydawanie polityki agentom; wyłączenie zostawia konfigurację w bazie |
| `CMDB_MONITORING_REPORT_SECONDS` | `900` | co ile agent przysyła podsumowanie okresu |
| `CMDB_MONITORING_MAX_TARGETS` | `200` | limit celów na firmę |
| `CMDB_MONITORING_MAX_PER_AGENT` | `50` | limit celów przypisanych jednemu agentowi |
| `CMDB_MONITORING_HISTORY_DAYS` | `90` | retencja okien i przerw |
| `CMDB_MONITORING_ALLOW_LOOPBACK` | `false` | patrz wyżej |

Limit na agenta nie jest ozdobą: agent sonduje z maszyny, która ma też inną
pracę do wykonania — przy dwustu celach co minutę monitorowanie przestaje być
dodatkiem, a zaczyna być jej głównym zajęciem.

Serwer nie ma tu żadnej pętli w tle. Sprzątanie starych okien i przerw jedzie
z istniejącym harmonogramem raportów, pod tą samą blokadą doradczą PostgreSQL.

## Ustawienia celu

| Pole | Domyślnie | Znaczenie |
|---|---|---|
| Sonda co | 60 s | minimum 60 s — częściej to już generator ruchu |
| Certyfikat co | 24 h | ważność zmienia się raz na miesiące |
| Limit czasu | 10 s | musi być krótszy niż odstęp sondy |
| Prób przed alarmem | 2 | ile kolejnych błędów oznacza awarię |
| Ostrzeżenie o certyfikacie | 30 dni | |
| Alarm o certyfikacie | 7 dni | musi być mniejszy niż próg ostrzeżenia |

Agent sprawdza te wartości **jeszcze raz u siebie**, mimo że polityka
przychodzi po uwierzytelnionym HTTPS: to jego maszynę obciąży odstęp ustawiony
na sekundę. Jeden błędny cel jest pomijany, a pozostałe działają dalej.

## Raport cykliczny

Alarmy idą do dyżurnego w chwili awarii. Raport **Dostępność usług
i certyfikaty** odpowiada na inne pytanie — „jak nam się to wiodło przez
ostatni okres” — i trafia do kogoś, kto nie siedzi przy alarmach. Zawiera
usługi, które nie odpowiadają, cele bez raportów agenta, certyfikaty wymagające
uwagi oraz średnią i najniższą dostępność z ostatnich siedmiu dni.

## Czego to nie zastępuje

To nie jest system monitorowania z prawdziwego zdarzenia. Nie ma tu:

* zależności między usługami („nie alarmuj o aplikacji, gdy padł jej host”),
* okien serwisowych,
* eskalacji dyżurów ani potwierdzania alarmów,
* sprawdzania treści odpowiedzi ani czasów transakcji.

Odpowiadamy na dwa pytania i powiadamiamy, gdy odpowiedź się zmieni.
