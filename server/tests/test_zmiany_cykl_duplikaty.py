"""Historia zmian, cykl zycia zasobu i wykrywanie duplikatow.

To sa trzy rzeczy, ktore odrozniaja CMDB od zwyklego spisu sprzetu:
wiadomo co sie zmienilo, wiadomo co jeszcze jest w uzyciu i wiadomo,
kiedy ta sama maszyna zglosila sie dwa razy.
"""
from __future__ import annotations

import copy

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    Asset,
    AssetChange,
)
from cmdb_server.services.changes import wykryj_zmiany
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login


def _zarejestruj(client, tenant, machine_id="maszyna-1", hostname="WS-01"):
    token = enroll(client, tenant["token"], machine_id=machine_id, hostname=hostname).json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id=machine_id, hostname=hostname),
    )
    return token


def _kolejny_raport(machine_id="maszyna-1", hostname="WS-01"):
    """Raport tej samej maszyny - machine_id musi zgadzac sie z poswiadczeniem."""
    return build_report(machine_id=machine_id, hostname=hostname)


def _wyslij(client, token, raport):
    return client.post(
        "/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport
    )


# --- wykrywanie roznic (bez bazy) -------------------------------------------

def test_pierwszy_raport_nie_jest_rozpisywany_na_zmiany():
    """Rozpisanie kilkuset zainstalowanych programow na osobne wpisy "dodano"
    nie niesie informacji - to, ze maszyna sie pojawila, widac po first_seen."""
    assert wykryj_zmiany(None, build_report()) == []


def test_raport_bez_zmian_nie_daje_wpisow():
    raport = build_report()
    assert wykryj_zmiany(raport, copy.deepcopy(raport)) == []


def test_wykrywa_instalacje_i_usuniecie_programu():
    stary = build_report()
    nowy = copy.deepcopy(stary)
    nowy["software"]["packages"].append({"name": "Nowy Program", "version": "1.0"})
    nowy["software"]["packages"] = [
        p for p in nowy["software"]["packages"] if p["name"] != "7-Zip 23.01"
    ]

    zmiany = wykryj_zmiany(stary, nowy)
    dodane = [z for z in zmiany if z["action"] == "dodano"]
    usuniete = [z for z in zmiany if z["action"] == "usunieto"]
    assert any("Nowy Program" in z["label"] for z in dodane)
    assert any("7-Zip" in z["label"] for z in usuniete)


def test_wykrywa_dodanie_administratora():
    """Najwazniejszy pojedynczy sygnal w calej historii zmian."""
    stary = build_report()
    nowy = copy.deepcopy(stary)
    nowy["users"]["administrators"].append({"name": "FIRMA-nowy-admin", "type": "uzytkownik"})

    zmiany = wykryj_zmiany(stary, nowy)
    admini = [z for z in zmiany if z["category"] == "users" and z["action"] == "dodano"]
    assert any("nowy-admin" in z["label"] for z in admini)


def test_wykrywa_zmiane_sprzetu():
    stary = build_report()
    nowy = copy.deepcopy(stary)
    nowy["hardware"]["memory"]["total_bytes"] = 34359738368

    zmiany = wykryj_zmiany(stary, nowy)
    pamiec = [z for z in zmiany if "pamiec" in z["label"]]
    assert pamiec and pamiec[0]["old_value"] == "16 GB" and pamiec[0]["new_value"] == "32 GB"


def test_procesy_i_sesje_nie_zasmiecaja_historii():
    """Lista procesow zmienia sie przy kazdym odczycie - gdyby liczyla sie
    jako zmiana, historia bylaby bezuzyteczna."""
    stary = build_report()
    nowy = copy.deepcopy(stary)
    nowy["software"]["processes"] = [{"name": "cokolwiek.exe", "pid": 999}]
    nowy["users"]["sessions"] = [{"user": "KTOS", "session_type": "konsola"}]
    nowy["os"]["uptime_seconds"] = 999999

    assert wykryj_zmiany(stary, nowy) == []


# --- zapis zmian przy raportowaniu ------------------------------------------

def test_zmiany_trafiaja_do_bazy_przy_raporcie(client, tenant_a):
    token = _zarejestruj(client, tenant_a)

    drugi = _kolejny_raport()
    drugi["software"]["packages"].append({"name": "Doinstalowany", "version": "2.0"})
    assert _wyslij(client, token, drugi).status_code == 200

    with SessionLocal() as db:
        zmiany = db.execute(select(AssetChange)).scalars().all()
        assert any("Doinstalowany" in z.label for z in zmiany)
        assert all(z.tenant_id == tenant_a["id"] for z in zmiany)


def test_historia_zmian_nie_przecieka_miedzy_firmami(client, tenant_a, tenant_b, make_user):
    token_a = _zarejestruj(client, tenant_a, "maszyna-a-1", "A-01")
    token_b = _zarejestruj(client, tenant_b, "maszyna-b-1", "B-01")

    zmieniony_a = build_report(machine_id="maszyna-a-1", hostname="A-01")
    zmieniony_a["software"]["packages"].append({"name": "TylkoWFirmieA", "version": "1.0"})
    _wyslij(client, token_a, zmieniony_a)

    zmieniony_b = build_report(machine_id="maszyna-b-1", hostname="B-01")
    zmieniony_b["software"]["packages"].append({"name": "TylkoWFirmieB", "version": "1.0"})
    _wyslij(client, token_b, zmieniony_b)

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    strona = client.get("/zmiany").text
    assert "TylkoWFirmieA" in strona
    assert "TylkoWFirmieB" not in strona


def test_widok_zmian_filtruje_po_rodzaju(client, tenant_a, make_user):
    token = _zarejestruj(client, tenant_a)
    drugi = _kolejny_raport()
    drugi["software"]["packages"].append({"name": "ProgramTestowy", "version": "1.0"})
    drugi["hardware"]["memory"]["total_bytes"] = 34359738368
    assert _wyslij(client, token, drugi).status_code == 200

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    tylko_sprzet = client.get("/zmiany?kategoria=hardware").text
    assert "pamiec RAM" in tylko_sprzet
    assert "ProgramTestowy" not in tylko_sprzet


# --- cykl zycia zasobu ------------------------------------------------------

def test_nowa_maszyna_jest_aktywna(client, tenant_a):
    _zarejestruj(client, tenant_a)
    with SessionLocal() as db:
        assert db.execute(select(Asset)).scalar_one().lifecycle == LIFECYCLE_AKTYWNY


def test_wycofanie_zachowuje_zasob_i_historie(client, tenant_a, make_user):
    """Wycofany zasob znika z list, ale nie z systemu - inwentarz ma pamietac,
    co bylo, a nie tylko co jest."""
    token = _zarejestruj(client, tenant_a)
    drugi = _kolejny_raport()
    drugi["software"]["packages"].append({"name": "SladWHistorii", "version": "1.0"})
    assert _wyslij(client, token, drugi).status_code == 200

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    client.post(
        f"/assets/{asset_id}/lifecycle",
        data={"akcja": "wycofaj", "powod": "sprzet oddany do utylizacji", "csrf_token": csrf},
        follow_redirects=True,
    )

    with SessionLocal() as db:
        maszyna = db.get(Asset, asset_id)
        assert maszyna.lifecycle == LIFECYCLE_WYCOFANY
        assert maszyna.retired_by == "admin@firma-a.pl"
        assert "utylizacji" in maszyna.retired_reason
        # Historia zostaje nietknieta.
        assert db.execute(select(AssetChange)).scalars().all()

    # Domyslna lista pomija wycofane, ale mozna je pokazac.
    assert "WS-01" not in client.get("/assets").text
    assert "WS-01" in client.get("/assets?lifecycle=wycofany").text
    assert "WS-01" in client.get("/assets?lifecycle=wszystkie").text


def test_przywrocenie_zasobu(client, tenant_a, make_user):
    _zarejestruj(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    for akcja in ("wycofaj", "przywroc"):
        client.post(
            f"/assets/{asset_id}/lifecycle",
            data={"akcja": akcja, "csrf_token": csrf},
            follow_redirects=True,
        )

    with SessionLocal() as db:
        maszyna = db.get(Asset, asset_id)
        assert maszyna.lifecycle == LIFECYCLE_AKTYWNY
        assert maszyna.retired_at is None


def test_viewer_nie_wycofa_zasobu(client, tenant_a, make_user):
    _zarejestruj(client, tenant_a)
    make_user(tenant_a["id"], "viewer@firma-a.pl", "bardzo-dlugie-haslo", role="viewer")
    _login(client, "viewer@firma-a.pl", "bardzo-dlugie-haslo")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    odpowiedz = client.post(
        f"/assets/{asset_id}/lifecycle",
        data={"akcja": "wycofaj", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 403


def test_wycofana_maszyna_nie_liczy_sie_w_zestawieniu(client, tenant_a, make_user):
    """Superadmin nie powinien widziec wycofanego sprzetu jako nieaktywnych agentow."""
    _zarejestruj(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    client.post(
        f"/assets/{asset_id}/lifecycle",
        data={"akcja": "wycofaj", "csrf_token": csrf},
        follow_redirects=True,
    )

    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    assert "WS-01" not in client.get("/admin").text


# --- duplikaty --------------------------------------------------------------

def test_wykrywa_maszyny_o_tym_samym_numerze_seryjnym(client, tenant_a, make_user):
    """Sklonowana maszyna wirtualna dziedziczy numer seryjny oryginalu."""
    _zarejestruj(client, tenant_a, "maszyna-1", "ORYGINAL")
    _zarejestruj(client, tenant_a, "maszyna-2", "KLON")

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    strona = client.get("/duplikaty").text
    assert "ORYGINAL" in strona and "KLON" in strona
    assert "SN-ABC-123" in strona          # wspolny numer seryjny z raportu testowego


def test_wycofanie_usuwa_zgloszenie_duplikatu(client, tenant_a, make_user):
    _zarejestruj(client, tenant_a, "maszyna-1", "ORYGINAL")
    _zarejestruj(client, tenant_a, "maszyna-2", "KLON")

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    with SessionLocal() as db:
        klon_id = db.execute(select(Asset.id).where(Asset.hostname == "KLON")).scalar_one()
    csrf = _extract_csrf(client.get(f"/assets/{klon_id}").text)
    client.post(
        f"/assets/{klon_id}/lifecycle",
        data={"akcja": "wycofaj", "powod": "duplikat po sklonowaniu", "csrf_token": csrf},
        follow_redirects=True,
    )

    assert "ORYGINAL" not in client.get("/duplikaty").text


def test_maszyny_roznych_firm_nie_sa_duplikatami(client, tenant_a, tenant_b, make_user):
    """Ten sam numer seryjny w dwoch firmach to dwa odrebne zasoby."""
    _zarejestruj(client, tenant_a, "maszyna-a", "A-01")
    _zarejestruj(client, tenant_b, "maszyna-b", "B-01")

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    strona = client.get("/duplikaty").text
    assert "B-01" not in strona
    assert "Nie znaleziono" in strona
