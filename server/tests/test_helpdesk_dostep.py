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


def test_technik_pracuje_w_firmie_z_prawami_jej_administratora(client, tenant_a):
    """Dostep helpdeskowy daje prawa operatora CMDB tej firmy - takze kontu
    z rola "viewer". Technik, ktory moze tylko patrzec, musialby prosic kogos
    innego przy kazdym zgloszeniu o niedzialajacym agencie."""
    _maszyna(tenant_a["id"], "A-01")
    _technik("czytelnik@mojadomena.pl", [tenant_a["id"]], rola="viewer")
    _login(client, "czytelnik@mojadomena.pl", HASLO)

    strona = client.get("/assets")
    assert strona.status_code == 200
    assert "A-01" in strona.text
    # Widok wersji agentow jest widokiem operacyjnym - technik ma go widziec.
    assert client.get("/wersje-agentow").status_code == 200


def test_zwykly_viewer_firmy_nadal_nie_zapisuje(client, tenant_a, make_user):
    """Zmiana dotyczy technikow helpdesku, nie kazdego konta z rola viewer."""
    _maszyna(tenant_a["id"], "A-01")
    make_user(tenant_a["id"], "viewer@firma-a.pl", HASLO, role="viewer")
    _login(client, "viewer@firma-a.pl", HASLO)

    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalars().first()
    assert client.get("/assets").status_code == 200
    assert client.post(f"/assets/{asset_id}/dane", data={"nazwa": "ZMIENIONA"}).status_code == 403
    assert client.get("/wersje-agentow").status_code == 403


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


# --- wdrazanie wersji agenta ------------------------------------------------
#
# Zgloszenie "agent nie wysyla danych" konczy sie wdrozeniem innej wersji.
# Dotad mogl to zrobic wylacznie superadmin, wiec kazda taka sprawa czekala
# na niego.

def _wydanie(wersja="1.2.0", system="linux"):
    """Wydanie dopuszczone do rozsylania.

    Bierzemy paczke linuksowa, bo wydanie Windows przechodzi dodatkowa
    kontrole pliku na dysku - a tu sprawdzamy uprawnienia technika, nie
    zaufanie do pliku.
    """
    from cmdb_server.models import AgentRelease

    with SessionLocal() as db:
        wydanie = AgentRelease(
            version=wersja, os_family=system, arch="x86_64",
            filename=f"cmdb-agent-{wersja}.tar.gz", storage_name=f"{wersja}.tar.gz",
            sha256="a" * 64, size_bytes=1024,
        )
        db.add(wydanie)
        db.commit()
        return wydanie.id


def _maszyna_z_agentem(tenant_id: str, hostname: str, system: str = "linux") -> str:
    with SessionLocal() as db:
        maszyna = Asset(
            tenant_id=tenant_id, machine_id=f"id-{hostname}", hostname=hostname,
            os_family=system, agent_version="1.1.0",
        )
        db.add(maszyna)
        db.commit()
        return maszyna.id


def _csrf(client, sciezka="/wersje-agentow") -> str:
    from .test_tenant_isolation import _extract_csrf

    return _extract_csrf(client.get(sciezka).text)


def test_technik_wdraza_wersje_na_jednej_maszynie(client, tenant_a):
    maszyna_id = _maszyna_z_agentem(tenant_a["id"], "A-01")
    wydanie_id = _wydanie("1.2.0")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    odpowiedz = client.post("/wersje-agentow/cel", data={
        "csrf_token": _csrf(client), "zakres": "maszyny",
        "asset_id": maszyna_id, "release_id": wydanie_id,
    }, follow_redirects=False)

    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        maszyna = db.get(Asset, maszyna_id)
        assert maszyna.target_release_id == wydanie_id
        assert maszyna.upgrade_status == "zlecona"


def test_technik_wdraza_wersje_calej_firmie(client, tenant_a):
    maszyna_id = _maszyna_z_agentem(tenant_a["id"], "A-01")
    wydanie_id = _wydanie("1.2.0")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    odpowiedz = client.post("/wersje-agentow/cel", data={
        "csrf_token": _csrf(client), "zakres": "firma",
        "os_family": "linux", "release_id": wydanie_id,
    }, follow_redirects=False)

    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        from cmdb_server.models import TenantAgentTarget

        cel = db.execute(select(TenantAgentTarget)).scalar_one()
        assert cel.tenant_id == tenant_a["id"] and cel.release_id == wydanie_id
        # Ustawienie firmowe zdejmuje przypiecia pojedynczych maszyn.
        assert db.get(Asset, maszyna_id).target_release_id is None


def test_technik_cofa_maszyne_do_starszej_wersji(client, tenant_a):
    """Nowa wersja zawiodla na jednej maszynie - technik wraca na poprzednia,
    nie ruszajac reszty floty."""
    maszyna_id = _maszyna_z_agentem(tenant_a["id"], "A-01")
    stara = _wydanie("1.1.0")
    _wydanie("1.2.0")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    client.post("/wersje-agentow/cel", data={
        "csrf_token": _csrf(client), "zakres": "maszyny",
        "asset_id": maszyna_id, "release_id": stara,
    }, follow_redirects=False)

    with SessionLocal() as db:
        assert db.get(Asset, maszyna_id).target_release_id == stara


def test_technik_nie_zleci_wersji_maszynie_spoza_swoich_firm(client, tenant_a, tenant_b):
    obca_maszyna = _maszyna_z_agentem(tenant_b["id"], "B-01")
    wydanie_id = _wydanie("1.2.0")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    odpowiedz = client.post("/wersje-agentow/cel", data={
        "csrf_token": _csrf(client), "zakres": "maszyny",
        "asset_id": obca_maszyna, "release_id": wydanie_id,
    }, follow_redirects=False)

    assert odpowiedz.status_code == 400
    with SessionLocal() as db:
        assert db.get(Asset, obca_maszyna).target_release_id is None


def test_technik_nie_publikuje_wydan(client, tenant_a):
    """Kto wgrywa plik agenta, decyduje o tym, co uruchomi sie na maszynach
    klientow - to zostaje u operatora systemu."""
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert client.get("/admin/wersje", follow_redirects=False).status_code == 403
