# Badanie podatności (CVE)

Karta maszyny ma zakładkę **Podatności**, a administrator serwera —
stronę **Podatności** ze stanem kanałów danych.

## Skąd dane

| System | Źródło | Klucz |
|---|---|---|
| Debian | Debian Security Tracker (JSON, ~86 MB) | pakiet źródłowy + wersja źródłowa |
| Ubuntu | baza biuletynów USN (JSON, ~44 MB) | pakiet źródłowy + wersja źródłowa |

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
`security-tracker.debian.org` albo `ubuntu.com/security` opisują, co dana
dystrybucja zrobiła z konkretnym pakietem, co jest praktyczniejsze niż sam
opis luki.

Kafelek **„poważne (CVSS ≥ 7) z gotową poprawką"** to lista, od której zaczyna
się pracę: rzeczy jednocześnie groźne i możliwe do naprawienia od ręki.

## Odświeżanie

Ręcznie, przyciskiem na stronie **Podatności**. Pobieramy dane wyłącznie dla
wydań faktycznie używanych we flocie — kanał Debiana ma 86 MB. Nieudane
pobranie **nie kasuje** starych wpisów: dane sprzed tygodnia z widocznym
wiekiem są lepsze niż żadne. Panel ostrzega, gdy dane mają ponad tydzień.

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
