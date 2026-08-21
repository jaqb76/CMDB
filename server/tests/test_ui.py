"""Smoke testy panelu - kazdy widok musi sie wyrenderowac."""
from __future__ import annotations

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login


def _seed(client, tenant):
    token = enroll(client, tenant["token"], machine_id="maszyna-0001", hostname="SRV-PLIKI").json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id="maszyna-0001", hostname="SRV-PLIKI"),
    )
    with SessionLocal() as db:
        return db.execute(select(Asset)).scalar_one().id


def test_all_pages_render(client, tenant_a, make_user):
    asset_id = _seed(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    for path in ("/", "/assets", "/owners", "/tokens", "/audit", f"/assets/{asset_id}"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "Traceback" not in response.text

    detail = client.get(f"/assets/{asset_id}").text
    # Dane ze wszystkich sekcji raportu trafiaja do widoku.
    assert "Intel Core i5-11500" in detail          # sprzet
    assert "Mozilla Firefox" in detail              # oprogramowanie
    assert "jkowalski" in detail                    # uzytkownicy
    assert "Print Spooler" in detail                # uslugi
    assert "10.10.5.21" in detail                   # siec


def test_asset_without_report_renders(client, tenant_a, make_user):
    """Maszyna zarejestrowana, ale bez raportu - widok nie moze sie wysypac."""
    enroll(client, tenant_a["token"], machine_id="maszyna-nowa-01", hostname="NOWA")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset)).scalar_one().id

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    response = client.get(f"/assets/{asset_id}")
    assert response.status_code == 200
    assert "nie przyslala jeszcze zadnego raportu" in response.text


def test_owner_lifecycle(client, tenant_a, make_user):
    asset_id = _seed(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    csrf = _extract_csrf(client.get("/owners").text)
    client.post(
        "/owners",
        data={
            "full_name": "Anna Nowak",
            "email": "anna.nowak@firma-a.pl",
            "phone": "+48 600 100 200",
            "department": "IT",
            "notes": "",
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )
    owners_page = client.get("/owners")
    assert "Anna Nowak" in owners_page.text

    from cmdb_server.models import Owner

    with SessionLocal() as db:
        owner_id = db.execute(select(Owner)).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    client.post(
        f"/assets/{asset_id}/owner",
        data={"owner_id": owner_id, "role_label": "serwer plikow", "csrf_token": csrf},
        follow_redirects=True,
    )

    detail = client.get(f"/assets/{asset_id}").text
    assert "Anna Nowak" in detail
    assert "serwer plikow" in detail
    assert "Anna Nowak" in client.get("/assets").text


def test_token_issued_once_and_revocable(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    csrf = _extract_csrf(client.get("/tokens").text)
    response = client.post(
        "/tokens",
        data={"name": "laptopy handlowcow", "expires_days": 30, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "cmdb_ent_" in location

    issued = location.split("issued=")[1]
    # Nowy token dziala od razu do rejestracji agenta.
    assert enroll(client, issued, machine_id="maszyna-z-panelu-1").status_code == 201

    # Po odswiezeniu listy tokenu juz nie widac (jest tylko skrot w bazie).
    plain_list = client.get("/tokens").text
    assert issued not in plain_list


def test_raw_json_endpoint_returns_full_payload(client, tenant_a, make_user):
    asset_id = _seed(client, tenant_a)
    make_user(tenant_a["id"], "viewer@firma-a.pl", "bardzo-dlugie-haslo", role="viewer")
    _login(client, "viewer@firma-a.pl", "bardzo-dlugie-haslo")

    payload = client.get(f"/assets/{asset_id}/raw.json").json()
    assert payload["identity"]["hostname"] == "SRV-PLIKI"
    assert payload["software"]["packages"][0]["name"].startswith("7-Zip")
    assert payload["users"]["local_accounts"][1]["name"] == "jkowalski"


# --- motyw ------------------------------------------------------------------
#
# Ciemny motyw dzialal wczesniej wylacznie wedlug ustawienia systemu. Wybor
# uzytkownika wstawia SERWER do atrybutu data-theme - gdyby robil to dopiero
# JavaScript, przy kazdym wejsciu mignelaby wersja jasna.

def test_bez_ciasteczka_motyw_idzie_za_systemem(client, tenant_a, make_user):
    make_user(tenant_a["id"], "motyw@firma.pl", "haslo-do-testow-123")
    _login(client, "motyw@firma.pl", "haslo-do-testow-123")

    strona = client.get("/").text
    assert 'data-theme' not in strona, "brak wyboru = decyduje ustawienie systemu"


def test_wybrany_motyw_trafia_do_html(client, tenant_a, make_user):
    make_user(tenant_a["id"], "motyw@firma.pl", "haslo-do-testow-123")
    _login(client, "motyw@firma.pl", "haslo-do-testow-123")
    client.cookies.set("cmdb_motyw", "ciemny")

    assert 'data-theme="ciemny"' in client.get("/").text


def test_obca_wartosc_ciasteczka_jest_ignorowana(client, tenant_a, make_user):
    """Wartosc trafia wprost do HTML, wiec przepuszczamy wylacznie znane nazwy."""
    make_user(tenant_a["id"], "motyw@firma.pl", "haslo-do-testow-123")
    _login(client, "motyw@firma.pl", "haslo-do-testow-123")
    client.cookies.set("cmdb_motyw", '"><script>alert(1)</script>')

    strona = client.get("/").text
    assert "<script>alert(1)</script>" not in strona
    assert "data-theme" not in strona


def test_logowanie_tez_honoruje_motyw(client):
    """Inaczej po wylogowaniu motyw by znikal."""
    client.cookies.set("cmdb_motyw", "ciemny")
    assert 'data-theme="ciemny"' in client.get("/login").text


def test_przelacznik_jest_w_obu_panelach(client, tenant_a, make_user):
    make_user(tenant_a["id"], "motyw@firma.pl", "haslo-do-testow-123")
    _login(client, "motyw@firma.pl", "haslo-do-testow-123")
    assert "data-motyw" in client.get("/").text

    make_user(None, "root@motyw.pl", "haslo-do-testow-123")
    _login(client, "root@motyw.pl", "haslo-do-testow-123")
    assert "data-motyw" in client.get("/admin").text
