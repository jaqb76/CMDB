"""Odczyt VMware vCenter po stronie agenta."""
from __future__ import annotations

import base64
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from cmdb_agent import vmware
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg

# Ksztalt odpowiedzi jak w vCenter REST API (/api, vSphere 7.0 U2+).
KLASTRY = [{"cluster": "domain-c8", "name": "RCHO-VSAN-01", "ha_enabled": True, "drs_enabled": True}]
HOSTY = [
    {"host": "host-10", "name": "esx01.firma.pl", "connection_state": "CONNECTED", "power_state": "POWERED_ON"},
    {"host": "host-11", "name": "10.30.0.12", "connection_state": "CONNECTED", "power_state": "POWERED_ON"},
    {"host": "host-99", "name": "esx-samotny.firma.pl", "connection_state": "CONNECTED"},
]
CZLONKOWIE = {"domain-c8": ["host-10", "host-11"]}
VM_NA_HOSCIE = {
    "host-10": [{"vm": "vm-101", "name": "app-01", "power_state": "POWERED_ON", "cpu_count": 4,
                 "memory_size_MiB": 8192}],
    "host-11": [{"vm": "vm-102", "name": "db-01", "power_state": "POWERED_OFF", "cpu_count": 2,
                 "memory_size_MiB": 4096}],
    "host-99": [],
}
SZCZEGOLY = {
    "vm-101": {
        "name": "app-01", "power_state": "POWERED_ON", "guest_OS": "RHEL_9_64",
        "cpu": {"count": 4, "cores_per_socket": 2}, "memory": {"size_MiB": 8192},
        "disks": {"2000": {"label": "Hard disk 1", "type": "SCSI", "capacity": 100 * 2**30,
                           "backing": {"type": "VMDK_FILE", "vmdk_file": "[ds-ssd-01] app-01/app-01.vmdk"}}},
        "nics": {"4000": {"label": "Network adapter 1", "mac_address": "00:50:56:aa:bb:01",
                          "backing": {"type": "DISTRIBUTED_PORTGROUP", "network": "dvportgroup-21",
                                      "network_name": "VLAN120"}}},
        "identity": {"name": "app-01", "bios_uuid": "4231a8f2-1c2d-3e4f-5a6b-7c8d9e0f1a2b",
                     "instance_uuid": "5031aaaa-0000-0000-0000-000000000001"},
    },
    "vm-102": {"name": "db-01", "power_state": "POWERED_OFF", "guest_OS": "WINDOWS_SERVER_2021",
               "cpu": {"count": 2, "cores_per_socket": 1}, "memory": {"size_MiB": 4096},
               "disks": {}, "nics": {},
               "identity": {"bios_uuid": "4231a8f2-0000-0000-0000-000000000002"}},
}
# VI/JSON (vSphere 8.0 U1+): tylko dla host-10; host-11 "nie ma" szczegolow.
PODSUMOWANIE_HOSTA = {"host-10": {
    "_typeName": "HostListSummary",
    "hardware": {"vendor": "Dell Inc.", "model": "PowerEdge R760", "cpuModel": "Intel(R) Xeon(R) Gold 6430",
                 "numCpuPkgs": 2, "numCpuCores": 64, "numCpuThreads": 128, "memorySize": 1024 * 2**30,
                 "otherIdentifyingInfo": [
                     {"identifierValue": "ABC1234", "identifierType": {"key": "ServiceTag"}},
                     {"identifierValue": "None", "identifierType": {"key": "AssetTag"}}]},
    "config": {"product": {"fullName": "VMware ESXi 8.0.3 build-24280767"}},
    "runtime": {"inMaintenanceMode": False, "bootTime": "2026-08-01T10:00:00Z"},
}}
KONFIGURACJA_HOSTA = {"host-10": {
    "network": {"vnic": [
        {"device": "vmk1", "key": "key-vim.host.VirtualNic-vmk1", "portgroup": "vMotion",
         "spec": {"ip": {"ipAddress": "10.40.0.11"}}},
        {"device": "vmk0", "key": "key-vim.host.VirtualNic-vmk0", "portgroup": "Management Network",
         "spec": {"ip": {"ipAddress": "10.30.0.11"}}}]},
    "virtualNicManagerInfo": {"netConfig": [
        {"nicType": "management", "selectedVnic": ["management.key-vim.host.VirtualNic-vmk0"]}]},
}}
VI = "/sdk/vim25/8.0.1.0"

TOZSAMOSC = {"vm-101": {"full_name": {"default_message": "Red Hat Enterprise Linux 9 (64-bit)"},
                        "ip_address": "10.30.5.21", "host_name": "app-01"}}
INTERFEJSY = {"vm-101": [{"mac_address": "00:50:56:AA:BB:01", "ip": {"ip_addresses": [
    {"ip_address": "10.30.5.21", "prefix_length": 24}, {"ip_address": "fe80::1", "prefix_length": 64}]}}]}


@contextmanager
def falszywy_vcenter(tmp_path, haslo="tajne"):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania = []
    sesje = set()

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _odpowiedz(self, kod, dane=None):
            tresc = json.dumps(dane).encode() if dane is not None else b""
            self.send_response(kod)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(tresc)))
            self.end_headers()
            self.wfile.write(tresc)

        def do_POST(self):
            zapytania.append(("POST", urlsplit(self.path).path))
            if self.path == f"{VI}/SessionManager/SessionManager/Login":
                dane = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if dane != {"userName": "cmdb-ro@vsphere.local", "password": haslo}:
                    return self._odpowiedz(401, {"_typeName": "InvalidLogin"})
                sesje.add("vi-1")
                tresc = json.dumps({"_typeName": "UserSession"}).encode()
                self.send_response(200)
                self.send_header("vmware-api-session-id", "vi-1")
                self.send_header("Content-Length", str(len(tresc)))
                self.end_headers()
                self.wfile.write(tresc)
                return
            if self.path == f"{VI}/SessionManager/SessionManager/Logout":
                sesje.discard(self.headers.get("vmware-api-session-id"))
                return self._odpowiedz(204)
            oczekiwane = "Basic " + base64.b64encode(f"cmdb-ro@vsphere.local:{haslo}".encode()).decode()
            if self.path != "/api/session" or self.headers.get("Authorization") != oczekiwane:
                return self._odpowiedz(401, {"error_type": "UNAUTHENTICATED"})
            sesje.add("sesja-1")
            self._odpowiedz(201, "sesja-1")

        def do_DELETE(self):
            zapytania.append(("DELETE", urlsplit(self.path).path))
            sesje.discard(self.headers.get("vmware-api-session-id"))
            self._odpowiedz(204)

        def do_GET(self):
            adres = urlsplit(self.path)
            sciezka, q = adres.path, parse_qs(adres.query)
            zapytania.append(("GET", sciezka))
            if self.headers.get("vmware-api-session-id") not in sesje:
                return self._odpowiedz(401, {"error_type": "UNAUTHENTICATED"})
            if sciezka.startswith(VI + "/HostSystem/"):
                if self.headers.get("vmware-api-session-id") != "vi-1":
                    return self._odpowiedz(401, {})
                _, ident, wlasciwosc = sciezka[len(VI) + 1:].split("/")
                zrodlo = PODSUMOWANIE_HOSTA if wlasciwosc == "summary" else KONFIGURACJA_HOSTA
                return self._odpowiedz(200, zrodlo[ident]) if ident in zrodlo else self._odpowiedz(404, {})
            if sciezka == "/api/appliance/system/version":
                return self._odpowiedz(200, {"version": "8.0.3.00300", "build": "24322831"})
            if sciezka == "/api/vcenter/cluster":
                return self._odpowiedz(200, KLASTRY)
            if sciezka == "/api/vcenter/host":
                if "clusters" in q:
                    ids = CZLONKOWIE.get(q["clusters"][0], [])
                    return self._odpowiedz(200, [h for h in HOSTY if h["host"] in ids])
                return self._odpowiedz(200, HOSTY)
            if sciezka == "/api/vcenter/vm":
                return self._odpowiedz(200, VM_NA_HOSCIE.get(q["hosts"][0], []))
            czesci = [unquote(c) for c in sciezka.split("/")]
            if sciezka.startswith("/api/vcenter/vm/"):
                ident = czesci[4]
                if len(czesci) == 5:
                    return self._odpowiedz(200, SZCZEGOLY[ident])
                if czesci[5:] == ["guest", "identity"] and ident in TOZSAMOSC:
                    return self._odpowiedz(200, TOZSAMOSC[ident])
                if czesci[5:] == ["guest", "networking", "interfaces"] and ident in INTERFEJSY:
                    return self._odpowiedz(200, INTERFEJSY[ident])
                return self._odpowiedz(503, {"error_type": "SERVICE_UNAVAILABLE"})
            self._odpowiedz(404, {"error_type": "NOT_FOUND"})

    serwer = ThreadingHTTPServer(("127.0.0.1", 0), Obsluga)
    kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontekst.load_cert_chain(tmp_path / "c.pem", tmp_path / "c.key")
    serwer.socket = kontekst.wrap_socket(serwer.socket, server_side=True)
    watek = threading.Thread(target=serwer.serve_forever, daemon=True)
    watek.start()
    try:
        yield f"https://localhost:{serwer.server_address[1]}", pem, zapytania, sesje
    finally:
        serwer.shutdown()
        serwer.server_close()


def test_pelny_odczyt_vcenter(tmp_path):
    with falszywy_vcenter(tmp_path) as (adres, pem, zapytania, sesje):
        wynik = vmware.wykonaj(vmware.VCenter(adres, "cmdb-ro@vsphere.local", "tajne", pem), "odczyt")
    assert wynik["ok"] and wynik["wersja_pc"] == "8.0.3.00300 (build 24322831)"
    assert wynik["klastry"] == [{"ext_id": "domain-c8", "nazwa": "RCHO-VSAN-01", "wersja": None,
                                 "hipernadzorca": "ESXi", "liczba_hostow": 2}]
    hosty = {h["ext_id"]: h for h in wynik["hosty"]}
    esx = hosty["host-10"]
    assert esx["klaster_id"] == "domain-c8"
    assert (esx["producent"], esx["model"], esx["numer_seryjny"]) == ("Dell Inc.", "PowerEdge R760", "ABC1234")
    assert (esx["gniazda"], esx["rdzenie"], esx["watki"], esx["ram_bajty"]) == (2, 64, 128, 1024 * 2**30)
    assert esx["hipernadzorca"] == "VMware ESXi 8.0.3 build-24280767"
    assert esx["ip"] == "10.30.0.11"  # interfejs zarzadzania, nie vMotion
    assert esx["tryb_serwisowy"] is False and esx["uruchomiony_o"] == "2026-08-01T10:00:00Z"
    assert hosty["host-11"]["ip"] == "10.30.0.12"
    assert hosty["host-99"]["klaster_id"] is None  # host spoza klastra
    vmy = {v["ext_id"]: v for v in wynik["vm"]}
    app = vmy["vm-101"]
    assert app["host_id"] == "host-10" and app["klaster_id"] == "domain-c8" and app["stan"] == "ON"
    assert (app["gniazda"], app["rdzenie_na_gniazdo"], app["ram_bajty"]) == (2, 2, 8 * 2**30)
    assert app["bios_uuid"] == "4231a8f2-1c2d-3e4f-5a6b-7c8d9e0f1a2b"
    assert app["dyski"] == [{"rozmiar_bajty": 100 * 2**30, "magistrala": "SCSI", "kontener": "ds-ssd-01"}]
    assert app["karty"] == [{"mac": "00:50:56:aa:bb:01", "ip": ["10.30.5.21"], "siec": "VLAN120"}]
    assert app["system"] == "Red Hat Enterprise Linux 9 (64-bit)" and app["ngt"] is True
    db = vmy["vm-102"]
    assert db["stan"] == "OFF" and db["system"] == "WINDOWS SERVER 2021" and db["ngt"] is None
    # Tylko odczyt: jedyne zapisy to zalozenie i zamkniecie sesji.
    assert {m for m, _ in zapytania} == {"GET", "POST", "DELETE"}
    assert [s for m, s in zapytania if m != "GET"] == [
        "/api/session", f"{VI}/SessionManager/SessionManager/Login",
        f"{VI}/SessionManager/SessionManager/Logout", "/api/session"]
    assert not sesje  # obie sesje zamkniete
    # Wylaczona maszyna nie jest pytana o VMware Tools.
    assert not any(s.startswith("/api/vcenter/vm/vm-102/") for _, s in zapytania)


def test_test_polaczenia_czyta_tylko_klastry(tmp_path):
    with falszywy_vcenter(tmp_path) as (adres, pem, zapytania, _):
        wynik = vmware.wykonaj(vmware.VCenter(adres, "cmdb-ro@vsphere.local", "tajne", pem), "test")
    assert wynik["rodzaj"] == "test" and len(wynik["klastry"]) == 1 and "vm" not in wynik
    assert not any(s.startswith("/api/vcenter/vm") for _, s in zapytania)


def test_zle_haslo_i_niezaufany_certyfikat(tmp_path):
    with falszywy_vcenter(tmp_path) as (adres, pem, _, _s):
        with pytest.raises(vmware.BladVcenter, match="HTTP 401"):
            vmware.wykonaj(vmware.VCenter(adres, "cmdb-ro@vsphere.local", "zle", pem), "test")
        with pytest.raises(vmware.BladVcenter, match="nie jest zaufany"):
            vmware.wykonaj(vmware.VCenter(adres, "cmdb-ro@vsphere.local", "tajne"), "test")


def test_czytnik_vmware_wysyla_wynik_na_wlasny_adres(tmp_path):
    with falszywy_vcenter(tmp_path) as (adres, pem, _, _s):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro@vsphere.local",
                    "haslo": "tajne", "ca_pem": pem, "interwal_sekund": 3600}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        pytane = []
        klient_get = klient.get
        klient.get = lambda sciezka, token: (pytane.append(sciezka), klient_get(sciezka, token))[1]
        czytnik = vmware.CzytnikVmware(klient, STAN)
        przebieg(czytnik, 1000.0)
    assert pytane[0].startswith("/api/v1/agent/vmware-policy?nonce=")
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/vmware"
    assert tresc["ok"] and len(tresc["vm"]) == 2 and "tajne" not in json.dumps(tresc)
    assert czytnik.status()["wlaczony"] is True


def test_czytnik_obsluguje_kilka_vcenter_naraz(tmp_path):
    with falszywy_vcenter(tmp_path) as (adres, pem, _, _s):
        wspolne = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro@vsphere.local",
                   "haslo": "tajne", "ca_pem": pem, "interwal_sekund": 3600}
        polaczenia = [dict(wspolne, id="p1", revision="r1", nazwa="POD01"),
                      dict(wspolne, id="p2", revision="r2", nazwa="POD02", enabled=False, test=True)]
        klient = KlientPolityki(lambda n: odpowiedz(n, {"enabled": False, "test": False},
                                                    polaczenia=[dict(p) for p in polaczenia]))
        czytnik = vmware.CzytnikVmware(klient, STAN)
        przebieg(czytnik, 1000.0)
    wyniki = {t["polaczenie_id"]: t for _, t in klient.wyslane}
    assert wyniki["p1"]["rodzaj"] == "odczyt" and wyniki["p1"]["revision"] == "r1"
    assert wyniki["p2"]["rodzaj"] == "test" and wyniki["p2"]["revision"] == "r2"
    assert {p["nazwa"] for p in czytnik.status()["polaczenia"]} == {"POD01", "POD02"}

    # Polaczenie usuniete w panelu znika ze stanu czytnika.
    klient.odpowiedz_dla = lambda n: odpowiedz(n, {"enabled": False, "test": False},
                                               polaczenia=[dict(polaczenia[0], enabled=False)])
    czytnik.krok(1000.0 + 300)
    assert set(czytnik.stany) == {"p1"}


def test_bez_vi_json_hosty_zostaja_z_rest(tmp_path, monkeypatch):
    """vCenter 7.x nie ma VI/JSON - odczyt przechodzi, hosty maja podstawowe dane."""
    monkeypatch.setattr(vmware, "VI_JSON", "/sdk/vim25/nie-ma")
    with falszywy_vcenter(tmp_path) as (adres, pem, _, sesje):
        wynik = vmware.wykonaj(vmware.VCenter(adres, "cmdb-ro@vsphere.local", "tajne", pem), "odczyt")
    esx = {h["ext_id"]: h for h in wynik["hosty"]}["host-10"]
    assert wynik["ok"] and esx["nazwa"] == "esx01.firma.pl" and "model" not in esx
    assert not sesje
