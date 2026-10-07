"""OpenStack: ta sama droga co Nutanix i vCenter, plus domena/projekt i host z agentem."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetRelation, AuditLog, NutanixObiekt
from cmdb_server.services import nutanix
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, polityka as polityka_nutanix, ustawienia_agenta, zarejestruj
from .test_review_improvements import enroll, send
from .test_vmware import odczyt as odczyt_vmware, polityka as polityka_vmware, wlacz as wlacz_vmware
from .test_vmware import wyslij as wyslij_vmware

VM_Z_AGENTEM = "5031aaaa-0000-4000-8000-000000000001"
HIP_A = "a1b2c3d4-0000-4000-8000-00000000000a"
HIP_B = "a1b2c3d4-0000-4000-8000-00000000000b"


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, **{"adres": "keystone.cloud.firma.pl", "nazwa": "Openstack G1 DC1",
                              "uzytkownik": "0f3c8e6b2a5d4c1e9b7a6f5e4d3c2b1a", **extra})
    odp = client.post(f"/assets/{asset_id}/openstack", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="d" * 32):
    odp = client.get("/api/v1/agent/openstack-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, vm=None, hosty=None):
    return {
        "protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 1200,
        "wersja_pc": "Nova API 2.96",
        "klastry": [{"ext_id": "RegionOne", "nazwa": "RegionOne", "wersja": "Nova API 2.96",
                     "hipernadzorca": "QEMU", "liczba_hostow": 2}],
        "hosty": hosty if hosty is not None else [
            {"ext_id": HIP_A, "nazwa": "cmp01.cloud.firma.pl", "klaster_id": "RegionOne",
             "cpu_model": "Cascadelake-Server", "gniazda": 2, "rdzenie": 64, "watki": 128,
             "ram_bajty": 512 * 2**30, "hipernadzorca": "QEMU 6.2.0", "ip": "10.50.0.11",
             "tryb_serwisowy": False, "strefa": "az1"},
            {"ext_id": HIP_B, "nazwa": "cmp02.cloud.firma.pl", "klaster_id": "RegionOne",
             "hipernadzorca": "QEMU 6.2.0", "ip": "10.50.0.12", "strefa": "az2"},
        ],
        "vm": vm if vm is not None else [
            {"ext_id": VM_Z_AGENTEM, "nazwa": "bastionjps", "klaster_id": "RegionOne", "host_id": HIP_A,
             "stan": "ON", "bios_uuid": VM_Z_AGENTEM, "gniazda": 4, "rdzenie_na_gniazdo": 1},
            {"ext_id": "5031aaaa-0000-4000-8000-000000000002", "nazwa": "web-01", "klaster_id": "RegionOne",
             "host_id": HIP_B, "stan": "OFF", "projekt": "sklep", "strefa": "az2", "typ": "m1.large",
             "dyski": [{"rozmiar_bajty": 200 * 2**30, "kontener": "ceph-ssd"}],
             "karty": [{"mac": "fa:16:3e:00:00:02", "ip": ["192.168.10.2"], "siec": "web-net"}]},
        ],
    }


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/openstack", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user, **extra):
    enrolled = zarejestruj(client, tenant, uuid=VM_Z_AGENTEM)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"], **extra)
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def test_adres_keystone_moze_miec_sciezke():
    assert nutanix.normalizuj_adres("keystone.firma.pl", 5000, True) == "https://keystone.firma.pl:5000"
    assert nutanix.normalizuj_adres("https://chmura.firma.pl/identity/", 5000, True) == \
        "https://chmura.firma.pl/identity"
    assert nutanix.normalizuj_adres("https://chmura.firma.pl:5000/v3", 5000, True) == \
        "https://chmura.firma.pl:5000/v3"
    for zly in ("https://chmura.firma.pl/identity?x=1", "https://u@chmura.firma.pl", "http://chmura"):
        try:
            nutanix.normalizuj_adres(zly, 5000, True)
        except ValueError:
            continue
        raise AssertionError(zly)
    # Inni dostawcy dalej bez sciezki.
    try:
        nutanix.normalizuj_adres("https://prism.firma.pl/api", 9440)
    except ValueError:
        pass
    else:
        raise AssertionError("sciezka przy Prism")


def test_konfiguracja_openstack_z_domena_i_projektem(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = wlacz(client, csrf, enrolled["asset_id"], uzytkownik="cmdb-ro", domena="Firma", projekt="admin",
                adres="https://chmura.firma.pl/identity")
    assert odp.headers["location"].endswith("?funkcja=openstack#agent")
    p = polityka(client, enrolled)["policy"]
    assert p["enabled"] is True and p["adres"] == "https://chmura.firma.pl/identity"
    assert (p["uzytkownik"], p["domena"], p["projekt"], p["haslo"]) == ("cmdb-ro", "Firma", "admin", "tajne-haslo-1")
    # Inne platformy na tej samej maszynie nie dostaja pol OpenStacka i zostaja wylaczone.
    assert polityka_nutanix(client, enrolled)["policy"] == {"enabled": False, "test": False}
    with SessionLocal() as db:
        wpis = db.scalars(select(AuditLog).where(AuditLog.action == "openstack.config_changed")).one()
        assert wpis.detail["after"]["projekt"] == "admin"
        assert "tajne-haslo-1" not in str(wpis.detail)

    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=openstack").text
    assert "Odczytuj OpenStack z tej maszyny" in strona and "ID application credential" in strona
    assert 'name="projekt"' in strona and 'value="admin"' in strona
    # Formularz Prism nie ma pol domeny i projektu.
    assert 'name="projekt"' not in client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix").text

    blad = client.post(f"/assets/{enrolled['asset_id']}/openstack",
                       data=formularz(csrf, revision=polityka(client, enrolled)["revision"],
                                      adres="https://chmura.firma.pl/x?y=1"), follow_redirects=False)
    assert blad.headers["location"].endswith("?funkcja=openstack&nutanix_komunikat=adres#agent")
    assert "Niepoprawny adres OpenStack" in client.get(blad.headers["location"]).text


def test_application_credential_bez_projektu(client, tenant_a, make_user):
    enrolled, _, _ = _wlaczony(client, tenant_a, make_user)
    p = polityka(client, enrolled)["policy"]
    # Bez dopisanego portu: 5000 albo 443 (RHOSO, load balancer) zalezy od wdrozenia.
    assert p["adres"] == "https://keystone.cloud.firma.pl"
    assert (p["domena"], p["projekt"]) == ("", "")


def test_adres_keystone_bez_portu_i_z_portem(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    wlacz(client, csrf, enrolled["asset_id"], adres="keystone-public-openstack.apps.rhoso-pod1.firma.pl")
    wlacz(client, csrf, enrolled["asset_id"], adres="keystone.dc1.firma.pl:5000")
    adresy = sorted(p["adres"] for p in polityka(client, enrolled)["polaczenia"])
    assert adresy == ["https://keystone-public-openstack.apps.rhoso-pod1.firma.pl", "https://keystone.dc1.firma.pl:5000"]
    # Adres Compute bez portu (RHOSO) tez zostaje bez portu.
    rhoso = next(p for p in polityka(client, enrolled)["polaczenia"] if "rhoso" in p["adres"])
    wlacz(client, csrf, enrolled["asset_id"], polaczenie_id=rhoso["id"], revision=rhoso["revision"],
          adres="keystone-public-openstack.apps.rhoso-pod1.firma.pl",
          adres_compute="https://nova-public-openstack.apps.rhoso-pod1.firma.pl",
          adres_volumes="cinder.dc1.firma.pl:8776/v3")
    rhoso = next(p for p in polityka(client, enrolled, nonce="f" * 32)["polaczenia"] if "rhoso" in p["adres"])
    assert rhoso["adres_compute"] == "https://nova-public-openstack.apps.rhoso-pod1.firma.pl"
    assert rhoso["adres_volumes"] == "https://cinder.dc1.firma.pl:8776/v3"


def test_odczyt_openstack_zaklada_drzewo_i_laczy_vm_z_agentem(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        obiekty = db.scalars(select(NutanixObiekt)).all()
        assert {o.dostawca for o in obiekty} == {"openstack"}
        assert all("/" in o.ext_id for o in obiekty)  # przestrzen adresu Keystone
        agent = db.get(Asset, enrolled["asset_id"])
        web = db.scalars(select(Asset).where(Asset.hostname == "web-01")).one()
        assert web.zrodlo == "openstack" and web.manufacturer == "OpenStack" and web.model == "Nova"
        region = db.scalars(select(Asset).where(Asset.typ == "klaster")).one()
        # Jeden region w chmurze - klaster nazywa sie jak polaczenie, nie "RegionOne".
        assert region.zrodlo == "openstack" and region.hostname == "Openstack G1 DC1"
        relacje = {(r.source.hostname, r.kind, r.target.hostname, r.created_by)
                   for r in db.scalars(select(AssetRelation))}
        assert (agent.hostname, "vm_host", "cmp01.cloud.firma.pl", "openstack") in relacje
        assert ("web-01", "vm_host", "cmp02.cloud.firma.pl", "openstack") in relacje
        assert ("cmp01.cloud.firma.pl", "host_cluster", "Openstack G1 DC1", "openstack") in relacje
        assert ustawienia_agenta(db, enrolled["asset_id"], "openstack").odczyt_liczby["vm_z_agentem"] == 1

    strona = client.get("/wirtualizacja").text
    assert "OpenStack" in strona and "Openstack G1 DC1" in strona and "projekt sklep" in strona
    assert "źródło: Openstack G1 DC1" in strona
    assert "strefa az2" in strona
    karta = client.get(f"/assets/{web.id}").text
    assert "odczytany z OpenStack" in karta and "Identyfikator w OpenStack" in karta
    assert "m1.large" in karta and "Strefa dostępności" in karta and "ceph-ssd" in karta
    assert "Nutanix Guest Tools" not in karta
    assert "z OpenStack" in client.get("/assets").text
    assert "bastionjps" in client.get("/assets?funkcja=openstack").text


def test_hypervisor_z_agentem_nie_dostaje_drugiej_karty(client, tenant_a, make_user):
    """Compute z agentem CMDB: host z odczytu dopisuje sie do jego karty."""
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    compute, report = enroll(client, tenant_a, machine="compute-cmp01")
    report["identity"]["hostname"] = "cmp01"
    report["identity"]["fqdn"] = "cmp01.cloud.firma.pl"
    assert send(client, compute, report).status_code == 200

    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        karta = db.get(Asset, compute["asset_id"])
        assert karta.zrodlo == "agent"
        assert not db.scalars(select(Asset).where(Asset.machine_id == f"openstack:host:{karta.id}")).all()
        relacje = {(r.source_id, r.kind, r.target.hostname) for r in db.scalars(select(AssetRelation))}
        assert (karta.id, "host_cluster", "Openstack G1 DC1") in relacje
        # VM na tym hypervisorze wskazuje karte agenta compute.
        assert (enrolled["asset_id"], "vm_host", karta.hostname) in relacje
        # Drugi hypervisor (bez agenta) dostal zwykly wpis z odczytu.
        assert db.scalars(select(Asset).where(Asset.hostname == "cmp02.cloud.firma.pl")).one().zrodlo == "openstack"
    assert "połączono z raportem agenta po nazwie hosta" in client.get(f"/assets/{compute['asset_id']}").text

    # Hypervisor znika z Novy: karta agenta zostaje aktywna, znika tylko relacja.
    assert wyslij(client, enrolled, odczyt(rev, hosty=odczyt(rev)["hosty"][1:])) == "przyjeto"
    with SessionLocal() as db:
        karta = db.get(Asset, compute["asset_id"])
        assert karta.lifecycle == "aktywny"
        assert not db.scalars(select(AssetRelation).where(AssetRelation.source_id == karta.id,
                                                          AssetRelation.kind == "host_cluster")).all()


def test_dwie_maszyny_o_tej_samej_nazwie_nie_lacza_hosta(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    for maszyna in ("compute-cmp01-a", "compute-cmp01-b"):
        e, report = enroll(client, tenant_a, machine=maszyna)
        report["identity"]["hostname"] = "cmp01"
        assert send(client, e, report).status_code == 200
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        host = db.scalars(select(Asset).where(Asset.hostname == "cmp01.cloud.firma.pl")).one()
        assert host.zrodlo == "openstack"


def test_region_one_w_dwoch_chmurach_to_dwa_klastry(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev))
    wlacz(client, csrf, enrolled["asset_id"], adres="keystone.druga.firma.pl", nazwa="Openstack G2 DC1")
    druga = next(p for p in polityka(client, enrolled)["polaczenia"] if "druga" in p["adres"])
    dane = odczyt(druga["revision"], hosty=[], vm=[])
    dane["polaczenie_id"] = druga["id"]
    assert wyslij(client, enrolled, dane) == "przyjeto"
    with SessionLocal() as db:
        regiony = db.scalars(select(Asset).where(Asset.typ == "klaster", Asset.lifecycle == "aktywny")).all()
        assert sorted(r.hostname for r in regiony) == ["Openstack G1 DC1", "Openstack G2 DC1"]


def test_kilka_regionow_w_jednej_chmurze_ma_region_w_nazwie(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    dane = odczyt(rev, hosty=[], vm=[])
    dane["klastry"].append({"ext_id": "RegionTwo", "nazwa": "RegionTwo"})
    assert wyslij(client, enrolled, dane) == "przyjeto"
    with SessionLocal() as db:
        regiony = db.scalars(select(Asset).where(Asset.typ == "klaster")).all()
        assert sorted(r.hostname for r in regiony) == ["Openstack G1 DC1 (RegionOne)", "Openstack G1 DC1 (RegionTwo)"]


def test_openstack_i_vmware_nie_wycofuja_sobie_obiektow(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wlacz_vmware(client, csrf, enrolled["asset_id"])
    rev_vc = polityka_vmware(client, enrolled)["revision"]
    assert wyslij_vmware(client, enrolled, odczyt_vmware(rev_vc, vm=[])) == "przyjeto"
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    assert wyslij_vmware(client, enrolled, odczyt_vmware(rev_vc, vm=[])) == "przyjeto"
    with SessionLocal() as db:
        assert db.scalars(select(Asset).where(Asset.hostname == "web-01")).one().lifecycle == "aktywny"

    assert wyslij(client, enrolled, odczyt(rev, vm=odczyt(rev)["vm"][:1])) == "przyjeto"
    with SessionLocal() as db:
        web = db.scalars(select(Asset).where(Asset.hostname == "web-01")).one()
        assert web.lifecycle == "wycofany" and web.retired_by == "openstack"
        assert web.retired_reason == "zniknęła z OpenStack"


def test_http_i_reczne_adresy_jak_w_dc1(client, tenant_a, make_user):
    """Konfiguracja jak w DC1: Keystone po http pod IP, adresy uslug podane recznie."""
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    dc1 = {"adres": "http://172.17.30.199:5000/v3", "uzytkownik": "admin", "projekt": "admin",
           "adres_compute": "http://172.17.30.199:8774/v2.1", "adres_volumes": "http://172.17.30.199:8776/v3"}
    # Bez zgody http jest odrzucone - i nic nie zostaje zapisane.
    odp = wlacz(client, csrf, enrolled["asset_id"], **dc1)
    assert odp.headers["location"].endswith("nutanix_komunikat=tls#agent")
    assert "Zezwól na połączenie bez TLS" in client.get(odp.headers["location"]).text
    assert polityka(client, enrolled)["polaczenia"] == []

    wlacz(client, csrf, enrolled["asset_id"], bez_tls="true", **dc1)
    p = polityka(client, enrolled)["policy"]
    assert p["adres"] == "http://172.17.30.199:5000/v3" and p["bez_tls"] is True
    assert p["adres_compute"] == "http://172.17.30.199:8774/v2.1"
    assert p["adres_volumes"] == "http://172.17.30.199:8776/v3"
    assert (p["domena"], p["projekt"]) == ("", "admin")
    with SessionLocal() as db:
        wpis = db.scalars(select(AuditLog).where(AuditLog.action == "openstack.config_changed")).one()
        assert wpis.detail["after"]["bez_tls"] is True
    assert "bez TLS" in client.get(f"/assets/{enrolled['asset_id']}?funkcja=openstack").text
    assert "bez TLS" in client.get("/wirtualizacja").text


def test_http_nie_dla_prism(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = client.post(f"/assets/{enrolled['asset_id']}/nutanix",
                      data=formularz(csrf, adres="http://prism.firma.pl", bez_tls="true"), follow_redirects=False)
    assert odp.headers["location"].endswith("nutanix_komunikat=adres#agent")
    p = polityka_nutanix(client, enrolled)
    assert p["polaczenia"] == [] and "bez_tls" not in p["policy"]


def test_wirtualizacja_zwinieta_i_filtrowana_po_zrodle(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    wlacz_vmware(client, csrf, enrolled["asset_id"])
    rev_vc = polityka_vmware(client, enrolled)["revision"]
    assert wyslij_vmware(client, enrolled, odczyt_vmware(rev_vc)) == "przyjeto"
    with SessionLocal() as db:
        os_id = ustawienia_agenta(db, enrolled["asset_id"], "openstack").id
        vc_id = ustawienia_agenta(db, enrolled["asset_id"], "vmware").id

    strona = client.get("/wirtualizacja").text
    assert "web-01" in strona and "db-01" in strona
    # Domyslnie wszystko zwiniete: klastry i hosty bez atrybutu open.
    assert 'class="panel wirt-klaster"' in strona and '<details class="wirt-host" data-szukaj=' in strona
    # Wyszukiwarka: VM po IP, MAC, projekcie; host po adresie i strefie (male litery).
    assert "data-wirt-szukaj" in strona
    assert 'data-szukaj="web-01' in strona and "192.168.10.2" in strona and "fa:16:3e:00:00:02" in strona
    assert "10.50.0.11" in strona.split('data-szukaj="cmp01.cloud.firma.pl', 1)[1].split('"', 1)[0]
    assert 'value="esx"' in client.get("/wirtualizacja?q=esx").text
    assert "<details class=\"panel wirt-klaster wirt-zrodla-panel\">" in strona  # zrodla tez zwiniete
    assert " open" not in strona.split("wirt-klaster", 1)[1]
    assert f'data-href="/wirtualizacja?zrodlo={os_id}"' in strona

    tylko_os = client.get(f"/wirtualizacja?zrodlo={os_id}").text
    assert "web-01" in tylko_os and "Openstack G1 DC1" in tylko_os
    assert "db-01" not in tylko_os and "RCHO-VSAN-01" not in tylko_os
    assert "wirt-zrodlo-wybrane" in tylko_os and "pokaż wszystkie źródła" in tylko_os
    tylko_vc = client.get(f"/wirtualizacja?zrodlo={vc_id}").text
    assert "db-01" in tylko_vc and "web-01" not in tylko_vc
    # Nieznane zrodlo (np. innej firmy) = bez filtra.
    assert "web-01" in client.get("/wirtualizacja?zrodlo=00000000-0000-0000-0000-000000000000").text
