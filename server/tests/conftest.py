"""Wspolne fixtury testow.

Zmienne srodowiskowe ustawiamy PRZED importem aplikacji - config i engine
tworza sie na poziomie modulu.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_TMPDIR = tempfile.mkdtemp(prefix="cmdb-tests-")
os.environ["CMDB_ENV"] = "dev"
os.environ["CMDB_SECRET_KEY"] = "test-secret-key-that-is-long-enough-123456"
os.environ["CMDB_REQUIRE_HTTPS"] = "false"
# Wgrywane wersje agenta laduja w katalogu tymczasowym testu, a nie
# w katalogu roboczym repozytorium.
os.environ["CMDB_RELEASE_DIR"] = str(Path(_TMPDIR) / "releases")
# Testy chodza na tym samym silniku co produkcja. Wczesniej domyslnym byl
# SQLite - szybki i bez zaleznosci, ale rozniacy sie od Postgresa dokladnie
# w tych miejscach, w ktorych roznica boli: typ boolean, JSONB, arytmetyka
# dat, blokady doradcze. Blad tego rodzaju przechodzil przez caly zestaw
# testow i wychodzil dopiero przy starcie serwera klienta.
#
# Wskaz wlasna baze, jesli Twoja instalacja ma inne dane dostepowe:
#   CMDB_DATABASE_URL=postgresql+psycopg://uzytkownik:haslo@localhost:5432/cmdb_test
os.environ.setdefault(
    "CMDB_DATABASE_URL", "postgresql+psycopg://cmdb:cmdb@localhost:5432/cmdb_test"
)

from fastapi.testclient import TestClient  # noqa: E402

from cmdb_server.db import SessionLocal, engine, init_db  # noqa: E402
from cmdb_server.main import app  # noqa: E402
from cmdb_server.models import Base, EnrollmentToken, PortalUser, Tenant  # noqa: E402
from cmdb_server.security import generate_token, hash_password  # noqa: E402


def _guard_destructive_database() -> None:
    """Testy kasuja caly schemat przed kazdym przypadkiem.

    Wskazanie CMDB_DATABASE_URL na baze z danymi skasowaloby ja bez
    ostrzezenia. Dopuszczamy wiec tylko baze, ktorej nazwa zawiera "test",
    chyba ze ktos swiadomie ustawi CMDB_TEST_ALLOW_DESTRUCTIVE=1.
    """
    url = os.environ["CMDB_DATABASE_URL"]
    if os.environ.get("CMDB_TEST_ALLOW_DESTRUCTIVE") == "1":
        return
    database_name = urlsplit(url).path.lstrip("/")
    if "test" in database_name.lower():
        return
    pytest.exit(
        f"Odmawiam uruchomienia testow na bazie '{database_name}': testy kasuja "
        "caly schemat. Uzyj bazy z 'test' w nazwie albo ustaw "
        "CMDB_TEST_ALLOW_DESTRUCTIVE=1, jesli wiesz co robisz.",
        returncode=1,
    )


def _guard_polaczenie() -> None:
    """Sprawdza polaczenie ZANIM ruszy pierwszy test.

    Bez tego brak bazy objawia sie sciana bledow polaczenia przy kazdym
    z kilkuset przypadkow i trzeba ja przewinac, zeby zobaczyc jedna
    informacje: nie ma z czym sie polaczyc.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from cmdb_server.db import engine

    try:
        with engine.connect():
            pass
    except SQLAlchemyError as blad:
        pytest.exit(
            "Nie moge polaczyc sie z baza testowa "
            f"({os.environ['CMDB_DATABASE_URL'].split('@')[-1]}):\n  {blad}\n\n"
            "CMDB dziala wylacznie na PostgreSQL. Zaloz baze testowa, np.:\n"
            "  psql -U postgres -c \"CREATE ROLE cmdb LOGIN PASSWORD 'cmdb'\"\n"
            "  psql -U postgres -c \"CREATE DATABASE cmdb_test OWNER cmdb\"\n"
            "albo wskaz wlasna przez CMDB_DATABASE_URL.",
            returncode=1,
        )


_guard_destructive_database()
_guard_polaczenie()


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_tenant(slug: str, name: str) -> tuple[str, str]:
    """Zwraca (tenant_id, jawny token rejestracyjny)."""
    with SessionLocal() as db:
        tenant = Tenant(name=name, slug=slug)
        db.add(tenant)
        db.flush()
        token = generate_token("ent")
        db.add(
            EnrollmentToken(
                tenant_id=tenant.id,
                name=f"token {slug}",
                prefix=token.prefix,
                token_hash=token.token_hash,
            )
        )
        db.commit()
        return tenant.id, token.plaintext


def _make_user(tenant_id: str | None, email: str, password: str, role: str = "admin") -> str:
    with SessionLocal() as db:
        user = PortalUser(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
            role=role,
            is_superadmin=tenant_id is None,
        )
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture
def tenant_a():
    tenant_id, token = _make_tenant("firma-a", "Firma A")
    return {"id": tenant_id, "token": token, "slug": "firma-a"}


@pytest.fixture
def tenant_b():
    tenant_id, token = _make_tenant("firma-b", "Firma B")
    return {"id": tenant_id, "token": token, "slug": "firma-b"}


@pytest.fixture
def make_user():
    return _make_user
