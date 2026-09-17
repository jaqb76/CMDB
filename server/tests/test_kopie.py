"""Kopie zapasowe: tworzenie, rotacja, sprawdzanie i uzbrajanie przywrocenia.

Testy chodza na PRAWDZIWYM pg_dump i pg_restore, bo cala wartosc tego modulu
polega na tym, ze rozmawia z Postgresem. Atrapa potwierdzalaby wylacznie, ze
skladamy poprawna liste argumentow - a to nie jest rzecz, ktora sie psuje.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import time
from collections import namedtuple
from pathlib import Path

import pytest
from sqlalchemy import text

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal, engine
from cmdb_server.models import Tenant

from .test_tenant_isolation import _extract_csrf, _login

HASLO = "haslo-kopii-2026"

pytestmark = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="brak klienta PostgreSQL - kopie sa nierozdzielne od pg_dump",
)


@pytest.fixture
def kopie(tmp_path, monkeypatch):
    """Katalog kopii i zalacznikow w tmp - zeby test nie ruszal dysku serwera."""
    from cmdb_server.services import kopie as modul

    ustawienia = get_settings()
    monkeypatch.setattr(ustawienia, "kopie_dir", str(tmp_path / "kopie"))
    monkeypatch.setattr(ustawienia, "release_dir", str(tmp_path / "releases"))
    monkeypatch.setattr(ustawienia, "helpdesk_dir", str(tmp_path / "helpdesk"))
    (tmp_path / "releases").mkdir()
    (tmp_path / "helpdesk").mkdir()
    return modul


def _baza_pomocnicza(nazwa: str) -> None:
    polaczenie = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        polaczenie.execute(text(f'DROP DATABASE IF EXISTS "{nazwa}" WITH (FORCE)'))
        polaczenie.execute(text(f'CREATE DATABASE "{nazwa}"'))
    finally:
        polaczenie.close()


def _skasuj_baze(nazwa: str) -> None:
    polaczenie = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        polaczenie.execute(text(f'DROP DATABASE IF EXISTS "{nazwa}" WITH (FORCE)'))
    finally:
        polaczenie.close()


# --- tworzenie --------------------------------------------------------------

def test_archiwum_zawiera_baze_pliki_i_manifest(client, tenant_a, kopie):
    """Kopia to nie sam zrzut bazy - zalaczniki i wydania tez sa stanem."""
    Path(get_settings().helpdesk_dir, "zrzut.png").write_bytes(b"udawany-png")
    Path(get_settings().release_dir, "agent.exe").write_bytes(b"udawany-exe")

    kopia = kopie.utworz("reczna", autor="test@mojadomena.pl")

    with tarfile.open(kopia.sciezka, "r:gz") as paczka:
        nazwy = paczka.getnames()
    assert "baza.dump" in nazwy
    assert "manifest.json" in nazwy
    assert "helpdesk/zrzut.png" in nazwy
    assert "releases/agent.exe" in nazwy

    assert kopia.opis["autor"] == "test@mojadomena.pl"
    assert kopia.opis["format"] == 1
    assert kopia.rodzaj == "reczna"


def test_przerwana_kopia_nie_zostaje_na_liscie(client, tenant_a, kopie, monkeypatch):
    """Niekompletny plik na liscie wygladalby jak kopia, ktorej nie ma."""
    def wybuchnij(*_args, **_kwargs):
        raise kopie.BladKopii("pg_dump nie powiodlo sie: test")

    monkeypatch.setattr(kopie, "_uruchom", wybuchnij)
    with pytest.raises(kopie.BladKopii):
        kopie.utworz("reczna")

    assert kopie.lista() == []


def test_rotacja_zostawia_limit_i_nie_rusza_recznych(client, tenant_a, kopie, monkeypatch):
    """Kopia zrobiona swiadomie przed ryzykowna zmiana ma przetrwac noc."""
    ustawienia = get_settings()
    monkeypatch.setattr(ustawienia, "kopie_dziennych", 2)

    # Kolejnosc ma znaczenie dla rotacji, a rozdzielczosc nazwy to minuty -
    # dlatego rozsuwamy czasy modyfikacji recznie.
    for numer in range(4):
        kopia = kopie.utworz("dzienna")
        os.utime(kopia.sciezka, (time.time() - (10 - numer) * 60,) * 2)
    reczna = kopie.utworz("reczna")

    usuniete = kopie.rotuj()

    zostale = {k.nazwa for k in kopie.lista()}
    assert len(usuniete) == 2
    assert reczna.nazwa in zostale, "reczna kopia nie moze zniknac w rotacji"
    assert len([k for k in kopie.lista() if k.rodzaj == "dzienna"]) == 2


def test_brak_miejsca_zatrzymuje_kopie(client, tenant_a, kopie, monkeypatch):
    """Lepiej nie zaczac, niz zapchac dysk pod dzialajaca baza."""
    kopie.utworz("dzienna")
    Uzycie = namedtuple("Uzycie", "total used free")
    monkeypatch.setattr(kopie.shutil, "disk_usage", lambda _sciezka: Uzycie(100, 99, 1))

    with pytest.raises(kopie.BladKopii, match="za malo miejsca"):
        kopie.utworz("reczna")


# --- odtwarzanie ------------------------------------------------------------

def test_kopia_odtwarza_sie_do_bazy_pomocniczej(client, tenant_a, kopie):
    """Kopia, ktorej nikt nie odtworzyl, to plik. Tu sprawdzamy, ze to kopia."""
    kopia = kopie.utworz("reczna")
    _baza_pomocnicza("cmdb_test_sprawdzenie")
    try:
        kopie.odtworz(kopia.sciezka, do_bazy="cmdb_test_sprawdzenie")

        from sqlalchemy import create_engine
        adres = get_settings().database_url.rsplit("/", 1)[0] + "/cmdb_test_sprawdzenie"
        silnik = create_engine(adres)
        with silnik.connect() as polaczenie:
            firmy = polaczenie.execute(text("SELECT count(*) FROM tenants")).scalar_one()
        silnik.dispose()
        assert firmy >= 1
    finally:
        _skasuj_baze("cmdb_test_sprawdzenie")


def test_sprawdzenie_kopii_nie_rusza_zalacznikow(client, tenant_a, kopie):
    """Odtworzenie do bazy pomocniczej dotyczy bazy - nie plikow na dysku."""
    kopia = kopie.utworz("reczna")
    zalacznik = Path(get_settings().helpdesk_dir, "po-kopii.txt")
    zalacznik.write_text("plik dodany po zrobieniu kopii", encoding="utf-8")

    _baza_pomocnicza("cmdb_test_sprawdzenie2")
    try:
        kopie.odtworz(kopia.sciezka, do_bazy="cmdb_test_sprawdzenie2")
    finally:
        _skasuj_baze("cmdb_test_sprawdzenie2")

    assert zalacznik.exists(), "sprawdzenie kopii skasowalo plik z dysku"


def test_obce_archiwum_jest_odrzucane(client, tenant_a, kopie, tmp_path):
    """Manifest nie chroni przed zlym zamiarem - chroni przed pomylka."""
    obce = tmp_path / "cos-innego.tar.gz"
    with tarfile.open(obce, "w:gz") as paczka:
        plik = tmp_path / "readme.txt"
        plik.write_text("to nie jest kopia CMDB", encoding="utf-8")
        paczka.add(plik, arcname="readme.txt")

    with pytest.raises(kopie.BladKopii, match="manifest"):
        kopie.sprawdz_archiwum(obce)


# --- uzbrajanie -------------------------------------------------------------

def test_uzbrojenie_nie_rusza_bazy(client, tenant_a, kopie):
    """Panel tylko odklada archiwum. Baza zmienia sie dopiero przy starcie."""
    kopia = kopie.utworz("reczna")
    with SessionLocal() as db:
        przed = db.execute(text("SELECT count(*) FROM tenants")).scalar_one()

    znacznik = kopie.uzbroj(kopia.sciezka, autor="szef@mojadomena.pl")

    assert znacznik["uzbroil"] == "szef@mojadomena.pl"
    assert kopie.uzbrojone() is not None
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM tenants")).scalar_one() == przed

    assert kopie.rozbroj() is True
    assert kopie.uzbrojone() is None


def test_wejscie_odtwarza_uzbrojona_kopie(client, tenant_a, kopie, make_user):
    """Znacznik + start kontenera = odtworzona baza. To jest cala droga."""
    from cmdb_server import wejscie

    kopia = kopie.utworz("reczna")
    with SessionLocal() as db:
        db.add(Tenant(name="Firma po kopii", slug="po-kopii"))
        db.commit()
        assert db.execute(
            text("SELECT count(*) FROM tenants WHERE slug = 'po-kopii'")
        ).scalar_one() == 1

    kopie.uzbroj(kopia.sciezka, autor="szef@mojadomena.pl")
    wejscie.odtworz_jesli_uzbrojone()

    with SessionLocal() as db:
        # Firma dodana PO kopii znika - baza wrocila do stanu z archiwum.
        assert db.execute(
            text("SELECT count(*) FROM tenants WHERE slug = 'po-kopii'")
        ).scalar_one() == 0
    assert kopie.uzbrojone() is None, "znacznik mial zniknac po udanym odtworzeniu"


def test_nieudane_odtworzenie_zostawia_znacznik(client, tenant_a, kopie, monkeypatch):
    """Ciche skasowanie znacznika wygladaloby jak sukces."""
    from cmdb_server import wejscie

    kopia = kopie.utworz("reczna")
    kopie.uzbroj(kopia.sciezka, autor="szef@mojadomena.pl")

    monkeypatch.setattr(
        kopie, "odtworz",
        lambda *_a, **_k: (_ for _ in ()).throw(kopie.BladKopii("baza nie odpowiada")),
    )
    wejscie.odtworz_jesli_uzbrojone()

    assert kopie.uzbrojone() is not None


# --- panel ------------------------------------------------------------------

def test_kopie_widzi_tylko_superadmin(client, tenant_a, kopie, make_user):
    make_user(tenant_a["id"], "operator@bongo.pl", HASLO)
    _login(client, "operator@bongo.pl", HASLO)
    assert client.get("/admin/kopie").status_code == 403


def test_panel_robi_kopie_i_pokazuje_ja_na_liscie(client, tenant_a, kopie, make_user):
    make_user(None, "szef@mojadomena.pl", HASLO)
    _login(client, "szef@mojadomena.pl", HASLO)

    csrf = _extract_csrf(client.get("/admin/kopie").text)
    client.post("/admin/kopie", data={"csrf_token": csrf}, follow_redirects=False)

    wszystkie = kopie.lista()
    assert len(wszystkie) == 1
    assert wszystkie[0].opis["autor"] == "szef@mojadomena.pl"
    assert wszystkie[0].nazwa in client.get("/admin/kopie").text


def test_przywracanie_wymaga_poprawnego_hasla(client, tenant_a, kopie, make_user):
    """Przejeta sesja superadmina nie moze wystarczyc do zastapienia bazy."""
    make_user(None, "szef@mojadomena.pl", HASLO)
    _login(client, "szef@mojadomena.pl", HASLO)
    kopia = kopie.utworz("reczna")

    csrf = _extract_csrf(client.get("/admin/kopie").text)
    odpowiedz = client.post("/admin/kopie/przywroc", data={
        "csrf_token": csrf, "nazwa": kopia.nazwa, "haslo": "nie-to-haslo",
    }, follow_redirects=False)

    assert "blad=" in odpowiedz.headers["location"]
    assert kopie.uzbrojone() is None, "zle haslo nie moze niczego uzbroic"


def test_przywracanie_uzbraja_i_mowi_o_restarcie(client, tenant_a, kopie, make_user):
    make_user(None, "szef@mojadomena.pl", HASLO)
    _login(client, "szef@mojadomena.pl", HASLO)
    kopia = kopie.utworz("reczna")

    csrf = _extract_csrf(client.get("/admin/kopie").text)
    client.post("/admin/kopie/przywroc", data={
        "csrf_token": csrf, "nazwa": kopia.nazwa, "haslo": HASLO,
    }, follow_redirects=False)

    assert kopie.uzbrojone() is not None
    strona = client.get("/admin/kopie").text
    assert "restart" in strona.lower()
