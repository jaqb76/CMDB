# Aktualizacje i poprawki

Karta maszyny ma osobną zakładkę **Aktualizacje** z dwiema sekcjami:

* **Brakujące aktualizacje** — czego na maszynie nie ma, z wyróżnieniem
  poprawek bezpieczeństwa. To ta sekcja mówi cokolwiek o bezpieczeństwie.
* **Zainstalowane poprawki** — co już jest. Na Windows to lista KB;
  systemy z rodziny Linux nie prowadzą osobnej listy, bo poprawki są tam
  częścią wersji pakietów.

## Trzy stany, nie dwa

Najważniejsze rozróżnienie w całym module:

| Stan | Znaczenie |
|---|---|
| `ok`, pusta lista | **sprawdzono i nic nie brakuje** |
| `ok`, lista niepusta | sprawdzono, brakuje wymienionych |
| `nieznany` | **sprawdzić się nie udało** — to nie to samo co „zero braków" |

W narzędziu do oceny bezpieczeństwa zlanie pierwszego i trzeciego przypadku
w jedno uspokajałoby zamiast ostrzegać. Dlatego przy stanie `nieznany`
`count` i `security_count` są `null`, a nie `0`, a panel pokazuje ostrzeżenie
z powodem niepowodzenia.

## Skąd dane

| System | Źródło | Czas | Ruch sieciowy |
|---|---|---|---|
| Windows | usługa Windows Update (COM `Microsoft.Update.Session`) | 15 s – kilka minut | tak — Windows Update albo WSUS |
| Debian, Ubuntu | `apt-get -s dist-upgrade` (na własnym indeksie agenta) | poniżej sekundy + odświeżenie indeksu raz na dobę | raz na dobę — repozytoria maszyny |
| RHEL, Fedora, Rocky | `dnf check-update` (na własnym buforze agenta) | kilka sekund + odświeżenie raz na dobę | raz na dobę — repozytoria maszyny |

Na Windows to ten sam mechanizm, z którego korzysta panel sterowania, więc
wynik zgadza się z tym, co użytkownik widzi u siebie. Wyszukiwanie bywa
kosztowne, dlatego ma osobny, wyższy limit czasu (420 s), a niepowodzenie
kończy się stanem `nieznany`, nie psuje zaś całego raportu.

## Indeks pakietów: agent odświeża własną kopię

Lista braków na Linuksie jest tak dobra, jak indeks pakietów, z którego
powstaje. Na maszynach, na których nikt nie robi `apt update` / `dnf
makecache`, indeks ma miesiące i wynik niczego nie mówi.

Dlatego agent **sam pobiera indeks — do własnego katalogu danych**
(`<data_dir>/indeks-pakietow`), nie ruszając systemowego:

| Menedżer | Jak | Co zostaje nietknięte |
|---|---|---|
| apt | `apt-get -c <agent>/apt.conf update` z własnym `Dir::State::Lists`, wyłączonymi skryptami `APT::Update::*-Invoke` i bez `pkgcache.bin` | `/var/lib/apt/lists`, `/var/cache/apt`, blokady apt |
| dnf / yum | `--setopt=cachedir=<agent>/cache --setopt=metadata_expire=<wiek>` | `/var/cache/dnf`, `/var/cache/yum` |

Źródła repozytoriów, klucze, proxy i uprawnienia (np. subskrypcja RHEL) są
systemowe, więc wynik jest taki, jaki dałby zwykły `apt update`. Maszyna
musi mieć dostęp do swoich repozytoriów — tak jak do zwykłych aktualizacji.

* Indeks odświeża się najwyżej raz na `package_index_max_age_hours`
  (domyślnie 24 h), a nie przy każdym raporcie.
* Nieudane odświeżenie: agent wraca do indeksu systemowego, panel pokazuje
  powód („Agent nie odświeżył indeksu pakietów…”), a kolejna próba jest
  najwcześniej po 6 godzinach.
* Własna kopia indeksu zajmuje zwykle od kilkudziesięciu do kilkuset MB
  (zależnie od liczby repozytoriów).

Wyłączenie (np. maszyny bez dostępu do repozytoriów):

```json
{ "refresh_package_index": false, "package_index_max_age_hours": 24 }
```

Wtedy agent czyta indeks systemowy jak dawniej, a panel ostrzega, gdy jest
starszy niż tydzień.

**Zakładka Podatności nie zależy od indeksu.** Porównuje zainstalowane
wersje pakietów z danymi dystrybucji, które pobiera serwer — patrz
[podatnosci.md](podatnosci.md).

## Czego to NIE jest

To **nie jest skanowanie podatności**. Raport mówi „brakuje poprawki X
z kieszeni bezpieczeństwa", a nie „maszyna jest podatna na CVE-2024-1234".
Pełna ocena podatności wymaga zestawienia wersji pakietów z bazą CVE
(OVAL, OSV) i jest osobnym zagadnieniem.

To rozróżnienie ma znaczenie praktyczne: brak poprawki bezpieczeństwa jest
mocną przesłanką, ale nie dowodem podatności — pakiet może być
zainstalowany, lecz nieużywany, a dystrybucje wstecznie łatają wersje bez
zmiany numeru głównego.

## Wyłączenie

Wykrywanie braków włącza się domyślnie. Gdy skan Windows Update jest zbyt
kosztowny albo maszyny nie mają wychodzić do sieci:

```json
{ "collect_pending_updates": false }
```

Na Linuksie wyłączanie zwykle nie ma sensu. Jeśli przeszkadza tylko ruch
do repozytoriów, wystarczy `"refresh_package_index": false`.

## Historia zmian

Zainstalowane poprawki trafiają do historii zmian: „zainstalowano KB5001"
to sensowne zdarzenie. Brakujące **nie** — definicje Microsoft Defender
pojawiają się i znikają po kilka razy dziennie i utopiłyby dziennik zmian.
Braki są stanem bieżącym, nie zdarzeniem.

Z tego samego powodu znacznik `checked_at` i `index_age_hours` są pominięte
przy liczeniu odcisku raportu — zmieniają się przy każdym przebiegu, więc
bez tego każdy raport wyglądałby na zmianę i deduplikacja przestałaby
działać.
