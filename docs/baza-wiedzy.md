# Baza wiedzy

Procedury, runbooki incydentów, opisy rozwiązań i dokumentacja architektury —
w układzie znanym z Confluence (przestrzenie, drzewo stron), ale **powiązane
z maszynami z CMDB**. Artykuł wskazuje, których maszyn dotyczy, a pojawia się
sam na karcie każdej pasującej maszyny, w zakładce „Wiedza”.

Moduł zastępuje „Bazę wiedzy” z portalu hubzso. Wymagania i znane problemy
tamtej implementacji opisuje dokument założeń; tutaj — jak są spełnione.
Wygląd uzgodniono na makiecie [`makieta-baza-wiedzy.html`](makieta-baza-wiedzy.html).

```
  przestrzeń „Bazy danych”                karta maszyny db02
  ┌─────────────────────────────┐        ┌──────────────────────────────┐
  │ SQL Server                  │        │ … │ Uwagi │ Wiedza (3) │ …   │
  │  └ Runbooki                 │        │ Awaria węzła SQL …           │
  │     └ Awaria węzła SQL …    │ ─────► │   nazwa hosta db0*,          │
  │        dotyczy:             │        │   oprogramowanie SQL Server  │
  │          host  db0*         │        │ Zanik zasilania …            │
  │          oprogr. SQL Server │        │   lokalizacja Serwerownia A  │
  └─────────────────────────────┘        └──────────────────────────────┘
```

## Czego dotyczy artykuł

Są dwa sposoby i **wystarczy jeden**:

| Sposób | Jak pasuje |
|---|---|
| **Systemy** (lista nazw, jak w hubzso) | nazwa równa nazwie hosta albo FQDN maszyny, bez względu na wielkość liter; nazwa spoza CMDB (`system-a`) zostaje etykietą i działa w filtrze listy |
| **Nazwa hosta** | wzorzec z `*` i `?` na hostname albo FQDN: `db0*`, `*.example.local` |
| **System operacyjny** | fragment rodziny, nazwy i wersji systemu: `Windows Server 2019` |
| **Oprogramowanie** | fragment nazwy pakietu z ostatniego raportu agenta: `SQL Server 2019` |
| **Rodzaj sprzętu** | klucz albo etykieta rodzaju: `drukarka` / `Drukarka / skaner` |
| **Lokalizacja** | fragment nazwy lokalizacji ze słownika firmy |
| **Etykieta zasobu** | wzorzec na etykietach zasobu |

Pusty warunek jest pomijany — przy regule „wystarczy jeden” pasowałby do
wszystkiego. Z tego samego powodu nie ma warunków przeczących („nie pasuje”)
i nie da się zapisać warunku złożonego z samych `*`. Znaki `%` i `_` wpisane
w wartość są traktowane dosłownie.

Dopasowanie liczy się **przy odczycie**, więc nowa maszyna pasuje od
pierwszego raportu, bez zadania w tle. Całe dopasowanie to **jedno zapytanie
SQL**, a warunki są w nim danymi (kolumna `dotyczy`), nie sklejanym tekstem
zapytania. Z tej samej logiki korzystają:

* zakładka „Wiedza” na karcie maszyny (z powodem dopasowania),
* licznik przy nazwie na liście zasobów — jedno zapytanie dla całej listy,
* panel „Dotyczy” na stronie artykułu,
* licznik pasujących maszyn w edytorze, przed zapisem,
* trasa „nazwy → linki” (niżej).

Artykuł nigdy nie dopasuje maszyny z innej firmy: warunek `tenant_id` jest
w każdym złączeniu.

## Treść

Markdown renderowany przez serwer z **wyłączonym surowym HTML** — znacznik
wpisany w treść, tytuł czy tag zawsze wychodzi jako tekst, a odnośniki
`javascript:` są odrzucane. Rozszerzenia:

```
:::uwaga Zanim zaczniesz
Przełączenie ręczne tylko wtedy, gdy automatyczne nie zadziałało.
:::

Test-NetConnection {hostname} -Port 5022
```

* panele `info`, `uwaga`, `stop`, `ok` — tylko na najwyższym poziomie
  dokumentu i poza blokami kodu,
* pola maszyny `{hostname}`, `{fqdn}`, `{ip}`, `{system}`, `{lokalizacja}` —
  artykuł otwarty z karty maszyny (albo z wyborem „Czytaj w kontekście
  maszyny”) podstawia jej dane; podstawienie działa w tekście i w blokach
  kodu, nigdy wewnątrz atrybutów HTML.

Edytor to pole Markdown z paskiem przycisków i podglądem renderowanym przez
serwer (ten sam kod co strona artykułu). Działa bez bibliotek z CDN —
portal pracuje także w sieciach bez internetu.

## Model danych

| Tabela | Zawartość |
|---|---|
| `wiedza_przestrzenie` | przestrzenie firmy (nazwa unikalna bez względu na wielkość liter) |
| `wiedza_artykuly` | **stan bieżący** w kolumnach z limitami: tytuł 200, streszczenie 500, treść, kategoria (`CHECK`), warunki `dotyczy` (JSONB), właściciel, przegląd, „na dyżur”, numer wersji, soft delete, wektor wyszukiwania (GIN) |
| `wiedza_slownik` + `wiedza_artykuly_slownik` | tagi i systemy; słownik tylko rośnie — usunięcie artykułu nie kasuje podpowiedzi |
| `wiedza_zalaczniki` | metadane plików; pliki na dysku w `CMDB_WIEDZA_DIR/zalaczniki/<artykuł>/` pod nazwą nadaną przez serwer |
| `wiedza_wersje` | historia: `(artykuł, wersja)`, kto, kiedy, operacja, opis zmiany, `zmiany` = `{pole: {old, new}}` tylko dla zmienionych pól, `tresc_przed` |

Kategorie są przejęte z hubzso razem z kodami (`procedures`,
`troubleshooting`, `runbooks`, `architecture`, `monitoring`, `security`,
`deployment`), więc import starych artykułów nie wymaga tłumaczenia.
Środowiska i priorytetu świadomie nie ma.

### Historia

* Zapis bez zmian **nie tworzy wersji** (i nie zmienia daty modyfikacji).
* Zmiana jednego pola tworzy **jedną wersję z tym jednym polem**.
* Przy zmianie treści zapisujemy **raz** tekst sprzed zmiany (`tresc_przed`),
  zamiast pary old/new całej treści. Treść wersji N to `tresc_przed`
  najbliższej późniejszej wersji, która zmieniła treść — jedno zapytanie.
* Przywrócenie treści starej wersji tworzy nową wersję; historii się nie
  nadpisuje.
* Dodanie i usunięcie załącznika, przegląd, przeniesienie do kosza
  i przywrócenie też są wersjami.
* Dwie osoby edytujące naraz: formularz niesie numer wersji, a zapis na
  nieaktualnej wersji dostaje 409 z zachowaną treścią, zamiast nadpisać
  cudze zmiany po cichu.

### Wyszukiwanie

`tsvector` konfiguracji `simple`, liczony przy zapisie z tekstu **bez
polskich znaków i interpunkcji** (tytuł waga A, streszczenie + tagi + systemy
B, treść + notatki C). Bez rozszerzenia `unaccent`, którego założenie wymaga
uprawnień superużytkownika bazy. Zapytanie szuka wszystkich słów jako
początków wyrazów, a wynik ma fragment treści z podświetleniem.

## Uprawnienia

Zapis sprawdza **każda trasa**, nie tylko menu (w hubzso menu chowało moduł,
a trasy przyjmowały zapis od każdego zalogowanego):

| Konto | Odczyt | Zapis |
|---|---|---|
| `admin` firmy, superadmin | tak | tak |
| technik helpdesku w swoich firmach | tak | tak |
| `viewer`, audytor globalny | tak | **403** |

Każdy zapis wymaga tokenu CSRF i trafia do audytu (`wiedza.*`).

## Trasy

| Metoda | Adres | Działanie |
|---|---|---|
| GET | `/wiedza` | start: przestrzenie, przypięte na dyżur, ostatnio zmienione, do przeglądu |
| GET | `/wiedza/artykuly` | lista i wyszukiwanie: `q`, `kategoria`, `przestrzen`, `tag`, `system`, `maszyna` |
| GET/POST | `/wiedza/przestrzenie`, `/wiedza/p/{id}` | przestrzenie, drzewo stron, zmiana, `…/usun` (tylko pusta) |
| GET/POST | `/wiedza/nowy` | nowy artykuł (`?przestrzen=`, `?rodzic=`, `?maszyna=`) |
| GET | `/wiedza/a/{id}` | artykuł (`?maszyna=` — kontekst maszyny) |
| GET/POST | `/wiedza/a/{id}/edycja`, `/wiedza/a/{id}` | edycja i zapis |
| POST | `/wiedza/a/{id}/usun`, `…/przywroc`, `…/przejrzany` | kosz, przywrócenie, potwierdzenie przeglądu |
| GET/POST | `/wiedza/a/{id}/historia`, `…/historia/{n}/przywroc` | historia, porównanie, przywrócenie treści |
| POST | `/wiedza/a/{id}/zalaczniki`, `…/zalaczniki/{z}/usun` | załączniki |
| GET | `/wiedza/zalacznik/{z}`, `…/podglad` | pobranie (zawsze jako plik) i podgląd obrazka |
| GET | `/wiedza/kosz` | usunięte artykuły |
| GET | `/wiedza/podpowiedzi?rodzaj=tag\|system&q=` | podpowiedzi (max 15, alfabetycznie) |
| GET | `/wiedza/dopasowanie?pole=&wartosc=&systemy=` | licznik maszyn dla niezapisanych warunków |
| GET/POST | `/wiedza/linki` | **nazwy → linki** |

### Nazwy → linki

Kontrakt z hubzso (`get-system-links`): moduł z listą obiektów wysyła nazwy
z bieżącej strony i dostaje tylko te, przy których warto pokazać ikonę.

```
GET  /wiedza/linki?nazwa=host01.example.local&nazwa=system-a&nazwa=inny
POST /wiedza/linki   {"nazwy": [...]}      (albo {"systems": [...]})

→ {"host01.example.local": "/wiedza/artykuly?system=host01.example.local",
   "system-a": "/wiedza/artykuly?system=system-a"}
```

Nazwa ma artykuł, gdy pasuje do niej maszyna objęta artykułem (systemy albo
warunki) albo gdy jest wprost jednym z systemów artykułu. Jedno zapytanie do
bazy bez względu na liczbę nazw; nazwy wracają w postaci, w jakiej je podano.

## Załączniki

Limit jednego pliku: `CMDB_WIEDZA_ZALACZNIK_MB` (domyślnie 16 MB). nginx
przepuszcza 20 MB wyłącznie pod adresem wysyłki załączników
(`deploy/nginx/*.conf`). Pobranie idzie zawsze jako plik
(`application/octet-stream`), a podgląd tylko dla obrazków, których początek
pliku zgadza się z typem.

Usunięcie załącznika kasuje wiersz, dopisuje wersję i **po zatwierdzeniu
transakcji** kasuje plik z dysku — w odwrotnej kolejności nieudany zapis
zostawiłby wiersz bez pliku.

Katalog `CMDB_WIEDZA_DIR` (w Dockerze wolumen `wiedza:/app/wiedza`) wchodzi do
kopii zapasowej obok zrzutu bazy, tak jak załączniki helpdesku.

## Ustawienia

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `CMDB_WIEDZA_DIR` | `./wiedza` | katalog załączników artykułów (w Dockerze `/app/wiedza`, wolumen `wiedza`) |
| `CMDB_WIEDZA_ZALACZNIK_MB` | `16` | największy dopuszczalny załącznik |

## Kryteria akceptacji z hubzso

| Kryterium | Test w `server/tests/test_wiedza.py` |
|---|---|
| Utworzony artykuł ma wszystkie pola | `test_kryterium_utworzony_artykul_ma_wszystkie_pola` |
| Edycja bez zmian nie dodaje wersji; jedno pole = jedna wersja | `test_kryterium_edycja_bez_zmian_nie_dodaje_wersji_a_jedno_pole_dodaje_jedna` |
| Usunięty załącznik znika z dysku i metadanych, historia to odnotowuje | `test_kryterium_usuniety_zalacznik_znika_z_dysku_i_metadanych` |
| Tag z `<script>` wyświetla się jako tekst | `test_kryterium_tag_ze_skryptem_wyswietla_sie_jako_tekst` |
| Bez roli redakcyjnej 403 na zapis | `test_kryterium_bez_prawa_zapisu_dostaje_403` |
| Nazwy → linki jednym zapytaniem | `test_kryterium_linki_dla_listy_nazw_jednym_zapytaniem` |

## Poza pierwszą wersją

* import artykułów z hubzso i z Confluence,
* artykuły dotyczące maszyny w panelu zgłoszenia helpdesku,
* komentarze i ocena „pomógł / nieaktualny”,
* warunki po relacjach (wszystkie VM na hoście X),
* WYSIWYG w stylu Confluence (zapis pozostanie w Markdown).
