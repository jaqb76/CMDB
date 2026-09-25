"""Konfiguracja serwera CMDB (zmienne srodowiskowe / plik .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CMDB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "prod"] = "dev"

    # PostgreSQL - jedyny wspierany silnik, takze w developmencie i testach.
    # Wczesniej dev i testy chodzily na SQLite. Wygoda kosztowala wiecej, niz
    # dawala: SQLite nie ma typu boolean ani JSONB, wiec migracje i zapytania
    # zachowywaly sie tam inaczej niz na produkcji, a roznica wychodzila
    # dopiero przy starcie serwera klienta. Jeden silnik wszedzie znaczy, ze
    # to, co przeszlo testy, zadziala tak samo na produkcji.
    database_url: str = "postgresql+psycopg://cmdb:cmdb@localhost:5432/cmdb"

    # Klucz do podpisywania ciasteczek sesyjnych. W prod MUSI byc ustawiony.
    secret_key: str = "dev-only-insecure-key-change-me"

    session_cookie: str = "cmdb_session"
    session_max_age: int = 8 * 3600

    # Po ilu nieudanych probach logowanie zostaje zablokowane i na ile godzin.
    # Blokada obejmuje konto ORAZ adres, z ktorego przyszly proby.
    login_max_failures: int = 3
    login_lockout_hours: int = 24

    # Wymuszenie HTTPS (Secure cookie + HSTS + redirect). Wylaczane tylko w dev.
    require_https: bool = False

    # Ile snapshotow trzymamy per maszyna (0 = bez limitu).
    snapshot_retention: int = 50

    # Maksymalny rozmiar raportu przyjmowany od agenta.
    max_report_bytes: int = 8 * 1024 * 1024

    # Katalog z wgranymi wersjami agenta (pliki .exe rozsylane na maszyny).
    release_dir: str = "./releases"
    # Zalaczniki zgloszen helpdesku. Osobny katalog od wydan agenta: te pliki
    # przysyla klient, wiec nie moga lezec obok plikow, ktore serwer wydaje
    # jako zaufane.
    helpdesk_dir: str = "./helpdesk"
    # Gorna granica jednego zalacznika. Skrzynka i tak odrzuci wieksze, ale
    # serwer nie moze polegac na cudzych limitach.
    helpdesk_zalacznik_mb: int = 25
    # Zalaczniki artykulow bazy wiedzy. Osobny katalog, bo to pliki wgrywane
    # przez zespol, a nie przysylane przez klientow - i osobno je sie
    # przenosi oraz odtwarza z kopii.
    wiedza_dir: str = "./wiedza"
    # Gorna granica jednego zalacznika artykulu. Odpowiednik w nginx:
    # location dla /wiedza/a/.../zalaczniki w deploy/nginx.
    wiedza_zalacznik_mb: int = 16

    # --- kopie zapasowe ---
    # Katalog archiwow. Ten sam dla nocnego zadania i dla panelu, zeby lista
    # w panelu pokazywala to, co faktycznie lezy na dysku.
    kopie_dir: str = "./kopie"
    # Ile kopii trzymamy. Dzienne rotuja sie codziennie, tygodniowe to kopie
    # z niedzieli - razem daje to miesiac wstecz bez trzymania trzydziestu
    # plikow.
    kopie_dziennych: int = 7
    kopie_tygodniowych: int = 4
    # Maksymalny rozmiar archiwum przyjmowanego przez formularz przywracania.
    # Wiekszej kopii nie przepchniesz przegladarka - zostaje scp i CLI.
    max_kopia_bytes: int = 512 * 1024 * 1024

    # --- oznaczenie instancji ---
    # Pusty napis znaczy produkcje: zaden pasek, zaden prefiks w tytule.
    # Na instancji testowej napis pojawia sie u gory kazdej strony i w tytule
    # karty przegladarki - wlasciwa karte znajduje sie po tytule, nie po
    # kolorze. Etykieta jest tekstem, a nie flaga, bo obsluguje tez instalacje
    # zapasowa ("KOPIA Z 17.09") i szkoleniowa.
    instancja: str = ""
    # Barwa z zamknietej listy, nie dowolny kolor: wartosc trafia do CSS jako
    # KLASA, bo Content-Security-Policy tej aplikacji ma style-src 'self'
    # i atrybut style="" jest w przegladarce ignorowany.
    instancja_barwa: Literal["bursztyn", "czerwony", "fiolet", "zielony"] = "bursztyn"

    @property
    def etykieta_instancji(self) -> str:
        """Napis do paska. Na dev wlacza sie sam, nawet przy pustym polu.

        Zapomniec mozna w obie strony, ale tylko jedna boli: instancja testowa
        wygladajaca jak produkcja. Dlatego CMDB_ENV=dev wystarcza, zeby
        oznaczenie bylo widac.
        """
        if self.instancja.strip():
            return self.instancja.strip()
        return "DEVELOPMENT" if self.env == "dev" else ''
    # Independent trust root, deployed by server operations, NEVER populated
    # from an upload/footer. SHA-256 -> {version, arch} of tested Windows workers.
    # Empty by default: no Windows worker can be activated/distributed.
    trusted_windows_builds: dict[str, dict[str, str]] = Field(default_factory=dict)
    release_import_enabled: bool = False
    release_repository: str = "jaqb76/CMDB"
    release_ref: str = "refs/heads/claude/os-data-collection-agent-gfz2o8"
    release_public_keys: dict[str, str] = Field(default_factory=dict)
    release_github_token: SecretStr = SecretStr("")
    release_import_interval: int = Field(default=300, ge=60, le=86400)

    @field_validator("release_public_keys", mode="before")
    @classmethod
    def empty_release_keys(cls, value):
        return {} if value is None else value

    @field_validator("release_public_keys")
    @classmethod
    def check_release_keys(cls, value):
        from .release_manifest import decode, key_id
        for identifier, public in value.items():
            if key_id(decode(public, 32)) != identifier:
                raise ValueError("release_public_keys: identyfikator nie odpowiada kluczowi")
        return value

    @field_validator("release_repository")
    @classmethod
    def check_release_repository(cls, value):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("release_repository wymaga owner/repository")
        return value

    @field_validator("trusted_windows_builds", mode="before")
    @classmethod
    def empty_trust_catalog(cls, value):
        return {} if value is None else value

    @field_validator("trusted_windows_builds")
    @classmethod
    def validate_trusted_builds(cls, value):
        import re
        for digest, build in value.items():
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("trusted_windows_builds: wymagany maly hex SHA-256")
            if (set(build) != {"version", "arch"} or
                    not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", build["version"]) or
                    build["arch"] not in {"x86_64", "x86", "aarch64", "arm"}):
                raise ValueError("trusted_windows_builds: wymagane version i arch sprawdzonego workera")
        return value
    # Wgrywana wersja agenta jest znacznie wieksza niz raport - spakowany
    # PyInstallerem agent z interfejsem ma okolo 30 MB.
    max_release_bytes: int = 128 * 1024 * 1024

    # Publiczny adres serwera, wpisywany w wydawany skrypt instalacyjny.
    # Za posrednikiem (nginx) adres z zadania bywa adresem kontenera, wiec
    # zgadywanie go z naglowkow konczyloby sie instalacja wskazujaca w nicosc.
    public_url: str = ""

    # Katalog ze zrodlami agenta - jest tylko w repozytorium, nie w obrazie
    # produkcyjnym. Gdy istnieje, serwer sam odswieza paczke do pobrania.
    agent_source_dir: str = ""

    # Katalog z paczka zrodel agenta. Domyslnie ten sam co magazyn wydan, ale
    # w obrazie produkcyjnym musi byc osobny: wydania wgrywa administrator
    # i zyja w wolumenie, a paczka jest czescia obrazu i zmienia sie razem
    # z kodem. Pusty wolumen podmontowany na magazyn wydan przyslonilby ja.
    agent_bundle_dir: str = ""

    # Klucz do NVD. Bez niego limit wynosi 5 zapytan na 30 sekund, z nim 50 -
    # przy wiekszej flocie roznica miedzy kilkoma minutami a godzina.
    # Bezplatny: https://nvd.nist.gov/developers/request-an-api-key
    nvd_api_key: str = ""

    # Co ile godzin serwer sam pobiera dane o podatnosciach (kanaly dystrybucji
    # i brakujace oceny CVSS). Dystrybucje wydaja poprawki codziennie, wiec
    # doba to rozsadny rytm. 0 wylacza pobieranie w tle - zostaje przycisk
    # na stronie Podatnosci.
    cve_refresh_hours: int = Field(default=24, ge=0)

    # --- monitorowanie uslug i certyfikatow ---
    # Sonduje AGENT, nie serwer: usluga zyje w sieci klienta, ktorej serwer
    # zwykle nie widzi. Serwer wydaje polityke, przyjmuje wyniki i powiadamia.
    monitoring_enabled: bool = True
    # Co ile sekund agent ma przysylac podsumowanie okresu. Awarie ida osobno
    # i natychmiast, wiec ten odstep dotyczy wylacznie potwierdzen, ze
    # wszystko dziala - a takie potwierdzenie to jeden wiersz na cel.
    monitoring_report_seconds: int = Field(default=900, ge=60, le=3600)
    # Ile celow moze miec jedna firma.
    monitoring_max_targets: int = Field(default=200, ge=1, le=5000)
    # Ile celow wolno przypisac jednemu agentowi. Agent sonduje z maszyny,
    # ktora ma tez inna prace do wykonania - przy dwustu celach co minute
    # monitorowanie przestaje byc dodatkiem, a zaczyna byc jej glownym zajeciem.
    monitoring_max_per_agent: int = Field(default=50, ge=1, le=500)
    # Ile dni trzymamy okna i przerwy. Dostepnosc liczy sie z historii, wiec
    # musi ona siegac dalej niz okno, ktore pokazujemy w panelu.
    monitoring_history_days: int = Field(default=90, ge=1, le=730)
    # Adres petli zwrotnej jako cel jest domyslnie zabroniony: 127.0.0.1
    # w panelu znaczy "maszyna agenta", a nie to, co zwykle ma na mysli
    # wpisujacy - i takiego celu nie da sie sprawdzic z zewnatrz.
    monitoring_allow_loopback: bool = False

    # Po ilu godzinach bez kontaktu maszyna jest oznaczana jako "stale".
    stale_after_hours: int = 48

    # Co ile sekund agent ma raportowac (serwer narzuca to w odpowiedzi).
    report_interval_seconds: int = 3600

    log_level: str = "INFO"

    @field_validator("secret_key")
    @classmethod
    def _check_secret(cls, v: str, info) -> str:
        return v

    def validate_for_runtime(self) -> None:
        """Twarde zabezpieczenia, ktore nie moga przejsc na produkcje."""
        # Silnik bazy sprawdzamy w KAZDYM trybie, nie tylko na produkcji.
        # Kod uzywa JSONB, blokad doradczych i indeksow GIN - na innym silniku
        # nie tyle dziala gorzej, co nie dziala wcale, a komunikat "nieznana
        # funkcja pg_advisory_lock" w polowie startu nie mowi nikomu, o co chodzi.
        if self.release_import_enabled and (not self.release_public_keys or not self.release_ref.startswith("refs/heads/")):
            raise RuntimeError("Import wydań wymaga zaufanych kluczy i zatwierdzonej gałęzi")
        if not self.database_url.startswith("postgresql"):
            raise RuntimeError(
                "CMDB dziala wylacznie na PostgreSQL - ustaw CMDB_DATABASE_URL "
                "w postaci postgresql+psycopg://uzytkownik:haslo@host:5432/baza "
                f"(jest: {self.database_url.split('://')[0]})"
            )
        if self.env != "prod":
            return
        problems = []
        if self.secret_key == "dev-only-insecure-key-change-me" or len(self.secret_key) < 32:
            problems.append("CMDB_SECRET_KEY musi byc ustawiony i miec >= 32 znaki")
        if not self.require_https:
            problems.append("CMDB_REQUIRE_HTTPS musi byc wlaczone w trybie prod")
        if problems:
            raise RuntimeError("Bledna konfiguracja produkcyjna: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
