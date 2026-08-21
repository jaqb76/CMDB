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
| Debian, Ubuntu | `apt-get -s dist-upgrade` | poniżej sekundy | **nie** |
| RHEL, Fedora, Rocky | `dnf -C check-update` | poniżej sekundy | **nie** |

Na Windows to ten sam mechanizm, z którego korzysta panel sterowania, więc
wynik zgadza się z tym, co użytkownik widzi u siebie. Wyszukiwanie bywa
kosztowne, dlatego ma osobny, wyższy limit czasu (420 s), a niepowodzenie
kończy się stanem `nieznany`, nie psuje zaś całego raportu.

## Czego agent NIE robi

**Nie odświeża indeksu pakietów.** `apt update` pobiera dane z sieci, bierze
blokadę i zmienia stan maszyny — agent ma maszynę inwentaryzować, a nie
modyfikować. Odczytujemy to, co system już wie.

Konsekwencja jest taka, że wiek indeksu staje się częścią wyniku. „Brak
brakujących aktualizacji" przy indeksie sprzed trzech miesięcy nie znaczy
„maszyna aktualna", tylko „nie wiemy". Panel ostrzega, gdy indeks jest
starszy niż tydzień.

Na maszynach, gdzie to przeszkadza, indeks odświeża się normalnymi środkami
systemu — `unattended-upgrades`, `apt-daily.timer`, `dnf-makecache.timer`.

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

Na Linuksie wyłączanie zwykle nie ma sensu — odczyt jest lokalny i trwa
ułamek sekundy.

## Historia zmian

Zainstalowane poprawki trafiają do historii zmian: „zainstalowano KB5001"
to sensowne zdarzenie. Brakujące **nie** — definicje Microsoft Defender
pojawiają się i znikają po kilka razy dziennie i utopiłyby dziennik zmian.
Braki są stanem bieżącym, nie zdarzeniem.

Z tego samego powodu znacznik `checked_at` i `index_age_hours` są pominięte
przy liczeniu odcisku raportu — zmieniają się przy każdym przebiegu, więc
bez tego każdy raport wyglądałby na zmianę i deduplikacja przestałaby
działać.
