"""Odczyt Nutanix Prism Central po stronie agenta."""
from __future__ import annotations

import base64
import datetime
import json
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from cmdb_agent import nutanix

# Ksztalt odpowiedzi jak w API v4 (clustermgmt v4.0, vmm v4.0).
KLASTRY = [
    {"extId": "pc-0001", "name": "prism-central",
     "config": {"clusterFunction": ["PRISM_CENTRAL"], "buildInfo": {"version": "pc.2024.3.1"}}},
    {"extId": "00061c8e-aaaa-bbbb-cccc-000000000001", "name": "RCHO-AHV-01",
     "config": {"clusterFunction": ["AOS"], "buildInfo": {"version": "6.10.1"},
                "hypervisorTypes": ["AHV"]},
     "nodes": {"numberOfNodes": 3}},
]
HOSTY = [
    {"extId": "aaaaaaaa-0000-0000-0000-00000000000a", "hostName": "NTNX-A",
     "cluster": {"uuid": "00061c8e-aaaa-bbbb-cccc-000000000001", "name": "RCHO-AHV-01"},
     "blockModel": "NX-3170-G8", "blockSerial": "24SM3A410012", "cpuModel": "Intel Xeon Gold 6342",
     "numberOfCpuSockets": 2, "numberOfCpuCores": 48, "memorySizeBytes": 824633720832,
     "hypervisor": {"fullName": "AHV 20230302.102001", "externalAddress": {"ipv4": {"value": "10.20.0.11"}}},
     "ipmi": {"ip": {"ipv4": {"value": "10.20.1.11"}}}},
]


def vm_v4(numer: int) -> dict:
    return {
        "extId": f"0c53f7eb-5cd6-44d4-7a4c-{numer:012d}", "name": f"vm-{numer:03d}",
        "powerState": "ON", "numSockets": 2, "numCoresPerSocket": 2, "memorySizeBytes": 8 * 2**30,
        "cluster": {"extId": "00061c8e-aaaa-bbbb-cccc-000000000001"},
        "host": {"extId": "aaaaaaaa-0000-0000-0000-00000000000a"},
        "disks": [{"diskAddress": {"busType": "SCSI", "index": 0},
                   "backingInfo": {"diskSizeBytes": 100 * 2**30,
                                   "storageContainer": {"extId": "ctr-1"}}}],
        "nics": [{"backingInfo": {"macAddress": "50:6b:8d:3a:11:c2"},
                  "networkInfo": {"subnet": {"extId": "subnet-120"},
                                  "ipv4Info": {"learnedIpAddresses": [{"value": "10.20.0.50"}]}}}],
        "guestTools": {"isInstalled": True, "guestOsVersion": "Ubuntu 24.04"},
    }


VMY = [vm_v4(i) for i in range(1, 251)]  # trzy strony po 100


# --- splaszczenie ------------------------------------------------------------

def test_splaszczenie_klastra_hosta_i_vm():
    assert nutanix.jest_prism_central(KLASTRY[0]) and not nutanix.jest_prism_central(KLASTRY[1])
    k = nutanix.klaster(KLASTRY[1])
    assert k == {"ext_id": "00061c8e-aaaa-bbbb-cccc-000000000001", "nazwa": "RCHO-AHV-01",
                 "wersja": "6.10.1", "hipernadzorca": "AHV", "liczba_hostow": 3}
    h = nutanix.host(HOSTY[0])
    assert h["klaster_id"] == k["ext_id"] and h["ip"] == "10.20.0.11" and h["ipmi_ip"] == "10.20.1.11"
    assert h["model"] == "NX-3170-G8" and h["ram_bajty"] == 824633720832
    v = nutanix.vm(VMY[0])
    assert v["host_id"] == h["ext_id"] and v["stan"] == "ON" and v["ngt"] is True
    assert v["dyski"] == [{"rozmiar_bajty": 100 * 2**30, "magistrala": "SCSI", "kontener": "ctr-1"}]
    assert v["karty"] == [{"mac": "50:6b:8d:3a:11:c2", "ip": ["10.20.0.50"], "siec": "subnet-120"}]
    assert v["system"] == "Ubuntu 24.04"


def test_brakujace_pola_nie_wywracaja_odczytu():
    assert nutanix.vm({"extId": "x", "disks": [None, "zle"], "nics": "zle"})["dyski"] == []
    assert nutanix.host({"extId": "y"})["nazwa"] == ""
    assert nutanix.klaster({})["ext_id"] is None


# --- polityka ----------------------------------------------------------------

class KlientPolityki:
    def __init__(self, odpowiedz_dla):
        self.odpowiedz_dla = odpowiedz_dla
        self.wyslane = []

    def get(self, sciezka, token):
        nonce = parse_qs(urlsplit(sciezka).query)["nonce"][0]
        return self.odpowiedz_dla(nonce)

    def post(self, sciezka, token, payload):
        self.wyslane.append((sciezka, json.loads(json.dumps(payload))))
        return {"wynik": "przyjeto"}


STAN = SimpleNamespace(is_enrolled=True, agent_token="t", asset_id="asset-1", machine_id="m-1")


def odpowiedz(wartosc, polityka, **zmiany):
    wynik = {"protocol": 1, "nonce": wartosc, "asset_id": "asset-1", "machine_id": "m-1",
             "revision": "rev-1", "policy": polityka,
             "expires_at": (datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=60)).isoformat()}
    wynik.update(zmiany)
    return wynik


POLITYKA = {"enabled": True, "test": False, "adres": "https://prism.firma.pl:9440",
            "uzytkownik": "cmdb-ro", "haslo": "tajne", "ca_pem": "", "interwal_sekund": 3600}


@pytest.mark.parametrize("zmiana", [
    {"nonce": "inny"}, {"asset_id": "cudzy"}, {"machine_id": "inna"},
    {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"policy": dict(POLITYKA, adres="http://prism.firma.pl")},
])
def test_polityka_odrzuca_niezgodna_odpowiedz(zmiana):
    klient = KlientPolityki(lambda n: odpowiedz(n, POLITYKA, **zmiana))
    with pytest.raises(ValueError):
        nutanix.pobierz_polityke(klient, STAN)


def test_polityka_wylaczona_nie_niesie_hasla():
    klient = KlientPolityki(lambda n: odpowiedz(n, {"enabled": False, "test": False}))
    assert "haslo" not in nutanix.pobierz_polityke(klient, STAN)


# --- czytnik w petli monitora -------------------------------------------------

class FalszywyPrism:
    utworzone: list = []

    def __init__(self, adres, uzytkownik, haslo, ca_pem=""):
        FalszywyPrism.utworzone.append((adres, uzytkownik, haslo))

    def lista(self, sciezka, maks_stron=None):
        return {nutanix.SCIEZKA_KLASTROW: KLASTRY, nutanix.SCIEZKA_HOSTOW: HOSTY,
                nutanix.SCIEZKA_VM: VMY[:3]}[sciezka]


class OdmowaPrism(FalszywyPrism):
    def lista(self, sciezka, maks_stron=None):
        raise nutanix.BladPrism("Prism odrzucil dane logowania (HTTP 401) - sprawdz uzytkownika i haslo")


def przebieg(czytnik, teraz):
    czytnik.krok(teraz)
    if czytnik.watek:
        czytnik.watek.join(timeout=5)


def test_czytnik_wykonuje_odczyt_i_nie_powtarza_go_przed_czasem():
    klient = KlientPolityki(lambda n: odpowiedz(n, dict(POLITYKA)))
    czytnik = nutanix.CzytnikNutanix(klient, STAN, fabryka=FalszywyPrism)
    przebieg(czytnik, 1000.0)
    sciezka, tresc = klient.wyslane[0]
    assert sciezka == "/api/v1/agent/nutanix"
    assert tresc["rodzaj"] == "odczyt" and tresc["ok"] and tresc["revision"] == "rev-1"
    assert tresc["wersja_pc"] == "pc.2024.3.1"
    assert [k["nazwa"] for k in tresc["klastry"]] == ["RCHO-AHV-01"]  # bez samego Prism Central
    assert len(tresc["hosty"]) == 1 and len(tresc["vm"]) == 3
    assert "tajne" not in json.dumps(tresc)

    przebieg(czytnik, 1000.0 + nutanix.ODSTEP_POLITYKI)  # polityka tak, odczyt jeszcze nie
    assert len(klient.wyslane) == 1
    przebieg(czytnik, 1000.0 + 3600)
    assert len(klient.wyslane) == 2


def test_odczyt_zlecony_z_panelu_nie_czeka_na_odstep():
    zlecony = {"teraz": False}
    klient = KlientPolityki(lambda n: odpowiedz(n, dict(POLITYKA, odczyt_teraz=zlecony["teraz"])))
    czytnik = nutanix.CzytnikNutanix(klient, STAN, fabryka=FalszywyPrism)
    przebieg(czytnik, 1000.0)
    przebieg(czytnik, 1000.0 + nutanix.ODSTEP_POLITYKI)
    assert len(klient.wyslane) == 1  # zwykly odstep jeszcze nie minal
    zlecony["teraz"] = True
    przebieg(czytnik, 1000.0 + 2 * nutanix.ODSTEP_POLITYKI)
    assert len(klient.wyslane) == 2 and klient.wyslane[1][1]["rodzaj"] == "odczyt"


def test_zlecony_test_czyta_tylko_klastry():
    klient = KlientPolityki(lambda n: odpowiedz(n, dict(POLITYKA, enabled=False, test=True)))
    czytnik = nutanix.CzytnikNutanix(klient, STAN, fabryka=FalszywyPrism)
    przebieg(czytnik, 50.0)
    _, tresc = klient.wyslane[0]
    assert tresc["rodzaj"] == "test" and tresc["ok"] and "vm" not in tresc


def test_blad_logowania_trafia_do_panelu_bez_hasla():
    klient = KlientPolityki(lambda n: odpowiedz(n, dict(POLITYKA)))
    czytnik = nutanix.CzytnikNutanix(klient, STAN, fabryka=OdmowaPrism)
    przebieg(czytnik, 10.0)
    _, tresc = klient.wyslane[0]
    assert tresc["ok"] is False and "HTTP 401" in tresc["blad"] and "tajne" not in json.dumps(tresc)
    assert "401" in czytnik.status()["ostatni_blad"]


def test_brak_serwera_nie_wywraca_czytnika():
    class Padniety(KlientPolityki):
        def get(self, sciezka, token):
            raise OSError("brak sieci")
    czytnik = nutanix.CzytnikNutanix(Padniety(None), STAN, fabryka=FalszywyPrism)
    czytnik.krok(0.0)
    assert czytnik.watek is None


# --- prawdziwe HTTPS: TLS, Basic, stronicowanie ------------------------------

def _certyfikat():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    klucz = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nazwa = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    teraz = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nazwa).issuer_name(nazwa)
            .public_key(klucz.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(teraz - datetime.timedelta(days=1))
            .not_valid_after(teraz + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(klucz, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            klucz.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()).decode())


@contextmanager
def falszywy_prism(tmp_path, haslo="tajne"):
    pem, klucz = _certyfikat()
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "c.key").write_text(klucz)
    dane = {nutanix.SCIEZKA_KLASTROW: KLASTRY, nutanix.SCIEZKA_HOSTOW: HOSTY, nutanix.SCIEZKA_VM: VMY}
    zapytania = []

    class Obsluga(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            adres = urlsplit(self.path)
            zapytania.append((self.command, adres.path))
            oczekiwane = "Basic " + base64.b64encode(f"cmdb-ro:{haslo}".encode()).decode()
            if self.headers.get("Authorization") != oczekiwane:
                self.send_response(401)
                self.end_headers()
                return
            q = parse_qs(adres.query)
            strona, limit = int(q["$page"][0]), int(q["$limit"][0])
            wszystko = dane[adres.path]
            tresc = json.dumps({"data": wszystko[strona * limit:(strona + 1) * limit],
                                "metadata": {"totalAvailableResults": len(wszystko)}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(tresc)))
            self.end_headers()
            self.wfile.write(tresc)

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


def test_pelny_odczyt_po_https_ze_stronicowaniem(tmp_path):
    with falszywy_prism(tmp_path) as (adres, pem, zapytania):
        wynik = nutanix.wykonaj(nutanix.PrismCentral(adres, "cmdb-ro", "tajne", pem), "odczyt")
    assert len(wynik["vm"]) == 250 and len(wynik["hosty"]) == 1 and len(wynik["klastry"]) == 1
    assert {metoda for metoda, _ in zapytania} == {"GET"}  # tylko odczyt
    assert sum(1 for _, s in zapytania if s == nutanix.SCIEZKA_VM) == 3


def test_zle_haslo_i_niezaufany_certyfikat_daja_czytelny_blad(tmp_path):
    with falszywy_prism(tmp_path) as (adres, pem, _):
        with pytest.raises(nutanix.BladPrism, match="HTTP 401"):
            nutanix.wykonaj(nutanix.PrismCentral(adres, "cmdb-ro", "zle", pem), "test")
        # Bez wskazanego CA certyfikat self-signed nie przechodzi - i nie ma
        # przelacznika, ktory by to wylaczyl.
        with pytest.raises(nutanix.BladPrism, match="nie jest zaufany"):
            nutanix.wykonaj(nutanix.PrismCentral(adres, "cmdb-ro", "tajne"), "test")
