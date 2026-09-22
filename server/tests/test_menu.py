"""Menu nowego ukladu: zwijane grupy, menu konta i audyt w panelu administratora."""
from __future__ import annotations

from cmdb_server.db import SessionLocal
from cmdb_server.models import AuditLog

from .test_tenant_isolation import _login

HASLO = "haslo-do-testow-123"


def _superadmin(client, make_user):
    make_user(None, "root-menu@cmdb.pl", HASLO)
    _login(client, "root-menu@cmdb.pl", HASLO)


def _zdarzenia(tenant_a, tenant_b):
    with SessionLocal() as db:
        db.add_all([AuditLog(tenant_id=t, actor="test", action="test.menu", target=cel)
                    for t, cel in [(tenant_a["id"], "zdarzenie-a"), (tenant_b["id"], "zdarzenie-b"),
                                   (None, "zdarzenie-systemowe")]])
        db.commit()


# --- audyt ------------------------------------------------------------------

def test_stary_adres_audytu_prowadzi_do_panelu_z_filtrem_firmy(client, tenant_a, make_user):
    _superadmin(client, make_user)
    client.get(f"/switch-tenant?slug={tenant_a['slug']}")
    odpowiedz = client.get("/audit", follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert odpowiedz.headers["location"] == f"/admin/audyt?firma={tenant_a['slug']}"
    # Pozycja w menu portalu prowadzi w to samo miejsce.
    assert f'href="/admin/audyt?firma={tenant_a["slug"]}"' in client.get("/").text


def test_audyt_w_panelu_filtruje_po_firmie(client, tenant_a, tenant_b, make_user):
    _superadmin(client, make_user)
    _zdarzenia(tenant_a, tenant_b)

    wszystkie = client.get("/admin/audyt").text
    assert all(c in wszystkie for c in ("zdarzenie-a", "zdarzenie-b", "zdarzenie-systemowe"))

    firma_a = client.get(f"/admin/audyt?firma={tenant_a['slug']}").text
    assert "zdarzenie-a" in firma_a
    assert "zdarzenie-b" not in firma_a and "zdarzenie-systemowe" not in firma_a

    systemowe = client.get("/admin/audyt?firma=_systemowe").text
    assert "zdarzenie-systemowe" in systemowe and "zdarzenie-a" not in systemowe

    # Literowka w adresie nie moze pokazac zdarzen wszystkich firm.
    nieznana = client.get("/admin/audyt?firma=nie-ma-takiej").text
    assert "zdarzenie-a" not in nieznana and "zdarzenie-b" not in nieznana


def test_konto_firmy_nie_ma_audytu(client, tenant_a, make_user):
    make_user(tenant_a["id"], "firma-menu@firma.pl", HASLO)
    _login(client, "firma-menu@firma.pl", HASLO)
    strona = client.get("/").text
    assert "/admin/audyt" not in strona and 'href="/audit"' not in strona
    assert client.get("/audit", follow_redirects=False).status_code == 403
    assert client.get("/admin/audyt", follow_redirects=False).status_code == 403


# --- grupy i menu konta -----------------------------------------------------

def test_zwinieta_grupa_przychodzi_z_serwera_a_aktywna_zostaje_otwarta(client, tenant_a, make_user):
    make_user(tenant_a["id"], "grupy@firma.pl", HASLO)
    _login(client, "grupy@firma.pl", HASLO)
    client.cookies.set("cmdb_menu_zwiniete", "agenty.zasoby")

    pulpit = client.get("/").text
    assert 'class="nav-group is-zwinieta" data-nav-group="agenty"' in pulpit
    assert 'class="nav-group is-zwinieta" data-nav-group="zasoby"' in pulpit
    assert 'class="nav-group" data-nav-group="monitorowanie"' in pulpit

    # Na liscie zasobow grupa "Zasoby" jest otwarta mimo zapisanego zwiniecia.
    zasoby = client.get("/assets").text
    assert 'class="nav-group" data-nav-group="zasoby"' in zasoby
    assert 'class="nav-group is-zwinieta" data-nav-group="agenty"' in zasoby


def test_menu_konta_zawiera_konto_pomoc_uklad_i_wylogowanie(client, tenant_a, make_user):
    make_user(tenant_a["id"], "konto-menu@firma.pl", HASLO)
    _login(client, "konto-menu@firma.pl", HASLO)
    strona = client.get("/").text
    menu = strona.split("data-konto-menu")[1].split("</details>")[0]
    for fragment in ('href="/konto"', 'href="/pomoc"', 'data-layout-switch="classic"', 'action="/logout"'):
        assert fragment in menu, fragment
    # Dolny pasek menu bocznego zostaje dla administratora glownego - zwykle
    # konto nie ma go wcale.
    assert "sidebar-bottom" not in strona
    # Grupy "Analiza" juz nie ma - raporty sa w "Monitorowanie i raporty".
    assert "Monitorowanie i raporty" in strona and ">Analiza<" not in strona


def test_panel_administratora_ma_menu_konta(client, tenant_a, make_user):
    _superadmin(client, make_user)
    strona = client.get("/admin").text
    assert "data-konto-menu" in strona
    assert 'data-nav-group="admin-nadzor"' in strona
