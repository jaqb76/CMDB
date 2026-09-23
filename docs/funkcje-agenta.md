# Zakładka „Agent” i dodatkowe funkcjonalności

Każda maszyna z agentem ma na karcie zakładkę **Agent**. Zbiera ona w jednym
miejscu wszystko, co ten konkretny agent robi, i odpowiada na pytanie, którego
wcześniej nie dało się zadać: czy agent już wie o ostatniej zmianie.

Wpis ręczny (drukarka, switch) tej zakładki nie ma, bo nie ma agenta.

## Co jest w zakładce

| Część | Zawartość |
|---|---|
| Stan | wersja agenta, ostatni kontakt, ile dodatkowych funkcji jest włączonych |
| Funkcje podstawowe | inwentaryzacja (zawsze), lista procesów, aktualizacje agenta |
| Dodatkowe funkcjonalności | podzakładki pogrupowane tematycznie, po jednej na funkcję |

Dziś dodatkowe funkcjonalności to:

| Grupa | Funkcja | Gdzie była wcześniej |
|---|---|---|
| Sieć | Skaner sieci | osobna strona „Polityka skanowania” |
| Sieć | Monitorowanie usług | sekcja „Ta maszyna sprawdza” na przeglądzie karty |
| Wirtualizacja | Nutanix Prism Central | nowa — [`nutanix.md`](nutanix.md) |
| Wirtualizacja | VMware vCenter | nowa — [`vmware.md`](vmware.md) |

Kolejne funkcje dochodzą jako kolejne podzakładki. Menu panelu i rząd zakładek karty się nie zmieniają — funkcji
będzie przybywać, a zakładek karty i tak jest już sporo.

Podzakładka to zwykły adres, np. `/assets/<id>?funkcja=skaner#agent`, więc da
się go wkleić komuś w wiadomości.

## „Czeka na odebranie”

Serwer nigdy nie łączy się z agentem. Zapis w panelu zmienia tylko to, co
agent dostanie, **gdy sam przyjdzie** po konfigurację — przed skanem, przed
okresem monitorowania. Do tej chwili pracuje na poprzednich ustawieniach.

Dlatego serwer zapamiętuje, którą wersję konfiguracji każdej funkcji agent
ostatnio pobrał (tabela `agent_odbior_funkcji`), i porównuje ją z bieżącą:

| Stan | Znaczenie |
|---|---|
| agent ma aktualną konfigurację | ostatnio pobrana wersja = bieżąca |
| czeka na odebranie | zmieniono po ostatnim pobraniu albo włączono, a agent jeszcze nie pobierał |
| agent jeszcze jej nie pobierał | funkcja wyłączona i nigdy niepobrana — nie ma na co czekać |

Stan „czeka” widać w trzech miejscach: w żółtym komunikacie na górze zakładki,
przy nazwie podzakładki (⏳) i na przycisku zakładki **Agent**.

Jeśli „czeka” nie znika, najczęstsze przyczyny to maszyna wyłączona albo bez
kontaktu (patrz „Ostatni kontakt”) oraz agent w wersji, która tej funkcji nie
obsługuje (skaner wymaga 0.5.10).

Wersją skanera jest rewizja polityki, którą agent dostaje w odpowiedzi.
Monitorowanie nie ma rewizji, więc wersją jest skrót treści listy celów —
bez pól, które zmieniają się same (znany odcisk certyfikatu, jednorazowe
„Sprawdź teraz”). Inaczej każde kliknięcie „Sprawdź teraz” zapalałoby „czeka”,
choć konfiguracja się nie zmieniła.

## Który agent co robi

Lista sprzętu ma filtr **„każda funkcja agenta”**. Wybranie funkcji zostawia
na liście maszyny, na których jest włączona — np. wszystkie skanery sieci
w firmie. Adres: `/assets?funkcja=skaner`, `/assets?funkcja=monitorowanie`
`/assets?funkcja=nutanix` albo `/assets?funkcja=vmware`.

## Uprawnienia

Bez zmian względem dotychczasowych stron: zmieniać ustawienia może
administrator firmy i superadmin, widz tylko ogląda (formularz jest
zablokowany). Każda zmiana polityki skanera trafia do audytu.
