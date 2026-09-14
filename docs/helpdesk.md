# Helpdesk — zgłoszenia klientów

Moduł obsługi zgłoszeń wbudowany w CMDB. Klienci piszą maile pod jeden adres,
system zakłada z nich zgłoszenia, technicy odpowiadają z panelu i rejestrują
czas pracy, a raport miesięczny mówi, ile czasu poszło na którą firmę.

```
   klient                    skrzynka helpdesku                 panel
  ┌──────────────┐          ┌──────────────────────┐        ┌──────────────┐
  │ jan@bongo.pl │  e-mail  │ IMAP: odbiera        │        │ tablica      │
  │              │ ───────► │ domena → firma       │ ─────► │ karta sprawy │
  │              │ ◄─────── │ firma  → numer BON-1 │        │ czas pracy   │
  └──────────────┘   SMTP   │ SMTP: odpowiada      │        │ raporty      │
                            └──────────────────────┘        └──────────────┘
```

## Skąd system wie, czyje jest zgłoszenie

Skrzynka jest **jedna i wspólna** dla wszystkich klientów, więc każdy mail
trzeba zakwalifikować. Kolejność pytań nie jest dowolna:

1. **Czy to odpowiedź w istniejącym wątku?** Wtedy firma i numer są już znane.
   Rozpoznajemy trzema drogami, od najpewniejszej: nagłówki `In-Reply-To`
   i `References`, numer w temacie (`[BON-123]`), a na końcu ten sam nadawca
   z tym samym tematem w sprawie otwartej w ostatnich 7 dniach. Ostatnia jest
   najsłabsza i dlatego jest ostatnia — ale bez niej klient, który skasuje
   numer z tematu i odpowie z telefonu, zakłada duplikat.
2. **Czy domena nadawcy wskazuje firmę?** Wtedy powstaje nowe zgłoszenie,
   a numer nadaje firma.
3. **Nie wskazuje** — wiadomość czeka w „Nierozpoznanych" i **nikt nie
   dostaje odpowiedzi**. Cisza jest celowa: odpowiedź nieznanemu nadawcy
   potwierdza mu, że adres istnieje i jest czytany.

Wątek wygrywa z domeną nadawcy: odpowiedź z domeny innej firmy zostaje w swoim
zgłoszeniu, bo rozpad wątku na dwie sprawy jest gorszy niż jedna wiadomość
w cudzej firmie — i tak widoczna tylko dla jej techników.

**Autoodpowiedzi nie zakładają zgłoszeń.** „Jestem na urlopie do 15 września"
z domeny klienta trafia do nierozpoznanych; rozpoznajemy je po `Auto-Submitted`,
`Precedence` i typowych nadawcach (`mailer-daemon@`, `noreply@`). Bez tego
tablica zapełnia się sprawami, których nikt nie zgłaszał, a przy włączonym
potwierdzeniu dwa automaty piszą do siebie w kółko.

## Numery

Numeracja idzie **osobno dla każdej firmy**, a skrót firmy jest częścią numeru:
`BON-123` (Bongo), `KLE-88` (Klepsydra), `LKS-14` (ŁKS). Bez skrótu „123" dwóch
firm byłoby tym samym napisem w temacie maila i odpowiedź klienta trafiałaby do
cudzego zgłoszenia.

Numer powstaje w jednym miejscu (`services/helpdesk.nadaj_numer`) jednym
`UPDATE ... RETURNING`, więc dwa procesy odbierające pocztę równocześnie nie
nadadzą tego samego numeru. Licznik trzyma **ostatni nadany** numer, nie liczbę
zgłoszeń: numer, który poszedł już w mailu do klienta, nie wróci w innej
sprawie nawet po skasowaniu tamtej.

## Kto co może

| Konto | Zakres |
|---|---|
| **superadmin** | cały helpdesk: skrzynka, firmy, domeny, nierozpoznana poczta, nadawanie dostępów |
| **technik** | zgłoszenia firm, które dostał — i te same firmy w CMDB, z prawami ich operatora |
| **administrator / viewer firmy** | jak dotąd w CMDB; helpdesku nie widzi |

Technik nie należy do żadnej firmy (`portal_users.tenant_id` puste) — obsługuje
tyle firm, ile mu przypisano w `helpdesk_dostepy`. Dostęp nadaje **wyłącznie
superadmin**, bo przypisanie firmy otwiera technikowi także jej kartotekę
w CMDB: zgłoszenie „agent nie wysyła danych" kończy się wdrożeniem innej wersji
agenta, więc technik pracuje w firmie z prawami jej operatora. Publikowanie
wydań agenta zostaje u superadmina — kto wgrywa plik, decyduje o tym, co
uruchomi się na maszynach klientów.

Odebranie dostępu nie kasuje historii: zgłoszenia i czas pracy zostają, technik
po prostu przestaje je widzieć.

## Wątek zgłoszenia

Wiadomości klienta, odpowiedzi techników, komentarze wewnętrzne i zdarzenia
(przypisanie, zmiana statusu, dopisany czas) leżą **w jednej tabeli** i tworzą
jedną chronologiczną rozmowę. Trzy osobne listy trzeba by scalać przy każdym
wyświetleniu, a przy każdym scalaniu można pomylić kolejność.

O tym, czy treść wyjdzie na zewnątrz, decyduje jedno pole `rodzaj`, sprawdzane
w jednym miejscu — w funkcji wysyłki. Komentarz wewnętrzny nie ma jak wyjść,
nawet gdyby jakiś widok o tym zapomniał. W panelu jest celowo brzydszy od
reszty: żółte tło, kropkowana ramka, znacznik „tylko dla techników".

**Odpowiedź zapisuje się przed wysyłką.** Gdyby najpierw szedł SMTP, awaria
kasowałaby treść, którą technik właśnie napisał; teraz zostaje w wątku z opisem
błędu i da się ją powtórzyć.

**DW**: kopia z ostatniej wiadomości klienta wraca w odpowiedzi — bez adresu
helpdesku (wróciłby do nas) i bez samego zgłaszającego (dostałby dwa razy).

**Załączniki** klienta lądują na dysku (`CMDB_HELPDESK_DIR`) i w wątku. Nazwa
pliku od klienta jest wyłącznie opisem — ścieżkę budujemy z identyfikatorów,
więc `..\..\etc\passwd` nie dotknie systemu plików. Pliki serwujemy zawsze jako
pobranie, nigdy do wyświetlenia w przeglądarce.

## Czas pracy

Każdy technik dopisuje **swoje** minuty; jedno zgłoszenie zbiera czas kilku
osób i każda widnieje w rozbiciu osobno. Wpisu nie da się zmienić ani usunąć —
te minuty idą na fakturę, więc są rejestrem zdarzeń, a nie polem, które ktoś
poprawi przed końcem miesiąca. Pomyłkę prostuje kolejny wpis.

Dwa raporty to jedno zestawienie oglądane z dwóch stron:

- **Firma → technik → zgłoszenie** — ile czasu poszło na tę firmę i kto go zużył,
- **Technik → firma → zgłoszenie** — ile czasu ten technik oddał każdej firmie.

Oba eksportują się do **CSV** (średnik i BOM, żeby polski Excel nie wrzucił
wiersza do jednej kolumny) i **XLSX**. W eksporcie minuty stoją obok czasu
opisowego: pierwsze da się zsumować w arkuszu, drugie da się przeczytać.

## Sprzęt i karta napraw

Zgłoszenie wiąże się ze sprzętem tabelą łączącą, nie polem: jedna awaria bywa
o dwóch urządzeniach, a jedno urządzenie zbiera zgłoszenia przez całe życie —
i to jest jego karta napraw (`zgloszenia_sprzetu`, `czas_sprzetu`).

Domyślne powiązanie idzie **po zgłaszającym**: adres nadawcy wskazuje osobę
w kartotece firmy (słownik osób ma e-mail jako pole wymagane), a osoba —
sprzęt, przy którym siedzi. Szukamy po użytkowniku, nie po opiekunie:
opiekunem laptopa prezesa jest ktoś z IT, a awarię zgłasza prezes. Podpinamy
sami tylko wtedy, gdy kandydat jest jeden; przy kilku wybiera technik, bo
doklejenie monitora i telefonu do zgłoszenia o VPN zaśmieciłoby ich karty
napraw.

## Nierozpoznana poczta

Wszystko, czego domena nie wskazuje firmy — także poczta z własnej domeny, bo
skrzynka służy wyłącznie helpdeskowi. Operator decyduje: utwórz zgłoszenie
(wskazując firmę), zignoruj, albo zignoruj całą domenę.

Reguła „ignoruj domenę" jest **widoczna**: tabela pokazuje, kto i kiedy ją
założył oraz ile poczty przechwyciła. Reguła odsuwa wiadomości od listy, ale
ich nie kasuje — lądują w zignorowanych. Domeny należącej do firmy nie da się
zignorować: jedno kliknięcie zamykałoby drogę zgłoszeniom klienta po cichu.

Helpdesk **nie kasuje niczego automatycznie**. Termin usuwania zignorowanych
i spamu ustawia człowiek w ustawieniach skrzynki; domyślnie „nigdy". Dotyczy
wyłącznie poczty nierozpoznanej — zgłoszeń i ich historii nie usuwa nigdy.

## Skrzynka

Jeden adres (np. `helpdesk@mojadomena.pl`), ten sam w odbiorze i wysyłce, więc
odpowiedzi wracają tam, skąd wyszły. Hasła IMAP i SMTP są szyfrowane kluczem
serwera (`services/sekrety`), bo oba protokoły wymagają ich w postaci jawnej.

Pętla odbioru chodzi we własnym rytmie (domyślnie co 2 minuty) i bierze własną
blokadę doradczą Postgresa — bez niej dwa procesy robocze pobrałyby tę samą
wiadomość i założyły dwa zgłoszenia. Każda wiadomość ma własną transakcję,
a oznaczenie w skrzynce idzie **po** zapisie w bazie: odwrotna kolejność gubi
zgłoszenia przy awarii w środku. Jedna wadliwa wiadomość nie zatrzymuje
reszty — wraca w kolejnym obiegu i zostaje rozpoznana po `Message-ID`.

Wiadomości nie kasujemy nigdy: helpdesk czyta cudzą skrzynkę i zmienia w niej
tylko znacznik przeczytania albo folder. Błąd połączenia ląduje w ustawieniach
i widać go w panelu — skrzynka, która przestała się logować, wygląda inaczej
niż skrzynka, do której nikt nie napisał.

**Automatyczne potwierdzenie** idzie tylko wtedy, gdy domena nadawcy wskazuje
firmę, wiadomość zakłada nowe zgłoszenie i ma treść. Nie idzie na odpowiedzi
w wątku, na autoodpowiedzi ani do nieznanych domen. Jest zwykłym wpisem „do
klienta" w wątku, więc technik widzi dokładnie to, co klient dostał.

## Konfiguracja

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_HELPDESK_DIR` | `./helpdesk` | katalog załączników zgłoszeń |
| `CMDB_HELPDESK_ZALACZNIK_MB` | `25` | górna granica jednego załącznika |

Resztę (serwery, hasła, interwał, potwierdzenie, przechowywanie) ustawia się
w panelu: **Helpdesk → Skrzynka**. Katalog załączników jest osobny od katalogu
wydań agenta — te pliki przysyła klient, więc nie mogą leżeć obok plików, które
serwer wydaje jako zaufane.

## Co jeszcze nie jest zrobione

- **AI**: rozpoznawanie firmy z treści maila, kategoryzacja spamu, sugerowany
  technik, streszczenie wątku. Model ma na to miejsce (`ocena_ai`,
  `ocena_pewnosc` w nierozpoznanych), ale helpdesk działa bez tego w całości.
- **Timer start/stop** zamiast wpisywania minut.
- **Aplikacja mobilna** rozpoznaje firmę po `portal_users.tenant_id`, więc
  technik helpdesku (konto bez własnej firmy) dostanie tam 404.
- **SLA, priorytety, terminy reakcji.**
