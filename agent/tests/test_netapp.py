"""Odczyt NetApp ONTAP po stronie agenta."""
from __future__ import annotations

import base64
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from cmdb_agent import netapp
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg

UUID = "9f1b6c2e-1a2b-11ee-8c3d-00a098e1c2d3"
KLASTER = {"name": "pod01-netapp01", "uuid": UUID, "serial_number": "1-80-000011",
           "version": {"full": "NetApp Release 9.12.1P3: Thu Apr 27 18:41:38 UTC 2023", "generation": 9,
                       "major": 12, "minor": 1},
           "location": "DC1 rzad 4", "contact": "storage@firma.pl",
           "management_interfaces": [{"name": "cluster_mgmt", "ip": {"address": "172.16.2.137"}}]}
WEZLY = [{"name": "pod01-netapp01-01", "model": "AFF-A400", "serial_number": "951913000123", "system_id": "0537123456",
          "state": "up", "uptime": 8640000, "version": KLASTER["version"],
          "ha": {"partners": [{"name": "pod01-netapp01-02"}], "takeover": {"state": "not_possible"}},
          "management_interfaces": [{"ip": {"address": "172.16.2.138"}}]},
         {"name": "pod01-netapp01-02", "model": "AFF-A400", "serial_number": "951913000124", "state": "up"}]
DYSKI = [{"name": f"1.0.{i}", "serial_number": f"S4ABC{i:04d}", "model": "X4011WBORA3T8NTF", "vendor": "NETAPP",
          "type": "ssd", "class": "solid_state", "container_type": "aggregate", "state": "present",
          "usable_size": 3840000000000, "node": {"name": "pod01-netapp01-01"}, "shelf": {"uid": "SHFMS123"},
          "bay": i, "firmware_version": "NA02", "aggregates": [{"name": "aggr1"}]} for i in range(1500)]
DYSKI[3] = dict(DYSKI[3], state="broken", container_type="broken")
AGREGATY = [{"name": "aggr1", "node": {"name": "pod01-netapp01-01"}, "state": "online",
             "space": {"block_storage": {"size": 50 * 2**40, "used": 20 * 2**40, "available": 30 * 2**40}},
             "block_storage": {"primary": {"disk_count": 23, "raid_type": "raid_dp", "disk_class": "solid_state"}}}]
SVM = [{"name": "svm_nfs", "state": "running", "subtype": "default", "nfs": {"enabled": True},
        "cifs": {"enabled": False}, "iscsi": {"enabled": True}, "ip_interfaces": [{"ip": {"address": "10.70.1.10"}}]}]
WOLUMENY = [{"name": "vol_vmware01", "svm": {"name": "svm_nfs"}, "aggregates": [{"name": "aggr1"}], "state": "online",
             "type": "rw", "style": "flexvol", "space": {"size": 10 * 2**40, "used": 4 * 2**40},
             "nas": {"path": "/vol_vmware01"}, "snapshot_policy": {"name": "default"}, "is_svm_root": False}]
LUNY = [{"name": "/vol/vol_lun/lun0", "svm": {"name": "svm_nfs"}, "space": {"size": 2**40, "used": 2**39},
         "os_type": "vmware", "serial_number": "wPx1Z$Q3abcd", "status": {"state": "online", "mapped": True}}]
POLKI = [{"name": "1.0", "uid": "SHFMS123", "model": "NS224", "serial_number": "SHFMS123", "module_type": "nsm100",
          "state": "ok", "disk_count": 24, "connection_type": "nvme"}]
LIFY = [{"name": "lif_nfs1", "ip": {"address": "10.70.1.10", "netmask": "24"}, "svm": {"name": "svm_nfs"},
         "location": {"home_node": {"name": "pod01-netapp01-01"}, "home_port": {"name": "e0e"}},
         "state": "up", "services": ["data_core", "data_nfs"]}]
SNAPMIRROR = [{"source": {"path": "svm_nfs:vol_vmware01"}, "destination": {"path": "svm_dr:vol_vmware01_dst"},
               "state": "snapmirrored", "healthy": False, "lag_time": "PT26H", "policy": {"name": "MirrorAllSnapshots"}}]
KOLEKCJE = {"/api/cluster/nodes": WEZLY, "/api/storage/aggregates": AGREGATY, "/api/svm/svms": SVM,
            "/api/storage/volumes": WOLUMENY, "/api/storage/luns": LUNY, "/api/storage/disks": DYSKI,
            "/api/storage/shelves": POLKI, "/api/network/ip/interfaces": LIFY,
            "/api/snapmirror/relationships": SNAPMIRROR}


@contextmanager
def falszywy_ontap(tmp_path, haslo="tajne", bez_pola_services=False, bez_snapmirror=False):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania = []

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _odpowiedz(self, kod, dane):
            tresc = json.dumps(dane).encode()
            self.send_response(kod)
            self.send_header("Content-Type", "application/hal+json")
            self.send_header("Content-Length", str(len(tresc)))
            self.end_headers()
            self.wfile.write(tresc)

        def do_GET(self):
            adres = urlsplit(self.path)
            sciezka, q = adres.path, parse_qs(adres.query)
            zapytania.append((self.command, self.path))
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(f"cmdb-ro:{haslo}".encode()).decode():
                return self._odpowiedz(401, {"error": {"message": "User is not authorized.", "code": "6691623"}})
            if sciezka == "/api/cluster":
                return self._odpowiedz(200, KLASTER)
            if sciezka not in KOLEKCJE or (bez_snapmirror and "snapmirror" in sciezka):
                return self._odpowiedz(404, {"error": {"message": "not found"}})
            pola = q.get("fields", [""])[0]
            if bez_pola_services and "services" in pola and pola != "*":
                # Starszy ONTAP (<9.10) nie zna pola "services" przy LIF-ach.
                return self._odpowiedz(400, {"error": {"message": 'The value "services" is invalid for field "fields"'}})
            rekordy = KOLEKCJE[sciezka]
            start = int(q.get("start", ["0"])[0])
            ile = int(q.get("max_records", ["1000"])[0])
            strona = {"records": rekordy[start:start + ile], "num_records": len(rekordy[start:start + ile])}
            if start + ile < len(rekordy):
                strona["_links"] = {"next": {"href": f"{sciezka}?start={start + ile}&max_records={ile}&fields={pola}"}}
            self._odpowiedz(200, strona)

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


def test_pelny_odczyt_klastra(tmp_path):
    with falszywy_ontap(tmp_path) as (adres, pem, zapytania):
        # Adres z przegladarki (System Manager) - API i tak jest pod /api.
        wynik = netapp.wykonaj(netapp.Ontap(adres + "/sysmgr/v4/", "cmdb-ro", "tajne", pem), "odczyt")
    assert wynik["ok"] and wynik["wersja_pc"] == "9.12.1P3"
    [k] = wynik["klastry"]
    assert (k["ext_id"], k["nazwa"], k["numer_seryjny"], k["ip"]) == (UUID, "pod01-netapp01", "1-80-000011", "172.16.2.137")
    assert k["wezly"][0] == {"nazwa": "pod01-netapp01-01", "model": "AFF-A400", "numer_seryjny": "951913000123",
                             "system_id": "0537123456", "wersja": "9.12.1P3", "stan": None, "czas_pracy_s": 8640000,
                             "partner_ha": "pod01-netapp01-02", "przejecie": "not_possible", "ip": "172.16.2.138",
                             "lokalizacja": None}
    assert k["agregaty"][0]["raid"] == "raid_dp" and k["agregaty"][0]["rozmiar_bajty"] == 50 * 2**40
    assert k["svm"][0]["protokoly"] == ["nfs", "iscsi"] and k["svm"][0]["ip"] == ["10.70.1.10"]
    assert k["wolumeny"][0]["sciezka"] == "/vol_vmware01" and k["wolumeny"][0]["agregat"] == "aggr1"
    assert k["luny"][0]["zmapowany"] is True and k["luny"][0]["os"] == "vmware"
    assert len(k["dyski"]) == 1500  # dwie strony po 1000
    assert k["dyski"][0]["numer_seryjny"] == "S4ABC0000" and k["dyski"][0]["polka"] == "SHFMS123"
    assert k["dyski"][3]["stan"] == "broken" and k["dyski"][0]["stan"] is None
    assert k["polki"][0]["model"] == "NS224" and k["interfejsy"][0]["uslugi"] == "data_core, data_nfs"
    assert k["snapmirror"][0] == {"zrodlo": "svm_nfs:vol_vmware01", "cel": "svm_dr:vol_vmware01_dst",
                                  "stan": "snapmirrored", "zdrowy": False, "opoznienie": "PT26H",
                                  "polityka": "MirrorAllSnapshots"}
    assert {m for m, _ in zapytania} == {"GET"}  # tylko odczyt, bez sesji
    assert sum(1 for _, s in zapytania if s.startswith("/api/storage/disks")) == 2


def test_test_polaczenia_czyta_klaster_i_wezly(tmp_path):
    with falszywy_ontap(tmp_path) as (adres, pem, zapytania):
        wynik = netapp.wykonaj(netapp.Ontap(adres, "cmdb-ro", "tajne", pem), "test")
    assert wynik["rodzaj"] == "test" and len(wynik["klastry"][0]["wezly"]) == 2
    assert "dyski" not in wynik["klastry"][0]
    assert not any(s.startswith("/api/storage") for _, s in zapytania)


def test_starszy_ontap_bez_pola_i_bez_snapmirror(tmp_path):
    with falszywy_ontap(tmp_path, bez_pola_services=True, bez_snapmirror=True) as (adres, pem, zapytania):
        wynik = netapp.wykonaj(netapp.Ontap(adres, "cmdb-ro", "tajne", pem), "odczyt")
    k = wynik["klastry"][0]
    assert k["interfejsy"][0]["ip"] == "10.70.1.10"  # ponowione z fields=*
    assert k["snapmirror"] == []  # funkcja niedostepna - pusta lista, nie blad
    assert any("fields=%2A" in s for _, s in zapytania if s.startswith("/api/network"))


def test_bledy(tmp_path):
    with falszywy_ontap(tmp_path) as (adres, pem, _):
        with pytest.raises(netapp.BladNetapp, match="odrzucil dane logowania.*not authorized"):
            netapp.wykonaj(netapp.Ontap(adres, "cmdb-ro", "zle", pem), "test")
        with pytest.raises(netapp.BladNetapp, match="nie jest zaufany"):
            netapp.wykonaj(netapp.Ontap(adres, "cmdb-ro", "tajne"), "test")
        # Certyfikat klastra bez zgodnej nazwy - przypiety przechodzi.
        assert netapp.wykonaj(netapp.Ontap(adres.replace("localhost", "127.0.0.1"), "cmdb-ro", "tajne", pem), "test")["ok"]


def test_czytnik_netapp_wysyla_wynik_na_wlasny_adres(tmp_path):
    with falszywy_ontap(tmp_path) as (adres, pem, _):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro", "haslo": "tajne",
                    "ca_pem": pem, "interwal_sekund": 3600}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        czytnik = netapp.CzytnikNetapp(klient, STAN)
        przebieg(czytnik, 1000.0)
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/netapp" and tresc["ok"] and len(tresc["klastry"][0]["dyski"]) == 1500
    assert "tajne" not in json.dumps(tresc)
