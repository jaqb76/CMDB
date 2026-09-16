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

Konta zakłada się i kasuje w jednym miejscu: **administracja → Konta i role**
(`/admin/konta`). Widać tam każde konto niezależnie od tego, skąd bierze się
jego uprawnienie — konta firm, techników bez własnej firmy, audytorów
i superadminów — bo technik przypisany do firm nie należy do żadnej z nich
i pod firmami byłby niewidoczny. Zmiana rodzaju konta unieważnia jego
zalogowane sesje: odebrane prawo ma przestać działać natychmiast, a nie
z wygaśnięciem ciasteczka. Konta z zapisanym czasem pracy nie da się usunąć —
te minuty są podstawą faktur — ale da się je wyłączyć i historia zostaje.

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

## Zgłoszenie z telefonu

Nie każda sprawa przychodzi mailem. **Helpdesk → Zgłoszenia → Nowe zgłoszenie**
zakłada zgłoszenie w imieniu klienta: technik wybiera firmę, wpisuje adres
i nazwisko zgłaszającego, temat i to, z czym klient dzwonił. Dalej droga jest
ta sama co przy poczcie — numer firmy, pierwszy wpis w wątku, ślad w historii
(`zgłoszenie utworzone (telefon) jako BON-12`).

Dwie rzeczy różnią ten formularz od poczty:

**Firmę wybiera technik, nie domena.** Dlatego tylko tutaj trzeba pilnować, żeby
adres nie należał do innej firmy — zgłoszenie z adresem `@klepsydra.pl`
założone firmie Bongo wyglądałoby dobrze do pierwszej odpowiedzi klienta:
ta wróciłaby pocztą i po domenie założyła **drugą** sprawę Klepsydrze. Taki
adres jest odrzucany z nazwą firmy, do której należy. Adres z domeny, której
nikt nie zgłosił (prywatna skrzynka pracownika), przechodzi — to nie pomyłka.

**Adres jest wymagany.** Bez niego nie ma jak odpisać ani wysłać wiadomości
o zakończeniu, a zgłoszenie żyłoby tylko w panelu.

Domyślnie zgłoszenie trafia do technika, który je zakłada, i od razu jest
„W trakcie" — skoro właśnie rozmawia z klientem, sprawa nie czeka w pierwszej
kolumnie na kogoś, kto ją przeczyta. Oba zachowania są checkboxami: dyżurny
przyjmujący telefony może zostawić zgłoszenie nieprzypisane, a potwierdzenie
z numerem odznaczyć, gdy rozmowa wszystko załatwiła.

## Status idzie za pocztą

Kolumna, w której stoi zgłoszenie, ma odpowiadać na jedno pytanie: **czyj jest
teraz ruch**. Dlatego status przestawia się sam razem z korespondencją i nikt
nie musi o tym pamiętać:

| Zdarzenie | Status po |
|---|---|
| technik wysyła odpowiedź do klienta | **Oczekuje** — czekamy na klienta |
| … a zaznaczył „zostaw w trakcie" | **W trakcie** — wiadomość była informacyjna |
| odpowiedź technika nie wyszła (awaria SMTP) | bez zmian — nie czekamy na kogoś, kto nic nie dostał |
| klient odpisuje na zgłoszenie „Oczekuje" | **W trakcie** — piłka wraca do technika |
| klient odpisuje na zgłoszenie **zamknięte** | **W trakcie** — sprawa otwiera się pod tym samym numerem |
| klient odpisuje na „Nowe" albo „W trakcie" | bez zmian |
| automatyczne potwierdzenie przyjęcia | bez zmian — zgłoszenie zostaje **Nowe** |
| komentarz wewnętrzny | tylko „Nowe" → „W trakcie" (ktoś się tym zajął) |

Automaty statusu nie ruszają celowo. Gdyby potwierdzenie przyjęcia ustawiało
„Oczekuje", kolumna „Nowe" byłaby zawsze pusta i nie dałoby się odróżnić sprawy
świeżej od takiej, którą ktoś już obejrzał.

**Zamknięcie zgłoszenia wysyła wiadomość do klienta.** Zamknięte zgłoszenia
znikają z tablicy, więc bez tego maila klient dowiaduje się o końcu sprawy
dopiero wtedy, gdy sam zapyta. Treść bierze się z szablonu w Skrzynce, a technik
może dopisać pod nim kilka zdań podsumowania — pole jest przy zmianie statusu.
Mail idzie tylko przy **przejściu** do zamkniętego: powtórne wybranie tego
samego statusu niczego nie wysyła. Wysyłka nie może przewrócić zamknięcia —
jeśli SMTP nie odpowiada, zgłoszenie i tak się zamyka, a błąd widać przy wpisie
w wątku.

## Kto zgłasza i czym pracuje

Adres nadawcy to nie tylko skrzynka zwrotna — to klucz do kartoteki. Karta
zgłoszenia pokazuje **Zgłaszającego**: rekord osoby z kartoteki tej firmy
(`kategoria = "osoba"`, dopasowanie po polu `email`) razem ze wszystkim, co
firma o nim trzyma — telefon, dział, lokalizacja — plus jego sprzęt. Wszystko
z linkiem w jedno kliknięcie, bez przechodzenia do drugiego modułu i szukania
po nazwisku.

Dopasowanie idzie po adresie, a nie po imieniu i nazwisku: przy dwóch
Kowalskich zgadywanie po nazwisku kończy się podpięciem cudzego laptopa.
Gdy adresu nie ma w kartotece, panel mówi to wprost i podaje link do jej
uzupełnienia — przy kolejnym zgłoszeniu osoba rozpozna się sama.

Linki do CMDB niosą `?tenant=<slug>` firmy zgłoszenia. Technik obsługuje kilka
firm i może mieć przełączoną inną niż ta, w której właśnie czyta zgłoszenie.

**Załączniki i zrzuty ekranu.** Wszystko, co przyszło mailem, ląduje na dysku
i w wątku. Obrazki (PNG, JPEG, GIF, WEBP) pokazują się od razu jako podgląd —
zrzut ekranu z komunikatem błędu bywa całą treścią zgłoszenia, a pobieranie
każdego z osobna robi z jednej sprawy kilkanaście kliknięć. Podgląd dostaje
plik tylko wtedy, gdy jego **początek zgadza się z deklarowanym typem**: typ
deklaruje nadawca, więc sam nagłówek nie wystarcza. SVG zostaje przy pobieraniu
mimo że jest obrazkiem — potrafi nieść skrypty. Reszta plików to zwykłe
pobranie, zawsze jako `application/octet-stream` z `nosniff`.

## Czas pracy

Każdy technik dopisuje **swoje** minuty; jedno zgłoszenie zbiera czas kilku
osób i każda widnieje w rozbiciu osobno. Wpisu nie da się zmienić ani usunąć —
te minuty idą na fakturę, więc są rejestrem zdarzeń, a nie polem, które ktoś
poprawi przed końcem miesiąca. Pomyłkę prostuje kolejny wpis.

**Pomyłkę prostuje wpis na minus.** Skoro wpisu nie da się poprawić ani
skasować, jedyną drogą jest wpisanie `-15` — system odejmie 15 minut. Rejestr
zostaje rejestrem: widać i błąd, i korektę, zamiast cichej podmiany liczby.
Dlatego korekta **wymaga opisu** (na fakturze ma być widać, skąd się wzięła)
i nie może zejść poniżej zera — ujemny czas pracy nic nie znaczy. W bazie
pilnuje tego warunek `minuty <> 0`; że suma nie spadnie poniżej zera, pilnuje
`dodaj_czas`, bo jednym warunkiem na wierszu tego sprawdzić się nie da.

Trzy raporty to jedno zestawienie oglądane z trzech stron:

- **Podsumowanie** — wszyscy technicy za miesiąc w jednej tabeli: wiersz to
  technik, kolumna to firma, ostatnia kolumna jego suma. Odpowiada na „ile kto
  zrobił w tym miesiącu" bez otwierania raportu po jednym. Superadmin widzi
  wszystkich; technik swój wiersz i tylko firmy, które obsługuje — suma godzin
  kolegi nie jest jego sprawą.

Dwa pozostałe schodzą do pojedynczych zgłoszeń:

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

**Wiadomość o zakończeniu** ma własny włącznik i własny szablon — to osobna
decyzja od potwierdzenia przyjęcia, bo jedna firma chce obu, inna tylko
domknięcia. Oba szablony podstawiają `{numer}` i `{temat}` i oba są wpisami
w wątku, nie zdarzeniami systemowymi: to są wiadomości, które wyszły na zewnątrz.

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
