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


def test_pomoc_uzywa_zewnetrznych_stylow_zgodnych_z_csp(client, tenant_a, make_user):
    make_user(tenant_a["id"], "pomoc@firma.pl", "bardzo-dlugie-haslo")
    _login(client, "pomoc@firma.pl", "bardzo-dlugie-haslo")

    response = client.get("/pomoc")
    assert response.status_code == 200
    assert 'href="/static/pomoc.css?v=' in response.text
    assert 'src="/static/pomoc.js?v=' in response.text
    assert "<style>" not in response.text
    assert "<script>" not in response.text
    assert 'style="' not in response.text
    assert client.get("/static/pomoc.css").status_code == 200
    assert client.get("/static/pomoc.js").status_code == 200


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
            "department_id": "",
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


# --- ikona ------------------------------------------------------------------

def test_favicon_jest_wydawany(client):
    """Przegladarki pytaja o /favicon.ico niezaleznie od naglowka strony -
    bez tej trasy kazde wejscie zostawialo w logu 404."""
    odpowiedz = client.get("/favicon.ico")
    assert odpowiedz.status_code == 200
    assert odpowiedz.content[:4] == bytes([0, 0, 1, 0]), "to nie jest plik ICO"


def test_favicon_ma_kilka_rozmiarow():
    """Jeden rozmiar wystarcza przegladarce, ale zakladka, pasek zadan
    i skrot na pulpicie prosza o rozne."""
    from pathlib import Path

    from PIL import Image

    plik = Path(__file__).resolve().parent.parent / "cmdb_server" / "static" / "favicon.ico"
    rozmiary = sorted(Image.open(plik).info.get("sizes", []))
    assert (16, 16) in rozmiary
    assert (256, 256) in rozmiary


def test_strony_wskazuja_ikone(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ikona@firma.pl", "haslo-do-testow-123")
    _login(client, "ikona@firma.pl", "haslo-do-testow-123")

    for sciezka in ("/", "/login"):
        assert 'rel="icon"' in client.get(sciezka).text, sciezka


# --- pamiec podreczna plikow statycznych -------------------------------------

def test_arkusz_stylow_ma_znacznik_wersji(client, tenant_a, make_user):
    """Bez znacznika przegladarka trzymala poprzedni arkusz i uzytkownik po
    wdrozeniu poprawki wygladu dalej widzial stary uklad."""
    make_user(tenant_a["id"], "styl@firma.pl", "bardzo-dlugie-haslo")
    _login(client, "styl@firma.pl", "bardzo-dlugie-haslo")

    strona = client.get("/").text
    assert "/static/app.css?v=" in strona
    assert 'href="/static/app.css"' not in strona


def test_znacznik_zmienia_sie_razem_z_trescia(tmp_path, monkeypatch):
    """Znacznik ma odwzorowywac tresc pliku - staly numer wersji wypuszczalby
    nowy arkusz dopiero przy pamietaniu o jego podniesieniu."""
    from cmdb_server.api import ui

    plik = tmp_path / "probny.css"
    plik.write_text("a{}", encoding="utf-8")
    monkeypatch.setattr(ui, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(ui, "_ODCISKI", {})

    pierwszy = ui._odcisk("probny.css")

    plik.write_text("a{color:red}", encoding="utf-8")
    monkeypatch.setattr(ui, "_ODCISKI", {})
    assert ui._odcisk("probny.css") != pierwszy


def test_brak_pliku_nie_wywraca_strony(tmp_path, monkeypatch):
    from cmdb_server.api import ui

    monkeypatch.setattr(ui, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(ui, "_ODCISKI", {})
    assert ui._odcisk("nie-ma.css") == "/static/nie-ma.css"


# --- czas w strefie czytajacego ----------------------------------------------

def test_daty_wychodza_jako_element_time(client, tenant_a, make_user):
    """Sam napis "16:25 UTC" zmusza czytajacego do przeliczania w pamieci."""
    asset_id = _seed(client, tenant_a)
    make_user(tenant_a["id"], "czas@firma.pl", "bardzo-dlugie-haslo")
    _login(client, "czas@firma.pl", "bardzo-dlugie-haslo")

    strona = client.get(f"/assets/{asset_id}").text
    assert "<time datetime=" in strona
    assert "data-czas" in strona


def test_znacznik_czasu_zostaje_w_utc(client, tenant_a, make_user):
    """Atrybut datetime musi byc jednoznaczny - przeliczaniem zajmuje sie
    przegladarka, a nie serwer, ktory nie zna strefy czytajacego."""
    from cmdb_server.api.ui import _fmt_dt
    from datetime import datetime, timezone

    wynik = str(_fmt_dt(datetime(2026, 8, 31, 16, 25, tzinfo=timezone.utc)))
    assert 'datetime="2026-08-31T16:25:00Z"' in wynik
    # Bez JavaScriptu widac nadal poprawna godzine, tyle ze w UTC.
    assert "2026-08-31 16:25 UTC" in wynik


def test_brak_daty_nie_tworzy_pustego_znacznika():
    from cmdb_server.api.ui import _fmt_dt

    assert _fmt_dt(None) == "-"
    assert "<time" not in str(_fmt_dt(""))


def test_skrypt_przelicza_strefe():
    from pathlib import Path

    skrypt = (Path(__file__).resolve().parent.parent
              / "cmdb_server" / "static" / "app.js").read_text(encoding="utf-8")
    assert "time[data-czas]" in skrypt
    assert "resolvedOptions" in skrypt
    # Zapis UTC ma zostac w podpowiedzi - to wspolny punkt odniesienia
    # przy porownywaniu z logami serwera.
    assert "el.title" in skrypt
