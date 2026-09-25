# Badanie podatności (CVE)

Karta maszyny ma zakładkę **Podatności**, a administrator serwera —
stronę **Podatności** ze stanem kanałów danych. Obsługiwane są Debian,
Ubuntu oraz RHEL z pochodnymi (Rocky, Alma, CentOS).

## Skąd dane

| System | Źródło | Klucz |
|---|---|---|
| Debian | Debian Security Tracker (JSON, ~86 MB) | pakiet źródłowy + wersja źródłowa |
| Ubuntu | baza biuletynów USN (JSON, ~44 MB) | pakiet źródłowy + wersja źródłowa |
| RHEL, Rocky, Alma, CentOS | OVAL v2 Red Hata, osobny plik na wydanie główne (`rhel-9.oval.xml.bz2`) | pakiet **binarny** + wersja z epoką |

**Niezależnie od `apt update` na maszynach.** Zestawienie porównuje wersje
*zainstalowanych* pakietów (z raportu agenta) z danymi pobieranymi przez
**serwer**. Indeks pakietów na maszynie nie ma tu żadnego znaczenia — nawet
maszyna, na której od roku nikt nie zrobił `apt update`, ma aktualną listę
podatności, o ile serwer ma świeże dane.

**Nie używamy NVD.** Dystrybucje łatają wstecznie, nie zmieniając numeru
wersji: pakiet `22.08.8-6+deb12u1` zawiera poprawkę, o której numer upstream
`22.08.8` nic nie mówi. Porównywanie wersji z NVD dałoby lawinę fałszywych
alarmów, bo NVD opisuje wersje upstream. Wiarygodną odpowiedź na pytanie
„czy **ta** wersja jest podatna" ma wyłącznie dystrybucja, która poprawkę wydała.

## Trzy kategorie zamiast jednej liczby

| Kategoria | Znaczenie |
|---|---|
| **do zrobienia** | poprawka istnieje, maszyna jej nie ma — jedyna kategoria, na którą można zareagować |
| **bez poprawki** | dystrybucja uznaje sprawę za istotną, ale poprawki jeszcze nie ma |
| **drobne** | dystrybucja świadomie nie wyda poprawki (`nodsa`, zwykle „Minor issue") |

Bez tego podziału lista tonie: na Debianie 12 typowa maszyna ma ~110 rzeczy do
zrobienia i ~300 pozycji, na które i tak nic nie poradzi.

Osobno, jak wszędzie w tym systemie: **stan „nieznany" to nie zero.**
Nierozpoznana dystrybucja albo niepobrane dane kończą się `null`, nie `0`,
a panel mówi wprost, że nie sprawdziliśmy.

## Porównywanie wersji

Sercem dopasowania jest porównywanie wersji według reguł dpkg
(Debian Policy 5.6.12) — [`wersje_pakietow.py`](../server/cmdb_server/services/wersje_pakietow.py).
Zwykłe porównanie tekstowe daje złe odpowiedzi w obie strony:

```
"1.10" < "1.9"        tekstowo, choć 1.10 jest nowsze
"1.0~rc1" > "1.0"     tekstowo, choć wersja przedwydawnicza jest starsza
```

Druga pomyłka jest groźniejsza: podatność uznana za naprawioną nie pojawi się
w żadnym raporcie.

## RHEL i pochodne

* **Wydanie** to główny numer wersji z `/etc/os-release` (`9.4` → `9`):
  Red Hat publikuje jeden plik OVAL na całe wydanie główne.
* **Rocky, Alma i CentOS** korzystają z danych RHEL — przebudowują te same
  pakiety z tymi samymi numerami wersji. Oracle Linux i Fedora świadomie
  nie są dopasowywane (inne poprawki, inne numery).
* **Porównanie według reguł RPM**, nie dpkg (`rpmvercmp`: separatory są
  równoważne, `^` po końcu wersji, segment liczbowy nowszy od literowego).
* **Epoka.** Red Hat podaje `1:3.0.7-27.el9`. Agent przysyła ją w polu `evr`.
  Starsze agenty jej nie podają — wtedy epokę pomijamy, zamiast uznawać ją
  za 0 (co dałoby fałszywy alarm na każdym pakiecie z epoką).
* **Moduły** (`nodejs:20`, `postgresql:15`). Poprawka ze strumienia modułu
  dotyczy tylko pakietów z tego samego strumienia (ten sam główny numer
  wersji) — inaczej każdy starszy strumień byłby „podatny”.
* **Jądro.** dnf trzyma kilka jąder naraz; stare `kernel-core` leżące na
  dysku nie jest podatnością, jeśli działa nowsze (porównanie z `uname -r`).
* **Tylko wydane poprawki.** Używamy pliku bez `including-unpatched`, więc
  kategoria „bez poprawki” dla RHEL jest pusta — to nie znaczy, że takich
  luk nie ma.

## Pakiet źródłowy, nie binarny

Dystrybucje indeksują podatności po pakiecie **źródłowym** (`openssl`),
a zainstalowane są pakiety **binarne** (`libssl3`). Agent podaje jedno
i drugie oraz **wersję źródłową** — to ona jest porównywalna z danymi
dystrybucji.

Ma to konkretny powód. Binaria z jednego źródła miewają różne schematy wersji:
ze źródła `e2fsprogs` powstaje binarny `comerr-dev` w wersji `2.1-1.46.5-…`.
Porównanie ich ze sobą dawało poprawkę rzekomo nowszą niż zainstalowana wersja,
czyli fałszywy alarm na pakiecie, który był aktualny.

## Oceny CVSS i odnośniki

Dane dystrybucji mówią, **czy** pakiet jest podatny, ale nie podają wagi
w skali CVSS — Debian ma własną skalę `urgency`, Ubuntu nie podaje żadnej.
Ocena („Base Score: 6.5 MEDIUM") pochodzi więc z NVD i jest buforowana na
stałe: dla wydanego CVE zmienia się rzadko.

Pobieramy oceny **wyłącznie dla podatności faktycznie znalezionych** na
maszynach. Kanał Debiana ma 45 tys. wpisów, a flotę dotyczy kilkaset —
ściąganie reszty to zapytania o luki, których u nikogo nie ma.

NVD dopuszcza **5 zapytań na 30 sekund**, więc jeden przebieg obejmuje do 200
podatności, a kolejne kliknięcie dobiera następną porcję. Bezpłatny klucz
podnosi limit do 50 na 30 sekund:

```bash
CMDB_NVD_API_KEY=...    # https://nvd.nist.gov/developers/request-an-api-key
```

Brak oceny **nie jest** traktowany jak ocena niska: taka podatność ląduje na
końcu swojej grupy, a nie udaje najłagodniejszej, a panel pokazuje, ile ocen
jeszcze brakuje.

Odnośnik przy każdym CVE prowadzi do strony **dystrybucji**, nie do NVD —
`security-tracker.debian.org`, `ubuntu.com/security` albo
`access.redhat.com/security/cve` opisują, co dana
dystrybucja zrobiła z konkretnym pakietem, co jest praktyczniejsze niż sam
opis luki.

Kafelek **„poważne (CVSS ≥ 7) z gotową poprawką"** to lista, od której zaczyna
się pracę: rzeczy jednocześnie groźne i możliwe do naprawienia od ręki.

## Odświeżanie

**Automatycznie.** Serwer co godzinę sprawdza, czy dane któregoś wydania
używanego we flocie są starsze niż `CMDB_CVE_REFRESH_HOURS` (domyślnie 24 h),
i wtedy je pobiera. W tym samym obiegu dobiera porcję brakujących ocen CVSS
z NVD. Pierwszy obieg rusza ok. 2 minuty po starcie serwera. Przy kilku
procesach roboczych pracę wykonuje jeden — uzgadniają się blokadą doradczą
Postgresa, osobną od harmonogramu raportów, żeby długie pobieranie nie
wstrzymywało wysyłki raportów.

```bash
CMDB_CVE_REFRESH_HOURS=24   # 0 = tylko ręcznie
```

Przyciski na stronie **Podatności** wymuszają odświeżenie od razu.

Pobieramy dane wyłącznie dla wydań faktycznie używanych we flocie — kanał
Debiana ma 86 MB. Nowe wydanie (np. pierwsza maszyna z RHEL 8) dostaje dane
w najbliższym obiegu. Nieudane pobranie **nie kasuje** starych wpisów: dane
sprzed tygodnia z widocznym wiekiem są lepsze niż żadne; próba powtarza się
w następnym obiegu (co godzinę). Panel ostrzega, gdy dane mają ponad tydzień.

Serwer musi mieć dostęp do: `security-tracker.debian.org`, `usn.ubuntu.com`,
`security.access.redhat.com` i `services.nvd.nist.gov`.

## Czego to nie zastąpi

Raport mówi „zainstalowana wersja jest starsza niż ta z poprawką na CVE-X".
Nie sprawdza konfiguracji, nie bada usług sieciowych i nie wie, czy podatny
pakiet jest w ogóle uruchamiany. Do pełnej oceny służy skaner podatności —
to zestawienie odpowiada na węższe, ale konkretne pytanie: **czego brakuje**.

Poprawki Ubuntu z przyrostkiem `+esm` pochodzą z rozszerzonego wsparcia
(Ubuntu Pro). Poprawka istnieje, ale bez subskrypcji może nie być dostępna
do zainstalowania.

Windows nie jest jeszcze objęty — mapowanie brakujących poprawek KB na CVE
przez katalog MSRC jest następnym krokiem.
