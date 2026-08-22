"""Konfiguracja serwera CMDB (zmienne srodowiskowe / plik .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CMDB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "prod"] = "dev"

    # Postgres w produkcji (JSONB), SQLite wystarcza do developmentu i testow.
    database_url: str = "sqlite:///./cmdb.db"

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
        if self.env != "prod":
            return
        problems = []
        if self.secret_key == "dev-only-insecure-key-change-me" or len(self.secret_key) < 32:
            problems.append("CMDB_SECRET_KEY musi byc ustawiony i miec >= 32 znaki")
        if not self.require_https:
            problems.append("CMDB_REQUIRE_HTTPS musi byc wlaczone w trybie prod")
        if self.database_url.startswith("sqlite"):
            problems.append("SQLite nie jest wspierany w trybie prod - uzyj PostgreSQL")
        if problems:
            raise RuntimeError("Bledna konfiguracja produkcyjna: " + "; ".join(problems))

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
