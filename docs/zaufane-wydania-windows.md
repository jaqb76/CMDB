# Zaufane wydania Windows — 0.5.10

`entry_mode: unified` jest metadanymi, a nie dowodem obsługi workera. Sam
podsystem PE też nie stanowi dowodu: można go zmienić w pliku. Dlatego zarówno
okienkowe, jak i konsolowe wydania Windows wymagają niezależnego przypięcia
**pełnego SHA-256 pliku, wersji i architektury** w konfiguracji wdrożenia serwera.
Panel uploadu nie może zmieniać tego katalogu ani automatycznie ufać uploadowi.
Serwer nie uruchamia przesłanego EXE.

## Wdrożenie

1. Wdróż nowy serwer. Domyślny pusty katalog blokuje aktywację, oferty i pobieranie
   workerów Windows, także z wcześniej zapisanych przypisań. Już zainstalowane
   agenty nadal raportują. Linux pozostaje bez zmiany mechanizmu dystrybucji.
2. Sprawdź konkretny commit i pomyślny przebieg zaufanego workflow
   `CMDB regression tests`. Job Windows buduje EXE i setup oraz sprawdza
   CLI, `worker-probe`, cichy tray, pojedynczą instancję, restart po podmianie
   i GUI. Plik `verified-worker.json` zawiera skrót rzeczywiście testowanego EXE.
3. Administrator wdrożenia umieszcza zawartość tego pliku w ustawieniu serwera
   `CMDB_TRUSTED_WINDOWS_BUILDS`, np. w `deploy/.env` jako jedno liniowe JSON
   w pojedynczych cudzysłowach. Zastosuj ustawienie przez odtworzenie kontenera
   serwera. W procesie bez Compose użyj tej samej zmiennej środowiskowej.
4. Dopiero wtedy wgraj odpowiadający plik `cmdb-agent.exe` i aktywuj wydanie
   najpierw dla grupy pilotażowej. Setup NIE jest workerem do tego uploadu.
5. Po aktualizacji agentów włącz wymagane polityki skanowania w kartach zasobów.
   Polityka domyślnie wyłącza skaner; stare lokalne ustawienia nie dają zgody.

Nie kopiuj skrótu z dowolnego uploadu tylko po to, by usunąć ostrzeżenie:
zaufanie pochodzi z przeglądu kodu i kontrolowanego buildu. Chroń uprawnienia
do repozytorium, workflow i konfiguracji serwera. `verified-worker.json` jest
pomocą wdrożeniową, nie podpisaną atestacją ani samodzielnym źródłem zaufania.
To jawny katalog skrótów, nie automatyczne sprawdzanie podpisów Authenticode/SLSA.
Jeśli podpisujesz EXE po buildzie, przetestuj i przypnij jego końcowe bajty:
podpisanie zmienia SHA-256. Standardowy artefakt CI jest niepodpisany.

## Kontrole w czasie pracy

Przy aktywacji, wyznaczaniu oferty i pobraniu serwer ponownie sprawdza katalog,
architekturę PE oraz rzeczywisty SHA-256 pliku w magazynie. Wycofanie wpisu lub
zmiana bajtów blokuje kolejne oferty/pobrania; nie odinstalowuje już wdrożonego
programu. Magazyn pozostaje zapisywalny wyłącznie dla procesu serwera/administratorów.

Aktualizator 0.5.10 przed podmianą pliku sprawdza dokładny numer wersji oraz
odpowiedź `worker-probe` z losowym nonce i protokołem `cmdb-policy-v1`.
Nie jest to dowód zaufania do dowolnego pliku — jest dodatkowym testem
kompatybilności dla binariów dopuszczonych przez serwer. Test nie uruchamia
inwentaryzacji, skanowania ani konfiguracji. Niezgodny kandydat nie zastępuje
bieżącego EXE; test po podmianie zachowuje dotychczasową ścieżkę rollbacku.

Starszy agent 0.5.9 nie ma tego testu ani centralnej polityki. Ochronę jego
aktualizacji zapewnia nowa bramka serwerowa. Automatyczny downgrade z 0.5.10
do 0.5.9 jest odrzucany jako niezgodny protokół; awaryjny rollback do starego
pakietu wymaga świadomej instalacji ręcznej i wyłączenia skanera starej wersji.

Użytkownik zwykłej sesji Windows nie steruje skanerem w GUI. Lokalny
administrator/SYSTEM może jednak zastąpić program lub jego konfigurację
połączenia — ta implementacja nie próbuje chronić systemu przed jego właścicielem.
