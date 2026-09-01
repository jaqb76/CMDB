"""Zakladka aktualizacji na karcie maszyny.

Zainstalowane poprawki i brakujace to dwie rozne rzeczy i tylko druga mowi
cokolwiek o bezpieczenstwie. Testy pilnuja przede wszystkim tego, zeby
interfejs nie uspokajal tam, gdzie serwer nie ma wiedzy.
"""
from __future__ import annotations

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import PortalUser
from cmdb_server.services import inventory
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _login


def _zaloguj(client, make_user, tenant):
    make_user(tenant["id"], "admin@aktualizacje.pl", "haslo-do-testow-123")
    _login(client, "admin@aktualizacje.pl", "haslo-do-testow-123")


def _wyslij(client, tenant, updates_pending=None, updates=None, machine_id="maszyna-akt-0001"):
    token = enroll(client, tenant["token"], machine_id=machine_id, hostname="AKT-01").json()[
        "agent_token"
    ]
    raport = build_report(machine_id=machine_id, hostname="AKT-01")
    raport.setdefault("software", {})
    if updates is not None:
        raport["software"]["updates"] = updates
    if updates_pending is not None:
        raport["software"]["updates_pending"] = updates_pending
    odpowiedz = client.post(
        "/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport
    )
    assert odpowiedz.status_code == 200, odpowiedz.text
    return odpowiedz.json()["asset_id"]


def _braki(items, status="ok", index_age_hours=1.0):
    return {
        "status": status,
        "detail": None if status == "ok" else "apt-get nie odpowiedzial",
        "source": "apt",
        "checked_at": "2026-08-21T10:00:00+00:00",
        "index_age_hours": index_age_hours,
        "count": len(items) if status == "ok" else None,
        "security_count": (
            sum(1 for i in items if i.get("security")) if status == "ok" else None
        ),
        "entries": items,
    }


POPRAWKA_BEZPIECZENSTWA = {
    "id": "libssl3",
    "title": "libssl3",
    "current_version": "3.0.2-0ubuntu1.10",
    "new_version": "3.0.2-0ubuntu1.15",
    "source_repo": "Ubuntu:22.04/jammy-security",
    "security": True,
    "severity": None,
}
POPRAWKA_ZWYKLA = dict(POPRAWKA_BEZPIECZENSTWA, id="vim-common", title="vim-common",
                       source_repo="Ubuntu:22.04/jammy-updates", security=False)


# --- zakladka ---------------------------------------------------------------

def test_aktualizacje_maja_wlasna_zakladke(client, tenant_a, make_user):
    asset_id = _wyslij(client, tenant_a, _braki([POPRAWKA_BEZPIECZENSTWA]))
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert 'data-tab="aktualizacje"' in strona
    assert 'data-panel="aktualizacje"' in strona


def test_braki_pokazane_z_wyroznieniem_bezpieczenstwa(client, tenant_a, make_user):
    asset_id = _wyslij(client, tenant_a, _braki([POPRAWKA_BEZPIECZENSTWA, POPRAWKA_ZWYKLA]))
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert "libssl3" in strona
    assert "vim-common" in strona
    assert "bezpieczeństwo" in strona
    assert "jammy-security" in strona


def test_maszyna_bez_brakow_dostaje_potwierdzenie(client, tenant_a, make_user):
    asset_id = _wyslij(client, tenant_a, _braki([]))
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert "nic nie brakuje" in strona


def test_nieznany_stan_nie_udaje_ze_wszystko_gra(client, tenant_a, make_user):
    """Sedno: "nie udalo sie sprawdzic" nie moze wygladac jak "0 brakow"."""
    asset_id = _wyslij(client, tenant_a, _braki([], status="nieznany"))
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert "Nie udało się sprawdzić" in strona
    assert "nie znaczy, że maszyna jest aktualna" in strona
    assert "nic nie brakuje" not in strona


def test_stary_indeks_jest_zglaszany(client, tenant_a, make_user):
    """Brak brakow przy indeksie sprzed miesiaca to nie jest dobra wiadomosc."""
    asset_id = _wyslij(client, tenant_a, _braki([], index_age_hours=24 * 30))
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert "Indeks pakietow ma 30 dni" in strona


def test_swiezy_indeks_nie_wywoluje_ostrzezenia(client, tenant_a, make_user):
    asset_id = _wyslij(client, tenant_a, _braki([], index_age_hours=3.0))
    _zaloguj(client, make_user, tenant_a)
    assert "Indeks pakietow ma" not in client.get(f"/assets/{asset_id}").text


def test_starszy_agent_nie_udaje_ze_sprawdzil(client, tenant_a, make_user):
    """Agenci sprzed tej zmiany nie przysylaja sekcji brakow. Interfejs ma
    powiedziec, ze nie sprawdza - a nie pokazac zero."""
    asset_id = _wyslij(client, tenant_a, updates=[{"id": "KB5001", "description": "Poprawka"}])
    _zaloguj(client, make_user, tenant_a)

    strona = client.get(f"/assets/{asset_id}").text
    assert "nie sprawdza brakujących aktualizacji" in strona
    assert "KB5001" in strona, "zainstalowane poprawki maja byc widoczne dalej"


# --- deduplikacja -----------------------------------------------------------

def test_znacznik_sprawdzenia_nie_uniewaznia_deduplikacji(client, tenant_a):
    """Znacznik czasu zmienia sie przy kazdym raporcie. Gdyby wchodzil do
    odcisku, kazdy raport wygladalby na zmiane i baza rosla by o pelna kopie
    co cykl, mimo ze na maszynie nic sie nie stalo."""
    raport = build_report(machine_id="maszyna-dedup-01", hostname="DED-01")
    raport.setdefault("software", {})["updates_pending"] = _braki([POPRAWKA_BEZPIECZENSTWA])

    pierwszy = inventory.stable_fingerprint(raport)

    pozniejszy = dict(raport)
    pozniejszy["software"] = dict(raport["software"])
    pozniejszy["software"]["updates_pending"] = dict(
        raport["software"]["updates_pending"],
        checked_at="2026-08-21T14:00:00+00:00",
        index_age_hours=5.0,
    )

    assert inventory.stable_fingerprint(pozniejszy) == pierwszy


def test_nowy_brak_jest_jednak_zmiana(client, tenant_a):
    """Deduplikacja nie moze zjesc informacji o nowej brakujacej poprawce."""
    raport = build_report(machine_id="maszyna-dedup-02", hostname="DED-02")
    raport.setdefault("software", {})["updates_pending"] = _braki([])

    przed = inventory.stable_fingerprint(raport)

    raport["software"]["updates_pending"] = _braki([POPRAWKA_BEZPIECZENSTWA])
    assert inventory.stable_fingerprint(raport) != przed


# --- slad po nieudanej probie aktualizacji ------------------------------------

def _agent(client, tenant):
    dane = enroll(client, tenant["token"], machine_id="maszyna-slad-0001").json()
    return dane["agent_token"], dane["asset_id"]


def _ustaw_wersje(asset_id, wersja, status, detail):
    from cmdb_server.models import Asset

    with SessionLocal() as db:
        maszyna = db.get(Asset, asset_id)
        maszyna.agent_version = wersja
        maszyna.upgrade_status = status
        maszyna.upgrade_detail = detail
        db.commit()


def _stan(asset_id):
    from cmdb_server.models import Asset

    with SessionLocal() as db:
        maszyna = db.get(Asset, asset_id)
        return maszyna.upgrade_status, maszyna.upgrade_detail


def test_odrzucenie_znika_gdy_maszyna_doszla_do_wersji_docelowej(client, tenant_a):
    """Panel pokazywal obok siebie "aktualna" i "odrzucona" z komunikatem
    sprzed naprawy. Nie bylo juz czego proponowac, wiec nic tego nie kasowalo."""
    token, asset_id = _agent(client, tenant_a)
    _ustaw_wersje(asset_id, "0.6.16+1", "odrzucona", "agent nie dziala z instalacji zalozonej instalatorem")

    odpowiedz = client.get("/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"})
    assert odpowiedz.status_code == 200
    assert odpowiedz.json()["available"] is False
    assert _stan(asset_id) == (None, None)


def test_przebieg_w_toku_nie_jest_kasowany(client, tenant_a):
    """"zlecona" i "pobrana" opisuja aktualizacje w trakcie, a nie jej porazke."""
    token, asset_id = _agent(client, tenant_a)
    _ustaw_wersje(asset_id, "0.6.16+1", "zlecona", None)

    client.get("/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"})
    assert _stan(asset_id)[0] == "zlecona"


def test_historia_prob_zostaje_w_dzienniku(client, tenant_a):
    """Kasujemy znacznik stanu przy maszynie, nie zapis zdarzenia."""
    from cmdb_server.models import Asset, AgentUpgradeLog
    from cmdb_server.services import upgrades

    token, asset_id = _agent(client, tenant_a)
    with SessionLocal() as db:
        maszyna = db.get(Asset, asset_id)
        upgrades.zapisz_wynik(db, maszyna, None, "odrzucona", "stara przyczyna")
        maszyna.agent_version = "0.6.16+1"
        db.commit()

    client.get("/api/v1/agent/version", headers={"Authorization": f"Bearer {token}"})
    with SessionLocal() as db:
        wpisy = db.execute(select(AgentUpgradeLog).where(AgentUpgradeLog.asset_id == asset_id)).scalars().all()
        assert [w.status for w in wpisy] == ["odrzucona"]
        assert wpisy[0].detail == "stara przyczyna"
    assert _stan(asset_id) == (None, None)
