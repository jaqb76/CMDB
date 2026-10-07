"""Odczyt OpenStack po stronie agenta."""
from __future__ import annotations

import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from cmdb_agent import openstack
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg

APP_ID = "0f3c8e6b2a5d4c1e9b7a6f5e4d3c2b1a"
HIP_A = "a1b2c3d4-0000-4000-8000-00000000000a"
HIP_B = "a1b2c3d4-0000-4000-8000-00000000000b"
VM_Z_AGENTEM = "4231a8f2-1c2d-3e4f-5a6b-7c8d9e0f1a2b"

# Ksztalt odpowiedzi jak w Nova API 2.53.
HYPERVISORY = [
    {"id": HIP_A, "hypervisor_hostname": "cmp01.cloud.firma.pl", "hypervisor_type": "QEMU",
     "hypervisor_version": 6002000, "host_ip": "10.50.0.11", "state": "up", "status": "enabled",
     "vcpus": 128, "memory_mb": 515874, "running_vms": 2,
     "cpu_info": {"model": "Cascadelake-Server", "vendor": "Intel",
                  "topology": {"sockets": 2, "cores": 32, "threads": 2}},
     "service": {"host": "cmp01", "id": "s-1", "disabled_reason": None}},
    {"id": HIP_B, "hypervisor_hostname": "cmp02.cloud.firma.pl", "hypervisor_type": "QEMU",
     "hypervisor_version": 6002000, "host_ip": "10.50.0.12", "state": "up", "status": "disabled",
     "vcpus": 64, "memory_mb": 257937, "cpu_info": {}, "service": {"host": "cmp02"}},
]


def serwer_nova(numer: int, **zmiany) -> dict:
    s = {
        "id": f"5031aaaa-0000-4000-8000-{numer:012d}", "name": f"vm-{numer:04d}", "status": "ACTIVE",
        "tenant_id": "p-web", "OS-EXT-AZ:availability_zone": "az1",
        "OS-EXT-SRV-ATTR:host": "cmp01", "OS-EXT-SRV-ATTR:hypervisor_hostname": "cmp01.cloud.firma.pl",
        "flavor": {"vcpus": 4, "ram": 8192, "disk": 40, "original_name": "m1.large"},
        "addresses": {"web-net": [
            {"addr": f"192.168.10.{numer % 250}", "version": 4, "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
             "OS-EXT-IPS:type": "fixed"},
            {"addr": "203.0.113.10", "version": 4, "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
             "OS-EXT-IPS:type": "floating"}]},
        "os-extended-volumes:volumes_attached": [],
        "description": None,
    }
    s.update(zmiany)
    return s


SERWERY = [serwer_nova(i) for i in range(1, 1201)]  # dwie strony po 1000
SERWERY[0] = serwer_nova(1, id=VM_Z_AGENTEM, name="bastionjps", description="skok",
                         **{"os-extended-volumes:volumes_attached": [{"id": "vol-1"}]})
SERWERY[1] = serwer_nova(2, status="SHUTOFF", **{"OS-EXT-SRV-ATTR:host": "cmp02",
                                                  "OS-EXT-SRV-ATTR:hypervisor_hostname": None})
WOLUMENY = [{"id": "vol-1", "name": "bastion-dane", "size": 200, "volume_type": "ceph-ssd"}]
STREFY = {"availabilityZoneInfo": [
    {"zoneName": "az1", "hosts": {"cmp01": {"nova-compute": {}}, "ctl01": {"nova-scheduler": {}}}},
    {"zoneName": "az2", "hosts": {"cmp02": {"nova-compute": {}}}},
    {"zoneName": "internal", "hosts": {"ctl01": {"nova-conductor": {}}}},
]}


@contextmanager
def falszywy_openstack(tmp_path, haslo="tajne", nova="2.96", admin=True, http_w_katalogu=False,
                       tls=True, katalog_controller=False, cinder_padniety=False):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania, logowania, tokeny = [], [], set()
    stan = {"adres": ""}

    def katalog():
        baza = stan["adres"].replace("https://", "http://") if http_w_katalogu else stan["adres"]
        if katalog_controller:  # nazwa, ktorej agent nie rozwiaze
            baza = "http://controller.invalid"
        return [
            {"type": "identity", "name": "keystone", "endpoints": [
                {"interface": "public", "region_id": "RegionOne", "url": baza + "/v3"}]},
            {"type": "compute", "name": "nova", "endpoints": [
                {"interface": "internal", "region_id": "RegionOne", "url": "https://wewnetrzny.invalid/v2.1"},
                {"interface": "public", "region_id": "RegionOne", "url": baza + "/compute/v2.1"}]},
            {"type": "volumev3", "name": "cinderv3", "endpoints": [
                {"interface": "public", "region_id": "RegionOne", "url": baza + "/volume/v3/p-admin"}]},
        ]

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _odpowiedz(self, kod, dane=None, naglowki=None):
            tresc = json.dumps(dane).encode() if dane is not None else b""
            self.send_response(kod)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(tresc)))
            for k, v in (naglowki or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(tresc)

        def do_POST(self):
            zapytania.append(("POST", self.path))
            if self.path != "/v3/auth/tokens":
                return self._odpowiedz(404, {})
            dane = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            logowania.append(dane)
            tozsamosc = dane["auth"]["identity"]
            if tozsamosc["methods"] == ["application_credential"]:
                ok = tozsamosc["application_credential"] == {"id": APP_ID, "secret": haslo}
            else:
                ok = (tozsamosc["password"]["user"]["name"] == "cmdb-ro"
                      and tozsamosc["password"]["user"]["password"] == haslo)
            if not ok:
                return self._odpowiedz(401, {"error": {"code": 401, "message": "The request you have made requires authentication."}})
            tokeny.add("tok-1")
            self._odpowiedz(201, {"token": {"catalog": katalog(), "project": {"id": "p-admin"}}},
                            {"X-Subject-Token": "tok-1"})

        def do_DELETE(self):
            zapytania.append(("DELETE", self.path))
            tokeny.discard(self.headers.get("X-Subject-Token"))
            self._odpowiedz(204)

        def do_GET(self):
            adres = urlsplit(self.path)
            sciezka, q = adres.path, parse_qs(adres.query)
            zapytania.append(("GET", self.path))
            if self.headers.get("X-Auth-Token") not in tokeny:
                return self._odpowiedz(401, {"error": {"message": "brak tokenu"}})
            if sciezka == "/compute/v2.1":
                return self._odpowiedz(200, {"version": {"id": "v2.1", "version": nova, "min_version": "2.1"}})
            if sciezka == "/compute/v2.1/os-hypervisors/detail":
                if not admin:
                    return self._odpowiedz(403, {"forbidden": {"code": 403, "message": "Policy doesn't allow os_compute_api:os-hypervisors:list to be performed."}})
                assert self.headers.get("OpenStack-API-Version") == "compute 2.53"
                return self._odpowiedz(200, {"hypervisors": HYPERVISORY})
            if sciezka == "/compute/v2.1/os-availability-zone/detail":
                return self._odpowiedz(200, STREFY)
            if sciezka == "/compute/v2.1/servers/detail":
                assert q["all_tenants"] == ["1"]
                limit = int(q["limit"][0])
                start = 0
                if "marker" in q:
                    start = next(i for i, s in enumerate(SERWERY) if s["id"] == q["marker"][0]) + 1
                return self._odpowiedz(200, {"servers": SERWERY[start:start + limit]})
            if sciezka == "/volume/v3/p-admin/volumes/detail":
                if "OpenStack-API-Version" in self.headers:  # jak prawdziwy Cinder: 400 na "compute ..."
                    return self._odpowiedz(400, {"badRequest": {"message": "malformed"}})
                if cinder_padniety:
                    return self._odpowiedz(500, {"computeFault": {"message": "boom"}})
                return self._odpowiedz(200, {"volumes": WOLUMENY})
            if sciezka == "/volume/v3/p-admin/scheduler-stats/get_pools":
                return self._odpowiedz(200, {"pools": [{"name": "cinder@rbd#rbd", "capabilities": {
                    "volume_backend_name": "rbd", "storage_protocol": "ceph",
                    "location_info": "ceph:/etc/ceph/ceph.conf:6f1c2a3e-5b7d-11ef-9c1a-0242ac120002:cinder:volumes"}}]})
            if sciezka == "/v3/projects":
                return self._odpowiedz(403, {"error": {"message": "nie wolno"}})
            self._odpowiedz(404, {})

    serwer = ThreadingHTTPServer(("127.0.0.1", 0), Obsluga)
    kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontekst.load_cert_chain(tmp_path / "c.pem", tmp_path / "c.key")
    if tls:
        serwer.socket = kontekst.wrap_socket(serwer.socket, server_side=True)
    watek = threading.Thread(target=serwer.serve_forever, daemon=True)
    watek.start()
    stan["adres"] = f"{'https' if tls else 'http'}://localhost:{serwer.server_address[1]}"
    try:
        yield stan["adres"], pem, zapytania, logowania, tokeny
    finally:
        serwer.shutdown()
        serwer.server_close()


def test_pelny_odczyt_z_application_credential(tmp_path):
    with falszywy_openstack(tmp_path) as (adres, pem, zapytania, logowania, tokeny):
        wynik = openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne", pem), "odczyt")
    assert logowania[0]["auth"]["identity"]["methods"] == ["application_credential"]
    assert "scope" not in logowania[0]["auth"]  # zakres jest w samym poswiadczeniu
    assert wynik["ok"] and wynik["wersja_pc"] == "Nova API 2.96"
    assert wynik["klastry"] == [{"ext_id": "RegionOne", "nazwa": "RegionOne", "wersja": "Nova API 2.96",
                                 "hipernadzorca": "QEMU", "liczba_hostow": 2,
                                 "magazyny": [{"fsid": "6f1c2a3e-5b7d-11ef-9c1a-0242ac120002",
                                               "pula": "volumes", "backend": "rbd"}]}]
    hosty = {h["ext_id"]: h for h in wynik["hosty"]}
    a = hosty[HIP_A]
    assert a["nazwa"] == "cmp01.cloud.firma.pl" and a["klaster_id"] == "RegionOne"
    assert (a["cpu_model"], a["gniazda"], a["rdzenie"], a["watki"]) == ("Cascadelake-Server", 2, 64, 128)
    assert a["ram_bajty"] == 515874 * 2**20 and a["hipernadzorca"] == "QEMU 6.2.0"
    assert a["ip"] == "10.50.0.11" and a["tryb_serwisowy"] is False and a["strefa"] == "az1"
    assert hosty[HIP_B]["tryb_serwisowy"] is True and hosty[HIP_B]["strefa"] == "az2"
    assert len(wynik["vm"]) == 1200  # obie strony
    vmy = {v["ext_id"]: v for v in wynik["vm"]}
    bastion = vmy[VM_Z_AGENTEM]
    assert bastion["bios_uuid"] == VM_Z_AGENTEM and bastion["host_id"] == HIP_A
    assert (bastion["gniazda"], bastion["rdzenie_na_gniazdo"], bastion["ram_bajty"]) == (4, 1, 8 * 2**30)
    assert bastion["dyski"] == [
        {"rozmiar_bajty": 40 * 2**30, "magistrala": None, "kontener": "dysk lokalny (flavor)"},
        {"rozmiar_bajty": 200 * 2**30, "magistrala": None, "kontener": "ceph-ssd"}]
    assert bastion["karty"] == [{"mac": "fa:16:3e:00:00:01", "ip": ["192.168.10.1", "203.0.113.10"],
                                 "siec": "web-net"}]
    assert (bastion["projekt"], bastion["strefa"], bastion["typ"]) == ("p-web", "az1", "m1.large")
    assert bastion["opis"] == "skok" and bastion["stan"] == "ON"
    # Wylaczona maszyna bez hypervisor_hostname trafia na host po nazwie uslugi.
    drugi = vmy[SERWERY[1]["id"]]
    assert drugi["stan"] == "OFF" and drugi["host_id"] == HIP_B
    # Tylko odczyt: jedyne zapisy to zalozenie i uniewaznienie tokenu.
    assert [(m, s) for m, s in zapytania if m != "GET"] == [("POST", "/v3/auth/tokens"),
                                                             ("DELETE", "/v3/auth/tokens")]
    assert not tokeny
    # Punkt "internal" z katalogu nie jest uzywany, gdy jest "public".
    assert not any("wewnetrzny" in s for _, s in zapytania)


def test_logowanie_haslem_z_domena_i_projektem(tmp_path):
    with falszywy_openstack(tmp_path) as (adres, pem, _, logowania, _t):
        wynik = openstack.wykonaj(openstack.Openstack(adres + "/v3/", "cmdb-ro", "tajne", pem,
                                                      domena="Firma", projekt="admin"), "test")
    auth = logowania[0]["auth"]
    assert auth["identity"]["password"]["user"] == {"name": "cmdb-ro", "domain": {"name": "Firma"},
                                                    "password": "tajne"}
    assert auth["scope"] == {"project": {"name": "admin", "domain": {"name": "Firma"}}}
    assert wynik["rodzaj"] == "test" and [k["nazwa"] for k in wynik["klastry"]] == ["RegionOne"]
    assert "vm" not in wynik


def test_bledy_maja_czytelny_opis(tmp_path):
    with falszywy_openstack(tmp_path) as (adres, pem, _, _l, _t):
        with pytest.raises(openstack.BladOpenstack, match="HTTP 401.*requires authentication"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "zle", pem), "test")
        with pytest.raises(openstack.BladOpenstack, match="nie jest zaufany"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne"), "test")
    with falszywy_openstack(tmp_path, admin=False) as (adres, pem, _, _l, tokeny):
        with pytest.raises(openstack.BladOpenstack, match="HTTP 403.*roli admin"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne", pem), "odczyt")
        assert not tokeny  # token uniewazniony takze po bledzie
    with falszywy_openstack(tmp_path, nova="2.42") as (adres, pem, _, _l, _t):
        with pytest.raises(openstack.BladOpenstack, match="Pike"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne", pem), "test")


def test_token_nie_idzie_do_punktu_bez_https(tmp_path):
    with falszywy_openstack(tmp_path, http_w_katalogu=True) as (adres, pem, zapytania, _l, _t):
        with pytest.raises(openstack.BladOpenstack, match="bez https"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne", pem), "test")
    assert not any(m == "GET" for m, _ in zapytania)


def test_czytnik_openstack_przekazuje_domene_i_projekt(tmp_path):
    with falszywy_openstack(tmp_path) as (adres, pem, _, logowania, _t):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro",
                    "haslo": "tajne", "ca_pem": pem, "interwal_sekund": 3600,
                    "domena": "Firma", "projekt": "admin"}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        pytane = []
        klient_get = klient.get
        klient.get = lambda sciezka, token: (pytane.append(sciezka), klient_get(sciezka, token))[1]
        czytnik = openstack.CzytnikOpenstack(klient, STAN)
        przebieg(czytnik, 1000.0)
    assert pytane[0].startswith("/api/v1/agent/openstack-policy?nonce=")
    assert logowania[0]["auth"]["scope"]["project"]["name"] == "admin"
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/openstack"
    assert tresc["ok"] and len(tresc["vm"]) == 1200 and "tajne" not in json.dumps(tresc)
    assert czytnik.status()["wlaczony"] is True


def test_http_tylko_po_jawnej_zgodzie(tmp_path):
    with falszywy_openstack(tmp_path, tls=False, katalog_controller=True) as (adres, _, zapytania, _l, _t):
        with pytest.raises(openstack.BladOpenstack, match="bez https"):
            openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne"), "test")
        assert not zapytania  # haslo nie wyszlo
        # Zgoda + reczne adresy: katalog podaje "controller", ktorego agent nie widzi.
        klient = openstack.Openstack(adres, APP_ID, "tajne", bez_tls=True,
                                     adres_compute=adres + "/compute/v2.1", adres_volumes=adres + "/volume/v3/")
        wynik = openstack.wykonaj(klient, "odczyt")
    assert wynik["ok"] and len(wynik["vm"]) == 1200
    assert [k["ext_id"] for k in wynik["klastry"]] == ["RegionOne"]  # region z katalogu
    # Cinder bez ID projektu w adresie dostal je z tokenu.
    assert any(s.startswith("/volume/v3/p-admin/volumes/detail") for _, s in zapytania)
    bastion = next(v for v in wynik["vm"] if v["ext_id"] == VM_Z_AGENTEM)
    assert bastion["dyski"][-1]["kontener"] == "ceph-ssd"
    assert not any("controller" in s for _, s in zapytania)


def test_polityka_http_tylko_dla_openstacka():
    from cmdb_agent import nutanix
    from tests.test_nutanix import POLITYKA
    zgoda = dict(POLITYKA, adres="http://172.17.30.199:5000/v3", bez_tls=True)
    klient = KlientPolityki(lambda n: odpowiedz(n, dict(zgoda)))
    p = nutanix.pobierz_polityke(klient, STAN, "/api/v1/agent/openstack-policy", "OpenStack")[0]
    assert p["bez_tls"] is True and p["adres"] == "http://172.17.30.199:5000/v3"
    with pytest.raises(ValueError):
        nutanix.pobierz_polityke(klient, STAN)  # Prism: http nigdy
    bez_zgody = KlientPolityki(lambda n: odpowiedz(n, dict(zgoda, bez_tls=False)))
    with pytest.raises(ValueError):
        nutanix.pobierz_polityke(bez_zgody, STAN, "/api/v1/agent/openstack-policy", "OpenStack")


def test_adres_uslugi_bez_sciezki_dostaje_wersje(tmp_path):
    klient = openstack.Openstack("http://172.17.30.199:5000", "admin", "x", bez_tls=True,
                                 adres_compute="http://172.17.30.199:8774/",
                                 adres_volumes="http://172.17.30.199:8776")
    assert klient.adres == "http://172.17.30.199:5000/v3"
    assert klient.adres_compute == "http://172.17.30.199:8774/v2.1"
    assert klient.adres_volumes == "http://172.17.30.199:8776/v3"
    assert openstack.Openstack("https://k", "a", "x", adres_compute="https://n/compute/v2.1").adres_compute \
        == "https://n/compute/v2.1"


def test_nieznane_application_credential_podpowiada_projekt(tmp_path):
    with falszywy_openstack(tmp_path) as (adres, pem, _, _l, _t):
        # Keystone odpowiada 404 na nieznane ID poswiadczenia.
        with pytest.raises(openstack.BladOpenstack, match="wpisz projekt"):
            klient = openstack.Openstack(adres + "/brak", "admin", "tajne", pem)
            openstack.wykonaj(klient, "test")


def test_blad_cindera_nie_przerywa_odczytu(tmp_path):
    with falszywy_openstack(tmp_path, cinder_padniety=True) as (adres, pem, _, _l, _t):
        wynik = openstack.wykonaj(openstack.Openstack(adres, APP_ID, "tajne", pem), "odczyt")
    assert wynik["ok"] and len(wynik["vm"]) == 1200
    bastion = next(v for v in wynik["vm"] if v["ext_id"] == VM_Z_AGENTEM)
    assert [d["kontener"] for d in bastion["dyski"]] == ["dysk lokalny (flavor)"]
