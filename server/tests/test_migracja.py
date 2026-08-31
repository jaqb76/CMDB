"""Migracja pomostowa dokladajaca kolumny do bazy z danymi.

Sprawdzamy przypadek, ktory najlatwiej przeoczyc: kolumny wymaganej nie da sie
dodac do tabeli z wierszami bez wartosci domyslnej, ale ta wartosc jest tylko
zgadywaniem. Dla architektury zgadywanie ma konsekwencje - wydanie dla ARM
oznaczone jako x86_64 trafiloby na Raspberry Pi jako plik nie do uruchomienia.

Stan "przed migracja" odtwarzamy, podmieniajac tabele w bazie testowej na jej
starszy uklad. Robimy to na tym samym silniku co produkcja - roznice miedzy
silnikami wychodza wlasnie tutaj (typ boolean, arytmetyka dat), wiec migracja
sprawdzana gdzie indziej nie sprawdzalaby tego, o co chodzi.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from cmdb_server.db import engine


def _naglowek_elf(maszyna: int) -> bytes:
    return (
        bytes.fromhex("7f") + b"ELF" + bytes([2, 1, 1]) + bytes(9)
        + (2).to_bytes(2, "little") + maszyna.to_bytes(2, "little")
    ) + bytes(200)


def _naglowek_pe(maszyna: int) -> bytes:
    trzon = bytearray(b"MZ" + bytes(0x3E))
    trzon[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(trzon) + b"PE" + bytes(2) + maszyna.to_bytes(2, "little") + bytes(200)


def _cofnij_tabele(nazwa: str, definicja: str) -> None:
    """Podmienia tabele na jej starszy uklad.

    CASCADE zdejmuje takze klucze obce wskazujace na te tabele - w bazie
    testowej to bez znaczenia, bo schemat powstaje od nowa przed kazdym
    przypadkiem, a bez tego nie da sie odtworzyc stanu sprzed migracji.
    """
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {nazwa} CASCADE"))
        conn.execute(text(f"CREATE TABLE {nazwa} ({definicja})"))


def _migruj() -> None:
    import cmdb_server.db as modul_bazy

    modul_bazy._dodaj_brakujace_kolumny()


@pytest.fixture()
def stara_baza():
    """Uklad sprzed rozroznienia architektur - agent_releases bez kolumny arch."""
    from cmdb_server.config import get_settings

    katalog_wydan = Path(get_settings().release_dir)
    katalog_wydan.mkdir(parents=True, exist_ok=True)

    _cofnij_tabele(
        "agent_releases",
        """id VARCHAR(36) PRIMARY KEY, version VARCHAR(32) NOT NULL,
           os_family VARCHAR(32) NOT NULL, filename VARCHAR(255) NOT NULL,
           storage_name VARCHAR(128) NOT NULL, sha256 VARCHAR(64) NOT NULL,
           size_bytes INTEGER NOT NULL, notes TEXT,
           created_at TIMESTAMP WITH TIME ZONE, created_by VARCHAR(255)""",
    )
    return katalog_wydan


def _dodaj_wydanie(katalog: Path, wersja: str, system: str, zawartosc: bytes) -> str:
    nazwa = f"{uuid.uuid4().hex}.bin"
    (katalog / nazwa).write_bytes(zawartosc)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_releases (id, version, os_family, filename, "
                "storage_name, sha256, size_bytes, created_by) "
                "VALUES (:id, :wersja, :system, 'agent', :plik, :skrot, :rozmiar, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "wersja": wersja, "system": system,
             "plik": nazwa, "skrot": "x" * 64, "rozmiar": len(zawartosc)},
        )
    return nazwa


def _architektury() -> dict[str, str]:
    with engine.connect() as conn:
        return {
            w.version: w.arch
            for w in conn.execute(text("SELECT version, arch FROM agent_releases"))
        }


def test_architektura_czytana_z_pliku_a_nie_zgadywana(stara_baza):
    katalog = stara_baza
    _dodaj_wydanie(katalog, "0.4.0", "windows", _naglowek_pe(0x8664))
    _dodaj_wydanie(katalog, "0.4.1", "linux", _naglowek_elf(0xB7))
    _dodaj_wydanie(katalog, "0.4.2", "linux", _naglowek_elf(0x3E))

    _migruj()

    assert _architektury() == {
        "0.4.0": "x86_64",
        "0.4.1": "aarch64",   # wartosc domyslna to x86_64 - naglowek ja poprawil
        "0.4.2": "x86_64",
    }


def test_wydanie_z_nieczytelnym_plikiem_zostaje_z_wartoscia_domyslna(stara_baza):
    """Brak pliku nie moze wywrocic startu serwera - zostaje ostrzezenie w logu."""
    katalog = stara_baza
    _dodaj_wydanie(katalog, "0.4.0", "linux", _naglowek_elf(0xB7))
    nazwa = _dodaj_wydanie(katalog, "0.4.1", "linux", b"cokolwiek")
    (katalog / nazwa).unlink()

    _migruj()

    architektury = _architektury()
    assert architektury["0.4.0"] == "aarch64"
    assert architektury["0.4.1"] == "x86_64"


def test_migracja_jest_idempotentna(stara_baza):
    katalog = stara_baza
    _dodaj_wydanie(katalog, "0.4.0", "linux", _naglowek_elf(0xB7))

    _migruj()
    _migruj()     # drugi start serwera

    assert _architektury() == {"0.4.0": "aarch64"}


def test_stare_assets_dostaja_rodzaj_i_zrodlo():
    """Baza zalozona przed podzialem na sprzet z agentem i wpisy reczne.

    Kolumny 'typ' i 'zrodlo' sa wymagane, wiec do tabeli z danymi da sie je
    dolozyc tylko z wartoscia domyslna. Wartosc jest tu faktem, a nie
    zgadywaniem: wszystko, co bylo w bazie wczesniej, przyszlo od agenta
    i jest komputerem - wpisow recznych wtedy jeszcze nie bylo.
    """
    _cofnij_tabele(
        "assets",
        """id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL,
           machine_id VARCHAR(128) NOT NULL, hostname VARCHAR(255) NOT NULL,
           is_active BOOLEAN NOT NULL,
           first_seen TIMESTAMP WITH TIME ZONE, last_seen TIMESTAMP WITH TIME ZONE,
           lifecycle VARCHAR(20) NOT NULL DEFAULT 'aktywny'""",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO assets (id, tenant_id, machine_id, hostname, is_active) "
                "VALUES (:id, :tenant, 'win-0001', 'SRV-STARY', TRUE)"
            ),
            {"id": str(uuid.uuid4()), "tenant": str(uuid.uuid4())},
        )

    _migruj()

    with engine.connect() as conn:
        wiersz = conn.execute(
            text("SELECT typ, zrodlo, lokalizacja_id, uzytkownik_id FROM assets")
        ).one()
    assert wiersz.typ == "komputer"
    assert wiersz.zrodlo == "agent"
    assert wiersz.lokalizacja_id is None
    assert wiersz.uzytkownik_id is None


def test_stare_konta_nie_staja_sie_audytorami():
    """Nowa flaga uprawnien musi dolozyc sie jako WYLACZONA.

    Kolumna wymagana bez poprawnej wartosci domyslnej zamienilaby kazde
    istniejace konto w audytora widzacego wszystkie firmy - czyli cicho
    zniosla izolacje danych przy zwyklej aktualizacji serwera.
    """
    _cofnij_tabele(
        "portal_users",
        """id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36), email VARCHAR(255) NOT NULL,
           password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL,
           is_superadmin BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL,
           created_at TIMESTAMP WITH TIME ZONE""",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO portal_users (id, tenant_id, email, password_hash, role, "
                "is_superadmin, is_active) "
                "VALUES (:id, :tenant, 'admin@firma.pl', 'hash', 'admin', FALSE, TRUE)"
            ),
            {"id": str(uuid.uuid4()), "tenant": str(uuid.uuid4())},
        )

    _migruj()

    with engine.connect() as conn:
        wartosc = conn.execute(text("SELECT is_global_viewer FROM portal_users")).scalar_one()
    assert wartosc is False


def test_wartosc_logiczna_jako_literal_postgresa():
    """PostgreSQL nie przyjmie liczby jako domyslnej wartosci kolumny boolean.

    ALTER TABLE z "DEFAULT 0" konczy sie bledem "column is of type boolean but
    default expression is of type integer", czyli serwer nie wstaje po
    aktualizacji. Sprawdzamy sam literal, bo dotyczy on KAZDEJ przyszlej
    kolumny logicznej, nie tylko tych, ktore juz sa w modelu.
    """
    from sqlalchemy import Boolean, Column

    import cmdb_server.db as modul

    assert modul._domyslna_wartosc(Column("f", Boolean, default=True, nullable=False)) == "TRUE"
    assert modul._domyslna_wartosc(Column("f", Boolean, default=False, nullable=False)) == "FALSE"


def test_konfiguracja_odrzuca_silnik_inny_niz_postgres():
    """Kod uzywa JSONB, blokad doradczych i indeksow GIN - na innym silniku nie
    dziala wcale, wiec lepiej powiedziec to przy starcie niz w polowie pracy."""
    from cmdb_server.config import Settings

    ustawienia = Settings(database_url="sqlite:///./cmdb.db")
    with pytest.raises(RuntimeError, match="wylacznie na PostgreSQL"):
        ustawienia.validate_for_runtime()


# --- rownoczesny start procesow roboczych -----------------------------------
#
# Serwer produkcyjny dziala w kilku procesach i kazdy wykonuje init_db przy
# starcie. Bez blokady wszystkie naraz stwierdzaja brak tabel i probuja je
# utworzyc: jeden wygrywa, reszta dostaje "duplicate key value violates
# unique constraint pg_type_typname_nsp_index" i nie wstaje.

class _PolaczenieAtrapa:
    def __init__(self, dziennik):
        self.dziennik = dziennik

    def execute(self, polecenie, parametry=None):
        self.dziennik.append((str(polecenie), parametry))
        return None

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_schemat_tworzony_pod_blokada(monkeypatch):
    import cmdb_server.db as modul

    dziennik = []
    monkeypatch.setattr(modul.engine, "connect", lambda: _PolaczenieAtrapa(dziennik))
    monkeypatch.setattr(modul, "_utworz_schemat", lambda: dziennik.append(("SCHEMAT", None)))

    modul.init_db()

    kolejnosc = [w[0] for w in dziennik]
    assert "pg_advisory_lock" in kolejnosc[0], "blokada musi byc przed tworzeniem"
    assert kolejnosc[1] == "SCHEMAT"
    assert "pg_advisory_unlock" in kolejnosc[2], "blokade trzeba zwolnic"
    assert dziennik[0][1] == dziennik[2][1], "ten sam klucz przy zajeciu i zwolnieniu"


def test_blokada_zwalniana_takze_po_bledzie(monkeypatch):
    """Nieudana migracja nie moze zostawic blokady - kolejne procesy czekalyby
    na nia w nieskonczonosc."""
    import cmdb_server.db as modul

    dziennik = []
    monkeypatch.setattr(modul.engine, "connect", lambda: _PolaczenieAtrapa(dziennik))

    def padnij():
        raise RuntimeError("migracja padla")

    monkeypatch.setattr(modul, "_utworz_schemat", padnij)

    with pytest.raises(RuntimeError):
        modul.init_db()

    assert any("pg_advisory_unlock" in w[0] for w in dziennik)
