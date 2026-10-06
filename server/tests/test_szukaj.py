"""Globalna wyszukiwarka w gornym pasku: zasoby, adresy z raportu i wirtualizacji, siec, firmy."""
from datetime import timedelta

from cmdb_server.db import SessionLocal
from cmdb_server.models import DiscoveryDevice, utcnow
from .test_monitoring import zaloguj
from .test_openstack import _wlaczony, odczyt, wyslij
from .test_review_improvements import enroll, send


def _maszyna(client, tenant, machine, hostname, ip, mac, fqdn=None, dodatkowy_ip=None):
    enrolled, report = enroll(client, tenant, machine=machine)
    report["identity"]["hostname"] = hostname
    if fqdn:
        report["identity"]["fqdn"] = fqdn
    karta = report["network"]["interfaces"][0]
    karta["mac_address"], karta["ip_addresses"] = mac, [ip] + ([dodatkowy_ip] if dodatkowy_ip else [])
    karta["gateways"], karta["dns_servers"] = ["10.99.0.1"], ["10.99.0.53"]
    assert send(client, enrolled, report).status_code == 200
    return enrolled


def _szukaj(client, q):
    odp = client.get("/szukaj.json", params={"q": q})
    assert odp.status_code == 200 and odp.headers["cache-control"] == "no-store"
    return [w["nazwa"] for w in odp.json()["wyniki"]]


def test_szuka_po_nazwie_dns_ip_i_mac_z_raportu(client, tenant_a, make_user):
    _maszyna(client, tenant_a, "szukaj-maszyna-01", "SRV-SQL01", "10.20.0.15", "00:50:56:AB:CD:01",
             fqdn="srv-sql01.firma.local", dodatkowy_ip="192.168.77.15")
    _maszyna(client, tenant_a, "szukaj-maszyna-02", "PC-KSIEG", "10.20.0.40", "00:50:56:AB:CD:02")
    zaloguj(client, tenant_a, make_user)

    assert _szukaj(client, "sql01") == ["SRV-SQL01"]
    assert _szukaj(client, "srv-sql01.firma") == ["SRV-SQL01"]  # DNS
    assert _szukaj(client, "192.168.77.15") == ["SRV-SQL01"]  # drugi adres karty, nie glowny
    assert _szukaj(client, "00-50-56-ab-cd-02") == ["PC-KSIEG"]  # MAC z myslnikami jak w Windows
    assert set(_szukaj(client, "10.20.0.")) == {"SRV-SQL01", "PC-KSIEG"}
    # Brama i serwer DNS sa wspolne - nie moga trafiac w kazda maszyne.
    assert _szukaj(client, "10.99.0.1") == [] and _szukaj(client, "10.99.0.53") == []
    assert _szukaj(client, "x") == []  # za krotkie

    strona = client.get("/szukaj", params={"q": "sql01"}).text
    assert "SRV-SQL01" in strona and "srv-sql01.firma.local" in strona
    assert '<td class="small muted">nazwa</td>' in strona  # kolumna "Pasuje" mowi, ktore pole trafilo
    assert '<td class="small muted">adres lub dane z raportu / wirtualizacji</td>' in \
        client.get("/szukaj", params={"q": "192.168.77.15"}).text
    assert "PC-KSIEG" not in strona


def test_szuka_w_wirtualizacji_i_w_sieci(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        teraz = utcnow()
        db.add(DiscoveryDevice(tenant_id=tenant_a["id"], scanner_id=enrolled["asset_id"], ip="10.30.0.250",
                               mac="aa:bb:cc:00:00:01", hostname="drukarka-hp", first_seen=teraz - timedelta(days=1),
                               last_seen=teraz, observation={}))
        db.commit()
    # IP karty VM z OpenStacka i adres zarzadzania hypervisora.
    assert _szukaj(client, "192.168.10.2") == ["web-01"]
    assert _szukaj(client, "10.50.0.11") == ["cmp01.cloud.firma.pl"]
    assert _szukaj(client, "drukarka") == ["drukarka-hp"]
    strona = client.get("/szukaj", params={"q": "10.30.0.250"}).text
    assert "Wykryte w sieci" in strona and "aa:bb:cc:00:00:01" in strona


def test_inna_firma_i_helpdesk_bez_dostepu(client, tenant_a, tenant_b, make_user):
    _maszyna(client, tenant_b, "szukaj-obca-01", "OBCY-SERWER", "10.20.0.77", "00:50:56:AB:CD:77")
    zaloguj(client, tenant_a, make_user)
    assert _szukaj(client, "OBCY") == [] and _szukaj(client, "10.20.0.77") == []
    strona = client.get("/szukaj", params={"q": "OBCY"}).text
    assert "Nic nie znaleziono" in strona and "Zgłoszenia helpdesku" not in strona


def test_pole_w_gornym_pasku(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    for adres in ("/", "/assets", "/wirtualizacja"):
        assert "data-szukaj-globalne" in client.get(adres).text, adres
    client.cookies.set("cmdb_layout", "classic")
    assert "data-szukaj-globalne" in client.get("/").text
