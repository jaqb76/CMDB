from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (Asset, DiscoveryDevice, DiscoveryScanner, InventorySnapshot,
                                as_utc, utcnow)
from .test_review_improvements import enroll, login, send


def observation(**overrides):
    return {"scanned_at": utcnow().isoformat(), "ranges": ["10.0.0.0/24"], "complete": True,
            "attempted_hosts": 254, "total_hosts": 254, "errors": [], "devices": [
                {"ip": "10.0.0.2", "mac": "aa:bb:cc:dd:ee:ff", "hostname": "printer.lan", "ports": [80, 9100],
                 "device_type": "drukarka", "os_hint": "", "manufacturer": "HP", "confidence": "medium",
                 "evidence": ["HP LaserJet", "TCP 9100 otwarty"]}], **overrides}


def setup_discovery(client, tenant):
    enrolled, report = enroll(client, tenant)
    report["report_id"] = str(uuid4())
    report["network_discovery"] = observation()
    response = send(client, enrolled, report)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        device_id = db.execute(select(DiscoveryDevice.id).where(DiscoveryDevice.tenant_id == tenant["id"])).scalar_one()
    return enrolled, report, device_id


def test_discovery_roundtrip_replay_no_inventory_or_history_noise(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    assert send(client, enrolled, report).status_code == 200
    newer = deepcopy(report)
    newer["report_id"] = str(uuid4())
    newer["network_discovery"]["scanned_at"] = utcnow().isoformat()
    newer["network_discovery"]["devices"][0]["ports"] = [9100]
    assert send(client, enrolled, newer).json()["changed"] is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Asset.id))) == 1
        assert db.scalar(select(func.count(DiscoveryDevice.id))) == 1
        assert db.scalar(select(func.count(InventorySnapshot.id))) == 1
        assert db.get(DiscoveryDevice, device_id).observation["ports"] == [9100]
    _, csrf = login(client, tenant_a, make_user)
    assert "printer.lan" in client.get("/wykrywanie").text
    assert "HP LaserJet" in client.get(f"/wykrywanie/{device_id}").text
    payload = {"csrf_token": csrf, "hostname": "Drukarka ksiegowosc", "typ": "drukarka"}
    response = client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).headers["location"] == response.headers["location"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Asset.id))) == 2
        asset = db.get(Asset, db.get(DiscoveryDevice, device_id).asset_id)
        assert asset.zrodlo == "reczne" and asset.typ == "drukarka"
        assert asset.primary_ip == "10.0.0.2" and asset.os_family is None


def test_older_or_partial_scan_does_not_refresh_absent_devices(client, tenant_a):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    initial = report["network_discovery"]["scanned_at"]
    older = deepcopy(report)
    older["report_id"] = str(uuid4())
    older["network_discovery"]["scanned_at"] = (utcnow() - timedelta(days=1)).isoformat()
    older["network_discovery"]["devices"][0]["hostname"] = "stary"
    assert send(client, enrolled, older).status_code == 200
    partial = deepcopy(report)
    partial["report_id"] = str(uuid4())
    partial["network_discovery"] = observation(devices=[], complete=False, attempted_hosts=1, errors=["limit czasu"])
    assert send(client, enrolled, partial).status_code == 200
    with SessionLocal() as db:
        device = db.get(DiscoveryDevice, device_id)
        # Porownujemy chwile, a nie napisy: psycopg zwraca czas w strefie sesji,
        # wiec ten sam moment ma inny zapis w UTC i w Warszawie. Test przechodzil
        # wylacznie na maszynach ustawionych na UTC - czyli w CI, ale nie u nas.
        assert device.hostname == "printer.lan"
        assert as_utc(device.last_seen) == datetime.fromisoformat(initial)
        assert db.get(DiscoveryScanner, enrolled["asset_id"]).details["errors"] == ["limit czasu"]


def test_changed_mac_clears_previous_approval_even_after_missing_mac(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    _, csrf = login(client, tenant_a, make_user)
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={"csrf_token": csrf, "hostname": "Printer", "typ": "drukarka"}, follow_redirects=False).status_code == 303
    for mac in ("", "aa:bb:cc:dd:ee:00"):
        report["report_id"] = str(uuid4())
        report["network_discovery"]["scanned_at"] = utcnow().isoformat()
        report["network_discovery"]["devices"][0]["mac"] = mac
        assert send(client, enrolled, report).status_code == 200
    with SessionLocal() as db:
        assert db.get(DiscoveryDevice, device_id).asset_id is None
        assert db.scalar(select(func.count(Asset.id))) == 2


def test_isolation_csrf_and_link_without_overwriting_agent(client, tenant_a, tenant_b, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    foreign, _, foreign_id = setup_discovery(client, tenant_b)
    _, csrf = login(client, tenant_a, make_user)
    assert client.get(f"/wykrywanie/{foreign_id}").status_code == 404
    payload = {"csrf_token": csrf, "hostname": "should-not-overwrite", "typ": "drukarka", "asset_id": foreign["asset_id"]}
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).status_code == 404
    assert client.post(f"/wykrywanie/{foreign_id}/adopt", data=payload, follow_redirects=False).status_code == 404
    payload["asset_id"] = enrolled["asset_id"]
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={**payload, "csrf_token": ""}, follow_redirects=False).status_code == 403
    assert client.post(f"/wykrywanie/{device_id}/adopt", data=payload, follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        asset = db.get(Asset, enrolled["asset_id"])
        assert asset.hostname == report["identity"]["hostname"] and asset.zrodlo == "agent"


def test_viewer_can_read_but_cannot_adopt(client, tenant_a, make_user):
    _, _, device_id = setup_discovery(client, tenant_a)
    _, csrf = login(client, tenant_a, make_user, role="viewer")
    assert client.get("/wykrywanie").status_code == 200
    assert client.post(f"/wykrywanie/{device_id}/adopt", data={"csrf_token": csrf, "hostname": "Printer", "typ": "drukarka"}, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("bad", [
    {"devices": [{"ip": "8.8.8.8"}]}, {"devices": [{"ip": "10.1.1.1"}]},
    {"ranges": ["10.0.0.0/8"]}, {"devices": [{"ip": "10.0.0.2", "ports": [70000]}]},
    {"devices": [{"ip": "10.0.0.2", "hostname": "x" * 256}]},
    {"devices": [{"ip": "10.0.0.2"}, {"ip": "10.0.0.2"}]},
    {"devices": [{"ip": "10.0.0.2", "evidence": ["bad\x00banner"]}]},
    {"errors": ["bad\x00error"]},
    {"attempted_hosts": 0}, {"scanned_at": "2099-01-01T00:00:00Z"},
])
def test_invalid_discovery_rejected_without_partial_writes(client, tenant_a, bad):
    enrolled, report = enroll(client, tenant_a)
    report["network_discovery"] = observation(**bad)
    assert send(client, enrolled, report).status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DiscoveryDevice.id))) == 0


def test_banners_are_escaped_in_html(client, tenant_a, make_user):
    enrolled, report, device_id = setup_discovery(client, tenant_a)
    report["report_id"] = str(uuid4())
    report["network_discovery"]["scanned_at"] = utcnow().isoformat()
    report["network_discovery"]["devices"][0]["evidence"] = ['<script>alert(1)</script>']
    assert send(client, enrolled, report).status_code == 200
    login(client, tenant_a, make_user)
    html = client.get(f"/wykrywanie/{device_id}").text
    assert '<script>alert(1)</script>' not in html and '&lt;script&gt;' in html


# --- automatyczne wiazanie z ewidencja ---------------------------------------

def _maszyna_z_interfejsami(tenant_id, hostname, maki, adresy, lifecycle="aktywny"):
    """Maszyna, ktora zglosila o sobie podane adresy sprzetowe i sieciowe."""
    with SessionLocal() as db:
        asset = Asset(tenant_id=tenant_id, machine_id="maszyna-" + hostname, hostname=hostname,
                      zrodlo="agent", lifecycle=lifecycle,
                      facts={"mac_addresses": maki, "ip_addresses": adresy})
        db.add(asset)
        db.commit()
        return asset.id


def _skan(client, tenant, urzadzenia):
    enrolled, report = enroll(client, tenant)
    report["report_id"] = str(uuid4())
    report["network_discovery"] = observation(devices=urzadzenia)
    assert send(client, enrolled, report).status_code == 200
    return enrolled


def _urzadzenie(ip, mac="", hostname=""):
    return {"ip": ip, "mac": mac, "hostname": hostname, "ports": [22],
            "device_type": "komputer", "os_hint": "", "manufacturer": "",
            "confidence": "low", "evidence": ["TCP 22 otwarty"]}


def test_maszyna_z_dwoma_interfejsami_wiaze_sie_sama(client, tenant_a):
    """Dwa interfejsy to dwie obserwacje, ale jedna maszyna. Recznie trzeba
    bylo przeklejac UUID przy kazdym adresie osobno."""
    asset_id = _maszyna_z_interfejsami(
        tenant_a["id"], "raspberry",
        maki=["AA:AA:AA:00:00:01", "AA:AA:AA:00:00:02"],
        adresy=["10.0.0.11", "10.0.0.12"],
    )
    _skan(client, tenant_a, [_urzadzenie("10.0.0.11", "aa:aa:aa:00:00:01"),
                             _urzadzenie("10.0.0.12", "aa:aa:aa:00:00:02")])

    with SessionLocal() as db:
        wiersze = db.execute(select(DiscoveryDevice).where(
            DiscoveryDevice.tenant_id == tenant_a["id"])).scalars().all()
        assert len(wiersze) == 2
        assert {w.asset_id for w in wiersze} == {asset_id}, "oba interfejsy do jednej maszyny"
        assert {w.link_mode for w in wiersze} == {"mac"}
        assert all("MAC" in (w.link_reason or "") for w in wiersze)


def test_wielkosc_liter_w_mac_nie_ma_znaczenia(client, tenant_a):
    """Agent zglasza MAC wielkimi literami, skaner malymi."""
    asset_id = _maszyna_z_interfejsami(tenant_a["id"], "serwer",
                                       maki=["BB:BB:BB:00:00:01"], adresy=["10.0.0.20"])
    _skan(client, tenant_a, [_urzadzenie("10.0.0.20", "bb:bb:bb:00:00:01")])
    with SessionLocal() as db:
        assert db.execute(select(DiscoveryDevice.asset_id)).scalar_one() == asset_id


def test_ten_sam_mac_na_dwoch_maszynach_czeka_na_czlowieka(client, tenant_a):
    """Stacja dokujaca uzycza MAC-a kolejnym laptopom, wirtualny MAC VRRP stoi
    na dwoch routerach. Automat nie ma z czego wybrac."""
    _maszyna_z_interfejsami(tenant_a["id"], "laptop-1", maki=["CC:CC:CC:00:00:01"], adresy=["10.0.0.30"])
    _maszyna_z_interfejsami(tenant_a["id"], "laptop-2", maki=["CC:CC:CC:00:00:01"], adresy=["10.0.0.31"])
    _skan(client, tenant_a, [_urzadzenie("10.0.0.30", "cc:cc:cc:00:00:01")])
    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice)).scalar_one()
        assert wiersz.asset_id is None
        assert "2 maszyny" in wiersz.link_reason


def test_znany_mac_bez_dopasowania_blokuje_wiazanie_po_ip(client, tenant_a):
    """Widzimy sprzet, ktorego zadna maszyna o sobie nie zglosila. To nie jest
    brak informacji, tylko informacja przeciwna - adres IP mogl sie przeniesc."""
    _maszyna_z_interfejsami(tenant_a["id"], "stary-serwer",
                            maki=["DD:DD:DD:00:00:01"], adresy=["10.0.0.40"])
    _skan(client, tenant_a, [_urzadzenie("10.0.0.40", "ee:ee:ee:99:99:99")])
    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice)).scalar_one()
        assert wiersz.asset_id is None, "DHCP mogl przeniesc adres na inne urzadzenie"
        assert "nieznany w ewidencji" in wiersz.link_reason


def test_bez_mac_wiaze_po_adresie_zgloszonym_przez_maszyne(client, tenant_a):
    """Urzadzenie spoza segmentu skanera nie ma wpisu ARP, wiec MAC-a nie znamy.
    Adres z wlasnego raportu maszyny nadal jest dobrym dowodem."""
    asset_id = _maszyna_z_interfejsami(tenant_a["id"], "zdalny", maki=[], adresy=["10.0.0.50"])
    _skan(client, tenant_a, [_urzadzenie("10.0.0.50")])
    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice)).scalar_one()
        assert wiersz.asset_id == asset_id
        assert wiersz.link_mode == "ip"


def test_nie_wiaze_z_wycofanym_zasobem(client, tenant_a):
    """Wiazanie z czyms, co przestalo istniec, tworzy falszywy obraz."""
    _maszyna_z_interfejsami(tenant_a["id"], "zlomowany", maki=["FF:FF:FF:00:00:01"],
                            adresy=["10.0.0.60"], lifecycle="wycofany")
    _skan(client, tenant_a, [_urzadzenie("10.0.0.60", "ff:ff:ff:00:00:01")])
    with SessionLocal() as db:
        assert db.execute(select(DiscoveryDevice)).scalar_one().asset_id is None


def test_automat_nie_zmienia_decyzji_czlowieka(client, tenant_a, make_user):
    """Czlowiek przypisal urzadzenie do maszyny A; skan nie moze go przeniesc
    do B tylko dlatego, ze B ma pasujacy adres."""
    ludzka = _maszyna_z_interfejsami(tenant_a["id"], "wybrana-recznie", maki=[], adresy=[])
    _maszyna_z_interfejsami(tenant_a["id"], "pasujaca", maki=["11:11:11:00:00:01"], adresy=["10.0.0.70"])
    enrolled = _skan(client, tenant_a, [_urzadzenie("10.0.0.70", "11:11:11:00:00:01")])

    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice)).scalar_one()
        wiersz.asset_id = ludzka
        wiersz.link_mode = "reczne"
        db.commit()

    report = enroll(client, tenant_a)[1]
    report["report_id"] = str(uuid4())
    report["network_discovery"] = observation(devices=[_urzadzenie("10.0.0.70", "11:11:11:00:00:01")],
                                              scanned_at=utcnow().isoformat())
    send(client, enrolled, report)
    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice).where(DiscoveryDevice.asset_id == ludzka)).scalar_one()
        assert wiersz.link_mode == "reczne"


def test_dopasowanie_zostaje_w_granicach_firmy(client, tenant_a, tenant_b):
    """Ten sam adres w dwoch firmach to dwa rozne urzadzenia."""
    obcy = _maszyna_z_interfejsami(tenant_b["id"], "obca", maki=["22:22:22:00:00:01"], adresy=["10.0.0.80"])
    _skan(client, tenant_a, [_urzadzenie("10.0.0.80", "22:22:22:00:00:01")])
    with SessionLocal() as db:
        wiersz = db.execute(select(DiscoveryDevice).where(
            DiscoveryDevice.tenant_id == tenant_a["id"])).scalar_one()
        assert wiersz.asset_id is None, "nie wolno siegnac do zasobu innej firmy"
        assert wiersz.asset_id != obcy
