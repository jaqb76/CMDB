"""Panel superadmina i mechanizm aktualizacji agentow."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    AgentRelease,
    Asset,
    EnrollmentToken,
    GlobalAgentTarget,
    PortalUser,
    Tenant,
)
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login

def _plik_pe(maszyna: int = 0x8664, wypelniacz: bytes = b"") -> bytes:
    """Minimalny, ale poprawny strukturalnie plik PE.

    Serwer odczytuje architekture z naglowka COFF, wiec atrapa musi miec
    prawdziwe przesuniecie do naglowka PE (spod adresu 0x3C) i pole Machine.
    """
    naglowek = bytearray(b"MZ" + bytes(0x3E))
    naglowek[0x3C:0x40] = (0x40).to_bytes(4, "little")
    naglowek += b"PE" + bytes(2) + maszyna.to_bytes(2, "little")
    return bytes(naglowek) + b"testowa zawartosc agenta" * 40 + wypelniacz


def _plik_elf(maszyna: int = 0xB7, wypelniacz: bytes = b"") -> bytes:
    """Minimalny plik ELF. Domyslnie ARM64 - taki jak agent na Raspberry Pi."""
    naglowek = bytearray(bytes.fromhex("7f") + b"ELF")
    naglowek += bytes([2, 1, 1]) + bytes(9)
    naglowek += (2).to_bytes(2, "little")
    naglowek += maszyna.to_bytes(2, "little")
    naglowek += bytes(32)
    return bytes(naglowek) + b"agent dla linuksa" * 40 + wypelniacz


PLIK_AGENTA = _plik_pe()
SKROT_AGENTA = hashlib.sha256(PLIK_AGENTA).hexdigest()


def _superadmin(client, make_user):
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    # Strona przegladu jest tylko do czytania - formularze sa na podstronach.
    return _extract_csrf(client.get("/admin/firmy").text)


def _oznacz_oficjalna(client, csrf, release_id):
    return client.post(
        f"/admin/releases/{release_id}/oficjalna",
        data={"csrf_token": csrf},
        follow_redirects=True,
    )


PLIK_LINUKSOWY = _plik_elf()          # ARM64 - taki agent trafia na Raspberry Pi
PLIK_LINUKSOWY_X86 = _plik_elf(0x3E)  # x86-64
SKROT_LINUKSOWY = hashlib.sha256(PLIK_LINUKSOWY).hexdigest()


def _wgraj_wersje(client, csrf, wersja="0.2.0", tresc=PLIK_AGENTA, system="windows"):
    return client.post(
        "/admin/releases",
        data={"version": wersja, "os_family": system, "notes": "testowa", "csrf_token": csrf},
        files={"plik": ("cmdb-agent.exe", io.BytesIO(tresc), "application/octet-stream")},
        follow_redirects=False,
    )


def _zlec_dla_firmy(client, csrf, tenant_id, release_id, system="windows"):
    return client.post(
        f"/admin/tenants/{tenant_id}/upgrade",
        data={"zakres": "firma", "os_family": system,
              "release_id": release_id, "csrf_token": csrf},
        follow_redirects=True,
    )


# --- dostep -----------------------------------------------------------------

def test_panel_globalny_wymaga_superadmina(client, tenant_a, make_user):
    """Administrator firmy nie moze ogladac danych innych firm."""
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")
    assert client.get("/admin").status_code == 403


def test_panel_globalny_wymaga_zalogowania(client):
    odpowiedz = client.get("/admin", follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert odpowiedz.headers["location"] == "/login"


def test_administrator_firmy_nie_zalozy_konta_gdzie_indziej(client, tenant_a, tenant_b, make_user):
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_b['id']}/users",
        data={"email": "obcy@firma-b.pl", "password": "bardzo-dlugie-haslo", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 403


# --- widok globalny ---------------------------------------------------------

def test_widok_globalny_pokazuje_wszystkie_firmy(client, tenant_a, tenant_b, make_user):
    token_a = enroll(client, tenant_a["token"], machine_id="maszyna-a-1", hostname="A-01").json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_a}"},
        json=build_report(machine_id="maszyna-a-1", hostname="A-01"),
    )
    enroll(client, tenant_b["token"], machine_id="maszyna-b-1", hostname="B-01")

    _superadmin(client, make_user)
    strona = client.get("/admin").text
    assert "Firma A" in strona and "Firma B" in strona
    assert "agentow aktywnych" in strona
    assert "agentow nieaktywnych" in strona


# --- zakladanie firm, kont i tokenow ---------------------------------------

def test_superadmin_zaklada_firme(client, make_user):
    csrf = _superadmin(client, make_user)
    client.post(
        "/admin/tenants",
        data={"name": "Nowa Firma", "slug": "nowa", "csrf_token": csrf},
        follow_redirects=True,
    )
    with SessionLocal() as db:
        firma = db.execute(select(Tenant).where(Tenant.slug == "nowa")).scalar_one()
        assert firma.name == "Nowa Firma"


def test_odrzuca_niepoprawny_identyfikator_firmy(client, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/tenants",
        data={"name": "Zla", "slug": "Zle Litery!", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_superadmin_zaklada_konto_dla_firmy(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    client.post(
        f"/admin/tenants/{tenant_a['id']}/users",
        data={
            "email": "klient@firma-a.pl",
            "password": "haslo-dla-klienta-2026",
            "full_name": "Jan Klient",
            "role": "admin",
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )
    with SessionLocal() as db:
        konto = db.execute(
            select(PortalUser).where(PortalUser.email == "klient@firma-a.pl")
        ).scalar_one()
        assert konto.tenant_id == tenant_a["id"]
        assert konto.is_superadmin is False
        # Haslo nigdy nie moze byc zapisane jawnie.
        assert "haslo-dla-klienta-2026" not in konto.password_hash


def test_odrzuca_zbyt_krotkie_haslo(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/users",
        data={"email": "x@firma-a.pl", "password": "krotkie", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_superadmin_wydaje_token_dla_firmy(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/tokens",
        data={"name": "stacje", "expires_days": 30, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    wydany = odpowiedz.headers["location"].split("wydany_token=")[1]
    assert wydany.startswith("cmdb_ent_")

    # Token dziala od razu i nalezy do wskazanej firmy.
    rejestracja = enroll(client, wydany, machine_id="maszyna-z-panelu")
    assert rejestracja.status_code == 201
    assert rejestracja.json()["tenant_slug"] == "firma-a"

    with SessionLocal() as db:
        zapisany = db.execute(
            select(EnrollmentToken).where(EnrollmentToken.tenant_id == tenant_a["id"])
        ).scalars().all()
        assert all(wydany not in t.token_hash for t in zapisany)


# --- wersje agenta ----------------------------------------------------------

def test_wgranie_wersji_liczy_skrot(client, make_user):
    csrf = _superadmin(client, make_user)
    assert _wgraj_wersje(client, csrf).status_code == 303

    with SessionLocal() as db:
        wydanie = db.execute(select(AgentRelease)).scalar_one()
        assert wydanie.version == "0.2.0"
        assert wydanie.sha256 == SKROT_AGENTA
        assert wydanie.size_bytes == len(PLIK_AGENTA)


def test_odrzuca_plik_ktory_nie_jest_programem(client, make_user):
    """Bez tej kontroli dalo by sie rozeslac na flote dowolny plik."""
    csrf = _superadmin(client, make_user)
    odpowiedz = _wgraj_wersje(client, csrf, tresc=b"to zwykly tekst, nie program")
    assert odpowiedz.status_code == 400


def test_odrzuca_plik_windows_wgrywany_jako_linuksowy(client, make_user):
    """Sygnatura pilnuje, ze plik pasuje do wskazanego systemu - pomylka
    wychodzi przy wgrywaniu, a nie dopiero na maszynie klienta."""
    csrf = _superadmin(client, make_user)
    odpowiedz = _wgraj_wersje(client, csrf, tresc=PLIK_AGENTA, system="linux")
    assert odpowiedz.status_code == 400


def test_ta_sama_wersja_dla_dwoch_systemow_jest_dozwolona(client, make_user):
    """0.2.0 dla Windows i 0.2.0 dla Linuksa to dwa rozne pliki."""
    csrf = _superadmin(client, make_user)
    assert _wgraj_wersje(client, csrf, "0.2.0", PLIK_AGENTA, "windows").status_code == 303
    assert _wgraj_wersje(client, csrf, "0.2.0", PLIK_LINUKSOWY, "linux").status_code == 303
    with SessionLocal() as db:
        assert len(db.execute(select(AgentRelease)).scalars().all()) == 2


def test_odrzuca_duplikat_wersji(client, make_user):
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    assert _wgraj_wersje(client, csrf).status_code == 400


# --- zlecanie i pobieranie aktualizacji ------------------------------------

def _przygotuj_maszyne(client, tenant, machine_id="maszyna-1", hostname="WS-01"):
    token = enroll(client, tenant["token"], machine_id=machine_id, hostname=hostname).json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id=machine_id, hostname=hostname),
    )
    return token


def test_agent_nie_dostaje_oferty_bez_zlecenia(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    oferta = client.get("/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"})
    assert oferta.status_code == 200
    assert oferta.json()["available"] is False


def test_zlecenie_dla_calej_firmy_dociera_do_agenta(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)

    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()

    _zlec_dla_firmy(client, csrf, tenant_a["id"], wydanie_id)

    oferta = client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert oferta["available"] is True
    assert oferta["version"] == "0.2.0"
    assert oferta["sha256"] == SKROT_AGENTA
    # Serwer nie podaje adresu pobierania - agent sklada go z wlasnej konfiguracji.
    assert "url" not in oferta


def test_agent_pobiera_plik_i_skrot_sie_zgadza(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()
    _zlec_dla_firmy(client, csrf, tenant_a["id"], wydanie_id)

    plik = client.get("/api/v1/agent/release", headers={"Authorization": f"Bearer {token}"})
    assert plik.status_code == 200
    assert hashlib.sha256(plik.content).hexdigest() == SKROT_AGENTA
    assert plik.headers["X-CMDB-SHA256"] == SKROT_AGENTA


def test_agent_bez_zlecenia_nie_pobierze_pliku(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)   # wersja istnieje, ale nikomu jej nie zlecono

    assert client.get(
        "/api/v1/agent/release", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 404


def test_zlecenie_nie_przecieka_miedzy_firmami(client, tenant_a, tenant_b, make_user):
    """Aktualizacja zlecona firmie A nie moze dotyczyc maszyn firmy B."""
    token_a = _przygotuj_maszyne(client, tenant_a, "maszyna-a-1", "A-01")
    token_b = _przygotuj_maszyne(client, tenant_b, "maszyna-b-1", "B-01")

    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()
    _zlec_dla_firmy(client, csrf, tenant_a["id"], wydanie_id)

    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token_a}"}
    ).json()["available"] is True
    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token_b}"}
    ).json()["available"] is False
    assert client.get(
        "/api/v1/agent/release", headers={"Authorization": f"Bearer {token_b}"}
    ).status_code == 404


def test_zlecenie_dla_wybranych_maszyn(client, tenant_a, make_user):
    token1 = _przygotuj_maszyne(client, tenant_a, "maszyna-1", "WS-01")
    token2 = _przygotuj_maszyne(client, tenant_a, "maszyna-2", "WS-02")

    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()
        wybrana = db.execute(
            select(Asset.id).where(Asset.machine_id == "maszyna-1")
        ).scalar_one()

    client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "wybrane", "release_id": wydanie_id,
              "asset_id": wybrana, "csrf_token": csrf},
        follow_redirects=True,
    )

    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token1}"}
    ).json()["available"] is True
    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token2}"}
    ).json()["available"] is False


def test_nie_mozna_wskazac_maszyny_spoza_firmy(client, tenant_a, tenant_b, make_user):
    _przygotuj_maszyne(client, tenant_a, "maszyna-a-1", "A-01")
    _przygotuj_maszyne(client, tenant_b, "maszyna-b-1", "B-01")

    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()
        obca = db.execute(
            select(Asset.id).where(Asset.tenant_id == tenant_b["id"])
        ).scalar_one()

    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "wybrane", "release_id": wydanie_id,
              "asset_id": obca, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_agent_zglasza_wynik_aktualizacji(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    odpowiedz = client.post(
        "/api/v1/agent/upgrade-result",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": "0.2.0", "status": "blad", "detail": "skrot sie nie zgadza"},
    )
    assert odpowiedz.status_code == 200
    with SessionLocal() as db:
        maszyna = db.execute(select(Asset)).scalar_one()
        assert maszyna.upgrade_status == "blad"
        assert "skrot" in maszyna.upgrade_detail


def test_oferta_znika_po_aktualizacji(client, tenant_a, make_user):
    """Gdy agent zglosi sie juz w oczekiwanej wersji, nie proponujemy jej ponownie."""
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf)
    with SessionLocal() as db:
        wydanie_id = db.execute(select(AgentRelease.id)).scalar_one()
    _zlec_dla_firmy(client, csrf, tenant_a["id"], wydanie_id)

    raport = build_report(machine_id="maszyna-1", hostname="WS-01")
    raport["agent"]["version"] = "0.2.0"
    odpowiedz = client.post(
        "/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport
    ).json()
    assert odpowiedz["upgrade"]["available"] is False


def test_nie_zlecimy_wersji_dla_innego_systemu(client, tenant_a, make_user):
    """Maszyna z Windows nie moze dostac pliku zbudowanego dla Linuksa -
    pobralaby cos, czego nie ma jak uruchomic."""
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_LINUKSOWY, "linux")
    with SessionLocal() as db:
        linuksowe = db.execute(
            select(AgentRelease.id).where(AgentRelease.os_family == "linux")
        ).scalar_one()
        maszyna = db.execute(select(Asset.id)).scalar_one()

    # Cel dla calej firmy: wersja linuksowa zadeklarowana jako windowsowa.
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "firma", "os_family": "windows",
              "release_id": linuksowe, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400

    # Cel dla wskazanej maszyny z Windows.
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "wybrane", "release_id": linuksowe,
              "asset_id": maszyna, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400
    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["available"] is False


def test_cel_linuksowy_nie_dotyczy_maszyn_windows(client, tenant_a, make_user):
    """Cel ustawiony dla Linuksa nie moze objac maszyn z Windows w tej firmie."""
    token = _przygotuj_maszyne(client, tenant_a)      # maszyna zglasza os_family=windows
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_LINUKSOWY, "linux")
    with SessionLocal() as db:
        linuksowe = db.execute(
            select(AgentRelease.id).where(AgentRelease.os_family == "linux")
        ).scalar_one()

    _zlec_dla_firmy(client, csrf, tenant_a["id"], linuksowe, system="linux")

    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["available"] is False
    assert client.get(
        "/api/v1/agent/release", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 404


def test_widok_globalny_pokazuje_wersje_agentow(client, tenant_a, make_user):
    """Zestawienie ma pokazywac firme, aktywnych, nieaktywnych i wersje."""
    _przygotuj_maszyne(client, tenant_a)
    _superadmin(client, make_user)
    strona = client.get("/admin").text
    assert "Wersje agentow" in strona
    assert "0.1.0" in strona          # wersja z raportu testowego
    assert "windows" in strona


def test_wersja_agenta_widoczna_przy_maszynie(client, tenant_a, make_user):
    """Administrator firmy widzi wersje przy kazdej maszynie, a nie zbiorczo."""
    _przygotuj_maszyne(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    lista = client.get("/assets").text
    assert "agent 0.1.0" in lista

    # Zbiorcze zestawienie zostalo usuniete z pulpitu firmy.
    assert "Wersje agenta w firmie" not in client.get("/").text


# --- trzy poziomy wyboru wersji ---------------------------------------------

def test_wersja_oficjalna_obejmuje_firmy_bez_wlasnego_ustawienia(
    client, tenant_a, tenant_b, make_user
):
    """Sedno mechanizmu: jedna firma na wersji probnej, reszta na oficjalnej."""
    token_a = _przygotuj_maszyne(client, tenant_a, "maszyna-a-1", "A-01")
    token_b = _przygotuj_maszyne(client, tenant_b, "maszyna-b-1", "B-01")

    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_AGENTA, "windows")
    _wgraj_wersje(client, csrf, "0.3.0-beta", _plik_pe(wypelniacz=b"beta"), "windows")
    with SessionLocal() as db:
        oficjalna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.2.0")
        ).scalar_one()
        probna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.3.0-beta")
        ).scalar_one()

    # 0.2.0 jako oficjalna dla Windows, wersja probna tylko dla firmy A.
    _oznacz_oficjalna(client, csrf, oficjalna)
    _zlec_dla_firmy(client, csrf, tenant_a["id"], probna)

    oferta_a = client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token_a}"}
    ).json()
    oferta_b = client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token_b}"}
    ).json()

    assert oferta_a["version"] == "0.3.0-beta", "firma z wlasnym ustawieniem dostaje wersje probna"
    assert oferta_b["version"] == "0.2.0", "pozostale firmy dostaja wersje oficjalna"


def test_tylko_jedna_wersja_oficjalna_na_system(client, make_user):
    """Podmieniamy wskazanie, zamiast dokladac drugie - inaczej rozstrzyganie
    przestaloby byc jednoznaczne."""
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_AGENTA, "windows")
    _wgraj_wersje(client, csrf, "0.3.0", _plik_pe(wypelniacz=b"nowsza"), "windows")
    with SessionLocal() as db:
        pierwsza, druga = [
            r for r in db.execute(
                select(AgentRelease.id).order_by(AgentRelease.version)
            ).scalars()
        ]

    _oznacz_oficjalna(client, csrf, pierwsza)
    _oznacz_oficjalna(client, csrf, druga)

    with SessionLocal() as db:
        cele = db.execute(select(GlobalAgentTarget)).scalars().all()
        assert len(cele) == 1
        assert cele[0].release_id == druga


def test_wersja_oficjalna_jest_osobna_dla_kazdego_systemu(client, make_user):
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_AGENTA, "windows")
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_LINUKSOWY, "linux")
    with SessionLocal() as db:
        for rid in db.execute(select(AgentRelease.id)).scalars():
            _oznacz_oficjalna(client, csrf, rid)
        cele = db.execute(select(GlobalAgentTarget)).scalars().all()
        assert {c.os_family for c in cele} == {"windows", "linux"}


def test_wyczyszczenie_ustawienia_firmy_wraca_do_oficjalnej(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.2.0", PLIK_AGENTA, "windows")
    _wgraj_wersje(client, csrf, "0.9.0", _plik_pe(wypelniacz=b"probna"), "windows")
    with SessionLocal() as db:
        oficjalna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.2.0")
        ).scalar_one()
        probna = db.execute(
            select(AgentRelease.id).where(AgentRelease.version == "0.9.0")
        ).scalar_one()

    _oznacz_oficjalna(client, csrf, oficjalna)
    _zlec_dla_firmy(client, csrf, tenant_a["id"], probna)
    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["version"] == "0.9.0"

    # Puste pole = "korzystaj z oficjalnej".
    _zlec_dla_firmy(client, csrf, tenant_a["id"], "")
    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["version"] == "0.2.0"


def test_ustawienie_maszyny_bije_firme_i_oficjalna(client, tenant_a, make_user):
    token = _przygotuj_maszyne(client, tenant_a)
    csrf = _superadmin(client, make_user)
    for wersja, dodatek in (("0.2.0", b""), ("0.5.0", b"firmowa"), ("0.9.0", b"maszynowa")):
        _wgraj_wersje(client, csrf, wersja, _plik_pe(wypelniacz=dodatek), "windows")
    with SessionLocal() as db:
        mapa = {
            w.version: w.id for w in db.execute(select(AgentRelease)).scalars()
        }
        maszyna = db.execute(select(Asset.id)).scalar_one()

    _oznacz_oficjalna(client, csrf, mapa["0.2.0"])
    _zlec_dla_firmy(client, csrf, tenant_a["id"], mapa["0.5.0"])
    client.post(
        f"/admin/tenants/{tenant_a['id']}/upgrade",
        data={"zakres": "wybrane", "release_id": mapa["0.9.0"],
              "asset_id": maszyna, "csrf_token": csrf},
        follow_redirects=True,
    )

    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["version"] == "0.9.0"


# --- odczyt wersji z pliku --------------------------------------------------

def _plik_z_metadanymi(wersja="0.7.0", system="windows", baza=None):
    """Plik agenta ze stopka, ktora dopisuje skrypt budujacy."""
    import json as _json

    tresc = baza if baza is not None else PLIK_AGENTA
    meta = _json.dumps({"version": wersja, "os_family": system, "built_at": "2026-08-20T10:00:00Z"})
    return tresc + b"\n<<<CMDB-AGENT-META>>>" + meta.encode("utf-8") + b"<<<KONIEC>>>"


def test_wersja_odczytana_z_pliku_bez_wpisywania(client, make_user):
    """Sedno poprawki: numeru nie trzeba podawac recznie."""
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "", "os_family": "windows", "notes": "", "csrf_token": csrf},
        files={"plik": ("agent.exe", io.BytesIO(_plik_z_metadanymi("0.7.0")), "application/octet-stream")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        assert db.execute(select(AgentRelease)).scalar_one().version == "0.7.0"


def test_numer_sprzeczny_z_plikiem_jest_odrzucany(client, make_user):
    """Rozbieznosc konczylaby sie aktualizacja proponowana bez konca - agent
    zglaszalby przeciez inna wersje niz oczekiwana."""
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "9.9.9", "os_family": "windows", "notes": "", "csrf_token": csrf},
        files={"plik": ("agent.exe", io.BytesIO(_plik_z_metadanymi("0.7.0")), "application/octet-stream")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_niezgodny_system_w_metadanych_jest_odrzucany(client, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "", "os_family": "windows", "notes": "", "csrf_token": csrf},
        files={"plik": ("agent.exe",
                        io.BytesIO(_plik_z_metadanymi("0.7.0", "linux")),
                        "application/octet-stream")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_plik_bez_metadanych_wymaga_podania_numeru(client, make_user):
    """Starszy build nadal da sie wgrac - trzeba tylko podac numer recznie."""
    csrf = _superadmin(client, make_user)
    assert _wgraj_wersje(client, csrf, "").status_code == 400
    assert _wgraj_wersje(client, csrf, "0.6.0").status_code == 303


# --- ustawienia firmy -------------------------------------------------------

def test_zmiana_nazwy_i_identyfikatora_firmy(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    client.post(
        f"/admin/tenants/{tenant_a['id']}/ustawienia",
        data={"name": "Firma A po zmianie", "slug": "firma-a-nowa",
              "report_interval_hours": "", "stale_after_hours": "",
              "snapshot_retention": "", "notes": "kontakt: Jan", "csrf_token": csrf},
        follow_redirects=True,
    )
    with SessionLocal() as db:
        firma = db.get(Tenant, tenant_a["id"])
        assert firma.name == "Firma A po zmianie"
        assert firma.slug == "firma-a-nowa"
        assert firma.notes == "kontakt: Jan"


def test_nie_mozna_zajac_identyfikatora_innej_firmy(client, tenant_a, tenant_b, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        f"/admin/tenants/{tenant_a['id']}/ustawienia",
        data={"name": "Firma A", "slug": "firma-b", "report_interval_hours": "",
              "stale_after_hours": "", "snapshot_retention": "", "notes": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


def test_ustawienia_firmy_maja_pierwszenstwo_przed_globalnymi(client, tenant_a, make_user):
    """Firma z laptopami moze miec inny prog niz serwerownia."""
    csrf = _superadmin(client, make_user)
    client.post(
        f"/admin/tenants/{tenant_a['id']}/ustawienia",
        data={"name": "Firma A", "slug": "firma-a", "report_interval_hours": "8",
              "stale_after_hours": "336", "snapshot_retention": "10",
              "notes": "", "csrf_token": csrf},
        follow_redirects=True,
    )

    # Agent tej firmy dostaje interwal firmowy, a nie globalny.
    token = enroll(client, tenant_a["token"], machine_id="maszyna-ustawienia").json()["agent_token"]
    odpowiedz = client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id="maszyna-ustawienia"),
    ).json()
    assert odpowiedz["report_interval_seconds"] == 8 * 3600

    with SessionLocal() as db:
        firma = db.get(Tenant, tenant_a["id"])
        assert firma.stale_after_hours == 336
        assert firma.snapshot_retention == 10


def test_puste_pole_oznacza_wartosc_globalna(client, tenant_a, make_user):
    """Nie zapisujemy wartosci domyslnej do firmy - inaczej zmiana globalna
    przestalaby dzialac dla firm, ktore nigdy niczego swiadomie nie ustawily."""
    csrf = _superadmin(client, make_user)
    client.post(
        f"/admin/tenants/{tenant_a['id']}/ustawienia",
        data={"name": "Firma A", "slug": "firma-a", "report_interval_hours": "6",
              "stale_after_hours": "", "snapshot_retention": "", "notes": "", "csrf_token": csrf},
        follow_redirects=True,
    )
    with SessionLocal() as db:
        firma = db.get(Tenant, tenant_a["id"])
        assert firma.report_interval_seconds == 6 * 3600
        assert firma.stale_after_hours is None
        assert firma.snapshot_retention is None


def test_odrzuca_bezsensowne_wartosci_ustawien(client, tenant_a, make_user):
    csrf = _superadmin(client, make_user)
    for pole, wartosc in (("report_interval_hours", "0"),
                          ("stale_after_hours", "-5"),
                          ("snapshot_retention", "nie-liczba")):
        dane = {"name": "Firma A", "slug": "firma-a", "report_interval_hours": "",
                "stale_after_hours": "", "snapshot_retention": "", "notes": "",
                "csrf_token": csrf}
        dane[pole] = wartosc
        odpowiedz = client.post(
            f"/admin/tenants/{tenant_a['id']}/ustawienia", data=dane, follow_redirects=False
        )
        assert odpowiedz.status_code == 400, f"{pole}={wartosc} powinno byc odrzucone"


# --- architektura procesora -------------------------------------------------

def _maszyna_arm(client, tenant, machine_id="raspberry-pi-1", hostname="PI-01"):
    """Raspberry Pi zglaszajacy sie jako linux/aarch64."""
    token = client.post(
        "/api/v1/agents/enroll",
        headers={"Authorization": f"Bearer {tenant['token']}"},
        json={
            "machine_id": machine_id,
            "identity": {"hostname": hostname, "os_family": "linux", "arch": "aarch64"},
            "agent_version": "0.5.0",
        },
    ).json()["agent_token"]
    raport = build_report(machine_id=machine_id, hostname=hostname)
    raport["identity"]["os_family"] = "linux"
    raport["identity"]["arch"] = "aarch64"
    client.post("/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport)
    return token


def test_architektura_odczytana_z_naglowka_pliku(client, make_user):
    """Czytamy naglowek, a nie deklaracje wgrywajacego - naglowek jest faktem."""
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY, "linux")          # ARM64
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY_X86, "linux")      # x86-64

    with SessionLocal() as db:
        architektury = {
            w.arch for w in db.execute(
                select(AgentRelease).where(AgentRelease.os_family == "linux")
            ).scalars()
        }
        assert architektury == {"aarch64", "x86_64"}


def test_raspberry_nie_dostanie_agenta_dla_x86(client, tenant_a, make_user):
    """Sedno zabezpieczenia: ELF dla x86-64 i dla ARM64 to oba "linux",
    ale Pi nie uruchomi pliku zbudowanego na serwerze x86."""
    token = _maszyna_arm(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY_X86, "linux")
    with SessionLocal() as db:
        x86 = db.execute(
            select(AgentRelease.id).where(AgentRelease.arch == "x86_64")
        ).scalar_one()

    _oznacz_oficjalna(client, csrf, x86)

    oferta = client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert oferta["available"] is False, "maszyna ARM nie moze dostac pliku dla x86"
    assert client.get(
        "/api/v1/agent/release", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 404


def test_raspberry_dostaje_agenta_dla_arm(client, tenant_a, make_user):
    token = _maszyna_arm(client, tenant_a)
    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY, "linux")     # ARM64
    with SessionLocal() as db:
        arm = db.execute(
            select(AgentRelease.id).where(AgentRelease.arch == "aarch64")
        ).scalar_one()

    _oznacz_oficjalna(client, csrf, arm)

    oferta = client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert oferta["available"] is True
    assert oferta["version"] == "0.5.0"
    assert client.get(
        "/api/v1/agent/release", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200


def test_ta_sama_wersja_dla_dwoch_architektur(client, make_user):
    """0.5.0 dla ARM64 i 0.5.0 dla x86-64 to dwa rozne pliki tego samego wydania."""
    csrf = _superadmin(client, make_user)
    assert _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY, "linux").status_code == 303
    assert _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY_X86, "linux").status_code == 303
    # ...ale ten sam plik dla tej samej architektury juz nie.
    assert _wgraj_wersje(client, csrf, "0.5.0", PLIK_LINUKSOWY, "linux").status_code == 400


def test_maszyna_bez_zgloszonej_architektury_nie_dostaje_nic(client, tenant_a, make_user):
    """Starszy agent lepiej niech zostanie na swojej wersji, niz ma pobrac
    plik, ktorego nie da sie wykonac."""
    token = client.post(
        "/api/v1/agents/enroll",
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
        json={
            "machine_id": "stary-agent-01",
            "identity": {"hostname": "STARY", "os_family": "windows"},   # bez arch
            "agent_version": "0.1.0",
        },
    ).json()["agent_token"]

    csrf = _superadmin(client, make_user)
    _wgraj_wersje(client, csrf, "0.5.0", PLIK_AGENTA, "windows")
    with SessionLocal() as db:
        wydanie = db.execute(select(AgentRelease.id)).scalar_one()
    _oznacz_oficjalna(client, csrf, wydanie)

    assert client.get(
        "/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"}
    ).json()["available"] is False


def test_odrzuca_plik_ktorego_architektury_nie_rozpoznajemy(client, make_user):
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post(
        "/admin/releases",
        data={"version": "0.5.0", "os_family": "windows", "notes": "", "csrf_token": csrf},
        files={"plik": ("agent.exe", io.BytesIO(b"MZ" + b"\x00" * 100), "application/octet-stream")},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


# --- agent dla Linuksa jako wydanie -----------------------------------------
#
# Na Linuksie agent jest paczka zrodel, nie plikiem wykonywalnym: PyInstaller
# nie kompiluje na inna architekture. Paczka musi mimo to podlegac tym samym
# regulom co plik dla Windows, inaczej maszyn linuksowych nie da sie
# aktualizowac z panelu.

def _zarejestruj_paczke(tmp_path, monkeypatch):
    from cmdb_server.config import get_settings
    from cmdb_server.services import pakiet

    zrodla = Path(__file__).resolve().parent.parent.parent / "agent"
    if not (zrodla / "cmdb_agent").is_dir():
        pytest.skip("zrodla agenta niedostepne")
    katalog = tmp_path / "paczka"
    metadane = pakiet.zbuduj(zrodla, katalog)
    monkeypatch.setattr(pakiet, "katalog_paczki", lambda: katalog)
    with SessionLocal() as db:
        pakiet.zarejestruj(db, metadane, katalog, Path(get_settings().release_dir))
    return metadane


def test_paczka_zrodel_jest_widoczna_jako_wydanie(client, make_user, tmp_path, monkeypatch):
    metadane = _zarejestruj_paczke(tmp_path, monkeypatch)
    _superadmin(client, make_user)
    strona = client.get("/admin/wersje").text

    assert "Agent dla Linuksa" in strona
    assert metadane["version"] in strona
    assert "zrodla" in strona, "paczka musi byc widoczna jako architektura zrodla"


def test_paczke_zrodel_da_sie_ustawic_jako_aktywna(client, make_user, tmp_path, monkeypatch):
    """O to chodzilo: maszyny linuksowe maja byc sterowane z panelu tak samo
    jak windowsowe."""
    _zarejestruj_paczke(tmp_path, monkeypatch)
    csrf = _superadmin(client, make_user)

    with SessionLocal() as db:
        wydanie = db.execute(
            select(AgentRelease.id).where(AgentRelease.os_family == "linux")
        ).scalar_one()

    _oznacz_oficjalna(client, csrf, wydanie)

    with SessionLocal() as db:
        cel = db.execute(
            select(GlobalAgentTarget).where(GlobalAgentTarget.os_family == "linux")
        ).scalar_one()
        assert cel.release_id == wydanie


def test_etykieta_tlumaczy_czym_jest_architektura_zrodla(client, make_user, tmp_path, monkeypatch):
    _zarejestruj_paczke(tmp_path, monkeypatch)
    _superadmin(client, make_user)
    assert "kazda architektura" in client.get("/admin/wersje").text


def test_brak_paczki_jest_zglaszany_wprost(client, make_user, tmp_path, monkeypatch):
    from cmdb_server.services import pakiet

    monkeypatch.setattr(pakiet, "katalog_paczki", lambda: tmp_path / "pusto")
    _superadmin(client, make_user)
    assert "nie zbudowal paczki zrodel" in client.get("/admin/wersje").text
