# Raporty

Każda firma konfiguruje raporty samodzielnie: własny serwer SMTP, własnych
adresatów, własną częstotliwość. Raporty idą **przez serwer tej firmy**, a nie
przez wspólną skrzynkę operatora — inaczej wiadomości o jednej organizacji
przechodziłyby przez infrastrukturę, do której ma dostęp inna, a cały system
jest zbudowany wokół tego, że firmy się nie widzą.

## Trzy rodzaje

| Rodzaj | Odpowiada na pytanie |
|---|---|
| **Podatności** | co wymaga łatania i jak pilnie |
| **Inwentaryzacja sprzętu** | co w ogóle mamy i w jakim stanie |
| **Gwarancje i wsparcie** | czemu kończy się wsparcie |
| **Wykorzystanie zasobów** | od czego zacząć — pierwsza dziesiątka w każdej kategorii |

### Wykorzystanie zasobów

Siedem zestawień, każde po najwyżej dziesięć pozycji: zajęcie pamięci,
najpełniejsze dyski, obciążenie procesora, podatności do zrobienia, brakujące
aktualizacje, najdłużej bez restartu i najwięcej kont administratorów. Osobno
maszyny bez kontaktu — dla nich dane są nieaktualne, więc pozostałe zestawienia
ich nie opisują.

Dziesięć pozycji to świadomy limit: lista, która nie mieści się na ekranie,
przestaje być listą rzeczy do zrobienia.

**Stan kontra tempo.** Zajętość pamięci i dysku to *stan* — pomiar z dowolnej
chwili jest o nich prawdziwy. Obciążenie procesora to *tempo*, a agent
raportuje raz na kilka godzin. Dlatego na Linuksie bierzemy **średnią z 15
minut** z `/proc/loadavg`, znormalizowaną przez liczbę rdzeni; na Windows
— trzy próbki licznika wydajności. Każda pozycja mówi, na czym się opiera,
bo w jednej kolumnie średnia i próbka wyglądają tak samo.

Licznik na Windows czytamy przez CIM, a nie przez `Get-Counter` ze ścieżką
opisaną po angielsku: **nazwy liczników wydajności są tłumaczone**, więc
angielska ścieżka zawodzi na każdym systemie w innym języku.

Dysk pokazujemy jako **najbardziej zapełniony wolumen**, a nie średnią —
maszyna z pełnym dyskiem systemowym i pustym dyskiem danych ma problem,
którego średnia nie widzi.

Każdy ma podgląd w panelu (`/raporty/podgląd/<rodzaj>`) — ta sama treść, która
pójdzie pocztą. Podgląd różniący się od wysyłki byłby gorszy niż jego brak.

## Wykresy

Paski są zbudowane z komórek tabeli z tłem, a nie z obrazków ani biblioteki
JavaScript. Powód jest prozaiczny: klienty poczty nie uruchamiają skryptów,
większość wycina SVG, a obrazek trzeba by dołączać jako załącznik i liczyć na
to, że odbiorca zgodzi się go wyświetlić. Tabela z tłem renderuje się wszędzie
— w Outlooku, Gmailu i w przeglądarce.

Szerokość paska liczy się względem **największej** pozycji, nie sumy: przy
kilkunastu kategoriach paski liczone od sumy byłyby nieczytelnie krótkie.

## Widok na stronie i wybór kolumn

Raport otwiera się **w panelu w całości** (`Raporty → Otwórz`), a nie tylko
jako podgląd. Ta sama treść idzie pocztą — widok różniący się od wysyłki byłby
gorszy niż jego brak.

Pod nagłówkiem jest wybór kolumn tabeli, pogrupowany tematycznie:

| Grupa | Przykłady |
|---|---|
| Identyfikacja | nazwa, FQDN, adres IP, opiekun, rola |
| System | system, wersja, jądro, architektura, wersja agenta, ostatni kontakt |
| Sprzęt | producent, model, numer seryjny, procesor, rdzenie, pamięć, dysk, wirtualizacja |
| Zakup | dostawca, data zakupu, gwarancja, cena, faktura, umowa wsparcia |
| Podatności | poważne CVE, CVE do zrobienia, CVE bez poprawki, brakujące aktualizacje |

Wybór zapisuje się **przy definicji raportu**, więc to, co widzisz w panelu,
jest tym, co dostaną adresaci.

### Dlaczego niektóre kolumny są oznaczone jako „wolne"

Każda kolumna zna swoje źródło:

* **pole maszyny** — dostępne od ręki,
* **z raportu agenta** — wymaga odczytania jego treści,
* **podatności** — wymaga zestawienia pakietów każdej maszyny z kanałem CVE.

Liczymy wyłącznie to, co zaznaczono. Kolumna z liczbą podatności przy stu
maszynach to sto dopasowań — bez tego rozróżnienia płacilibyśmy za nią także
wtedy, gdy nikt jej nie chce.

## Poczta

Ustawienia w zakładce **Raporty**. Hasło jest szyfrowane kluczem serwera —
musi być odwracalne, bo SMTP wymaga podania go przy każdym połączeniu, więc
skrót tu nie wystarczy.

**Konsekwencja:** zmiana `CMDB_SECRET_KEY` unieważnia zapisane hasła SMTP
i trzeba je wpisać ponownie. To ten sam kompromis co przy ciasteczkach sesji.
Serwer mówi wtedy wprost „nie mogę odczytać zapisanego hasła", zamiast
próbować połączenia i zwracać mylący błąd sieciowy.

Puste pole hasła przy edycji zostawia poprzednie — inaczej każda zmiana portu
wymagałaby wpisywania hasła od nowa, co kończy się trzymaniem go w notatniku obok.

Przycisk **Sprawdź** wysyła wiadomość próbną. Poprawnie wyglądająca
konfiguracja i działająca konfiguracja to dwie różne rzeczy.

## Harmonogram

Raport idzie, gdy od ostatniej wysyłki minął okres z definicji. Raport **nigdy
niewysłany idzie od razu** — inaczej po dodaniu definicji trzeba by czekać cały
okres, nie wiedząc, czy cokolwiek działa.

Wysyłkę obsługuje zadanie w tle, sprawdzające co 15 minut. Serwer produkcyjny
działa w kilku procesach roboczych, więc uzgadniają się blokadą doradczą
Postgresa pobieraną bez czekania: proces, który jej nie dostanie, pomija ten
obieg. Bez tego raport poszedłby tylokrotnie, ile jest procesów — a wiadomość
wysłana cztery razy jest gorsza niż niewysłana wcale, bo uczy odbiorców
ignorowania raportów.

Błąd jednego raportu **nie zatrzymuje pozostałych**: trafia do definicji
i jest widoczny w panelu przy „ostatniej wysyłce". Wyjątek przerwałby cały
przebieg.

## Zakup i gwarancja

Zakładka **Zakup i gwarancja** na karcie maszyny. Tych danych agent nie ma
skąd znać — data zakupu, numer faktury czy warunki umowy nie wynikają
z niczego, co da się odczytać z maszyny.

Data końca gwarancji zasila raport o wygasającym wsparciu, z podziałem na:
po terminie, kończy się w najbliższych 90 dniach, objęte wsparciem oraz
**bez wpisanej daty**. Ta ostatnia kategoria jest osobna celowo: brak daty nie
znaczy, że gwarancji nie ma — znaczy tylko, że nikt jej nie uzupełnił.
