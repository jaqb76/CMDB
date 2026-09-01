# CMDB Mobile

Natywna aplikacja Android dla portalu CMDB. Nie osadza portalu w WebView.

## Wymagania

- Android Studio z JDK 17
- Android SDK 35
- serwer CMDB z API `/api/v1/mobile`
- publiczny adres serwera z prawidlowym certyfikatem HTTPS

## Uruchomienie

1. Otworz katalog `android` w Android Studio.
2. Poczekaj na synchronizacje Gradle.
3. Uruchom konfiguracje `app` na emulatorze lub telefonie z Androidem 8+.
4. Na ekranie logowania wpisz pelny adres portalu, np. `https://cmdb.example.pl`.

Adres HTTP jest celowo odrzucany. Token sesji jest przechowywany przy uzyciu
Android Keystore, a kopie zapasowe aplikacji sa wylaczone, aby poswiadczenie
nie trafilo do backupu konta Google.

## Zakres API

- logowanie tym samym kontem co portal,
- izolacja firm i tryb globalnego audytora/superadmina,
- pulpit,
- maszyny i karty urzadzen,
- osoby, przypisania i slowniki,
- historia zmian i audyt,
- definicje raportow oraz wysylka na zadanie.

Uprawnienia `viewer`, `admin`, `superadmin` i `global_viewer` sa wyliczane po
stronie serwera. Ukrycie przycisku w aplikacji nie jest mechanizmem ochrony;
kazdy zapis jest ponownie autoryzowany i audytowany przez API.
