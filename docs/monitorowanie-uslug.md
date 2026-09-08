# Monitorowanie usług i certyfikatów SSL

Ewidencja odpowiada na pytanie „co mamy”. Ten moduł odpowiada na dwa inne:
**czy to działa** i **do kiedy ważny jest certyfikat**. Cel podaje się adresem
IP albo nazwą hosta — nie musi mieć wpisu w ewidencji.

## Dlaczego sprawdza serwer, a nie agent

Agent widzi maszynę od środka i nie wie, czy usługa odpowiada komukolwiek
innemu. Certyfikat, który wygasł, wygląda z maszyny dokładnie tak samo jak
ważny — problem widać dopiero z drugiej strony połączenia. Dlatego próbę
nawiązuje serwer CMDB: mierzymy to, co zobaczy użytkownik usługi.

Konsekwencja jest taka, że **serwer CMDB musi widzieć monitorowany adres**.
Usługa w zamkniętym segmencie sieci, do którego serwer nie ma dostępu, będzie
raportowana jako niedostępna — i będzie to prawda z jego punktu widzenia,
choć nie z punktu widzenia jej użytkowników.

## Cztery sposoby sprawdzenia

| Protokół | Co sprawdza | Kiedy używać |
|---|---|---|
| `tcp` | port przyjmuje połączenia | usługi bez TLS i bez HTTP: SQL, SMB, DNS po TCP |
| `tls` | połączenie szyfrowane **i certyfikat** | SMTPS, IMAPS, LDAPS, RDP — wszystko, co nie mówi po HTTP |
| `http` | kod odpowiedzi serwera WWW | usługi wewnętrzne bez szyfrowania |
| `https` | kod odpowiedzi **i certyfikat** | strony i API |

Otwarty port nie znaczy, że aplikacja za nim żyje — dlatego przy HTTP(S)
pytamy dodatkowo o kod odpowiedzi. Domyślnie akceptujemy każdy kod poniżej
400; można wskazać konkretny, gdy poprawną odpowiedzią jest np. `401`
na stronie wymagającej logowania.

Ścieżka jest ustawieniem celu, bo strona główna bywa przekierowaniem, a
`/health` mówi o aplikacji znacznie więcej. Żądanie idzie **bez podążania za
przekierowaniem**: przekierowanie prowadzi pod adres, którego nikt nie
sprawdził. Treść odpowiedzi nie jest czytana — pytanie brzmi „czy aplikacja
odpowiada”, a nie „co odpowiada”.

## Dostępność i certyfikat to dwie osobne oceny

Usługa potrafi odpowiadać z certyfikatem wygasającym jutro i potrafi milczeć
z certyfikatem ważnym rok. Zlanie tych dwóch rzeczy w jeden stan kończy się
tym, że jedna sprawa przykrywa drugą — a powiadomienia o nich trafiają do
kogo innego i w innym czasie.

* **Stan celu** widoczny na liście to *gorsza* z dwóch ocen.
* **Procent dostępności** liczy się wyłącznie z dostępności — certyfikat,
  który kończy się za trzy tygodnie, nie obniża go ani o promil.
* **Powiadomienia** idą osobno dla każdej ze spraw.

Stan `nieznany` nie jest tym samym co `ok`. Cel dopiero dodany albo wyłączony
nie został jeszcze zmierzony, a zielona kropka przy czymś, czego nikt nie
sprawdził, jest gorsza niż jawne przyznanie się do niewiedzy. Z tego samego
powodu brak pomiarów w oknie czasu pokazujemy jako „brak danych”, a nie 100%.

## Kiedy przychodzi wiadomość

Zapisujemy stan, **o którym powiadomiliśmy**, a nie sam fakt wysyłki. Dzięki
temu wiadomość idzie przy każdej zmianie i tylko przy zmianie: awaria trwająca
tydzień nie zamienia się w tysiąc wiadomości, a jej koniec zostaje zauważony.

**Dostępność**

* usługa nie odpowiada przez ustawioną liczbę prób → *Usługa niedostępna*,
* usługa znowu odpowiada → *Usługa działa*.

Jedna zgubiona odpowiedź zdarza się w każdej sieci, więc pierwszy błąd daje
tylko stan „ostrzeżenie” i nie wysyła niczego. Alarm idzie dopiero po
potwierdzeniu (domyślnie druga nieudana próba z rzędu).

**Certyfikat**

* przekroczony próg ostrzeżenia (domyślnie 30 dni) → *Certyfikat wygasa*,
* przekroczony próg alarmu (domyślnie 7 dni) → *Certyfikat wygasa*,
* po terminie → *Certyfikat wygasł*,
* łańcuch przestał się weryfikować → *Certyfikat niezaufany*,
* problem ustąpił albo certyfikat odnowiono → *Certyfikat w porządku*.

Same progi to za mało, a sam stan to za mało: w „ostrzeżeniu” certyfikat stoi
trzy tygodnie, więc bez drabinki progów nikt nie usłyszałby o nim ponownie
przed samym końcem. Dlatego pilnujemy obu rzeczy naraz.

Odnowiony certyfikat ma inny odcisk SHA-256 — to zeruje progi, więc przed
następnym końcem ważności ostrzeżenie przyjdzie ponownie. Przychodzi też
potwierdzenie, że odnowienie zadziałało: ktoś, kto o nie prosił, chce
wiedzieć, że zostało zrobione.

**Wysyłka, która się nie udała, nie przesuwa znacznika.** Powiadomienie,
które nie doszło, nie jest powiadomieniem, więc próba zostanie powtórzona
przy kolejnym sprawdzeniu, a przyczyna widnieje na stronie celu.

Wiadomości idą przez **serwer SMTP tej firmy**, ustawiony na stronie
*Raporty → Poczta wychodząca* — tak samo jak raporty i z tego samego powodu.

## Certyfikat własnego urzędu firmy

Usługi wewnętrzne mają zwykle certyfikat urzędu, którego serwer CMDB nie zna.
Weryfikacja łańcucha się wtedy nie powiedzie — i jest to stan normalny, a nie
awaria. Wyłącz przy takim celu *Wymagaj zaufanego łańcucha*.

**Data ważności jest sprawdzana zawsze**, niezależnie od tego ustawienia.
Certyfikat zdejmujemy z surowych bajtów połączenia, a nie ze słownika
`getpeercert()` — tamten jest pusty, gdy połączenie zestawiono bez
weryfikacji, czyli dokładnie wtedy, gdy certyfikat interesuje nas najbardziej.

Gdy weryfikacja jest włączona, a łańcuch jej nie przechodzi, robimy drugie
połączenie bez weryfikacji, żeby mimo wszystko zobaczyć certyfikat. Kosztuje
to dodatkowe połączenie tylko wtedy, gdy naprawdę coś jest nie tak.

## Monitorowanie po adresie IP

Serwer obsługujący kilka domen bez wskazówki poda certyfikat pierwszej
z brzegu. Pole **nazwa w certyfikacie** wysyłamy w SNI i to ją sprawdzamy —
wypełnij je, gdy cel podano adresem liczbowym.

## Adresy, pod które serwer nie pójdzie

Cel wpisuje administrator firmy, a połączenie nawiązuje serwer CMDB wspólny
dla wszystkich firm. Odmawiamy więc adresów, które znaczą dla serwera co
innego niż dla wpisującego:

| Adres | Dlaczego |
|---|---|
| `169.254.0.0/16`, `fe80::/10` | pod `169.254.169.254` odpowiada usługa metadanych chmury |
| `127.0.0.0/8`, `::1` | pętla zwrotna wskazuje na sam serwer CMDB |
| `0.0.0.0`, `::` | adres nieokreślony |
| multicast, adresy zarezerwowane | nie są usługą |

Sprawdzamy adres **po rozwiązaniu nazwy** i łączymy się dokładnie z tym, co
sprawdziliśmy — inaczej między sprawdzeniem a połączeniem odpowiedź DNS
mogłaby się zmienić. Adres IPv4 zapisany jako IPv6 (`::ffff:169.254.169.254`)
to ten sam adres i podlega tej samej odmowie.

Blokadę pętli zwrotnej można znieść świadomie — `CMDB_MONITORING_ALLOW_LOOPBACK=true`
— na instalacji, która ma pilnować usług stojących na tej samej maszynie.

## Konfiguracja celu

| Pole | Domyślnie | Znaczenie |
|---|---|---|
| Odstęp sprawdzeń | 300 s | minimum 60 s — częściej to już generator ruchu |
| Limit czasu | 10 s | musi być krótszy niż odstęp |
| Prób przed alarmem | 2 | ile kolejnych błędów oznacza awarię |
| Ostrzeżenie o certyfikacie | 30 dni | |
| Alarm o certyfikacie | 7 dni | musi być mniejszy niż próg ostrzeżenia |
| Powiadomienia | włączone | wymagają co najmniej jednego adresu e-mail |

Zmiana adresu, portu, protokołu albo ścieżki **zeruje stan i historię
powiadomień**: pod tą samą nazwą stoi wtedy inna usługa, a alarm o awarii
czegoś, czego już nie monitorujemy, byłby fałszywy. Historia pomiarów zostaje.

Wyłączenie celu też ustawia stan na „nieznany”: wyłączony cel nie jest
sprawny — jest niesprawdzany.

## Ustawienia serwera

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_MONITORING_ENABLED` | `true` | wyłączenie zostawia konfigurację w bazie i nic nie kasuje — przydaje się na instalacji zapasowej, która nie ma alarmować równolegle |
| `CMDB_MONITORING_TICK_SECONDS` | `60` | jak często budzi się pętla; dokładność dotrzymania odstępów celów |
| `CMDB_MONITORING_WORKERS` | `8` | ile sprawdzeń naraz |
| `CMDB_MONITORING_MAX_TARGETS` | `200` | limit celów na firmę |
| `CMDB_MONITORING_HISTORY_DAYS` | `30` | retencja pomiarów |
| `CMDB_MONITORING_ALLOW_LOOPBACK` | `false` | patrz wyżej |

Serwer produkcyjny działa w kilku procesach roboczych i każdy uruchamia ten
sam kod. Uzgadniamy się **blokadą doradczą PostgreSQL** pobieraną bez
czekania: proces, który jej nie dostanie, pomija ten obieg. Bez tego każdy
proces sprawdzałby te same cele i wysyłał te same alarmy — a alarm powtórzony
cztery razy uczy ludzi, że alarmy można ignorować.

Sprawdzenia idą w wątkach (czekanie na sieć, nie na procesor), ale wynik
zapisuje wątek główny: sesja SQLAlchemy nie jest bezpieczna w wątkach,
a równoległy zapis niczego by nie przyspieszył.

## Raport cykliczny

Alarmy idą do dyżurnego w chwili awarii. Raport **Dostępność usług
i certyfikaty** odpowiada na inne pytanie — „jak nam się to wiodło przez
ostatni okres” — i trafia do kogoś, kto nie siedzi przy alarmach. Zawiera
usługi, które nie odpowiadają, certyfikaty wymagające uwagi oraz średnią
i najniższą dostępność z ostatnich siedmiu dni. Dodaje się go tak samo jak
pozostałe raporty, na stronie *Raporty*.

## Czego to nie zastępuje

To nie jest system monitorowania z prawdziwego zdarzenia. Nie ma tu:

* zależności między usługami („nie alarmuj o aplikacji, gdy padł jej host”),
* okien serwisowych,
* eskalacji dyżurów ani potwierdzania alarmów,
* sprawdzania treści odpowiedzi ani czasów transakcji.

Odpowiadamy na dwa pytania i powiadamiamy, gdy odpowiedź się zmieni.
