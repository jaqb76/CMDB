"""Odczyt Dell OpenManage Enterprise po stronie agenta."""
from __future__ import annotations

import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from cmdb_agent import ome
from tests.test_nutanix import STAN, KlientPolityki, _certyfikat, odpowiedz, przebieg


def urzadzenie(numer: int, **zmiany) -> dict:
    d = {"Id": 10000 + numer, "Type": 1000, "DeviceServiceTag": f"SVC{numer:04d}", "DeviceName": f"srv-{numer:03d}",
         "Model": "PowerEdge R650", "Status": 1000, "PowerState": 17, "ConnectionState": True,
         "ChassisServiceTag": None, "LastInventoryTime": "2026-10-06 22:00:01.123",
         "DeviceManagement": [{"NetworkAddress": f"10.70.0.{numer % 250}", "ManagementType": 2}]}
    d.update(zmiany)
    return d


SERWERY = [urzadzenie(i) for i in range(1, 231)]  # trzy strony po 100
SERWERY[0] = urzadzenie(1, DeviceServiceTag="ABC1234", DeviceName="esx01-idrac", Model="PowerEdge R760",
                        Status=3000, PowerState=17)
SERWERY[1] = urzadzenie(2, Status=4000, PowerState=18)
INWENTARZ = {"value": [
    {"InventoryType": "serverProcessors", "InventoryInfo": [
        {"ModelName": "Intel(R) Xeon(R) Gold 6430", "NumberOfCores": 32, "NumberOfEnabledThreads": 64},
        {"ModelName": "Intel(R) Xeon(R) Gold 6430", "NumberOfCores": 32, "NumberOfEnabledThreads": 64}]},
    {"InventoryType": "serverMemoryDevices", "InventoryInfo": [
        {"Name": "DIMM.Socket.A1", "DeviceDescription": "DIMM A1", "Size": 65536, "Status": 1000,
         "Manufacturer": "Hynix Semiconductor", "PartNumber": "HMAA8GR7", "SerialNumber": "54C388A6",
         "TypeDetails": "DDR4", "Speed": 3200, "CurrentOperatingSpeed": 2933, "Rank": "Double Rank"}] * 16},
    {"InventoryType": "serverRaidControllers", "InventoryInfo": [
        {"Name": "PERC H750 Adapter", "DeviceDescription": "RAID Controller in Slot 3", "Status": 1000,
         "StatusTypeString": "NORMAL", "FirmwareVersion": "52.26.0-5179", "CacheSizeInMb": 8192,
         "ServerVirtualDisks": [{"Name": "NonRAID Disk 29", "Layout": "RAID-0", "Status": 1000, "State": "Online"},
                                {"Name": "VD1", "Layout": "RAID-1", "Status": 3000, "State": "Degraded"}]}]},
    {"InventoryType": "serverPowerSupplies", "InventoryInfo": [
        {"Name": "Power Supply 1", "Model": "PWR SPLY,750W,RDNT,DELTA", "OutputWatts": 750, "Status": 1000,
         "FirmwareVersion": "00.30.C2", "SerialNumber": "CNDED0017U6CR3", "InputVoltage": 228,
         "OperationalStatus": "OK"},
        {"Name": "Power Supply 2", "Model": "PWR SPLY,750W,RDNT,DELTA", "OutputWatts": 750, "Status": 4000,
         "OperationalStatus": "Failed", "State": "Presence Detected"}]},
    {"InventoryType": "serverDeviceCards", "InventoryInfo": [
        {"SlotNumber": "Video.Embedded.1-1", "Manufacturer": "Matrox", "Description": "Integrated Matrox G200eW3"}]},
    {"InventoryType": "deviceLicense", "InventoryInfo": [
        {"LicenseDescription": "iDRAC9 Enterprise License", "LicenseStatus": 1000, "LicenseType": {"Name": "Perpetual"}}]},
    # Ksztalt jak w OME 4.x: rozmiar bez jednostki (GB), BusType, pelna nazwa nosnika.
    {"InventoryType": "serverArrayDisks", "InventoryInfo": [
        {"Size": "698.64", "MediaType": "Solid State Drive", "BusType": "PCIe", "Status": 1000, "StatusString": "OK",
         "ModelNumber": "Dell Express Flash NVMe P4800X 750GB SF", "SerialNumber": "PHKE1126018D750BGN",
         "DiskNumber": "PCIe SSD in Slot 8 in Bay 1"},
        {"Size": "1,788.50", "MediaType": "Hard Disk Drive", "BusType": "SAS", "Status": 4000,
         "StatusString": "Critical", "ModelNumber": "ST2000NM", "SerialNumber": "ZC1ABC",
         "DiskNumber": "Disk 3 in Backplane 1", "RaidStatus": "Online"},
        # Uszkodzony dysk jak w prawdziwym OME: rozmiar 0, Error, RAID Failed.
        {"Size": "0", "MediaType": "Hard Disk Drive", "BusType": "SAS", "Status": 4000, "StatusString": "Error",
         "RaidStatus": "Failed", "ModelNumber": "AL15SEB24EQY", "SerialNumber": "",
         "DiskNumber": "Disk 11 in Backplane 1 of RAID Controller in Slot 3"}]},
    {"InventoryType": "serverNetworkInterfaces", "InventoryInfo": [
        {"NicId": "NIC.Integrated.1", "VendorName": "Broadcom Corp", "Ports": [
            {"PortId": "NIC.Integrated.1-1", "ProductName": "Broadcom BCM57414 25GbE - B0:26:28:AA:BB:01",
             "LinkStatus": "Up", "LinkSpeed": 25000, "Partitions": [{"CurrentMacAddress": "B0:26:28:AA:BB:01"}]},
            {"PortId": "NIC.Integrated.1-2", "ProductName": "Broadcom BCM57414 25GbE - B0:26:28:AA:BB:02",
             "LinkStatus": "Down", "LinkSpeed": 0, "Partitions": [{"CurrentMacAddress": "B0:26:28:AA:BB:02"}]}]}]},
    {"InventoryType": "deviceSoftware", "InventoryInfo": [
        {"DeviceDescription": "Integrated Dell Remote Access Controller", "Version": "7.10.30.00"},
        {"DeviceDescription": "BIOS", "Version": "2.3.5"}]},
    {"InventoryType": "serverOperatingSystems", "InventoryInfo": [
        {"OsName": "VMware ESXi 8.0.3", "Hostname": "esx01.firma.pl"}]},
]}


@contextmanager
def falszywe_ome(tmp_path, haslo="tajne"):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    zapytania, sesje = [], set()

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
            dane = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/api/SessionService/Sessions":
                return self._odpowiedz(404, {})
            if dane != {"UserName": "cmdb-ro", "Password": haslo, "SessionType": "API"}:
                return self._odpowiedz(401, {"error": {"@Message.ExtendedInfo": [
                    {"Message": "Unable to complete the request because the user name or password is invalid."}]}})
            sesje.add("tok-1")
            self._odpowiedz(201, {"Id": "sesja-1", "UserName": "cmdb-ro"}, {"X-Auth-Token": "tok-1"})

        def do_DELETE(self):
            zapytania.append(("DELETE", unquote(self.path)))
            if unquote(self.path) == "/api/SessionService/Sessions('sesja-1')":
                sesje.discard(self.headers.get("X-Auth-Token"))
            self._odpowiedz(204)

        def do_GET(self):
            adres = urlsplit(self.path)
            sciezka, q = unquote(adres.path), parse_qs(adres.query)
            zapytania.append(("GET", sciezka))
            if self.headers.get("X-Auth-Token") not in sesje:
                return self._odpowiedz(401, {})
            if sciezka == "/api/ApplicationService/Info":
                return self._odpowiedz(200, {"Version": "4.1.0", "BuildNumber": "125"})
            if sciezka == "/api/DeviceService/Devices":
                assert q["$filter"] == ["Type eq 1000"]
                top, skip = int(q["$top"][0]), int(q["$skip"][0])
                return self._odpowiedz(200, {"@odata.count": len(SERWERY), "value": SERWERY[skip:skip + top]})
            if sciezka == "/api/DeviceService/Devices(10001)/InventoryDetails":
                return self._odpowiedz(200, INWENTARZ)
            if sciezka.endswith("/InventoryDetails"):
                return self._odpowiedz(200, {"value": []})
            self._odpowiedz(404, {})

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


def test_pelny_odczyt_serwerow(tmp_path):
    with falszywe_ome(tmp_path) as (adres, pem, zapytania, sesje):
        wynik = ome.wykonaj(ome.Ome(adres, "cmdb-ro", "tajne", pem), "odczyt")
    assert wynik["ok"] and wynik["wersja_pc"] == "4.1.0" and len(wynik["urzadzenia"]) == 230
    esx = wynik["urzadzenia"][0]
    assert esx == {
        "ext_id": "ABC1234", "nazwa": "esx01-idrac", "model": "PowerEdge R760", "zdrowie": "WARNING",
        "zasilanie": "ON", "polaczony": True, "idrac_ip": "10.70.0.1", "idrac_wersja": "7.10.30.00",
        "bios_wersja": "2.3.5", "cpu_model": "Intel(R) Xeon(R) Gold 6430", "gniazda": 2, "rdzenie": 64,
        "watki": 128, "ram_bajty": 1024 * 2**30,
        "dyski": [{"rozmiar_bajty": int(698.64 * 2**30), "typ": "SSD PCIe",
                   "model": "Dell Express Flash NVMe P4800X 750GB SF", "producent": None, "numer_seryjny": "PHKE1126018D750BGN",
                   "stan": None, "miejsce": "PCIe SSD in Slot 8 in Bay 1"},
                  {"rozmiar_bajty": int(1788.5 * 2**30), "typ": "HDD SAS", "model": "ST2000NM", "producent": None,
                   "numer_seryjny": "ZC1ABC", "stan": "Critical", "miejsce": "Disk 3 in Backplane 1"},
                  {"rozmiar_bajty": None, "typ": "HDD SAS", "model": "AL15SEB24EQY", "producent": None, "numer_seryjny": None,
                   "stan": "Error, RAID: Failed", "miejsce": "Disk 11 in Backplane 1 of RAID Controller in Slot 3"}],
        "mac": ["b0:26:28:aa:bb:01", "b0:26:28:aa:bb:02"], "system": "VMware ESXi 8.0.3",
        "hostname_os": "esx01.firma.pl", "obudowa": None, "inwentaryzacja_o": "2026-10-06 22:00:01.123",
        "pamiec": [{"slot": "DIMM A1", "rozmiar_bajty": 64 * 2**30, "typ": "DDR4", "predkosc": 3200,
                    "predkosc_robocza": 2933, "producent": "Hynix Semiconductor", "part": "HMAA8GR7",
                    "numer_seryjny": "54C388A6", "ranga": "Double Rank", "stan": None}] * 16,
        "porty": [{"port": "NIC.Integrated.1-1", "karta": "NIC.Integrated.1", "producent": "Broadcom Corp",
                   "opis": "Broadcom BCM57414 25GbE", "mac": "b0:26:28:aa:bb:01", "lacze": "Up", "predkosc_mbps": 25000},
                  {"port": "NIC.Integrated.1-2", "karta": "NIC.Integrated.1", "producent": "Broadcom Corp",
                   "opis": "Broadcom BCM57414 25GbE", "mac": "b0:26:28:aa:bb:02", "lacze": "Down", "predkosc_mbps": None}],
        "kontrolery": [{"nazwa": "PERC H750 Adapter", "opis": "RAID Controller in Slot 3", "firmware": "52.26.0-5179",
                        "cache_mb": 8192, "stan": None, "dyski_wirtualne": 2, "uklady": "1 × RAID-0, 1 × RAID-1",
                        "wirtualne_z_bledem": ["VD1: Degraded"]}],
        "zasilacze": [{"nazwa": "Power Supply 1", "model": "PWR SPLY,750W,RDNT,DELTA", "moc_w": 750,
                       "firmware": "00.30.C2", "numer_seryjny": "CNDED0017U6CR3", "napiecie": 228, "stan": None},
                      {"nazwa": "Power Supply 2", "model": "PWR SPLY,750W,RDNT,DELTA", "moc_w": 750,
                       "firmware": None, "numer_seryjny": None, "napiecie": None, "stan": "Failed"}],
        "karty_pcie": [{"slot": "Video.Embedded.1-1", "producent": "Matrox", "opis": "Integrated Matrox G200eW3"}],
        "licencje": [{"opis": "iDRAC9 Enterprise License", "typ": "Perpetual", "stan": None}]}
    assert wynik["urzadzenia"][1]["zdrowie"] == "CRITICAL" and wynik["urzadzenia"][1]["zasilanie"] == "OFF"
    assert sum(1 for m, s in zapytania if s == "/api/DeviceService/Devices") == 3  # stronicowanie
    # Tylko odczyt: jedyne zapisy to zalozenie i zamkniecie sesji.
    assert [(m, s) for m, s in zapytania if m != "GET"] == [
        ("POST", "/api/SessionService/Sessions"), ("DELETE", "/api/SessionService/Sessions('sesja-1')")]
    assert not sesje


def test_test_polaczenia_czyta_jeden_serwer(tmp_path):
    with falszywe_ome(tmp_path) as (adres, pem, zapytania, _):
        wynik = ome.wykonaj(ome.Ome(adres, "cmdb-ro", "tajne", pem), "test")
    assert wynik["rodzaj"] == "test" and [u["ext_id"] for u in wynik["urzadzenia"]] == ["ABC1234"]
    assert not any(s.endswith("/InventoryDetails") for _, s in zapytania)


def test_bledy(tmp_path):
    with falszywe_ome(tmp_path) as (adres, pem, _, _s):
        with pytest.raises(ome.BladOme, match="odrzucil dane logowania.*password is invalid"):
            ome.wykonaj(ome.Ome(adres, "cmdb-ro", "zle", pem), "test")
        with pytest.raises(ome.BladOme, match="nie jest zaufany"):
            ome.wykonaj(ome.Ome(adres, "cmdb-ro", "tajne"), "test")
        # Certyfikat appliance bez zgodnej nazwy - przypiety przechodzi.
        assert ome.wykonaj(ome.Ome(adres.replace("localhost", "127.0.0.1"), "cmdb-ro", "tajne", pem), "test")["ok"]


def test_rozmiar_dysku_z_napisu():
    assert ome._rozmiar_bajty("698.64") == int(698.64 * 2**30)  # OME 4.x: bez jednostki, GB
    assert ome._rozmiar_bajty("558.38 GB") == int(558.38 * 2**30)
    assert ome._rozmiar_bajty("1,75 TB") == int(1.75 * 2**40)
    assert ome._rozmiar_bajty("1,788.50") == int(1788.5 * 2**30)
    assert ome._rozmiar_bajty("3,840") == 3840 * 2**30
    assert ome._rozmiar_bajty("0") is None
    assert ome._rozmiar_bajty("") is None and ome._rozmiar_bajty("n/a") is None


def test_czytnik_ome_wysyla_wynik_na_wlasny_adres(tmp_path):
    with falszywe_ome(tmp_path) as (adres, pem, _, _s):
        polityka = {"enabled": True, "test": False, "adres": adres, "uzytkownik": "cmdb-ro", "haslo": "tajne",
                    "ca_pem": pem, "interwal_sekund": 3600}
        klient = KlientPolityki(lambda n: odpowiedz(n, dict(polityka)))
        czytnik = ome.CzytnikOme(klient, STAN)
        przebieg(czytnik, 1000.0)
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/ome" and tresc["ok"] and len(tresc["urzadzenia"]) == 230
    assert "tajne" not in json.dumps(tresc) and czytnik.status()["wlaczony"] is True
