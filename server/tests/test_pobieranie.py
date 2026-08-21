"""Wydawanie instalatorow agenta po HTTPS.

Sens tej czesci: postawienie agenta na nowej maszynie nie moze wymagac
klonowania repozytorium ani rozdawania poswiadczen do niego. Sprawdzamy przy
tym, ze wydawanie plikow nie otwiera nowej drogi do cudzych danych.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import AgentRelease, EnrollmentToken, utcnow
from cmdb_server.services import pakiet
from sqlalchemy import select

from .test_admin import PLIK_AGENTA, _oznacz_oficjalna, _plik_pe, _superadmin, _wgraj_wersje

PLIK_PROBNY = _plik_pe(wypelniacz=b"wersja probna")


@pytest.fixture()
def paczka(tmp_path, monkeypatch):
    """Paczka zrodel zbudowana z prawdziwych zrodel i wpisana do magazynu wydan.

    Paczka jest pelnoprawnym wydaniem, tak samo jak plik dla Windows - serwer
    wydaje ja wedlug ustawien firmy, a nie wprost z dysku.
    """
    from cmdb_server.config import get_settings

    zrodla = Path(__file__).resolve().parent.parent.parent / "agent"
    if not (zrodla / "cmdb_agent").is_dir():
        pytest.skip("zrodla agenta niedostepne")
    katalog = tmp_path / "paczka"
    metadane = pakiet.zbuduj(zrodla, katalog)
    monkeypatch.setattr(pakiet, "katalog_paczki", lambda: katalog)

    with SessionLocal() as db:
        pakiet.zarejestruj(db, metadane, katalog, Path(get_settings().release_dir))
    return metadane


def _naglowek(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- skrypt startowy --------------------------------------------------------

def test_skrypt_startowy_jest_publiczny(client):
    """Wymaganie tokenu tutaj oznaczaloby podawanie go dwa razy w jednym
    poleceniu, a w samym skrypcie nie ma nic tajnego."""
    odpowiedz = client.get("/download/install.sh")
    assert odpowiedz.status_code == 200
    assert odpowiedz.text.startswith("#!/usr/bin/env bash")


def test_skrypt_ma_wpisany_adres_serwera(client):
    """Bez tego uzytkownik musialby podac adres, ktory wlasnie wpisal w curl."""
    tresc = client.get("/download/install.sh").text
    assert "@@ADRES_SERWERA@@" not in tresc, "znacznik nie zostal podmieniony"
    assert 'SERWER="http' in tresc


def test_adres_z_ustawien_ma_pierwszenstwo(client, monkeypatch):
    """Za proxy adres z zadania jest adresem kontenera, a nie tym, pod ktorym
    klienci widza serwer."""
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "public_url", "https://cmdb.firma.pl/")
    tresc = client.get("/download/install.sh").text
    assert 'SERWER="https://cmdb.firma.pl"' in tresc


# --- paczka dla Linuksa -----------------------------------------------------

def test_paczka_wymaga_tokenu_firmowego(client, paczka):
    assert client.get("/download/agent-linux.tar.gz").status_code == 401


def test_paczka_odrzuca_token_agenta(client, tenant_a, paczka):
    """Poswiadczenie maszyny sluzy do raportowania, nie do pobierania plikow."""
    rejestracja = client.post(
        "/api/v1/agents/enroll",
        headers=_naglowek(tenant_a["token"]),
        json={
            "machine_id": "maszyna-pobierajaca",
            "identity": {"hostname": "PC", "os_family": "linux", "arch": "aarch64"},
            "agent_version": "0.5.0",
        },
    ).json()
    assert client.get(
        "/download/agent-linux.tar.gz", headers=_naglowek(rejestracja["agent_token"])
    ).status_code == 401


def test_paczka_wydawana_z_tokenem(client, tenant_a, paczka):
    odpowiedz = client.get("/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"]))
    assert odpowiedz.status_code == 200
    assert odpowiedz.headers["x-cmdb-sha256"] == paczka["sha256"]
    assert odpowiedz.headers["x-cmdb-version"] == paczka["version"]
    assert hashlib.sha256(odpowiedz.content).hexdigest() == paczka["sha256"], \
        "skrot w naglowku musi opisywac faktycznie wydany plik"


def test_paczka_zawiera_agenta_i_instalator(client, tenant_a, paczka):
    """Paczka ma sie rozpakowac do postaci, ktorej oczekuje skrypt startowy."""
    odpowiedz = client.get("/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"]))
    with tarfile.open(fileobj=io.BytesIO(odpowiedz.content)) as archiwum:
        nazwy = archiwum.getnames()
        assert "cmdb-agent/packaging/install-agent.sh" in nazwy
        assert "cmdb-agent/cmdb_agent/main.py" in nazwy
        assert not any("__pycache__" in nazwa for nazwa in nazwy)
        instalator = archiwum.getmember("cmdb-agent/packaging/install-agent.sh")
        assert instalator.mode & 0o111, "instalator musi byc wykonywalny po rozpakowaniu"


def test_pobranie_nie_zuzywa_tokenu(client, tenant_a, paczka):
    """Licznik uzyc liczy zarejestrowane maszyny. Pobranie pliku maszyna moze
    powtorzyc kilka razy, zanim instalacja sie powiedzie - wliczanie tego
    zafalszowaloby obraz floty."""
    def licznik() -> int:
        with SessionLocal() as db:
            return db.execute(
                select(EnrollmentToken.use_count).where(
                    EnrollmentToken.tenant_id == tenant_a["id"]
                )
            ).scalar_one()

    przed = licznik()
    for _ in range(3):
        client.get("/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"]))
    assert licznik() == przed


def test_wycofany_token_nie_pobierze(client, tenant_a, paczka):
    with SessionLocal() as db:
        token = db.execute(
            select(EnrollmentToken).where(EnrollmentToken.tenant_id == tenant_a["id"])
        ).scalar_one()
        token.revoked_at = utcnow()
        db.commit()

    assert client.get(
        "/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"])
    ).status_code == 401


def test_brak_paczki_daje_czytelny_blad(client, tenant_a):
    """Instalacja ma powiedziec, co jest nie tak, a nie wywalic sie bledem 500.
    Bez fixture "paczka" w magazynie nie ma zadnego wydania dla Linuksa."""
    odpowiedz = client.get("/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"]))
    assert odpowiedz.status_code == 503
    assert "nie zostala przygotowana" in odpowiedz.json()["detail"]


# --- agent dla Windows ------------------------------------------------------

def test_windows_wydaje_wersje_obowiazujaca_firme(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_AGENTA, "windows")
    with SessionLocal() as db:
        wydanie = db.execute(select(AgentRelease.id)).scalar_one()
    _oznacz_oficjalna(client, csrf, wydanie)

    odpowiedz = client.get("/download/agent-windows.exe", headers=_naglowek(tenant_a["token"]))
    assert odpowiedz.status_code == 200
    assert odpowiedz.headers["x-cmdb-version"] == "0.5.0"
    assert odpowiedz.content == PLIK_AGENTA


def test_windows_bez_ustawionej_wersji_zwraca_404(client, tenant_a):
    assert client.get(
        "/download/agent-windows.exe", headers=_naglowek(tenant_a["token"])
    ).status_code == 404


def test_firma_dostaje_swoja_wersje_a_nie_cudza(client, tenant_a, tenant_b, make_user):
    """Wersja probna ustawiona jednej firmie nie moze wyciec do drugiej."""
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_AGENTA, "windows")
    _wgraj_wersje(client, csrf, "0.6.0-beta", PLIK_PROBNY, "windows")

    with SessionLocal() as db:
        oficjalna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.5.0")
        ).scalar_one()
        probna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.6.0-beta")
        ).scalar_one()
    _oznacz_oficjalna(client, csrf, oficjalna)

    client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={
            "zakres": "firma",
            "os_family": "windows",
            "release_id": probna,
            "csrf_token": csrf,
            "powrot": "/admin/wersje",
        },
        follow_redirects=False,
    )

    wersja_a = client.get(
        "/download/agent-windows.exe", headers=_naglowek(tenant_a["token"])
    ).headers["x-cmdb-version"]
    wersja_b = client.get(
        "/download/agent-windows.exe", headers=_naglowek(tenant_b["token"])
    ).headers["x-cmdb-version"]

    assert wersja_a == "0.6.0-beta"
    assert wersja_b == "0.5.0", "firma bez wlasnego ustawienia zostaje na oficjalnej"


# --- wymuszenie HTTPS -------------------------------------------------------

def test_zadanie_z_tokenem_po_http_jest_odrzucane(client, tenant_a, paczka, monkeypatch):
    """Nie przekierowujemy, tylko odmawiamy.

    Naglowek doszedl juz po czystym HTTP, wiec token wyciekl. Przekierowanie
    niczego by nie uratowalo - jedynie ukrylo problem przed uzytkownikiem,
    ktoremu nalezy sie informacja, ze musi ten token wymienic.
    """
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "require_https", True)
    odpowiedz = client.get(
        "/download/agent-linux.tar.gz",
        headers={**_naglowek(tenant_a["token"]), "X-Forwarded-Proto": "http"},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 403
    assert odpowiedz.json()["detail"] == "wymagane polaczenie HTTPS"


def test_skrypt_bez_tokenu_po_http_jest_przekierowywany(client, monkeypatch):
    """Sam skrypt nie niesie sekretu, wiec tu przekierowanie jest w porzadku
    i pozwala dzialac wywolaniu curl -L."""
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "require_https", True)
    odpowiedz = client.get(
        "/download/install.sh",
        headers={"X-Forwarded-Proto": "http"},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 308
    assert odpowiedz.headers["location"].startswith("https://")


# --- powtarzalnosc paczki ---------------------------------------------------

def test_paczka_jest_deterministyczna(tmp_path):
    """Te same zrodla musza dawac ten sam skrot.

    Inaczej kazde przebudowanie zmienialoby SHA-256 i nie dalo by sie
    stwierdzic, czy paczka faktycznie sie zmienila.
    """
    zrodla = Path(__file__).resolve().parent.parent.parent / "agent"
    if not (zrodla / "cmdb_agent").is_dir():
        pytest.skip("zrodla agenta niedostepne")

    pierwsza = pakiet.zbuduj(zrodla, tmp_path / "a")
    druga = pakiet.zbuduj(zrodla, tmp_path / "b")

    assert pierwsza["sha256"] == druga["sha256"]
    assert (tmp_path / "a" / pakiet.NAZWA_ARCHIWUM).read_bytes() == \
           (tmp_path / "b" / pakiet.NAZWA_ARCHIWUM).read_bytes()


def test_wersja_paczki_pochodzi_ze_zrodel(tmp_path):
    """Zeby nie trzeba jej bylo nigdzie wpisywac recznie."""
    zrodla = tmp_path / "agent"
    (zrodla / "cmdb_agent").mkdir(parents=True)
    (zrodla / "packaging").mkdir()
    (zrodla / "cmdb_agent" / "__init__.py").write_text(
        '__version__ = "9.9.9"\n', encoding="utf-8"
    )
    (zrodla / "packaging" / "install-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    assert pakiet.zbuduj(zrodla, tmp_path / "cel")["version"] == "9.9.9"


def test_katalog_bez_zrodel_zglasza_czytelny_blad(tmp_path):
    with pytest.raises(pakiet.BrakZrodel):
        pakiet.zbuduj(tmp_path / "nie-ma-tu-agenta", tmp_path / "cel")


# --- strona w panelu --------------------------------------------------------

def _zaloguj_do_firmy(client, make_user, tenant):
    make_user(tenant["id"], "admin@firma-a.pl", "haslo-do-testow-123")
    from .test_tenant_isolation import _login

    _login(client, "admin@firma-a.pl", "haslo-do-testow-123")


def test_strona_pokazuje_gotowe_polecenie(client, tenant_a, make_user, paczka):
    _zaloguj_do_firmy(client, make_user, tenant_a)
    strona = client.get("/pobierz")
    assert strona.status_code == 200
    assert "/download/install.sh" in strona.text
    assert paczka["version"] in strona.text
    assert paczka["sha256"] in strona.text


def test_strona_nie_wypisuje_tokenu(client, tenant_a, make_user, paczka):
    """W bazie sa wylacznie skroty tokenow - strona nie ma skad go wziac
    i nie moze udawac, ze ma."""
    _zaloguj_do_firmy(client, make_user, tenant_a)
    strona = client.get("/pobierz")
    assert tenant_a["token"] not in strona.text
    assert "TWOJ_TOKEN" in strona.text


def test_strona_wymaga_zalogowania(client):
    odpowiedz = client.get("/pobierz", follow_redirects=False)
    assert odpowiedz.status_code in (302, 303, 307)
    assert "/login" in odpowiedz.headers["location"]


def test_strona_mowi_wprost_gdy_brak_paczki(client, tenant_a, make_user, tmp_path, monkeypatch):
    """Lepiej napisac, ze paczki nie ma, niz pokazac polecenie, ktore zwroci blad."""
    monkeypatch.setattr(pakiet, "katalog_paczki", lambda: tmp_path / "pusto")
    _zaloguj_do_firmy(client, make_user, tenant_a)
    strona = client.get("/pobierz")
    assert strona.status_code == 200
    assert "nie przygotowano paczki" in strona.text


# --- zakonczenia linii ------------------------------------------------------
#
# Na Windows git domyslnie wystawia w kopii roboczej CRLF. Paczka budowana jest
# wprost z plikow na dysku, wiec bez normalizacji na maszyne docelowa trafialby
# skrypt, ktorego pierwsza linia konczy sie znakiem powrotu karetki - a Linux
# szuka wtedy interpretera o nazwie "bash" z tym znakiem i odmawia startu.

def _z_crlf(dane: bytes) -> bytes:
    return dane.replace(chr(10).encode(), (chr(13) + chr(10)).encode())


def test_paczka_ma_skrypty_z_zakonczeniami_lf(client, tenant_a, paczka):
    odpowiedz = client.get("/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"]))
    with tarfile.open(fileobj=io.BytesIO(odpowiedz.content)) as archiwum:
        skrypt = archiwum.extractfile("cmdb-agent/packaging/install-agent.sh").read()

    assert chr(13).encode() not in skrypt, "skrypt z CRLF nie uruchomi sie na Linuksie"
    assert skrypt.startswith(b"#!/usr/bin/env bash" + chr(10).encode())


def test_zrodla_z_crlf_daja_paczke_z_lf(tmp_path):
    """Sedno zabezpieczenia: nawet gdy kopia robocza ma CRLF, paczka ma LF."""
    zrodla = tmp_path / "agent"
    (zrodla / "cmdb_agent").mkdir(parents=True)
    (zrodla / "packaging").mkdir()
    (zrodla / "cmdb_agent" / "__init__.py").write_text(
        '__version__ = "1.0.0"\n', encoding="utf-8"
    )
    (zrodla / "packaging" / "install-agent.sh").write_bytes(
        _z_crlf(b"#!/usr/bin/env bash\necho gotowe\n")
    )

    katalog = tmp_path / "cel"
    pakiet.zbuduj(zrodla, katalog)

    with tarfile.open(pakiet.sciezka_archiwum(katalog)) as archiwum:
        skrypt = archiwum.extractfile("cmdb-agent/packaging/install-agent.sh").read()
    assert skrypt == b"#!/usr/bin/env bash\necho gotowe\n"


def test_wydawany_skrypt_startowy_ma_lf(client):
    """Ten sam problem dotyczy skryptu wydawanego pod /download/install.sh."""
    tresc = client.get("/download/install.sh").content
    assert chr(13).encode() not in tresc


def test_skrypty_w_repozytorium_maja_lf():
    """Wychwytuje przypadkowe zapisanie skryptu z CRLF - np. edytorem
    ustawionym na zakonczenia windowsowe."""
    korzen = Path(__file__).resolve().parent.parent.parent
    winne = [
        sciezka.relative_to(korzen).as_posix()
        for sciezka in korzen.rglob("*.sh")
        if ".git" not in sciezka.parts and chr(13).encode() in sciezka.read_bytes()
    ]
    assert not winne, f"skrypty z CRLF nie zadzialaja na Linuksie: {winne}"


# --- paczka zrodel jako pelnoprawne wydanie ---------------------------------
#
# O to chodzilo: jedna paczka obsluguje kazda architekture (agent stoi na samej
# bibliotece standardowej), a mimo to podlega tym samym regulom co plik dla
# Windows - wersji aktywnej i wersji probnej per firma.

def _wydanie_zrodel(wersja: str = "0.9.9"):
    """Wpisuje do magazynu dodatkowa paczke zrodel o wskazanej wersji."""
    import hashlib
    from cmdb_server.config import get_settings
    from cmdb_server.models import AgentRelease

    katalog = Path(get_settings().release_dir)
    katalog.mkdir(parents=True, exist_ok=True)
    tresc = f"paczka {wersja}".encode()
    odcisk = hashlib.sha256(tresc).hexdigest()
    (katalog / f"linux-zrodla-{odcisk}.tar.gz").write_bytes(tresc)

    with SessionLocal() as db:
        wydanie = AgentRelease(
            version=wersja, os_family="linux", arch=pakiet.ARCH_ZRODLA,
            filename=f"cmdb-agent-{wersja}.tar.gz",
            storage_name=f"linux-zrodla-{odcisk}.tar.gz",
            sha256=odcisk, size_bytes=len(tresc), created_by="test",
        )
        db.add(wydanie)
        db.commit()
        return wydanie.id


def test_paczka_pasuje_do_kazdej_architektury(client, tenant_a, paczka, make_user):
    """Sedno wyboru: jedna paczka dla Raspberry Pi i dla serwera x86.
    Wymaganie zgodnosci architektury odcieloby ja od wszystkich maszyn."""
    csrf = _superadmin(client, make_user)
    with SessionLocal() as db:
        wydanie = db.execute(
            select(AgentRelease.id).where(AgentRelease.arch == pakiet.ARCH_ZRODLA)
        ).scalar_one()
    _oznacz_oficjalna(client, csrf, wydanie)

    for maszyna, arch in (("pi-aarch64-001", "aarch64"), ("serwer-x86-001", "x86_64")):
        token = client.post(
            "/api/v1/agents/enroll",
            headers=_naglowek(tenant_a["token"]),
            json={"machine_id": maszyna,
                  "identity": {"hostname": maszyna, "os_family": "linux", "arch": arch},
                  "agent_version": "0.1.0"},
        ).json()["agent_token"]

        oferta = client.get("/api/v1/agent/version", headers=_naglowek(token)).json()
        assert oferta["available"] is True, f"{arch} nie dostal paczki"
        assert oferta["kind"] == "zrodla"


def test_oferta_mowi_czym_jest_wydanie(client, tenant_a, make_user):
    """Podmiana katalogu plikiem wykonywalnym - albo odwrotnie - zostawilaby
    maszyne bez dzialajacego agenta. Agent musi wiedziec, co pobiera."""
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_AGENTA, "windows")
    with SessionLocal() as db:
        wydanie = db.execute(
            select(AgentRelease.id).where(AgentRelease.os_family == "windows")
        ).scalar_one()
    _oznacz_oficjalna(client, csrf, wydanie)

    token = client.post(
        "/api/v1/agents/enroll",
        headers=_naglowek(tenant_a["token"]),
        json={"machine_id": "stacja-windows-01",
              "identity": {"hostname": "WIN-01", "os_family": "windows", "arch": "amd64"},
              "agent_version": "0.1.0"},
    ).json()["agent_token"]

    assert client.get("/api/v1/agent/version", headers=_naglowek(token)).json()["kind"] == "plik"


def test_wersja_probna_dziala_takze_dla_linuksa(client, tenant_a, tenant_b, paczka, make_user):
    csrf = _superadmin(client, make_user)
    with SessionLocal() as db:
        oficjalna = db.execute(
            select(AgentRelease.id).where(AgentRelease.arch == pakiet.ARCH_ZRODLA)
        ).scalar_one()
    _oznacz_oficjalna(client, csrf, oficjalna)
    probna = _wydanie_zrodel("0.9.9")

    client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "firma", "os_family": "linux", "release_id": probna,
              "csrf_token": csrf, "powrot": "/admin/wersje"},
        follow_redirects=False,
    )

    wersja_a = client.get(
        "/download/agent-linux.tar.gz", headers=_naglowek(tenant_a["token"])
    ).headers["x-cmdb-version"]
    wersja_b = client.get(
        "/download/agent-linux.tar.gz", headers=_naglowek(tenant_b["token"])
    ).headers["x-cmdb-version"]

    assert wersja_a == "0.9.9"
    assert wersja_b == paczka["version"], "firma bez wlasnego ustawienia zostaje na oficjalnej"


def test_wgranie_paczki_przez_panel(client, make_user, tmp_path):
    """Paczke mozna wgrac tak samo jak plik dla Windows - rozpoznajemy ja po
    zawartosci, a nie po nazwie pliku."""
    zrodla = Path(__file__).resolve().parent.parent.parent / "agent"
    if not (zrodla / "cmdb_agent").is_dir():
        pytest.skip("zrodla agenta niedostepne")
    katalog = tmp_path / "budowa"
    metadane = pakiet.zbuduj(zrodla, katalog)
    zawartosc = pakiet.sciezka_archiwum(katalog).read_bytes()

    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "", "os_family": "linux", "notes": "", "csrf_token": csrf},
        files={"plik": ("cmdb-agent.tar.gz", io.BytesIO(zawartosc), "application/gzip")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303, odpowiedz.text

    with SessionLocal() as db:
        wydanie = db.execute(
            select(AgentRelease).where(AgentRelease.arch == pakiet.ARCH_ZRODLA)
        ).scalar_one()
    assert wydanie.version == metadane["version"]
    assert wydanie.storage_name.endswith(".tar.gz")


def test_obce_archiwum_jest_odrzucane(client, make_user):
    """Sprawdzamy zawartosc, bo maszyna docelowa dostanie dokladnie to,
    co tu wpuscimy."""
    import tarfile as tar_mod

    bufor = io.BytesIO()
    with tar_mod.open(fileobj=bufor, mode="w:gz") as tar:
        dane = b"cokolwiek"
        info = tar_mod.TarInfo("obcy/plik.txt")
        info.size = len(dane)
        tar.addfile(info, io.BytesIO(dane))

    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "0.1.0", "os_family": "linux", "notes": "", "csrf_token": csrf},
        files={"plik": ("obce.tar.gz", io.BytesIO(bufor.getvalue()), "application/gzip")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400
    assert "paczka zrodel agenta" in odpowiedz.text
