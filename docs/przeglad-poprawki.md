# Sesje, agenci, relacje i jakość danych

## Wdrożenie

Przed aktualizacją wykonaj kopię bazy i magazynu wydań. Wdróż razem serwer,
nginx i konfigurację Compose. Start serwera dodaje `session_version`,
`enrollment_blocked` i trzy tabele: `asset_current_reports`, `report_receipts`,
`asset_relations`, używając dotychczasowego mechanizmu aktualizacji schematu.
Nie usuwa istniejących snapshotów ani danych ręcznych.

Dotychczasowe sesje nie mają wersji i zostają unieważnione. Użytkownicy muszą
zalogować się ponownie. Zmiana lub reset hasła oraz „Wyloguj wszystkie
urządzenia” unieważniają wszystkie wcześniejsze ciasteczka użytkownika.

Uvicorn ufa tylko adresowi nginx podanemu w `FORWARDED_ALLOW_IPS`.
Compose przydziela mu domyślnie `172.30.77.10` w sieci `172.30.77.0/24`.
Jeżeli zakres koliduje z infrastrukturą, zmień razem `CMDB_PROXY_IP` i
`CMDB_PROXY_SUBNET`. Przy własnym wdrożeniu ustaw rzeczywisty adres zaufanego
proxy; nie używaj `*`. Nginx nadpisuje X-Forwarded-For adresem klienta.
Aplikacja korzysta ze zweryfikowanego kontekstu ASGI, nie z surowych nagłówków.

## Cofanie dostępu agenta

Cofnięcie klucza w karcie maszyny blokuje również raportowanie i ponowny
enrollment tej maszyny. Obejmuje to stare agenty, które próbują automatycznie
rejestrować się po 401. Administrator może wybrać „Zezwól na ponowną
rejestrację”; stare klucze pozostają nieważne. Następnie trzeba jawnie
zarejestrować agenta tokenem firmowym.

Agent 0.5.7 nie rejestruje się automatycznie po odrzuceniu poświadczenia.
Po udanym enrollment i trwałym zapisie klucza maszyny usuwa wykorzystany
token firmowy z własnego pliku konfiguracji i pamięci procesu. Token
w zewnętrznym systemie dystrybucji konfiguracji lub środowisku usługi trzeba
usunąć również tam. Jeśli plik jest tylko do odczytu, agent zapisuje ostrzeżenie.
Instalatory Windows trzeba zbudować ponownie i wydać jako 0.5.7; tego PR nie
należy traktować jako gotowego podpisanego instalatora EXE.

## Odczyty i historia

Ostatni pełny odczyt jest przechowywany niezależnie od historii konfiguracji.
Zmiana wolnego miejsca, procesów, sesji lub błędów odświeża kartę maszyny
i surowy JSON, ale nie tworzy snapshotu tylko z tego powodu. Widok podaje
datę pomiaru i odbioru. Jawny wybór snapshotu nadal pokazuje stan historyczny.
Do pierwszego nowego raportu po aktualizacji wyświetlany jest dotychczasowy
snapshot. Raporty i zestawienia korzystają z bieżącego odczytu.

Kolejność znanych list będących zbiorami (np. pakietów) nie powoduje zmian.
Kolejność nieznanych rozszerzeń pozostaje znacząca. Nowy agent dodaje UUID
`report_id`, zachowywany również w buforze offline. Starsze agenty są
obsługiwane poprzez hash pełnej treści. Powtórzenie raportu jest bezpieczne;
ten sam UUID z inną treścią zwraca 409. Odpowiedź na powtórzenie może mieć
`snapshot_id: null`, bo raport nie tworzy nowego snapshotu.

Zapisy jednej maszyny są serializowane blokadą w bazie. Raport starszy niż
aktualny pomiar nie nadpisuje bieżącego stanu ani nie generuje odwrotnych
zdarzeń zmian. Może zostać zapisany w historii, z zastosowaniem jej retencji.
Znane błędne typy pól są odrzucane odpowiedzią 422; nowe pola nadal są dozwolone.
Małe potwierdzenia w `report_receipts` pozostają po retencji snapshotów,
aby późniejsze powtórzenie nie odtwarzało historii. Ich rozmiar należy
uwzględnić przy monitorowaniu wzrostu bazy.

## Relacje

`/relacje` pokazuje kierunkowe relacje i pozwala je dodawać oraz usuwać.
Obsługiwane są VM → host, host → klaster i aplikacja → serwer.
Typy VM, host, klaster i aplikacja są dostępne przy ręcznym dodawaniu zasobu.
Dotychczasowy typ „Komputer / serwer” jest akceptowany jako VM/host/serwer.
Jedna VM może mieć jeden host, host jeden klaster, a aplikacja wiele serwerów.
Zmianę hosta lub klastra wykonuje się przez usunięcie starej i dodanie nowej relacji.

Nie można utworzyć relacji do samego siebie, cyklu, duplikatu ani relacji
między firmami. Operacje są audytowane. Viewer i audytor globalny mają tylko
odczyt. Link z karty zasobu filtruje relacje przychodzące i wychodzące.

## Jakość danych

`/jakosc` podaje liczniki i listę aktywnych zasobów z problemami:
błędy kolektorów, nieaktualny lub brakujący pomiar, brak opiekuna i podejrzenie
duplikatu. Próg czasu pochodzi z ustawień firmy. Świeży kontakt nie ukrywa
starego pomiaru. Zasoby ręczne nie wymagają kontaktu ani raportów.
Wycofane zasoby są pomijane. Duplikaty są wskazówką do ręcznej weryfikacji.
Lista ma filtry kategorii i strony po 50 zasobów; karta zasobu zawiera
szczegóły błędów oraz formularz przypisania opiekuna.

## Testy

Nowe regresje: `server/tests/test_review_improvements.py` oraz
`agent/tests/test_enrollment_security.py`. Workflow `CMDB regression tests`
uruchamia komplet testów serwera z PostgreSQL 16 oraz testy agenta.
