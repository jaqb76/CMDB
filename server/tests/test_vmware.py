"""VMware vCenter: ta sama droga co Nutanix, osobna konfiguracja i osobne obiekty."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetRelation, AuditLog, NutanixObiekt
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, odczyt as odczyt_nutanix, polityka as polityka_nutanix
from .test_nutanix import ustawienia_agenta, wlacz as wlacz_nutanix, wyslij as wyslij_nutanix, zarejestruj

# vCenter UUID z BIOS-u raportuje w kolejnosci bajtow SMBIOS - agent w
# maszynie widzi te same bajty, czasem odwrocone.
BIOS_Z_AGENTEM = "4231a8f2-1c2d-3e4f-5a6b-7c8d9e0f1a2b"


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, adres="vcenter.firma.pl", uzytkownik="cmdb-ro@vsphere.local", **extra)
    odp = client.post(f"/assets/{asset_id}/vmware", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="c" * 32):
    odp = client.get("/api/v1/agent/vmware-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, vm=None, hosty=None):
    return {
        "protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 900,
        "wersja_pc": "8.0.3.00300",
        "klastry": [{"ext_id": "domain-c8", "nazwa": "RCHO-VSAN-01", "hipernadzorca": "ESXi",
                     "liczba_hostow": 2}],
        "hosty": hosty if hosty is not None else [
            {"ext_id": "host-10", "nazwa": "esx01.firma.pl", "klaster_id": "domain-c8",
             "hipernadzorca": "VMware ESXi 8.0.3 build-24280767", "producent": "Dell Inc.",
             "model": "PowerEdge R760", "numer_seryjny": "ABC1234", "cpu_model": "Intel Xeon Gold 6430",
             "gniazda": 2, "rdzenie": 64, "watki": 128, "ram_bajty": 1024 * 2**30, "ip": "10.30.0.11",
             "tryb_serwisowy": True, "uruchomiony_o": "2026-08-01T10:00:00Z"},
            {"ext_id": "host-11", "nazwa": "esx02.firma.pl", "klaster_id": "domain-c8"},
        ],
        "vm": vm if vm is not None else [
            {"ext_id": "vm-101", "nazwa": "bastionjps", "klaster_id": "domain-c8", "host_id": "host-10",
             "stan": "ON", "bios_uuid": BIOS_Z_AGENTEM, "gniazda": 2, "rdzenie_na_gniazdo": 2},
            {"ext_id": "vm-102", "nazwa": "db-01", "klaster_id": "domain-c8", "host_id": "host-11",
             "stan": "OFF", "system": "Microsoft Windows Server 2022 (64-bit)"},
        ],
    }


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/vmware", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user):
    enrolled = zarejestruj(client, tenant, uuid=BIOS_Z_AGENTEM)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def test_konfiguracja_vmware_jest_osobna_od_nutanix(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = wlacz(client, csrf, enrolled["asset_id"])
    assert odp.headers["location"].endswith("?funkcja=vmware#agent")
    p = polityka(client, enrolled)
    assert p["policy"]["enabled"] is True and p["policy"]["adres"] == "https://vcenter.firma.pl:443"
    assert p["policy"]["uzytkownik"] == "cmdb-ro@vsphere.local"
    # Nutanix na tej samej maszynie dalej wylaczony.
    assert polityka_nutanix(client, enrolled)["policy"] == {"enabled": False, "test": False}
    with SessionLocal() as db:
        assert ustawienia_agenta(db, enrolled["asset_id"], "vmware").haslo
        assert db.scalars(select(AuditLog).where(AuditLog.action == "vmware.config_changed")).one()

    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=vmware").text
    assert "Odczytuj vCenter z tej maszyny" in strona and 'action="/assets/' in strona
    assert f'/assets/{enrolled["asset_id"]}/vmware/test' in strona
    assert f'/assets/{enrolled["asset_id"]}/vmware/odczyt' in strona  # takze przy wlaczonym odczycie

    blad = client.post(f"/assets/{enrolled['asset_id']}/vmware",
                       data=formularz(csrf, revision=p["revision"], adres="http://vc"),
                       follow_redirects=False)
    assert blad.headers["location"].endswith("?funkcja=vmware&nutanix_komunikat=adres#agent")
    assert "Niepoprawny adres vCenter" in client.get(blad.headers["location"]).text


def test_odczyt_vcenter_zaklada_drzewo_i_laczy_vm_z_agentem(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        obiekty = db.scalars(select(NutanixObiekt)).all()
        assert {o.dostawca for o in obiekty} == {"vmware"}
        assert all("/" in o.ext_id for o in obiekty)  # przestrzen adresu vCenter
        agent = db.get(Asset, enrolled["asset_id"])
        db01 = db.scalars(select(Asset).where(Asset.hostname == "db-01")).one()
        assert db01.zrodlo == "vmware" and db01.manufacturer == "VMware" and db01.model == "vSphere"
        klaster = db.scalars(select(Asset).where(Asset.typ == "klaster")).one()
        assert klaster.zrodlo == "vmware" and klaster.hostname == "RCHO-VSAN-01"
        relacje = {(r.source.hostname, r.kind, r.target.hostname, r.created_by)
                   for r in db.scalars(select(AssetRelation))}
        assert (agent.hostname, "vm_host", "esx01.firma.pl", "vmware") in relacje
        assert ("db-01", "vm_host", "esx02.firma.pl", "vmware") in relacje
        assert ("esx01.firma.pl", "host_cluster", "RCHO-VSAN-01", "vmware") in relacje
        assert ustawienia_agenta(db, enrolled["asset_id"], "vmware").odczyt_liczby["vm_z_agentem"] == 1

    strona = client.get("/wirtualizacja").text
    assert "VMware vCenter" in strona and "RCHO-VSAN-01" in strona and "db-01" in strona
    karta = client.get(f"/assets/{db01.id}").text
    assert "odczytany z VMware vCenter" in karta and "Identyfikator w vCenter" in karta
    assert "vm-102" in karta
    mapa = client.get("/relacje/mapa.json").json()
    assert {"RCHO-VSAN-01", "esx01.firma.pl", "db-01"} <= {w["nazwa"] for w in mapa["wezly"]}


def test_nutanix_i_vmware_nie_wycofuja_sobie_obiektow(client, tenant_a, make_user):
    """Odczyt jednej platformy nie widzi obiektow drugiej - nie moze ich "zgubic"."""
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wlacz_nutanix(client, csrf, enrolled["asset_id"])
    rev_nx = polityka_nutanix(client, enrolled)["revision"]
    assert wyslij_nutanix(client, enrolled, odczyt_nutanix(rev_nx, vm=[])) == "przyjeto"
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    # Kolejny odczyt Nutanix (ten sam czytnik!) nie wycofuje maszyn z vCenter.
    assert wyslij_nutanix(client, enrolled, odczyt_nutanix(rev_nx, vm=[])) == "przyjeto"
    with SessionLocal() as db:
        db01 = db.scalars(select(Asset).where(Asset.hostname == "db-01")).one()
        assert db01.lifecycle == "aktywny"
        assert {o.dostawca for o in db.scalars(select(NutanixObiekt))} == {"nutanix", "vmware"}

    # A znikniecie z vCenter wycofuje - z wlasnym powodem.
    assert wyslij(client, enrolled, odczyt(rev, vm=[odczyt(rev)["vm"][0]])) == "przyjeto"
    with SessionLocal() as db:
        db01 = db.scalars(select(Asset).where(Asset.hostname == "db-01")).one()
        assert db01.lifecycle == "wycofany" and db01.retired_by == "vmware"
        assert db01.retired_reason == "zniknęła z vCenter"


def test_filtr_listy_po_funkcji_vmware(client, tenant_a, make_user):
    enrolled, _, _ = _wlaczony(client, tenant_a, make_user)
    lista = client.get("/assets?funkcja=vmware").text
    assert "bastionjps" in lista
    assert "bastionjps" not in client.get("/assets?funkcja=nutanix").text


def test_szczegoly_hosta_esxi_trafiaja_do_ewidencji(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev))
    with SessionLocal() as db:
        esx = db.scalars(select(Asset).where(Asset.hostname == "esx01.firma.pl")).one()
        assert (esx.manufacturer, esx.model, esx.serial_number) == ("Dell Inc.", "PowerEdge R760", "ABC1234")
        assert esx.primary_ip == "10.30.0.11" and esx.os_name == "VMware ESXi 8.0.3 build-24280767"
        # Host bez szczegolow (starszy vCenter) dostaje producenta platformy.
        assert db.scalars(select(Asset).where(Asset.hostname == "esx02.firma.pl")).one().manufacturer == "VMware"
    karta = client.get(f"/assets/{esx.id}").text
    assert "PowerEdge R760" in karta and "128 wątków" in karta and "tryb serwisowy" in karta
    assert "Adres zarządzania" in karta and "2026-08-01 10:00 UTC" in karta
    assert "s/n ABC1234" in client.get("/wirtualizacja").text
