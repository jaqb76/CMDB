"""Technik helpdesku w panelu: widzi firmy, ktore dostal, i tylko te.

Dostep helpdeskowy jest jedynym miejscem w systemie, w ktorym konto bez
wlasnej firmy oglada dane firm - i jedynym, ktore rozszerza granice izolacji
opisana w test_tenant_isolation. Dlatego kazde "moze" ma tu obok siebie
odpowiadajace mu "nie moze".
"""
from __future__ import annotations

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, PortalUser, Tenant
from cmdb_server.security import hash_password
from cmdb_server.services import helpdesk

from .test_tenant_isolation import _login

HASLO = "haslo-technika-2026"


def _technik(email: str, firmy: list[str], rola: str = "admin") -> str:
    """Konto bez wlasnej firmy, z dostepem helpdeskowym do wskazanych firm."""
    with SessionLocal() as db:
        user = PortalUser(
            tenant_id=None, email=email, full_name="Technik Testowy",
            password_hash=hash_password(HASLO), role=rola,
        )
        db.add(user)
        db.flush()
        for tenant_id in firmy:
            helpdesk.nadaj_dostep(db, user.id, tenant_id, nadal="superadmin")
        db.commit()
        return user.id


def _maszyna(tenant_id: str, hostname: str) -> None:
    with SessionLocal() as db:
        db.add(Asset(tenant_id=tenant_id, machine_id=f"id-{hostname}", hostname=hostname))
        db.commit()


def test_technik_widzi_maszyny_swojej_firmy(client, tenant_a, tenant_b):
    _maszyna(tenant_a["id"], "A-01")
    _maszyna(tenant_b["id"], "B-01")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    strona = client.get("/assets").text
    assert "A-01" in strona
    assert "B-01" not in strona


def test_technik_nie_wejdzie_do_firmy_bez_dostepu_parametrem(client, tenant_a, tenant_b):
    """Nieznany slug nie moze byc furtka: wracamy do pierwszej dozwolonej firmy,
    a nie do tej wskazanej w adresie."""
    _maszyna(tenant_a["id"], "A-01")
    _maszyna(tenant_b["id"], "B-01")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    strona = client.get(f"/assets?tenant={tenant_b['slug']}").text
    assert "A-01" in strona
    assert "B-01" not in strona


def test_technik_przelacza_sie_miedzy_swoimi_firmami(client, tenant_a, tenant_b):
    _maszyna(tenant_a["id"], "A-01")
    _maszyna(tenant_b["id"], "B-01")
    _technik("technik@mojadomena.pl", [tenant_a["id"], tenant_b["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert "A-01" in client.get(f"/assets?tenant={tenant_a['slug']}").text
    assert "B-01" in client.get(f"/assets?tenant={tenant_b['slug']}").text


def test_przelacznik_firm_pokazuje_sie_dopiero_przy_dwoch(client, tenant_a, tenant_b):
    _technik("jedna@mojadomena.pl", [tenant_a["id"]])
    _login(client, "jedna@mojadomena.pl", HASLO)
    assert "/switch-tenant" not in client.get("/assets").text

    client.post("/logout")
    _technik("dwie@mojadomena.pl", [tenant_a["id"], tenant_b["id"]])
    _login(client, "dwie@mojadomena.pl", HASLO)
    assert "/switch-tenant" in client.get("/assets").text


def test_switch_tenant_odmawia_firmy_bez_dostepu(client, tenant_a, tenant_b):
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    odpowiedz = client.get(f"/switch-tenant?slug={tenant_b['slug']}", follow_redirects=False)
    assert odpowiedz.status_code == 403

    # Ciasteczko sie nie ustawilo, wiec kolejne wejscie nadal pokazuje firme A.
    _maszyna(tenant_a["id"], "A-01")
    assert "A-01" in client.get("/assets").text


def test_konto_bez_firmy_i_bez_dostepow_nie_wchodzi(client, make_user):
    """Samo konto w portalu nie jest jeszcze uprawnieniem do czyichkolwiek danych."""
    with SessionLocal() as db:
        db.add(PortalUser(
            tenant_id=None, email="nikt@mojadomena.pl",
            password_hash=hash_password(HASLO), role="admin",
        ))
        db.commit()
    _login(client, "nikt@mojadomena.pl", HASLO)

    assert client.get("/assets").status_code == 403


def test_odebranie_dostepu_zamyka_wejscie_do_firmy(client, tenant_a):
    _maszyna(tenant_a["id"], "A-01")
    user_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)
    assert "A-01" in client.get("/assets").text

    with SessionLocal() as db:
        helpdesk.odbierz_dostep(db, user_id, tenant_a["id"])
        db.commit()

    assert client.get("/assets").status_code == 403


def test_technik_bez_prawa_zapisu_oglada_ale_nie_zmienia(client, tenant_a):
    """Rola konta nadal decyduje o zapisie: viewer obsluguje zgloszenia,
    ale nie przestawia danych w kartotece firmy."""
    _maszyna(tenant_a["id"], "A-01")
    _technik("czytelnik@mojadomena.pl", [tenant_a["id"]], rola="viewer")
    _login(client, "czytelnik@mojadomena.pl", HASLO)

    strona = client.get("/assets")
    assert strona.status_code == 200
    assert "A-01" in strona.text
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalars().first()
    assert client.post(f"/assets/{asset_id}/dane", data={"nazwa": "ZMIENIONA"}).status_code == 403


def test_technik_nie_wchodzi_do_panelu_superadmina(client, tenant_a):
    """Dostep do firm nie jest awansem na superadmina - dostepy nadaje tylko on."""
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert client.get("/admin", follow_redirects=False).status_code == 403
    assert client.get("/admin/firmy", follow_redirects=False).status_code == 403


def test_firma_nieaktywna_nie_wpuszcza_technika(client, tenant_a):
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    with SessionLocal() as db:
        db.get(Tenant, tenant_a["id"]).is_active = False
        db.commit()
    _login(client, "technik@mojadomena.pl", HASLO)

    assert client.get("/assets").status_code == 403
