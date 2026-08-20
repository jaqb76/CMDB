"""Silnik bazy danych i sesje SQLAlchemy."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()

_connect_args = {}
if _settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine: Engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover - infra
    """Klucze obce w SQLite sa domyslnie wylaczone - wlaczamy je jawnie."""
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db() -> Iterator[Session]:
    """Zaleznosc FastAPI: sesja per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesja dla skryptow CLI - commit/rollback automatycznie."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Tworzy schemat. Docelowo zastapione migracjami Alembic."""
    from . import models  # noqa: F401  (rejestracja mapperow)

    models.Base.metadata.create_all(bind=engine)
    _dodaj_brakujace_kolumny()
    if _settings.is_postgres:
        _create_postgres_indexes()


def _dodaj_brakujace_kolumny() -> None:
    """Doklada kolumny, ktorych nie ma w istniejacych tabelach.

    create_all tworzy brakujace TABELE, ale nie rusza tabel juz istniejacych -
    po dolozeniu pola do modelu baza z danymi zostawala ze starym ukladem
    i zapytania konczyly sie bledem o nieznana kolumne.

    Swiadomie obslugujemy tylko dokladanie kolumn dopuszczajacych NULL,
    czyli przypadek bezpieczny i odwracalny. Zmiany typow, usuwanie kolumn
    i przenoszenie danych wymagaja Alembica - to rozwiazanie pomostowe,
    ktore ma pozwolic zaktualizowac dzialajaca instalacje bez utraty danych.
    """
    from sqlalchemy import inspect, text

    from . import models

    inspector = inspect(engine)
    istniejace_tabele = set(inspector.get_table_names())

    for tabela in models.Base.metadata.sorted_tables:
        if tabela.name not in istniejace_tabele:
            continue
        obecne = {kolumna["name"] for kolumna in inspector.get_columns(tabela.name)}
        for kolumna in tabela.columns:
            if kolumna.name in obecne:
                continue
            typ = kolumna.type.compile(dialect=engine.dialect)
            definicja = f"{kolumna.name} {typ}"

            if not kolumna.nullable:
                # Kolumna wymagana da sie dolozyc do tabeli z danymi tylko
                # wtedy, gdy znamy wartosc dla juz istniejacych wierszy.
                wartosc = _domyslna_wartosc(kolumna)
                if wartosc is None:
                    log.warning(
                        "kolumna %s.%s jest wymagana i nie ma wartosci domyslnej - "
                        "dodaj ja migracja recznie",
                        tabela.name,
                        kolumna.name,
                    )
                    continue
                definicja += f" NOT NULL DEFAULT {wartosc}"

            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tabela.name} ADD COLUMN {definicja}"))
            log.info("dodano brakujaca kolumne %s.%s", tabela.name, kolumna.name)


def _domyslna_wartosc(kolumna) -> str | None:
    """Wartosc domyslna kolumny w postaci gotowej do wstawienia w SQL.

    Obslugujemy wylacznie proste wartosci stale. Domyslne wyliczane funkcja
    (np. znacznik czasu) nie daja jednej wartosci dla istniejacych wierszy,
    wiec takie przypadki zostawiamy do recznej migracji.
    """
    zrodlo = kolumna.default or kolumna.server_default
    if zrodlo is None:
        return None
    wartosc = getattr(zrodlo, "arg", None)
    if wartosc is None or callable(wartosc):
        return None
    if isinstance(wartosc, bool):
        return "1" if wartosc else "0"
    if isinstance(wartosc, (int, float)):
        return str(wartosc)
    if isinstance(wartosc, str):
        return "'" + wartosc.replace("'", "''") + "'"
    return None


def _create_postgres_indexes() -> None:
    """Indeksy GIN na kolumnach JSONB - tylko PostgreSQL."""
    from sqlalchemy import text

    statements = [
        "CREATE INDEX IF NOT EXISTS ix_snapshot_payload_gin "
        "ON inventory_snapshots USING gin (payload jsonb_path_ops)",
        "CREATE INDEX IF NOT EXISTS ix_asset_facts_gin "
        "ON assets USING gin (facts jsonb_path_ops)",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
