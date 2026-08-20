"""Panel superadmina i mechanizm aktualizacji agentow."""
from __future__ import annotations

import hashlib
import io

from cmdb_server.db import SessionLocal
from cmdb_server.models import AgentRelease, Asset, EnrollmentToken, PortalUser, Tenant
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login

# Minimalny plik z sygnatura programu Windows - tyle wystarczy do sprawdzenia
# sciezki wgrywania, weryfikacji skrotu i wydawania pliku agentowi.
PLIK_AGENTA = b"MZ" + b"\x90\x00\x03" + b"testowa zawartosc agenta" * 40
SKROT_AGENTA = hashlib.sha256(PLIK_AGENTA).hexdigest()


def _superadmin(client, make_user):
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    return _extract_csrf(client.get("/admin").text)


PLIK_LINUKSOWY = bytes.fromhex("7f") + b"ELF" + b"" + b"agent dla linuksa" * 40
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
    assert "firm w systemie" in strona


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
    """Zestawienie ma pokazywac firme, liczbe agentow, aktywnych i wersje."""
    _przygotuj_maszyne(client, tenant_a)
    _superadmin(client, make_user)
    strona = client.get("/admin").text
    assert "zainstalowanych agentow" in strona
    assert "Wersje agentow" in strona
    assert "0.1.0" in strona          # wersja z raportu testowego
    assert "windows" in strona


def test_pulpit_firmy_pokazuje_wersje_agentow(client, tenant_a, make_user):
    """Administrator firmy widzi wersje pracujace u siebie."""
    _przygotuj_maszyne(client, tenant_a)
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")
    strona = client.get("/").text
    assert "Wersje agenta w firmie" in strona
    assert "0.1.0" in strona
