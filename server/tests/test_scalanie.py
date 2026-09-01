"""Laczenie wpisu recznego z maszyna, ktora zglosila sie agentem.

Sprawdzamy przede wszystkim to, czego automat ma NIE zrobic: zle scalenie jest
nieodwracalne i nie zglasza sie samo.
"""
from __future__ import annotations

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    LIFECYCLE_WYCOFANY,
    Asset,
    AssetRelation,
    WpisSlownika,
)
from cmdb_server.services import scalanie

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login

HASLO = "bardzo-dlugie-haslo"
SERIAL = "PC0KJ4Y2-2024"


def _admin(client, tenant, make_user, email="admin@firma-a.pl"):
    make_user(tenant["id"], email, HASLO)
    _login(client, email, HASLO)


def _wpis_reczny(tenant_id, serial=SERIAL, typ="komputer", **dodatkowe):
    """Sprzet wpisany zanim trafil do sieci: znamy zakup, nie znamy systemu."""
    with SessionLocal() as db:
        dostawca = WpisSlownika(tenant_id=tenant_id, kategoria="dostawca",
                                wartosc="Komputronik", klucz="komputronik", atrybuty={})
        db.add(dostawca)
        db.flush()
        sprzet = Asset(tenant_id=tenant_id, machine_id="reczne:pre-0001",
                       hostname="Nowy laptop dla ksiegowosci", typ=typ, zrodlo="reczne",
                       serial_number=serial, dostawca_id=dostawca.id,
                       invoice_number="FV/2026/114", support_contract="NBD 36m",
                       role_label="laptop ksiegowosci", **dodatkowe)
        db.add(sprzet)
        db.commit()
        return sprzet.id


def _zglasza_sie_agent(client, tenant, serial=SERIAL, machine_id="maszyna-scal-01"):
    dane = enroll(client, tenant["token"], machine_id=machine_id, hostname="LAP-KSIEG-01").json()
    raport = build_report(machine_id=machine_id, hostname="LAP-KSIEG-01")
    raport["hardware"]["system"]["serial_number"] = serial
    odpowiedz = client.post("/api/v1/inventory",
                            headers={"Authorization": f"Bearer {dane['agent_token']}"},
                            json=raport)
    assert odpowiedz.status_code == 200, odpowiedz.text
    return dane["asset_id"]


# --- scalanie automatyczne --------------------------------------------------

def test_agent_przejmuje_dane_wpisu_recznego(client, tenant_a):
    """Typowa droga: sprzet wpisany przed dostawa, potem staje na nim agent."""
    reczny_id = _wpis_reczny(tenant_a["id"])
    maszyna_id = _zglasza_sie_agent(client, tenant_a)

    with SessionLocal() as db:
        maszyna = db.get(Asset, maszyna_id)
        reczny = db.get(Asset, reczny_id)
        assert maszyna.invoice_number == "FV/2026/114", "faktura przeszla"
        assert maszyna.support_contract == "NBD 36m"
        assert maszyna.dostawca_id == reczny.dostawca_id
        assert maszyna.role_label == "laptop ksiegowosci"
        # Wpis ręczny zostaje - wycofany, nie skasowany.
        assert reczny.lifecycle == LIFECYCLE_WYCOFANY
        assert reczny.hostname == "Nowy laptop dla ksiegowosci"


def test_dane_od_agenta_nie_sa_nadpisywane(client, tenant_a):
    """Raport i wpis reczny opisuja rozlaczne rzeczy - scalenie ma dolozyc,
    a nie zastapic."""
    _wpis_reczny(tenant_a["id"])
    maszyna_id = _zglasza_sie_agent(client, tenant_a)

    with SessionLocal() as db:
        maszyna = db.get(Asset, maszyna_id)
        assert maszyna.hostname == "LAP-KSIEG-01", "nazwa pochodzi z raportu"
        assert maszyna.os_family, "system z raportu zostal"


def test_smieciowy_numer_seryjny_nie_wiaze(client, tenant_a):
    """Producenci zostawiaja wypelniacze. Wiazanie po nich scalilo by ze soba
    maszyny nie majace ze soba nic wspolnego - i nikt by tego nie zauwazyl."""
    for wypelniacz in ("To Be Filled By O.E.M.", "Default string", "0000000000"):
        assert not scalanie.sensowny_serial(wypelniacz), wypelniacz
    assert scalanie.sensowny_serial(SERIAL)

    reczny_id = _wpis_reczny(tenant_a["id"], serial="Default string")
    maszyna_id = _zglasza_sie_agent(client, tenant_a, serial="Default string")

    with SessionLocal() as db:
        assert db.get(Asset, reczny_id).lifecycle != LIFECYCLE_WYCOFANY
        assert db.get(Asset, maszyna_id).invoice_number is None


def test_inny_rodzaj_sprzetu_nie_jest_kandydatem(client, tenant_a):
    """Agent stoi na komputerze, nie na monitorze - ten sam numer seryjny przy
    monitorze jest zbiegiem okolicznosci, a nie ta sama rzecza."""
    reczny_id = _wpis_reczny(tenant_a["id"], typ="monitor")
    maszyna_id = _zglasza_sie_agent(client, tenant_a)

    with SessionLocal() as db:
        assert db.get(Asset, reczny_id).lifecycle != LIFECYCLE_WYCOFANY
        assert db.get(Asset, maszyna_id).invoice_number is None


def test_dwa_pasujace_wpisy_czekaja_na_czlowieka(client, tenant_a):
    """Przy dwoch kandydatach automat nie ma z czego wybrac."""
    with SessionLocal() as db:
        for numer in ("a", "b"):
            db.add(Asset(tenant_id=tenant_a["id"], machine_id=f"reczne:{numer}",
                         hostname=f"Wpis {numer}", typ="komputer", zrodlo="reczne",
                         serial_number=SERIAL))
        db.commit()

    maszyna_id = _zglasza_sie_agent(client, tenant_a)
    with SessionLocal() as db:
        assert db.get(Asset, maszyna_id).invoice_number is None
        wycofane = db.execute(select(Asset).where(
            Asset.lifecycle == LIFECYCLE_WYCOFANY)).scalars().all()
        assert not wycofane, "zaden wpis nie zostal wycofany na chybil trafil"


def test_scalanie_nie_wychodzi_poza_firme(client, tenant_a, tenant_b):
    reczny_id = _wpis_reczny(tenant_b["id"])
    maszyna_id = _zglasza_sie_agent(client, tenant_a)

    with SessionLocal() as db:
        assert db.get(Asset, reczny_id).lifecycle != LIFECYCLE_WYCOFANY
        assert db.get(Asset, maszyna_id).invoice_number is None


# --- scalanie reczne --------------------------------------------------------

def test_reczne_polaczenie_ze_strony_duplikatow(client, tenant_a, make_user):
    """Automat sie wstrzymal, bo numer byl wypelniaczem - czlowiek decyduje."""
    reczny_id = _wpis_reczny(tenant_a["id"], serial="Default string")
    maszyna_id = _zglasza_sie_agent(client, tenant_a, serial="Default string")
    _admin(client, tenant_a, make_user)

    strona = client.get("/duplikaty")
    assert strona.status_code == 200
    assert "Połącz w jeden zasób" in strona.text

    csrf = _extract_csrf(strona.text)
    odpowiedz = client.post("/duplikaty/scal", data={
        "docelowy_id": maszyna_id, "zrodlowy_id": reczny_id, "csrf_token": csrf},
        follow_redirects=False)
    assert odpowiedz.status_code == 303

    with SessionLocal() as db:
        assert db.get(Asset, maszyna_id).invoice_number == "FV/2026/114"
        assert db.get(Asset, reczny_id).lifecycle == LIFECYCLE_WYCOFANY


def test_relacje_wpisu_recznego_przechodza_na_maszyne(client, tenant_a, make_user):
    """Relacja wskazujaca wycofany wpis wskazywalaby zasob, ktorego juz nie ma
    w ewidencji."""
    reczny_id = _wpis_reczny(tenant_a["id"])
    with SessionLocal() as db:
        klaster = Asset(tenant_id=tenant_a["id"], machine_id="reczne:klaster",
                        hostname="Klaster A", typ="klaster", zrodlo="reczne")
        db.add(klaster)
        db.flush()
        db.add(AssetRelation(tenant_id=tenant_a["id"], source_id=reczny_id,
                             target_id=klaster.id, kind="host_cluster"))
        db.commit()

    maszyna_id = _zglasza_sie_agent(client, tenant_a)
    with SessionLocal() as db:
        relacja = db.execute(select(AssetRelation)).scalar_one()
        assert relacja.source_id == maszyna_id, "relacja wskazuje juz maszyne"


def test_wypelnione_pole_nie_jest_zastepowane(client, tenant_a, make_user):
    """Wartosc wpisana przy maszynie z agentem jest nowsza i swiadoma."""
    _wpis_reczny(tenant_a["id"])
    maszyna_id = _zglasza_sie_agent(client, tenant_a)

    # Drugi wpis reczny z inna faktura, scalany recznie po tym, jak pierwszy
    # juz swoja przekazal.
    with SessionLocal() as db:
        drugi = Asset(tenant_id=tenant_a["id"], machine_id="reczne:drugi",
                      hostname="Inny wpis", typ="komputer", zrodlo="reczne",
                      serial_number=SERIAL, invoice_number="FV/INNA")
        db.add(drugi)
        db.commit()
        drugi_id = drugi.id

    _admin(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/duplikaty").text)
    client.post("/duplikaty/scal", data={
        "docelowy_id": maszyna_id, "zrodlowy_id": drugi_id, "csrf_token": csrf},
        follow_redirects=False)

    with SessionLocal() as db:
        assert db.get(Asset, maszyna_id).invoice_number == "FV/2026/114"
