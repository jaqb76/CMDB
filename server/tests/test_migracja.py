"""Migracja pomostowa dokladajaca kolumny do bazy z danymi.

Sprawdzamy przypadek, ktory najlatwiej przeoczyc: kolumny wymaganej nie da sie
dodac do tabeli z wierszami bez wartosci domyslnej, ale ta wartosc jest tylko
zgadywaniem. Dla architektury zgadywanie ma konsekwencje - wydanie dla ARM
oznaczone jako x86_64 trafiloby na Raspberry Pi jako plik nie do uruchomienia.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def _naglowek_elf(maszyna: int) -> bytes:
    return (
        bytes.fromhex("7f") + b"ELF" + bytes([2, 1, 1]) + bytes(9)
        + (2).to_bytes(2, "little") + maszyna.to_bytes(2, "little")
    ) + bytes(200)


def _naglowek_pe(maszyna: int) -> bytes:
    trzon = bytearray(b"MZ" + bytes(0x3E))
    trzon[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(trzon) + b"PE" + bytes(2) + maszyna.to_bytes(2, "little") + bytes(200)


@pytest.fixture()
def stara_baza(tmp_path, monkeypatch):
    """Baza w ukladzie sprzed rozroznienia architektur - agent_releases bez arch."""
    # Ustawienia sa zacachowane, wiec nie da sie ich przestawic zmienna
    # srodowiskowa po starcie - pliki kladziemy tam, gdzie serwer ich szuka.
    from cmdb_server.config import get_settings

    katalog_wydan = Path(get_settings().release_dir)
    katalog_wydan.mkdir(parents=True, exist_ok=True)

    plik_bazy = tmp_path / "stara.db"
    db = sqlite3.connect(plik_bazy)
    db.execute(
        """CREATE TABLE agent_releases (
               id TEXT PRIMARY KEY, version TEXT NOT NULL, os_family TEXT NOT NULL,
               filename TEXT NOT NULL, storage_name TEXT NOT NULL, sha256 TEXT NOT NULL,
               size_bytes INTEGER NOT NULL, notes TEXT, created_at TIMESTAMP,
               created_by TEXT)"""
    )
    db.commit()
    db.close()
    return plik_bazy, katalog_wydan


def _dodaj_wydanie(plik_bazy: Path, katalog: Path, wersja: str, system: str, zawartosc: bytes):
    nazwa = f"{uuid.uuid4().hex}.bin"
    (katalog / nazwa).write_bytes(zawartosc)
    db = sqlite3.connect(plik_bazy)
    db.execute(
        "INSERT INTO agent_releases VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), wersja, system, "agent", nazwa, "x" * 64,
         len(zawartosc), None, None, "admin"),
    )
    db.commit()
    db.close()
    return nazwa


def _migruj(plik_bazy: Path, monkeypatch):
    import cmdb_server.db as modul_bazy

    silnik = create_engine(f"sqlite:///{plik_bazy.as_posix()}", future=True)
    monkeypatch.setattr(modul_bazy, "engine", silnik)
    modul_bazy._dodaj_brakujace_kolumny()
    return silnik


def _architektury(silnik) -> dict[str, str]:
    with silnik.connect() as polaczenie:
        return {
            w.version: w.arch
            for w in polaczenie.execute(text("SELECT version, arch FROM agent_releases"))
        }


def test_architektura_czytana_z_pliku_a_nie_zgadywana(stara_baza, monkeypatch):
    plik_bazy, katalog = stara_baza
    _dodaj_wydanie(plik_bazy, katalog, "0.4.0", "windows", _naglowek_pe(0x8664))
    _dodaj_wydanie(plik_bazy, katalog, "0.4.1", "linux", _naglowek_elf(0xB7))
    _dodaj_wydanie(plik_bazy, katalog, "0.4.2", "linux", _naglowek_elf(0x3E))

    silnik = _migruj(plik_bazy, monkeypatch)

    assert _architektury(silnik) == {
        "0.4.0": "x86_64",
        "0.4.1": "aarch64",   # wartosc domyslna to x86_64 - naglowek ja poprawil
        "0.4.2": "x86_64",
    }


def test_wydanie_z_nieczytelnym_plikiem_zostaje_z_wartoscia_domyslna(stara_baza, monkeypatch):
    """Brak pliku nie moze wywrocic startu serwera - zostaje ostrzezenie w logu."""
    plik_bazy, katalog = stara_baza
    _dodaj_wydanie(plik_bazy, katalog, "0.4.0", "linux", _naglowek_elf(0xB7))
    nazwa = _dodaj_wydanie(plik_bazy, katalog, "0.4.1", "linux", b"cokolwiek")
    (katalog / nazwa).unlink()

    silnik = _migruj(plik_bazy, monkeypatch)

    architektury = _architektury(silnik)
    assert architektury["0.4.0"] == "aarch64"
    assert architektury["0.4.1"] == "x86_64"


def test_migracja_jest_idempotentna(stara_baza, monkeypatch):
    plik_bazy, katalog = stara_baza
    _dodaj_wydanie(plik_bazy, katalog, "0.4.0", "linux", _naglowek_elf(0xB7))

    _migruj(plik_bazy, monkeypatch)
    silnik = _migruj(plik_bazy, monkeypatch)     # drugi start serwera

    assert _architektury(silnik) == {"0.4.0": "aarch64"}


# --- rownoczesny start procesow roboczych -----------------------------------
#
# Serwer produkcyjny dziala w kilku procesach i kazdy wykonuje init_db przy
# starcie. Bez blokady wszystkie naraz stwierdzaja brak tabel i probuja je
# utworzyc: jeden wygrywa, reszta dostaje "duplicate key value violates
# unique constraint pg_type_typname_nsp_index" i nie wstaje.

class _UstawieniaAtrapa:
    """is_postgres jest wlasciwoscia tylko do odczytu, wiec podstawiamy calosc."""

    def __init__(self, is_postgres: bool):
        self.is_postgres = is_postgres


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


def test_schemat_tworzony_pod_blokada_na_postgresie(monkeypatch):
    import cmdb_server.db as modul

    dziennik = []
    monkeypatch.setattr(modul, "_settings", _UstawieniaAtrapa(True))
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
    monkeypatch.setattr(modul, "_settings", _UstawieniaAtrapa(True))
    monkeypatch.setattr(modul.engine, "connect", lambda: _PolaczenieAtrapa(dziennik))

    def padnij():
        raise RuntimeError("migracja padla")

    monkeypatch.setattr(modul, "_utworz_schemat", padnij)

    with pytest.raises(RuntimeError):
        modul.init_db()

    assert any("pg_advisory_unlock" in w[0] for w in dziennik)


def test_sqlite_nie_uzywa_blokady_doradczej(monkeypatch):
    """Blokada doradcza to konstrukcja Postgresa - na SQLite wywolanie jej
    zakonczyloby sie bledem skladni."""
    import cmdb_server.db as modul

    dziennik = []
    monkeypatch.setattr(modul, "_settings", _UstawieniaAtrapa(False))
    monkeypatch.setattr(modul.engine, "connect", lambda: _PolaczenieAtrapa(dziennik))
    monkeypatch.setattr(modul, "_utworz_schemat", lambda: dziennik.append(("SCHEMAT", None)))

    modul.init_db()
    assert dziennik == [("SCHEMAT", None)]


def test_stare_assets_dostaja_rodzaj_i_zrodlo(tmp_path, monkeypatch):
    """Baza zalozona przed podzialem na sprzet z agentem i wpisy reczne.

    Kolumny 'typ' i 'zrodlo' sa wymagane, wiec do tabeli z danymi da sie je
    dolozyc tylko z wartoscia domyslna. Wartosc jest tu faktem, a nie
    zgadywaniem: wszystko, co bylo w bazie wczesniej, przyszlo od agenta
    i jest komputerem - wpisow recznych wtedy jeszcze nie bylo.
    """
    plik_bazy = tmp_path / "assets.db"
    db = sqlite3.connect(plik_bazy)
    db.execute(
        """CREATE TABLE assets (
               id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, machine_id TEXT NOT NULL,
               hostname TEXT NOT NULL, is_active BOOLEAN NOT NULL,
               first_seen TIMESTAMP, last_seen TIMESTAMP,
               lifecycle TEXT NOT NULL DEFAULT 'aktywny')"""
    )
    db.execute(
        "INSERT INTO assets (id, tenant_id, machine_id, hostname, is_active) VALUES (?,?,?,?,1)",
        (str(uuid.uuid4()), str(uuid.uuid4()), "win-0001", "SRV-STARY"),
    )
    db.commit()
    db.close()

    silnik = _migruj(plik_bazy, monkeypatch)
    with silnik.connect() as polaczenie:
        wiersz = polaczenie.execute(
            text("SELECT typ, zrodlo, lokalizacja, uzytkownik_id FROM assets")
        ).one()
    assert wiersz.typ == "komputer"
    assert wiersz.zrodlo == "agent"
    assert wiersz.lokalizacja is None
    assert wiersz.uzytkownik_id is None


def test_stare_konta_nie_staja_sie_audytorami(tmp_path, monkeypatch):
    """Nowa flaga uprawnien musi dolozyc sie jako WYLACZONA.

    Kolumna wymagana bez poprawnej wartosci domyslnej zamienilaby kazde
    istniejace konto w audytora widzacego wszystkie firmy - czyli cicho
    zniosla izolacje danych przy zwyklej aktualizacji serwera.
    """
    plik_bazy = tmp_path / "konta.db"
    db = sqlite3.connect(plik_bazy)
    db.execute(
        """CREATE TABLE portal_users (
               id TEXT PRIMARY KEY, tenant_id TEXT, email TEXT NOT NULL,
               password_hash TEXT NOT NULL, role TEXT NOT NULL,
               is_superadmin BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL,
               created_at TIMESTAMP)"""
    )
    db.execute(
        "INSERT INTO portal_users (id, tenant_id, email, password_hash, role, "
        "is_superadmin, is_active) VALUES (?,?,?,?,?,0,1)",
        (str(uuid.uuid4()), str(uuid.uuid4()), "admin@firma.pl", "hash", "admin"),
    )
    db.commit()
    db.close()

    silnik = _migruj(plik_bazy, monkeypatch)
    with silnik.connect() as polaczenie:
        wartosc = polaczenie.execute(
            text("SELECT is_global_viewer FROM portal_users")
        ).scalar_one()
    assert not wartosc
