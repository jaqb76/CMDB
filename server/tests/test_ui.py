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
    assert "nie przysłała jeszcze żadnego raportu" in response.text


def test_owner_lifecycle(client, tenant_a, make_user):
    """Osoba jest wpisem slownika, wiec powstaje ta sama droga co lokalizacja."""
    from .test_sprzet_slowniki_konta import _dodaj_wpis

    asset_id = _seed(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    owner_id = _dodaj_wpis(client, "osoba", "Anna Nowak",
                           {"pole_email": "anna.nowak@firma-a.pl",
                            "pole_telefon": "600100200"})
    assert "Anna Nowak" in client.get("/slowniki?kategoria=osoba").text

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
    # Stary adres nadal dziala - prowadzi do slownika osob.
    assert client.get("/owners", follow_redirects=False).status_code == 303


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


# --- zwijane menu i nowy pulpit ---------------------------------------------

def test_zwijane_menu_jest_w_obu_panelach(client, tenant_a, make_user):
    make_user(tenant_a["id"], "menu@firma.pl", "haslo-do-testow-123")
    _login(client, "menu@firma.pl", "haslo-do-testow-123")

    portal = client.get("/").text
    assert 'data-sidebar-shell' in portal
    assert 'data-sidebar-toggle' in portal
    assert 'class="app-layout sidebar-collapsed"' in portal
    assert "Wszystkie zasoby" in portal
    assert "Wykrywanie sieci" in portal

    make_user(None, "root@menu.pl", "haslo-do-testow-123")
    _login(client, "root@menu.pl", "haslo-do-testow-123")
    administracja = client.get("/admin").text
    assert 'data-sidebar-shell' in administracja
    assert 'data-sidebar-toggle' in administracja
    assert "Firmy i konta" in administracja
    assert "Wydania agentów" in administracja
    # Administrator glowny dostaje wlasne menu systemowe, bez pozycji
    # operacyjnych konkretnej firmy.
    assert "Wykrywanie sieci" not in administracja


def test_pulpit_jest_nowym_widokiem_startowym(client, tenant_a, make_user):
    make_user(tenant_a["id"], "start@firma.pl", "haslo-do-testow-123")
    _login(client, "start@firma.pl", "haslo-do-testow-123")

    strona = client.get("/").text
    assert "dashboard-head" in strona
    assert "dashboard-cards" in strona
    assert "Najważniejsze informacje o zasobach" in strona


def test_skrypt_pamieta_szerokosc_menu():
    from pathlib import Path

    skrypt = (Path(__file__).resolve().parent.parent
              / "cmdb_server" / "static" / "app.js").read_text(encoding="utf-8")
    assert "cmdb_sidebar" in skrypt
    assert "rozwiniete" in skrypt
    assert "sidebar-mobile-open" in skrypt
    assert "max-width: 760px" in skrypt


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


# --- gorny pasek --------------------------------------------------------------

def test_przycisk_zmiany_firmy_jest_zapasowy(client, tenant_a, make_user):
    """Lista firm wysyla formularz sama, wiec przycisk obok tylko rozpycha
    pasek. Zostaje w kodzie, bo bez skryptu jest jedynym sposobem zmiany."""
    from pathlib import Path

    katalog = Path(__file__).resolve().parent.parent / "cmdb_server"
    szablon = (katalog / "templates" / "base_modern.html").read_text(encoding="utf-8")
    skrypt = (katalog / "static" / "app.js").read_text(encoding="utf-8")

    assert "data-zapasowy" in szablon, "przycisk musi zostac dla przegladarki bez skryptu"
    assert "data-zapasowy" in skrypt, "skrypt chowa go, gdy juz dziala"
    assert "data-autosubmit" in szablon


def test_prawa_strona_paska_nie_zawija(client, tenant_a, make_user):
    """Jeden zestaw akcji przeniesiony w polowie do drugiej linii wyglada
    jak usterka, a nie jak uklad."""
    from pathlib import Path

    styl = (Path(__file__).resolve().parent.parent
            / "cmdb_server" / "static" / "app.css").read_text(encoding="utf-8")
    fragment = styl.split(".userbox {")[1].split("}")[0]
    assert "flex-wrap: nowrap" in fragment
    # Adres skraca sie zamiast rozpychac pasek.
    assert "text-overflow: ellipsis" in styl.split(".user-email {")[1].split("}")[0]

# --- przelaczanie klasycznego i nowego ukladu -------------------------------

def test_uzytkownik_moze_przelaczac_stary_i_nowy_layout(client, tenant_a, make_user):
    make_user(tenant_a["id"], "layout@firma.pl", "haslo-do-testow-123")
    _login(client, "layout@firma.pl", "haslo-do-testow-123")

    nowy = client.get("/").text
    assert 'data-sidebar-shell' in nowy
    assert 'data-layout-switch="classic"' in nowy
    assert "Najważniejsze informacje o zasobach" in nowy

    client.cookies.set("cmdb_layout", "classic")
    klasyczny = client.get("/").text
    assert 'data-sidebar-shell' not in klasyczny
    assert 'class="mainnav"' in klasyczny
    assert 'data-layout-switch="modern"' in klasyczny
    assert "Najważniejsze informacje o zasobach" not in klasyczny

    make_user(None, "root-layout@cmdb.pl", "haslo-do-testow-123")
    _login(client, "root-layout@cmdb.pl", "haslo-do-testow-123")
    klasyczny_admin = client.get("/admin").text
    assert 'data-sidebar-shell' not in klasyczny_admin
    assert 'data-layout-switch="modern"' in klasyczny_admin


def test_rozwiniete_menu_nie_miga_przy_przejsciu_miedzy_stronami(client, tenant_a, make_user):
    """Stan menu przychodzi z serwera, wiec kolejna strona rysuje sie od razu
    rozwinieta - bez zwijania i rozwijania przy kazdym kliknieciu pozycji."""
    make_user(tenant_a["id"], "menu-stan@firma.pl", "haslo-do-testow-123")
    _login(client, "menu-stan@firma.pl", "haslo-do-testow-123")

    zwiniete = client.get("/assets").text
    assert 'class="app-layout sidebar-collapsed"' in zwiniete

    client.cookies.set("cmdb_sidebar", "rozwiniete")
    rozwiniete = client.get("/assets").text
    assert 'class="app-layout"' in rozwiniete
    assert "sidebar-collapsed" not in rozwiniete
    assert "Zwiń menu" in rozwiniete
    # Zaden skrypt nie poprawia juz szerokosci po narysowaniu strony.
    assert "localStorage" not in rozwiniete


def test_aktywna_pozycja_menu_jest_zaznaczana_przez_serwer(client, tenant_a, make_user):
    make_user(tenant_a["id"], "menu-akt@firma.pl", "haslo-do-testow-123")
    _login(client, "menu-akt@firma.pl", "haslo-do-testow-123")

    strona = client.get("/assets").text
    assert 'class="app-nav-link is-active" href="/assets"' in strona
    assert 'class="app-nav-link is-active" href="/zmiany"' not in strona

    pulpit = client.get("/").text
    assert 'class="app-nav-link is-active" href="/"' in pulpit


def test_szablony_nie_maja_skryptow_w_tresci_strony():
    """Naglowek CSP dopuszcza tylko script-src 'self', wiec skrypt wpisany
    wprost w szablon przegladarka blokuje - i widac to dopiero na serwerze,
    bo testy HTML-a same z siebie niczego nie wykonuja."""
    from pathlib import Path

    katalog = Path(__file__).resolve().parent.parent / "cmdb_server" / "templates"
    for szablon in sorted(katalog.glob("*.html")):
        tresc = szablon.read_text(encoding="utf-8")
        for fragment in tresc.split("<script")[1:]:
            naglowek = fragment.split(">")[0]
            assert "src=" in naglowek, f"{szablon.name}: skrypt w tresci strony"


def test_menu_nie_animuje_sie_przy_wczytywaniu_strony():
    """Przejscie miedzy stronami nie moze uruchamiac animacji menu - zwijanie
    i rozwijanie ma byc widoczne tylko wtedy, gdy klika je czlowiek."""
    from pathlib import Path

    katalog = Path(__file__).resolve().parent.parent / "cmdb_server"
    styl = (katalog / "static" / "app.css").read_text(encoding="utf-8")
    skrypt = (katalog / "static" / "app.js").read_text(encoding="utf-8")

    for regula in styl.split("}"):
        if "transition" in regula and ("grid-template-columns" in regula or ".nav-label" in regula
                                       or ".sidebar-brand-name" in regula or ".app-sidebar" in regula):
            assert "menu-animuje" in regula, regula
    assert 'classList.add("menu-animuje")' in skrypt


def test_odcisk_pliku_statycznego_zmienia_sie_po_podmianie(tmp_path, monkeypatch):
    """Bez tego poprawka w app.css albo app.js wygladala na nieskuteczna:
    adres z ?v= zostawal ten sam, wiec przegladarka podawala stary plik."""
    from cmdb_server.api import ui

    katalog = tmp_path
    plik = katalog / "app.css"
    plik.write_text("a{}", encoding="utf-8")
    monkeypatch.setattr(ui, "STATIC_DIR", katalog)
    monkeypatch.setattr(ui, "_ODCISKI", {})

    pierwszy = ui._odcisk("app.css")
    assert pierwszy == ui._odcisk("app.css")

    plik.write_text("a{color:red}", encoding="utf-8")
    import os

    os.utime(plik, (0, 0))
    assert ui._odcisk("app.css") != pierwszy


def test_menu_pamieta_szerokosc_w_ciasteczku():
    from pathlib import Path

    katalog = Path(__file__).resolve().parent.parent / "cmdb_server"
    skrypt = (katalog / "static" / "app.js").read_text(encoding="utf-8")

    assert 'cookieKey = "cmdb_sidebar"' in skrypt
    assert "document.cookie = cookieKey" in skrypt
    assert "data-layout-switch" in skrypt
    assert "cmdb_layout=" in skrypt
