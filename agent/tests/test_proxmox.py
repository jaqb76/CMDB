"""Odczyt Proxmox VE po stronie agenta."""
from __future__ import annotations

import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from cmdb_agent import proxmox
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg

TOKEN = "PVEAPIToken=cmdb@pve!cmdb=sekret-tokenu"
BILET = "PVE:cmdb@pve:66AA0000::podpis"

# Ksztalt odpowiedzi jak w API Proxmox VE 8 (/api2/json).
STATUS_KLASTRA = [
    {"type": "cluster", "id": "cluster", "name": "pve-dc1", "nodes": 2, "quorate": 1, "version": 4},
    {"type": "node", "id": "node/pve01", "name": "pve01", "ip": "10.20.0.11", "online": 1, "local": 1, "nodeid": 1},
    {"type": "node", "id": "node/pve02", "name": "pve02", "ip": "10.20.0.12", "online": 0, "nodeid": 2},
]
STATUS_WEZLA = {"pve01": {
    "cpuinfo": {"model": "AMD EPYC 7313 16-Core Processor", "sockets": 2, "cores": 16, "cpus": 64},
    "memory": {"total": 512 * 2**30, "used": 100 * 2**30},
    "pveversion": "pve-manager/8.2.4/faa83925c9641325", "kversion": "Linux 6.8.12-1-pve #1 SMP",
    "current-kernel": {"release": "6.8.12-1-pve"}, "uptime": 3600,
}}
ZASOBY = [
    {"id": "qemu/100", "vmid": 100, "name": "app-01", "node": "pve01", "type": "qemu", "status": "running",
     "maxcpu": 4, "maxmem": 8 * 2**30, "maxdisk": 32 * 2**30, "template": 0, "pool": "produkcja"},
    {"id": "qemu/101", "vmid": 101, "name": "win-01", "node": "pve01", "type": "qemu", "status": "stopped",
     "maxcpu": 2, "maxmem": 4 * 2**30, "template": 0},
    {"id": "lxc/200", "vmid": 200, "name": "dns-01", "node": "pve01", "type": "lxc", "status": "running",
     "maxcpu": 2, "maxmem": 2**30, "template": 0},
    {"id": "qemu/9000", "vmid": 9000, "name": "szablon-debian", "node": "pve01", "type": "qemu",
     "status": "stopped", "template": 1},
    # Wezel offline: maszyna jest na liscie, ale o szczegoly nie pytamy.
    {"id": "qemu/300", "vmid": 300, "name": "db-02", "node": "pve02", "type": "qemu", "status": "unknown",
     "maxcpu": 8, "maxmem": 16 * 2**30},
]
KONFIGURACJE = {
    ("qemu", "100"): {
        "name": "app-01", "sockets": 1, "cores": 4, "memory": "8192", "ostype": "l26", "agent": "1,fstrim_cloned_disks=1",
        "smbios1": "uuid=6f2e8c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "description": "Aplikacja kadrowa",
        "scsi0": "local-lvm:vm-100-disk-0,iothread=1,size=32G", "ide2": "local:iso/debian.iso,media=cdrom",
        "virtio1": "ceph-rbd:vm-100-disk-1,size=1T", "efidisk0": "local-lvm:vm-100-disk-2,size=4M",
        "net0": "virtio=BC:24:11:AA:BB:01,bridge=vmbr0,firewall=1,tag=120",
        "net1": "e1000=BC:24:11:AA:BB:02,bridge=vmbr1",
    },
    ("qemu", "101"): {"name": "win-01", "sockets": 2, "cores": 1, "memory": "current=4096", "ostype": "win11",
                      "smbios1": "uuid=0c53f7eb-0000-4000-8000-000000000101",
                      "sata0": "local-lvm:vm-101-disk-0,size=64G"},
    ("lxc", "200"): {"hostname": "dns-01", "cores": 2, "memory": 1024, "ostype": "debian",
                     "rootfs": "local-lvm:vm-200-disk-0,size=8G",
                     "mp0": "/srv/dane,mp=/dane", "mp1": "local-lvm:vm-200-disk-1,mp=/var/lib,size=20G",
                     "net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:CC:DD:01,ip=10.20.5.53/24,gw=10.20.5.1,type=veth"},
}
AGENT_SIEC = {"result": [
    {"name": "lo", "hardware-address": "00:00:00:00:00:00",
     "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
    {"name": "ens18", "hardware-address": "bc:24:11:aa:bb:01", "ip-addresses": [
        {"ip-address-type": "ipv4", "ip-address": "10.20.1.21", "prefix": 24},
        {"ip-address-type": "ipv6", "ip-address": "fe80::be24:11ff:feaa:bb01", "prefix": 64}]},
]}
AGENT_SYSTEM = {"result": {"name": "Ubuntu", "version": "24.04 LTS (Noble Numbat)",
                           "pretty-name": "Ubuntu 24.04.1 LTS", "kernel-release": "6.8.0-45-generic"}}
INTERFEJSY_LXC = [{"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8", "inet6": "::1/128"},
                  {"name": "eth0", "hwaddr": "bc:24:11:cc:dd:01", "inet": "10.20.5.53/24",
                   "inet6": "fd00::53/64"}]


@contextmanager
def falszywy_proxmox(tmp_path, haslo="tajne", widzi_wezly=True, standalone=False):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania = []

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _odpowiedz(self, kod, dane=None, powod=None):
            tresc = json.dumps({"data": dane}).encode()
            self.send_response(kod, powod)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(tresc)))
            self.end_headers()
            self.wfile.write(tresc)

        def do_POST(self):
            zapytania.append(("POST", urlsplit(self.path).path))
            dane = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if self.path != "/api2/json/access/ticket" or dane != {"username": ["cmdb@pve"], "password": [haslo]}:
                return self._odpowiedz(401, None, "authentication failure")
            self._odpowiedz(200, {"ticket": BILET, "CSRFPreventionToken": "x", "username": "cmdb@pve"})

        def do_GET(self):
            adres = urlsplit(self.path)
            sciezka, q = adres.path, parse_qs(adres.query)
            zapytania.append(("GET", sciezka))
            if (self.headers.get("Authorization") != TOKEN
                    and self.headers.get("Cookie") != "PVEAuthCookie=" + BILET):
                return self._odpowiedz(401, None, "no ticket")
            czesci = [unquote(c) for c in sciezka[len("/api2/json/"):].split("/")]
            if czesci == ["version"]:
                return self._odpowiedz(200, {"version": "8.2.4", "release": "8.2", "repoid": "faa83925"})
            if czesci == ["cluster", "status"]:
                if not widzi_wezly:
                    return self._odpowiedz(200, [])
                if standalone:
                    return self._odpowiedz(200, [{"type": "node", "name": "pve-solo", "ip": "10.9.0.1", "online": 1}])
                return self._odpowiedz(200, STATUS_KLASTRA)
            if czesci == ["cluster", "resources"]:
                assert q == {"type": ["vm"]}
                return self._odpowiedz(200, [] if standalone else ZASOBY)
            if czesci[0] == "nodes" and czesci[2:] == ["status"] and czesci[1] in STATUS_WEZLA:
                return self._odpowiedz(200, STATUS_WEZLA[czesci[1]])
            if czesci[0] == "nodes" and len(czesci) >= 5:
                klucz = (czesci[2], czesci[3])
                if czesci[4:] == ["config"] and klucz in KONFIGURACJE:
                    return self._odpowiedz(200, KONFIGURACJE[klucz])
                if czesci[4:] == ["agent", "network-get-interfaces"] and klucz == ("qemu", "100"):
                    return self._odpowiedz(200, AGENT_SIEC)
                if czesci[4:] == ["agent", "get-osinfo"] and klucz == ("qemu", "100"):
                    return self._odpowiedz(200, AGENT_SYSTEM)
                if czesci[4:] == ["interfaces"] and klucz == ("lxc", "200"):
                    return self._odpowiedz(200, INTERFEJSY_LXC)
                return self._odpowiedz(500, None, "QEMU guest agent is not running")
            self._odpowiedz(501, None, "Method not implemented")

    serwer = ThreadingHTTPServer(("127.0.0.1", 0), Obsluga)
    kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontekst.load_cert_chain(tmp_path / "c.pem", tmp_path / "c.key")
    serwer.socket = kontekst.wrap_socket(serwer.socket, server_side=True)
    watek = threading.Thread(target=serwer.serve_forever, daemon=True)
    watek.start()
    try:
        yield f"https://localhost:{serwer.server_address[1]}", pem, zapytania
    finally:
        serwer.shutdown()
        serwer.server_close()


def test_pelny_odczyt_tokenem_api(tmp_path):
    with falszywy_proxmox(tmp_path) as (adres, pem, zapytania):
        wynik = proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve!cmdb", "sekret-tokenu", pem), "odczyt")
    assert wynik["ok"] and wynik["wersja_pc"] == "8.2.4"
    assert wynik["klastry"] == [{"ext_id": "pve-dc1", "nazwa": "pve-dc1", "wersja": "8.2.4",
                                 "hipernadzorca": "Proxmox VE", "liczba_hostow": 2}]
    hosty = {h["ext_id"]: h for h in wynik["hosty"]}
    pve = hosty["pve01"]
    assert pve["klaster_id"] == "pve-dc1" and pve["ip"] == "10.20.0.11"
    assert pve["cpu_model"] == "AMD EPYC 7313 16-Core Processor"
    assert (pve["gniazda"], pve["rdzenie"], pve["watki"], pve["ram_bajty"]) == (2, 32, 64, 512 * 2**30)
    assert pve["hipernadzorca"] == "Proxmox VE 8.2.4 (jądro 6.8.12-1-pve)"
    assert pve["uruchomiony_o"].endswith("Z")
    assert "cpu_model" not in hosty["pve02"]  # wezel offline - bez szczegolow

    vmy = {v["ext_id"]: v for v in wynik["vm"]}
    assert "9000" not in vmy  # szablon to nie maszyna
    app = vmy["100"]
    assert (app["host_id"], app["klaster_id"], app["stan"], app["typ"]) == ("pve01", "pve-dc1", "ON", "QEMU/KVM")
    assert (app["gniazda"], app["rdzenie_na_gniazdo"], app["ram_bajty"]) == (1, 4, 8 * 2**30)
    assert app["bios_uuid"] == "6f2e8c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
    assert app["dyski"] == [{"rozmiar_bajty": 32 * 2**30, "magistrala": "SCSI", "kontener": "local-lvm"},
                            {"rozmiar_bajty": 2**40, "magistrala": "VIRTIO", "kontener": "ceph-rbd"}]
    assert app["karty"] == [{"mac": "bc:24:11:aa:bb:01", "ip": ["10.20.1.21"], "siec": "vmbr0 (VLAN 120)"},
                            {"mac": "bc:24:11:aa:bb:02", "ip": [], "siec": "vmbr1"}]
    assert app["system"] == "Ubuntu 24.04.1 LTS" and app["ngt"] is True
    assert app["opis"] == "Aplikacja kadrowa" and app["projekt"] == "produkcja"

    win = vmy["101"]
    assert win["stan"] == "OFF" and win["ram_bajty"] == 4 * 2**30 and win["ngt"] is None
    assert win["system"] == "Windows 11/2022/2025" and (win["gniazda"], win["rdzenie_na_gniazdo"]) == (2, 1)

    dns = vmy["200"]
    assert dns["typ"] == "LXC" and dns["nazwa"] == "dns-01" and dns["bios_uuid"] is None
    assert (dns["gniazda"], dns["rdzenie_na_gniazdo"], dns["ram_bajty"]) == (1, 2, 2**30)
    # Katalog hosta (mp0 bez rozmiaru) nie jest dyskiem kontenera.
    assert dns["dyski"] == [{"rozmiar_bajty": 8 * 2**30, "magistrala": "rootfs", "kontener": "local-lvm"},
                            {"rozmiar_bajty": 20 * 2**30, "magistrala": "mp1", "kontener": "local-lvm"}]
    assert dns["karty"] == [{"mac": "bc:24:11:cc:dd:01", "ip": ["10.20.5.53", "fd00::53"], "siec": "vmbr0"}]
    assert dns["system"] == "Debian"

    db = vmy["300"]
    assert db["host_id"] == "pve02" and db["ram_bajty"] == 16 * 2**30 and db["stan"] == "unknown"

    # Tokenem - same GET-y, bez biletu; o wylaczone maszyny i wezel offline nikt nie pyta.
    assert {m for m, _ in zapytania} == {"GET"}
    assert not any("/qemu/101/agent" in s or "/pve02/" in s for _, s in zapytania)


def test_logowanie_haslem_bierze_bilet(tmp_path):
    with falszywy_proxmox(tmp_path) as (adres, pem, zapytania):
        wynik = proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve", "tajne", pem), "test")
    assert wynik["rodzaj"] == "test" and len(wynik["klastry"]) == 1 and len(wynik["hosty"]) == 2
    assert "vm" not in wynik
    assert [s for m, s in zapytania if m == "POST"] == ["/api2/json/access/ticket"]
    assert not any(s.endswith("/cluster/resources") for _, s in zapytania)


def test_pojedynczy_serwer_bez_klastra(tmp_path):
    with falszywy_proxmox(tmp_path, standalone=True) as (adres, pem, _):
        wynik = proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve!cmdb", "sekret-tokenu", pem), "odczyt")
    assert wynik["klastry"] == [] and wynik["hosty"][0]["ext_id"] == "pve-solo"
    assert wynik["hosty"][0]["klaster_id"] is None and wynik["vm"] == []


def test_czytelne_bledy(tmp_path):
    with falszywy_proxmox(tmp_path) as (adres, pem, _):
        with pytest.raises(proxmox.BladProxmox, match="HTTP 401"):
            proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve", "zle", pem), "test")
        with pytest.raises(proxmox.BladProxmox, match="realmem"):
            proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb", "tajne", pem), "test")
        with pytest.raises(proxmox.BladProxmox, match="pve-root-ca.pem"):
            proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve!cmdb", "sekret-tokenu"), "test")
    with falszywy_proxmox(tmp_path, widzi_wezly=False) as (adres, pem, _):
        with pytest.raises(proxmox.BladProxmox, match="PVEAuditor"):
            proxmox.wykonaj(proxmox.ProxmoxVE(adres, "cmdb@pve!cmdb", "sekret-tokenu", pem), "test")


def test_splaszczenie_bez_konfiguracji():
    """Brak uprawnien do konfiguracji maszyny - zostaje to, co podaje lista zasobow."""
    v = proxmox.vm(ZASOBY[0], None, "pve-dc1")
    assert (v["gniazda"], v["rdzenie_na_gniazdo"], v["ram_bajty"]) == (1, 4, 8 * 2**30)
    assert v["dyski"] == [{"rozmiar_bajty": 32 * 2**30, "magistrala": None, "kontener": None}]
    assert v["bios_uuid"] is None and v["nazwa"] == "app-01"
    assert proxmox._rozmiar("1.5T") == int(1.5 * 2**40) and proxmox._rozmiar("x") is None


def test_czytnik_proxmox_wysyla_wynik_na_wlasny_adres(tmp_path):
    with falszywy_proxmox(tmp_path) as (adres, pem, _):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb@pve!cmdb",
                    "haslo": "sekret-tokenu", "ca_pem": pem, "interwal_sekund": 3600}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        pytane = []
        klient_get = klient.get
        klient.get = lambda sciezka, token: (pytane.append(sciezka), klient_get(sciezka, token))[1]
        czytnik = proxmox.CzytnikProxmox(klient, STAN)
        przebieg(czytnik, 1000.0)
    assert pytane[0].startswith("/api/v1/agent/proxmox-policy?nonce=")
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/proxmox"
    assert tresc["ok"] and len(tresc["vm"]) == 4 and "sekret-tokenu" not in json.dumps(tresc)
    assert czytnik.status()["wlaczony"] is True


def test_polityka_proxmoxa_tylko_po_https():
    from cmdb_agent import nutanix
    with pytest.raises(ValueError, match="https"):
        nutanix._polaczenie({"enabled": True, "adres": "http://pve01:8006", "bez_tls": True}, "Proxmox")
