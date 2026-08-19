# Bezpieczeństwo

## Model zagrożeń

| Zagrożenie | Odpowiedź systemu |
|---|---|
| Podsłuch danych inwentarzowych w sieci | wyłącznie HTTPS, TLS ≥ 1.2, agent nie ma opcji wyłączenia weryfikacji certyfikatu |
| Wyciek tokenu z jednej stacji | token firmowy służy tylko do rejestracji; maszyna posługuje się własnym poświadczeniem, unieważnialnym osobno |
| Firma A czyta maszyny firmy B | `tenant_id` wyłącznie z tokenu/sesji, izolacja wymuszona w jednym module, cudzy zasób zwraca 404 |
| Kradzież bazy danych | tokeny jako skróty SHA-256, hasła panelu jako Argon2id |
| Podszycie się pod serwer (MITM) | weryfikacja łańcucha i nazwy hosta, opcjonalne przypięcie odcisku certyfikatu, brak podążania za przekierowaniami |
| Przejęcie sesji w panelu | podpisane ciasteczko HttpOnly/Secure/SameSite=Lax, token CSRF na każdym formularzu |
| Przeciążenie serwera przez agenta | limit rozmiaru żądania, limit rozpakowania gzip, throttling nieudanych uwierzytelnień |
| Odczyt poświadczenia z dysku stacji | plik stanu tylko dla SYSTEM i administratorów (Windows) / `0600` (POSIX) |

## Tokeny

Format: `cmdb_<typ>_<prefiks>_<sekret>`

* `typ` — `ent` (rejestracyjny, firmowy) albo `agt` (poświadczenie maszyny).
  Typy nie są zamienne: tokenem `agt` nie zarejestrujesz maszyny, a tokenem
  `ent` nie wyślesz raportu (potwierdzone testami).
* `prefiks` — 12 znaków, jawny, służy wyłącznie do znalezienia wiersza w bazie.
* `sekret` — 256 bitów z `secrets.token_urlsafe`.

W bazie jest **wyłącznie SHA-256 całego tokenu**. Wartość jawna pokazywana
jest raz, przy wydaniu — potem nie da się jej odzyskać, można tylko wydać nową.

Dla haseł użytkowników panelu używamy **Argon2id**, nie SHA-256: hasło ma
niską entropię i wymaga wolnej funkcji odpornej na łamanie słownikowe.
Token jest w pełni losowy, więc SHA-256 w zupełności wystarcza, a jest szybki
— agenci uwierzytelniają się przy każdym raporcie.

Porównania robimy w czasie stałym (`hmac.compare_digest`). Logowanie
nieistniejącego konta również liczy hash-atrapę, żeby czas odpowiedzi nie
zdradzał, czy konto istnieje.

## Cykl życia poświadczeń

```
token firmowy (cmdb_ent_…)
   │  wydany w panelu lub CLI, opcjonalnie z datą ważności
   │  używany TYLKO przy pierwszym uruchomieniu agenta
   ▼
poświadczenie maszyny (cmdb_agt_…)
   │  przypisane do konkretnej maszyny, zapisane lokalnie z ograniczonym dostępem
   │  ponowna rejestracja tej samej maszyny unieważnia poprzednie poświadczenie
   ▼
unieważnienie
   • pojedyncza maszyna → panel: karta maszyny → Historia → Wycofaj
   • cała firma        → panel: Tokeny agentów → Wycofaj (blokuje nowe rejestracje)
```

**Uwaga operacyjna.** Jeśli poświadczenie maszyny zostanie wycofane, a agent
nadal ma ważny token firmowy w konfiguracji, przy następnym cyklu zarejestruje
się ponownie. Żeby trwale odciąć maszynę, usuń agenta ze stacji albo wycofaj
token firmowy i wydaj nowy pozostałym. Zachowanie jest celowe — pozwala
rotować poświadczenia bez ręcznego obchodzenia całej floty.

## Transport

Serwer w trybie `prod`:

* odmawia startu bez `CMDB_SECRET_KEY` (≥ 32 znaki), bez `CMDB_REQUIRE_HTTPS`
  i na SQLite — błędna konfiguracja nie wjedzie na produkcję po cichu,
* żądania do `/api/` po HTTP odrzuca kodem 403 **bez przekierowania** —
  przekierowanie nic by nie dało, token już wyciekł w pierwszym żądaniu,
* wysyła HSTS, CSP bez `unsafe-inline`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`,
* wyłącza `/api/docs`.

Agent:

* akceptuje wyłącznie adresy `https://` (sprawdzane w dwóch miejscach:
  walidacji konfiguracji i konstruktorze klienta),
* weryfikuje łańcuch i nazwę hosta — **nie ma opcji wyłączenia**,
* obsługuje własny plik CA (`--ca-bundle`) dla wewnętrznego PKI,
* opcjonalnie przypina odcisk certyfikatu (`--pin-sha256`); niezgodność
  przerywa pracę kodem 4, bez ponawiania,
* nie podąża za przekierowaniami — 3xx traktuje jako błąd konfiguracji,
  bo przekierowanie mogłoby wysłać token na inny host,
* przesyła token wyłącznie w nagłówku `Authorization`, nigdy w URL
  (adresy trafiają do logów proxy).

### Przypięcie certyfikatu

```powershell
# odczytanie odcisku z certyfikatu serwera
openssl x509 -in fullchain.pem -noout -fingerprint -sha256

.\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_… `
                    -PinSha256 68fc9ec862cdd140…
```

Przypięcie jest odciskiem **certyfikatu**, nie klucza — po odnowieniu
certyfikatu trzeba zaktualizować odcisk na stacjach. Przy Let's Encrypt
(odnowienie co 90 dni) zwykle nie warto; przy własnym CA z długim okresem
ważności — jak najbardziej.

## Panel WWW

* Sesja w ciasteczku podpisanym `itsdangerous`, `HttpOnly`, `Secure`
  (gdy `CMDB_REQUIRE_HTTPS`), `SameSite=Lax`, domyślnie 8 godzin.
* Token CSRF wymagany przy każdym `POST` — powiązany z identyfikatorem
  użytkownika i podpisany.
* Role: `admin` (zarządza tokenami, opiekunami, przypisaniami) i `viewer`
  (tylko odczyt). Próba zapisu przez `viewer` kończy się kodem 403.
* Widoki renderowane po stronie serwera, bez SPA i bez publicznego API
  przeglądarkowego — mniejsza powierzchnia ataku.
* CSP dopuszcza zasoby wyłącznie z `'self'`; w kodzie nie ma stylów ani
  skryptów inline.

## Dane wrażliwe w raportach

System **nie zbiera** haseł, skrótów haseł, zawartości plików,
historii przeglądarki ani wierszy poleceń procesów.

Zbiera dane, które w audycie bywają uznane za wrażliwe:

| Dane | Po co | Jak wyłączyć |
|---|---|---|
| lista procesów | „jakie oprogramowanie jest tam uruchomione" | `collect_processes: false` / `-NoProcessList` przy instalacji |
| nazwy kont i SID | kontrola dostępu, rozliczalność | wymagane przez założenia systemu |
| lista sesji | kto korzysta z maszyny | zbierane razem z kontami |
| poprawki systemu | ocena aktualności | `collect_updates: false` |

Przed wdrożeniem warto uzgodnić zakres z osobą odpowiedzialną za ochronę
danych osobowych — lista kont i sesji to dane pracowników. `docs/agent-windows.md`
zawiera gotową listę zbieranych pól do takiej rozmowy.

## Odporność serwera

* Limit rozmiaru żądania (`CMDB_MAX_REPORT_BYTES`, domyślnie 8 MB)
  sprawdzany przed zbudowaniem ciała w pamięci.
* Rozpakowanie gzip strumieniowo, z twardym limitem — mały plik
  rozpakowujący się do gigabajtów dostaje 413 zamiast zjadać pamięć
  (test `test_zip_bomb_is_rejected`).
* Throttling: 20 nieudanych uwierzytelnień z jednego adresu w 5 minut →
  429. Przy wielu instancjach serwera licznik trzeba przenieść do Redis —
  obecna implementacja jest lokalna dla procesu.
* `machine_id` z raportu musi zgadzać się z poświadczeniem, inaczej 409 —
  agent nie nadpisze cudzej maszyny nawet w obrębie własnej firmy.
* Czas zebrania z zegara klienta jest przycinany: raport „z przyszłości"
  albo sprzed roku dostaje czas serwera.

## Podział uprawnień na stacji

Na maszynie klienta pracują dwa procesy o różnych uprawnieniach:

| Proces | Konto | Co robi | Do czego ma dostęp |
|---|---|---|---|
| `cmdb-agent.exe` (zadanie) | SYSTEM | zbiera dane i wysyła raport | konfiguracja i poświadczenie |
| `cmdb-agent-tray.exe` (ikona) | zalogowany użytkownik | pokazuje status | wyłącznie plik statusu |

Rozdział plików wynika wprost z tego podziału:

```
%ProgramData%\CMDB\
├── agent.conf          token firmowy      → SYSTEM + Administratorzy
├── agent-state.json    poświadczenie      → SYSTEM + Administratorzy
├── agent.log                              → SYSTEM + Administratorzy
└── public\
    └── status.json     bez sekretów       → dodatkowo odczyt dla Użytkowników
```

Gdyby ikona czytała stan bezpośrednio, trzeba by otworzyć zwykłym użytkownikom
dostęp do pliku z tokenem. Zamiast tego agent publikuje osobną migawkę bez
żadnych sekretów — test `test_published_file_contains_no_secrets` pilnuje,
żeby token nigdy się tam nie znalazł.

Operacje wymagające uprawnień ikona uruchamia jako osobny, podniesiony proces
(monit UAC), a nie podnosząc uprawnień całej aplikacji działającej stale na
pulpicie. Wyjątkiem jest „Synchronizuj teraz": instalator nadaje grupie
Użytkownicy prawo *uruchomienia* zadania (`GRGX`), ale nie jego zmiany — dzięki
temu wymuszenie odświeżenia nie wymaga hasła administratora, a użytkownik nadal
nie może podmienić tego, co zadanie uruchamia.

## Ślad audytowy

`audit_log` zapisuje: logowania udane i nieudane, wydanie i wycofanie
tokenu, rejestrację agenta, wycofanie poświadczenia, zmianę opiekuna,
dodanie i usunięcie opiekuna — z adresem IP i znacznikiem czasu.
Podgląd w panelu: zakładka **Audyt**.
