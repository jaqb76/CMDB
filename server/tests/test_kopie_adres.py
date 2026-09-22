"""Adres bazy dla pg_dump czytany tak samo jak przez aplikacje.

Osobno od test_kopie.py, bo nie potrzebuje klienta PostgreSQL - sprawdza
tylko rozbior adresu, na ktorym kopia wykladala sie przy hasle ze znakami
specjalnymi, choc portal laczyl sie z baza bez klopotu.
"""
from __future__ import annotations

import pytest

from cmdb_server.config import get_settings
from cmdb_server.services import kopie


@pytest.mark.parametrize("haslo", ["ab/cd+ef=", "ab#cd", "ab?cd", "zwykle"])
def test_haslo_ze_znakami_specjalnymi_nie_gubi_nazwy_bazy(monkeypatch, haslo):
    monkeypatch.setattr(get_settings(), "database_url",
                        f"postgresql+psycopg://cmdb:{haslo}@db:5433/cmdb")
    argumenty, srodowisko, baza = kopie._parametry_bazy()
    assert baza == "cmdb"
    assert argumenty == ["--host", "db", "--port", "5433", "--username", "cmdb"]
    assert srodowisko["PGPASSWORD"] == haslo


def test_adres_bez_nazwy_bazy_to_czytelny_blad(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", "postgresql+psycopg://cmdb:x@db:5432")
    with pytest.raises(kopie.BladKopii, match="nazwy bazy"):
        kopie._parametry_bazy()
