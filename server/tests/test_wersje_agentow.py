"""Widok wersji agentow dla administratora firmy.

Dwie rzeczy sa tu wazne: administrator firmy ma WIDZIEC, gdzie aktualizacja
nie doszla, ale nie ma jej zlecac - ktora wersja jest rozsylana, decyduje
operator systemu. I druga: sprzet wpisany recznie nie ma agenta, wiec na
zadnej z tych list nie ma czego szukac.
"""
from __future__ import annotations

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _login

HASLO = "bardzo-dlugie-haslo"


def _zalogowany(client, tenant, make_user, email="admin@firma-a.pl", role="admin"):
    make_user(tenant["id"], email, HASLO, role)
    _login(client, email, HASLO)


def _maszyna_z_agentem(client, tenant, hostname="SRV-WER-01", machine_id="maszyna-wer-01"):
    token = enroll(client, tenant["token"], machine_id=machine_id,
                   hostname=hostname).json()["agent_token"]
    client.post("/api/v1/inventory", headers={"Authorization": f"Bearer {token}"},
                json=build_report(machine_id=machine_id, hostname=hostname))


def _wpis_reczny(tenant_id, hostname="Drukarka w sekretariacie"):
    with SessionLocal() as db:
        sprzet = Asset(tenant_id=tenant_id, machine_id="reczne:wer-0001",
                       hostname=hostname, typ="drukarka", zrodlo="reczne")
        db.add(sprzet)
        db.commit()
        return sprzet.id


def test_administrator_firmy_widzi_wersje_swoich_agentow(client, tenant_a, make_user):
    _maszyna_z_agentem(client, tenant_a)
    _zalogowany(client, tenant_a, make_user)

    strona = client.get("/wersje-agentow")
    assert strona.status_code == 200
    assert "SRV-WER-01" in strona.text


def test_wpis_reczny_nie_zalega_z_aktualizacja(client, tenant_a, make_user):
    """Sprzet bez agenta nie ma czego aktualizowac - na liscie wygladalby jak
    maszyna zalegajaca z wersja."""
    _maszyna_z_agentem(client, tenant_a)
    _wpis_reczny(tenant_a["id"])
    _zalogowany(client, tenant_a, make_user)

    strona = client.get("/wersje-agentow").text
    assert "SRV-WER-01" in strona
    assert "Drukarka w sekretariacie" not in strona


def test_widok_zleca_wersje_ale_nie_publikuje_wydan(client, tenant_a, make_user):
    """Ktora z OPUBLIKOWANYCH wersji ma stanac na maszynie, rozstrzyga ten, kto
    obsluguje zgloszenie - administrator firmy albo technik helpdesku.
    Publikowanie wydan zostaje u operatora systemu: kto wgrywa plik agenta,
    decyduje o tym, co uruchomi sie na maszynach klientow."""
    _maszyna_z_agentem(client, tenant_a)
    _zalogowany(client, tenant_a, make_user)

    strona = client.get("/wersje-agentow").text
    assert 'name="release_id"' in strona
    assert 'action="/wersje-agentow/cel"' in strona
    # Zadnego wgrywania pliku ani wejscia do panelu wydan.
    tresc = strona.split('<main class="content">')[1]
    assert 'type="file"' not in tresc
    assert "/admin/wersje" not in tresc


def test_konto_tylko_do_odczytu_nie_wchodzi(client, tenant_a, make_user):
    _zalogowany(client, tenant_a, make_user, email="widz@firma-a.pl", role="viewer")
    assert client.get("/wersje-agentow").status_code == 403


def test_widok_nie_pokazuje_maszyn_innej_firmy(client, tenant_a, tenant_b, make_user):
    _maszyna_z_agentem(client, tenant_b, hostname="OBCA-01", machine_id="maszyna-wer-obca")
    _maszyna_z_agentem(client, tenant_a)
    _zalogowany(client, tenant_a, make_user)

    strona = client.get("/wersje-agentow").text
    assert "SRV-WER-01" in strona
    assert "OBCA-01" not in strona
