# Logowanie i uprawnienia

CMDB obsługuje cztery sposoby logowania. Wszystkie kończą się tą samą sesją
panelu i tymi samymi rolami, więc nie ma osobnego modelu uprawnień dla
każdego z nich.

| Kto | Jak się loguje | Skąd uprawnienia |
|---|---|---|
| Firma z Active Directory | login i hasło domenowe (LDAP) | grupy AD → role |
| Firma bez AD, serwisanci, pojedyncze osoby | konto lokalne: e-mail i hasło w CMDB | rola nadana w CMDB |
| Podwykonawcy (opcjonalnie) | Google, Microsoft, GitHub — **tylko z zaproszenia** | rola nadana w CMDB |
| Operator | katalog AD operatora albo konto lokalne | grupy AD operatora albo rola w CMDB |
| Awaryjnie | lokalny superadmin — działa zawsze | superadmin |
| Aplikacja Android | pierwszy raz dowolną z dróg powyżej, potem odcisk palca | z konta |

## Użytkownik nie wybiera sposobu logowania

Formularz ma jedno pole loginu. CMDB rozpoznaje sposób po domenie:

- `jan@abc.pl` albo `ABC\jan` — domena należy do katalogu AD firmy ABC, więc
  hasło sprawdza AD. Pod polem pojawia się „Konto domenowe ABC”.
- każda inna domena — konto lokalne CMDB.

Podpowiedź pod polem (`GET /login/sposob`) zależy wyłącznie od domeny, nie od
tego, czy konto istnieje. Komunikat „Nieprawidłowy login lub hasło.” jest ten
sam dla złego hasła i nieistniejącego konta.

Przyciski „Zaloguj przez Google/Microsoft/GitHub” pojawiają się tylko wtedy,
gdy dostawca jest skonfigurowany (niżej), z dopiskiem, że są dla osób
zaproszonych.

## Active Directory / LDAP

Konfiguracja: **panel superadmina → Katalogi AD**.

Katalog należy do jednej firmy albo do operatora (bez firmy). Ustawia się:

- serwery w kolejności prób — **tylko `ldaps://`** albo `ldap://` z StartTLS;
  połączenie bez szyfrowania jest odrzucane, bo idą nim hasła,
- certyfikat CA w PEM, gdy AD ma certyfikat z wewnętrznego urzędu,
- base DN, konto serwisowe tylko do odczytu (hasło szyfrowane w bazie),
- domeny loginu (`abc.pl`, `ABC\`) — jedna domena należy do jednego katalogu,
- filtr wyszukiwania (domyślny działa z AD),
- co z osobą spoza zmapowanych grup: **nie wchodzi** (domyślnie) albo dostaje
  odczyt.

„Sprawdź połączenie” wykonuje bind kontem serwisowym i próbne wyszukiwanie.

### Przebieg logowania

1. Puste hasło jest odrzucane, zanim cokolwiek trafi do AD — w LDAP bind
   z pustym hasłem to bind anonimowy i serwer odpowiada na niego sukcesem.
2. Konto serwisowe wyszukuje osobę (login escapowany w filtrze).
3. Bind jako ta osoba z podanym hasłem — właściwa weryfikacja.
4. Grupy: `tokenGroups` (wszystkie, także zagnieżdżone, jako SID-y) oraz
   `memberOf` (DN, dla LDAP innych niż AD).
5. Konto CMDB zakłada się samo przy pierwszym logowaniu i jest wiązane po
   **objectGUID**, nie po adresie. Istniejące konto lokalne tej samej firmy
   z adresem w domenie katalogu przechodzi jednorazowo na AD.

Blokada po nieudanych próbach działa **przed** zapytaniem do AD, żeby atak na
CMDB nie blokował ludziom kont domenowych. Awaria katalogu nie jest liczona
jako złe hasło — użytkownik widzi „Nie udało się połączyć z serwerem domeny”.

### Grupy i role

Na stronie katalogu wyszukuje się grupę w AD i przypisuje jej rolę. Grupa jest
zapisana po SID (nie zmienia się przy przeniesieniu do innej OU); dla LDAP bez
SID — po DN. Gdy pasuje kilka grup, wygrywa wyższa rola.

„Sprawdź użytkownika” pokazuje grupy osoby i rolę, którą dostanie — bez
logowania jej. Przydaje się przy zgłoszeniach „nie mam dostępu”.

**Granica firmy.** Katalog firmy daje wyłącznie role `admin`/`viewer` w tej
firmie. Superadmina, audytora wszystkich firm i technika helpdesku (z wybraną
firmą) daje **wyłącznie katalog operatora**. Administrator domeny klienta może
dopisać się do dowolnej grupy u siebie — nie może przez to wyjść poza swoją
firmę. Reguła jest sprawdzana przy zapisie mapowania i drugi raz przy liczeniu
uprawnień (mapowanie wstawione z pominięciem panelu też nic nie da).

Konto z AD ma w panelu rolę zablokowaną (🔒) i pochodzenie roli („z grupy
CMDB-Administratorzy”). Nie da się mu ustawić hasła ani zmienić roli ręcznie —
nadpisałaby to najbliższa synchronizacja.

### Synchronizacja

Co 15 minut (i przyciskiem „Synchronizuj teraz”) CMDB sprawdza każde konto
z katalogu po objectGUID:

- konto wyłączone w AD (`userAccountControl`), usunięte albo bez zmapowanej
  grupy — **traci dostęp**, a jego sesje (panel i aplikacja) są unieważnione,
- zmiana grup — nowa rola od razu, sesje unieważnione,
- katalog nie odpowiada — **żadne konto nie jest ruszane**; błąd widać na
  liście katalogów.

Konto wyłączone ręcznie przez superadmina zostaje wyłączone — synchronizacja
przywraca tylko to, co sama wyłączyła. Dostępy techników nadane ręcznie
w helpdesku zostają; synchronizacja dotyka tylko tych, które wynikają z grup.

## Konta lokalne

Działają jak dotąd: hasło (Argon2id), blokada po nieudanych próbach, rola
ustawiana w panelu, `cmdb-admin user-create`.

- **Zaproszenie** (Konta i role → „Wyślij zaproszenie”): osoba dostaje link
  i sama ustawia hasło — administrator nie wymyśla ani nie zna cudzego hasła.
  Link jest ważny 7 dni (`CMDB_ZAPROSZENIE_DNI`) i działa raz. Wysyłamy go
  pocztą firmy, gdy ma skonfigurowany SMTP; inaczej pokazujemy go raz do
  przekazania.
- **„Nie pamiętasz hasła?”** na stronie logowania: link ważny godzinę
  (`CMDB_RESET_HASLA_MINUT`), najwyżej trzy na godzinę na adres, odpowiedź
  zawsze ta sama. Superadmin może też wydać link przyciskiem „Link do hasła”.
- Konto lokalne **nie może mieć adresu w domenie katalogu AD** — po wpisaniu
  takiego loginu nie byłoby wiadomo, kto sprawdza hasło.
- Firma może **wyłączyć konta lokalne** (ustawienia firmy albo strona
  katalogu) — loguje się wtedy wyłącznie przez AD. Superadmina, audytora
  i techników to nie dotyczy.

### Weryfikacja dwuetapowa (TOTP)

Kod z aplikacji Google Authenticator, Microsoft Authenticator lub podobnej
(RFC 6238, 6 cyfr, 30 s). Tylko dla kont lokalnych — konta z AD zabezpiecza AD.

- Włącza się ją w **Moje konto**; wyłączenie wymaga hasła i kodu.
- Firma może ją **wymagać** (ustawienia firmy) — kto jej nie ma, ustawia ją
  przy najbliższym logowaniu.
- `CMDB_MFA_SUPERADMIN=true` wymaga jej od lokalnych superadminów. Domyślnie
  wyłączone, żeby aktualizacja nikogo nie zaskoczyła — **zalecamy włączenie
  na produkcji**.
- Ten sam kod nie przejdzie drugi raz. Błędne kody liczą się do blokady.
- Zgubiony telefon: superadmin „Zdejmij 2FA” albo z serwera
  `cmdb-admin user-password --email ... --bez-2fa`.

## Konta Google, Microsoft, GitHub

Konto u zewnętrznego dostawcy mówi tylko, **kim** ktoś jest — nie, w jakiej
firmie pracuje. Dlatego samo zalogowanie przez Google **nie daje żadnego
dostępu**. Konto zewnętrzne działa wyłącznie po powiązaniu z kontem CMDB:

- z **zaproszenia** — na stronie zaproszenia obok ustawienia hasła są
  przyciski „Użyj konta Google/…”,
- przez zalogowaną osobę — **Moje konto → Dodaj konto Google**.

Powiązanie jest po parze (dostawca, identyfikator u dostawcy), nigdy po samym
adresie e-mail: inne konto Google z tym samym adresem nie wejdzie. Konto bez
powiązania dostaje „Konto … nie ma dostępu do CMDB. Poproś administratora
o zaproszenie”. Firma z wyłączonymi kontami lokalnymi blokuje też konta
zewnętrzne.

Technicznie: authorization code z PKCE, `state` i `nonce` w podpisanym
ciasteczku, sprawdzanie `iss`, `aud`, `exp`, `nonce` w id_tokenie (Google,
Microsoft); GitHub — zweryfikowany główny adres z API.

### Konfiguracja dostawców

Dostawca jest włączony, gdy w `.env` są oba pola:

```
CMDB_GOOGLE_CLIENT_ID=...        CMDB_GOOGLE_CLIENT_SECRET=...
CMDB_MICROSOFT_CLIENT_ID=...     CMDB_MICROSOFT_CLIENT_SECRET=...
CMDB_GITHUB_CLIENT_ID=...        CMDB_GITHUB_CLIENT_SECRET=...
```

U każdego dostawcy rejestruje się aplikację z adresem zwrotnym
`<CMDB_PUBLIC_URL>/login/zewn/callback`:

- **Google** — Google Cloud Console → APIs & Services → Credentials → OAuth
  client ID (Web application).
- **Microsoft** — Entra ID → App registrations; konta: „any organizational
  directory and personal Microsoft accounts” (endpoint `common`).
- **GitHub** — Settings → Developer settings → OAuth Apps.

Każda instalacja (produkcja, test) potrzebuje własnej rejestracji z publiczną
nazwą domenową — adres IP ani nazwa wewnętrzna nie przejdą u Google.

## Aplikacja Android: odcisk palca

Biometria niczego nie przyznaje — potwierdza, że telefon, który raz przeszedł
pełne logowanie, nadal jest w rękach właściciela. Odcisk palca nie opuszcza
telefonu.

1. Po pełnym logowaniu aplikacja proponuje „Logować się odciskiem palca?”.
   Tworzy wtedy w **Android Keystore** (StrongBox, gdy jest) parę kluczy
   EC P-256, która wymaga silnej biometrii przy każdym użyciu i jest
   unieważniana po dodaniu nowego odcisku w telefonie. Serwer dostaje tylko
   klucz publiczny (`POST /api/v1/mobile/devices`) — i tylko w ciągu
   10 minut od pełnego logowania.
2. Przy uruchomieniu aplikacja prosi serwer o jednorazowe wyzwanie, telefon
   podpisuje je po przyłożeniu palca, serwer sprawdza podpis i stan konta
   i wydaje **token na godzinę**, związany z urządzeniem.
3. Przy każdym pełnym logowaniu telefon dostaje nowy klucz.

Zasady firmy (ustawienia firmy → Aplikacja mobilna):

| Ustawienie | Domyślnie |
|---|---|
| biometria: dozwolona / wymagana / wyłączona | dozwolona |
| pełne logowanie co ile dni | 30 |
| blokada po ilu minutach w tle | 5 |
| PIN telefonu zamiast palca | nie |

Zapis po biometrii (np. przypisanie sprzętu) wymaga świeżego potwierdzenia —
po 5 minutach aplikacja prosi o palec ponownie i ponawia zapis.

Odłączanie telefonu działa **od razu** (także wydany już token):
Moje konto → Telefony, superadmin → „Odłącz telefony”. Wylogowanie ze
wszystkich urządzeń, zmiana hasła i nowe hasło z linku odłączają telefony
automatycznie. Wylogowanie w aplikacji wyłącza na niej biometrię.

Wymaga Androida 11 i zapisanego odcisku palca. Starsze telefony logują się
hasłem.

Aplikacja obsługuje też kod weryfikacji dwuetapowej i logowanie kontem
Google/Microsoft/GitHub — w przeglądarce systemowej, z powrotem do aplikacji
przez `pl.hubzso.cmdb:/logowanie` i jednorazowym kodem wymienianym na token
z weryfikatorem PKCE (RFC 8252).

## Audyt

Każde logowanie zapisuje sposób (`lokalne`, `ad`, `lokalne+totp`, `google`,
`biometria`…). Osobno odnotowujemy: zakładanie kont z AD, zmiany uprawnień
z synchronizacji, odebranie dostępu, zmiany katalogów i mapowań, zaproszenia,
linki do haseł, włączenie/zdjęcie 2FA, powiązanie kont zewnętrznych,
dodanie/odłączenie telefonów i odmowy biometrii.
