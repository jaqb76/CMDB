"""Odczyt Ceph Dashboard po stronie agenta."""
from __future__ import annotations

import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from cmdb_agent import ceph, openstack
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg

FSID = "6f1c2a3e-5b7d-11ef-9c1a-0242ac120002"

# Ksztalt odpowiedzi jak w Ceph Dashboard API v1.0 (Reef).
PODSUMOWANIE = {"health_status": "HEALTH_WARN", "mgr_host": "https://ceph-mon01:8443/",
                "version": "ceph version 18.2.4 (e7ad5345525c7aa95470c26863873b581076945d) reef (stable)"}
MINIMAL = {
    "health": {"status": "HEALTH_WARN", "checks": [
        {"type": "OSD_NEARFULL", "severity": "HEALTH_WARN", "summary": {"message": "1 nearfull osd(s)"}},
        {"type": "POOL_NO_REDUNDANCY", "severity": "HEALTH_WARN", "summary": {"message": "1 pool(s) have no replicas configured"}}]},
    "mon_status": {"monmap": {"fsid": FSID, "mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
                   "quorum": [0, 1, 2]},
    "osd_map": {"osds": [{"id": 0, "up": 1, "in": 1}, {"id": 1, "up": 1, "in": 1},
                         {"id": 2, "up": 0, "in": 1}]},
    "df": {"stats": {"total_bytes": 30 * 2**40, "total_used_raw_bytes": 12 * 2**40,
                     "total_avail_bytes": 18 * 2**40}},
    "hosts": 3,
}
HOSTY = [
    {"hostname": "ceph-osd01", "addr": "10.60.0.21", "ceph_version": "ceph version 18.2.4 (e7ad53) reef (stable)",
     "status": "", "services": [{"type": "mon", "id": "ceph-osd01"}, {"type": "osd", "id": "0"},
                                {"type": "osd", "id": "1"}, {"type": "mgr", "id": "ceph-osd01.abc"}]},
    # Reef z orchestratorem: services puste, liczby w service_instances.
    {"hostname": "ceph-osd02", "addr": "10.60.0.22", "services": [],
     "service_instances": [{"type": "osd", "count": 25}, {"type": "crash", "count": 1}]},
]
OSD = [
    {"osd": 0, "up": 1, "in": 1, "state": ["exists", "up"], "operational_status": "working",
     "host": {"name": "ceph-osd01"}, "tree": {"device_class": "ssd"},
     "stats": {"stat_bytes": 7681498677248, "stat_bytes_used": 676914929664, "numpg": 131}},
    {"osd": 1, "up": 0, "in": 0, "state": ["autoout", "exists"], "operational_status": "working",
     "host": {"name": "ceph-osd01"}, "tree": {"device_class": "ssd"}, "stats": {"stat_bytes": 0, "numpg": 0}},
]
BUCKETY = [{"bucket": "loki-ruler", "owner": "od-prod-logs", "versioning": "off", "creation_time": "2025-07-08T11:24:59Z",
            "usage": {"rgw.main": {"size_actual": 4096, "num_objects": 3}},
            "bucket_quota": {"enabled": True, "max_size": 10737418240}}]
UZYTKOWNIK = {"user_id": "ru-prod-tempo", "display_name": "Tempo", "email": "", "suspended": 0, "max_buckets": 1,
              "admin": False, "system": False, "user_quota": {"enabled": True, "max_size": 85899345920},
              "keys": [{"user": "ru-prod-tempo", "access_key": "AKIASEKRETNYKLUCZ", "secret_key": "SEKRETNYSECRET123"}],
              "swift_keys": []}
DEMONY = [{"id": "coi.pod2-ceph011.wwsvtj", "server_hostname": "pod2-ceph011", "realm_name": "coi",
           "zonegroup_name": "pod2", "zone_name": "data1", "port": 8080}]
CEPHFS = [{"id": 1, "mdsmap": {"fs_name": "CEPH_FS_PROD", "max_mds": 3, "up": {"mds_0": 1, "mds_1": 2},
                               "failed": [], "damaged": [], "data_pools": [21], "metadata_pool": 22}}]
PULE = [
    {"pool_name": "volumes", "type": "replicated", "size": 3, "min_size": 2, "pg_num": 128,
     "application_metadata": ["rbd"],
     "stats": {"bytes_used": {"latest": 3 * 2**40}, "max_avail": {"latest": 5 * 2**40}}},
    {"pool_name": "images", "type": "replicated", "size": 1, "pg_num": 32, "application_metadata": ["rbd"],
     "stats": {"bytes_used": {"latest": 2**30}, "max_avail": {"latest": 5 * 2**40}}},
]


@contextmanager
def falszywy_dashboard(tmp_path, haslo="tajne", tls=True, bez_fsid_w_minimal=False, standby_na=None, pem_klucz=None):
    pem, klucz = pem_klucz or _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania, tokeny = [], set()
    minimal = json.loads(json.dumps(MINIMAL))
    if bez_fsid_w_minimal:
        del minimal["mon_status"]["monmap"]["fsid"]

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _odpowiedz(self, kod, dane=None):
            tresc = json.dumps(dane).encode() if dane is not None else b""
            self.send_response(kod)
            self.send_header("Content-Type", "application/vnd.ceph.api.v1.0+json")
            self.send_header("Content-Length", str(len(tresc)))
            self.end_headers()
            self.wfile.write(tresc)

        def _standby(self):
            # Mgr w trybie standby: wszystko przekierowuje na aktywny mgr (jak Ceph: 303 na "/").
            zapytania.append((self.command, self.path))
            self.send_response(303)
            self.send_header("Location", standby_na + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if standby_na:
                return self._standby()
            zapytania.append(("POST", self.path))
            dane = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/auth":
                if dane != {"username": "cmdb-ro", "password": haslo}:
                    return self._odpowiedz(400, {"detail": "Invalid credentials", "code": "invalid_credentials"})
                tokeny.add("jwt-1")
                return self._odpowiedz(201, {"token": "jwt-1", "username": "cmdb-ro"})
            if self.path == "/api/auth/logout":
                tokeny.discard(self.headers.get("Authorization", "").removeprefix("Bearer "))
                return self._odpowiedz(200, {})
            self._odpowiedz(404, {})

        def do_GET(self):
            if standby_na:
                return self._standby()
            sciezka = urlsplit(self.path).path
            zapytania.append(("GET", self.path))
            assert self.headers.get("Accept") == ceph.ACCEPT
            if self.headers.get("Authorization", "").removeprefix("Bearer ") not in tokeny:
                return self._odpowiedz(401, {"detail": "Token expired"})
            dane = {"/api/summary": PODSUMOWANIE, "/api/health/minimal": minimal, "/api/host": HOSTY,
                    "/api/pool": PULE, "/api/health/get_cluster_fsid": FSID, "/api/osd": OSD,
                    "/api/rgw/bucket": BUCKETY, "/api/rgw/user": ["ru-prod-tempo"],
                    "/api/rgw/user/ru-prod-tempo": UZYTKOWNIK, "/api/rgw/daemon": DEMONY,
                    "/api/cephfs": CEPHFS}.get(sciezka)
            self._odpowiedz(200, dane) if dane is not None else self._odpowiedz(404, {"detail": "nie ma"})

    serwer = ThreadingHTTPServer(("127.0.0.1", 0), Obsluga)
    if tls:
        kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        kontekst.load_cert_chain(tmp_path / "c.pem", tmp_path / "c.key")
        serwer.socket = kontekst.wrap_socket(serwer.socket, server_side=True)
    watek = threading.Thread(target=serwer.serve_forever, daemon=True)
    watek.start()
    try:
        yield f"{'https' if tls else 'http'}://localhost:{serwer.server_address[1]}", pem, zapytania, tokeny
    finally:
        serwer.shutdown()
        serwer.server_close()


def test_pelny_odczyt_klastra(tmp_path):
    with falszywy_dashboard(tmp_path) as (adres, pem, zapytania, tokeny):
        wynik = ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne", pem), "odczyt")
    assert wynik["ok"] and wynik["wersja_pc"] == "18.2.4 reef"
    [k] = wynik["klastry"]
    assert k["ext_id"] == FSID and k["zdrowie"] == "HEALTH_WARN"
    assert k["ostrzezenia"] == ["WARN: 1 nearfull osd(s)", "WARN: 1 pool(s) have no replicas configured"]
    assert (k["pojemnosc_bajty"], k["zajete_bajty"], k["wolne_bajty"]) == (30 * 2**40, 12 * 2**40, 18 * 2**40)
    assert (k["liczba_osd"], k["osd_up"], k["osd_in"]) == (3, 2, 3)
    assert (k["liczba_mon"], k["mon_kworum"], k["liczba_hostow"]) == (3, 3, 2)
    assert k["hosty"][1]["role"] == ["crash", "osd"] and k["hosty"][1]["osd"] == 25  # service_instances (Reef)
    assert k["osd"] == [
        {"id": 0, "host": "ceph-osd01", "klasa": "ssd", "rozmiar_bajty": 7681498677248, "zajete_bajty": 676914929664,
         "pg": 131, "up": True, "in": True, "stan": None},
        {"id": 1, "host": "ceph-osd01", "klasa": "ssd", "rozmiar_bajty": 0, "zajete_bajty": None,
         "pg": 0, "up": False, "in": False, "stan": "autoout"}]
    assert k["buckety"] == [{"nazwa": "loki-ruler", "wlasciciel": "od-prod-logs", "rozmiar_bajty": 4096, "obiekty": 3,
                             "wersjonowanie": "off", "kwota_bajty": 10737418240, "utworzono": "2025-07-08T11:24:59Z"}]
    assert k["uzytkownicy_rgw"] == [{"uid": "ru-prod-tempo", "nazwa": "Tempo", "email": None, "zawieszony": False,
                                     "max_bucketow": 1, "kwota_bajty": 85899345920, "klucze_s3": 1, "klucze_swift": 0,
                                     "admin": False}]
    # Klucze S3 nigdy nie opuszczaja agenta - tylko ich liczba.
    assert "SEKRETNY" not in json.dumps(wynik) and "AKIA" not in json.dumps(wynik)
    assert k["bramy_rgw"] == [{"id": "coi.pod2-ceph011.wwsvtj", "host": "pod2-ceph011", "strefa": "data1",
                               "grupa_stref": "pod2", "realm": "coi", "port": 8080}]
    assert k["cephfs"] == [{"nazwa": "CEPH_FS_PROD", "max_mds": 3, "aktywne_mds": 2, "uszkodzone_mds": 0,
                            "pule_danych": [21], "pula_metadanych": 22}]
    assert k["hosty"][0] == {"nazwa": "ceph-osd01", "adres": "10.60.0.21", "role": ["mgr", "mon", "osd"],
                             "osd": 2, "wersja": "18.2.4 reef", "stan": None}
    assert k["pule"][0] == {"nazwa": "volumes", "typ": "replicated", "rozmiar": 3, "min_rozmiar": 2, "pg": 128,
                            "aplikacje": ["rbd"], "zajete_bajty": 3 * 2**40, "dostepne_bajty": 5 * 2**40}
    # Tylko odczyt: jedyne zapisy to logowanie i wylogowanie.
    assert [(m, s) for m, s in zapytania if m != "GET"] == [("POST", "/api/auth"), ("POST", "/api/auth/logout")]
    assert not tokeny


def test_test_polaczenia_bez_hostow_i_pul(tmp_path):
    with falszywy_dashboard(tmp_path) as (adres, pem, zapytania, _):
        wynik = ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne", pem), "test")
    assert wynik["rodzaj"] == "test" and wynik["klastry"][0]["ext_id"] == FSID
    assert not any(s.startswith(("/api/host", "/api/pool")) for _, s in zapytania)


def test_fsid_z_osobnego_zasobu(tmp_path):
    with falszywy_dashboard(tmp_path, bez_fsid_w_minimal=True) as (adres, pem, _, _t):
        wynik = ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne", pem), "test")
    assert wynik["klastry"][0]["ext_id"] == FSID


def test_bledy_i_http(tmp_path):
    with falszywy_dashboard(tmp_path) as (adres, pem, _, tokeny):
        with pytest.raises(ceph.BladCeph, match="odrzucil dane logowania.*Invalid credentials"):
            ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "zle", pem), "test")
        with pytest.raises(ceph.BladCeph, match="nie jest zaufany"):
            ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne"), "test")
    with falszywy_dashboard(tmp_path, tls=False) as (adres, _, zapytania, _t):
        with pytest.raises(ceph.BladCeph, match="bez https"):
            ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne"), "test")
        assert not zapytania  # haslo nie wyszlo
        assert ceph.wykonaj(ceph.CephDashboard(adres, "cmdb-ro", "tajne", bez_tls=True), "odczyt")["ok"]


def test_kontrole_zdrowia_jako_slownik():
    zdrowie = {"checks": {"MON_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1/3 mons down"}}}}
    assert ceph._ostrzezenia(zdrowie) == ["WARN: 1/3 mons down"]


def test_czytnik_ceph_wysyla_wynik_na_wlasny_adres(tmp_path):
    with falszywy_dashboard(tmp_path, tls=False) as (adres, _, _z, _t):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro", "haslo": "tajne",
                    "ca_pem": "", "interwal_sekund": 3600, "bez_tls": True}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        czytnik = ceph.CzytnikCeph(klient, STAN)
        pytane = []
        klient_get = klient.get
        klient.get = lambda sciezka, token: (pytane.append(sciezka), klient_get(sciezka, token))[1]
        przebieg(czytnik, 1000.0)
    assert pytane[0].startswith("/api/v1/agent/ceph-policy?nonce=")
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/ceph" and tresc["ok"] and tresc["klastry"][0]["ext_id"] == FSID
    assert "tajne" not in json.dumps(tresc)


def test_fsid_cepha_z_pul_cindera():
    pule = [
        {"name": "cinder@ceph-ssd#ceph-ssd", "capabilities": {
            "volume_backend_name": "ceph-ssd", "storage_protocol": "ceph",
            "location_info": f"ceph:/etc/ceph/ceph.conf:{FSID}:cinder:volumes"}},
        {"name": "cinder@ceph-hdd#ceph-hdd", "capabilities": {
            "volume_backend_name": "ceph-hdd", "storage_protocol": "ceph",
            "location_info": f"ceph:/etc/ceph/ceph.conf:{FSID.upper()}:cinder:volumes-hdd"}},
        {"name": "cinder@lvm#lvm", "capabilities": {"storage_protocol": "iSCSI", "location_info": "LVMVolumeDriver:x"}},
    ]
    assert openstack.magazyny_cindera(pule) == [
        {"fsid": FSID, "pula": "volumes", "backend": "ceph-ssd"},
        {"fsid": FSID, "pula": "volumes-hdd", "backend": "ceph-hdd"}]


def test_przypiety_certyfikat_bez_zgodnej_nazwy(tmp_path):
    """Domyslny certyfikat Dashboardu (CN "ceph-dashboard", bez SAN) - przypiety przechodzi."""
    with falszywy_dashboard(tmp_path) as (adres, pem, _, _t):
        po_ip = adres.replace("localhost", "127.0.0.1")  # certyfikat jest tylko na "localhost"
        assert ceph.wykonaj(ceph.CephDashboard(po_ip, "cmdb-ro", "tajne", pem), "test")["ok"]
        # Inny certyfikat w polu CA nie odblokowuje niczego.
        inny, _ = _certyfikat()
        with pytest.raises(ceph.BladCeph, match="nie jest zaufany"):
            ceph.wykonaj(ceph.CephDashboard(po_ip, "cmdb-ro", "tajne", inny), "test")


def test_kontekst_tls_wymaga_nazwy_przy_zwyklym_ca(tmp_path):
    from cmdb_agent import nutanix
    with falszywy_dashboard(tmp_path) as (adres, pem, _, _t):
        assert nutanix.kontekst_tls(adres.replace("localhost", "127.0.0.1"), pem).check_hostname is False
        # Wklejony certyfikat nie jest tym, ktory pokazuje serwer - nazwa hosta dalej sprawdzana.
        inny, _ = _certyfikat()
        assert nutanix.kontekst_tls(adres, inny).check_hostname is True
        assert nutanix.kontekst_tls(adres, "").check_hostname is True


def test_standby_mgr_przekierowuje_na_aktywny(tmp_path):
    para = _certyfikat()
    (tmp_path / "a").mkdir()
    (tmp_path / "s").mkdir()
    with falszywy_dashboard(tmp_path / "a", pem_klucz=para) as (aktywny, pem, zapytania, tokeny):
        with falszywy_dashboard(tmp_path / "s", pem_klucz=para, standby_na=aktywny) as (standby, _, w_standby, _t):
            klient = ceph.CephDashboard(standby, "cmdb-ro", "tajne", pem)
            wynik = ceph.wykonaj(klient, "odczyt")
    assert wynik["ok"] and wynik["klastry"][0]["ext_id"] == FSID
    assert klient.przekierowano_na == aktywny
    assert w_standby == [("POST", "/api/auth")]  # standby nie dostal juz nic wiecej
    assert ("POST", "/api/auth/logout") in zapytania and not tokeny


def test_przekierowanie_po_http_odrzucone(tmp_path):
    with falszywy_dashboard(tmp_path, tls=False, standby_na="http://10.0.0.99:8080") as (standby, _, _z, _t):
        with pytest.raises(ceph.BladCeph, match="przekierowuje na http://10.0.0.99:8080"):
            ceph.wykonaj(ceph.CephDashboard(standby, "cmdb-ro", "tajne", bez_tls=True), "test")
