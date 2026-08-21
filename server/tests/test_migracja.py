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
