"""Odswiezanie bez powtarzania pracy: kanaly pobierane warunkowo (ETag)
i oceny CVSS z plikow NVD zamiast zapytan po jednym CVE."""
from __future__ import annotations

import bz2
import gzip
import json
import urllib.error

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import CveEntry, CveFeed, CveScore
from cmdb_server.services import cve
from sqlalchemy import func, select

from tests.test_cve import UBUNTU
from tests.test_cve_rhel import OVAL


class _Odpowiedz:
    def __init__(self, dane: bytes, naglowki: dict):
        self.dane, self.headers = dane, naglowki

    def read(self, *_):
        return self.dane

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Serwer:
    """Udaje serwer plikow: ETag zalezy od tresci, If-None-Match daje 304."""

    def __init__(self, pliki: dict[str, bytes]):
        self.pliki = pliki
        self.zapytania: list[tuple[str, str | None]] = []

    def __call__(self, zadanie, timeout=0):
        adres = zadanie.full_url
        dane = next(v for k, v in self.pliki.items() if adres.endswith(k))
        etag = f'"{hash(dane) & 0xffffffff:x}"'
        warunek = zadanie.get_header("If-none-match")
        self.zapytania.append((adres, warunek))
        if warunek == etag:
            raise urllib.error.HTTPError(adres, 304, "Not Modified", {}, None)
        return _Odpowiedz(dane, {"ETag": etag, "Last-Modified": "Tue, 13 Oct 2026 09:00:00 GMT"})


@pytest.fixture()
def serwer(monkeypatch):
    def podstaw(pliki):
        s = _Serwer(pliki)
        monkeypatch.setattr(cve.urllib.request, "urlopen", s)
        return s
    return podstaw


def _liczba_rhel(db) -> int:
    return db.execute(select(func.count()).select_from(CveEntry).where(
        CveEntry.source == "rhel")).scalar_one()


# --- kanaly dystrybucji -----------------------------------------------------

def test_niezmieniony_kanal_nie_jest_pobierany_ani_przepisywany(serwer, monkeypatch):
    s = serwer({"rhel-9.oval.xml.bz2": bz2.compress(OVAL.encode())})
    with SessionLocal() as db:
        assert cve.odswiez(db, {"rhel": {"9"}})["rhel/9"].endswith("wpisow")
        przed = _liczba_rhel(db)

    czytane = []
    monkeypatch.setattr(cve, "wpisy_rhel", lambda *a: czytane.append(a) or [])
    with SessionLocal() as db:
        wynik = cve.odswiez(db, {"rhel": {"9"}})
        stan = db.execute(select(CveFeed).where(CveFeed.source == "rhel")).scalar_one()
        assert _liczba_rhel(db) == przed, "wpisy zostaja"
    assert wynik["rhel/9"] == "bez zmian"
    assert czytane == [], "plik bez zmian nie jest czytany"
    assert s.zapytania[1][1] is not None, "drugie zapytanie jest warunkowe"
    assert stan.status == "ok" and stan.entries == przed


def test_zmieniony_kanal_jest_pobierany_od_nowa(serwer):
    s = serwer({"rhel-9.oval.xml.bz2": bz2.compress(OVAL.encode())})
    with SessionLocal() as db:
        cve.odswiez(db, {"rhel": {"9"}})
    s.pliki["rhel-9.oval.xml.bz2"] = bz2.compress(OVAL.replace("27.el9", "28.el9").encode())
    with SessionLocal() as db:
        assert cve.odswiez(db, {"rhel": {"9"}})["rhel/9"].endswith("wpisow")
        wpis = db.execute(select(CveEntry).where(
            CveEntry.package == "openssl", CveEntry.cve == "CVE-2023-5678")).scalar_one()
    assert wpis.fixed_version == "1:3.0.7-28.el9"


def test_nowa_wersja_odczytu_wymusza_pelne_pobranie(serwer, monkeypatch):
    """Zmiana odczytu (np. poprawki linii RHEL) musi objac dane, ktore
    u dostawcy sie nie zmienily - inaczej czekalaby na jego nastepna zmiane."""
    s = serwer({"rhel-9.oval.xml.bz2": bz2.compress(OVAL.encode())})
    with SessionLocal() as db:
        cve.odswiez(db, {"rhel": {"9"}})
    monkeypatch.setattr(cve, "WERSJA_ODCZYTU", "v-nastepna")
    with SessionLocal() as db:
        assert cve.odswiez(db, {"rhel": {"9"}})["rhel/9"].endswith("wpisow")
    assert s.zapytania[1][1] is None


def test_plik_wspolny_dla_wydan_pobierany_w_calosci_dla_nowego_wydania(serwer):
    s = serwer({"database.json.bz2": bz2.compress(json.dumps(UBUNTU).encode())})
    with SessionLocal() as db:
        cve.odswiez(db, {"ubuntu": {"jammy"}})
        cve.odswiez(db, {"ubuntu": {"jammy", "noble"}})
    assert s.zapytania[1][1] is None, "noble nie ma jeszcze danych - bez warunku"


# --- czytanie plikow NVD ----------------------------------------------------

def _pozycja(numer: str, ocena: float | None = 7.5) -> dict:
    metryki = {"cvssMetricV31": [{"cvssData": {
        "baseScore": ocena, "baseSeverity": "HIGH", "vectorString": "CVSS:3.1/AV:N"}}]} if ocena else {}
    return {"cve": {"id": numer, "published": "2024-01-02T00:00:00.000",
                    "descriptions": [{"lang": "en", "value": f"opis {numer}"}],
                    "metrics": metryki}}


def _plik_nvd(*pozycje: dict) -> bytes:
    return gzip.compress(json.dumps({
        "resultsPerPage": len(pozycje), "format": "NVD_CVE", "version": "2.0",
        "vulnerabilities": list(pozycje)}, indent=1).encode())


def test_czytnik_nvd_przechodzi_przez_granice_porcji(monkeypatch):
    monkeypatch.setattr(cve, "_PORCJA_ODCZYTU", 37)
    numery = [f"CVE-2024-{n:04d}" for n in range(50)]
    plik = _plik_nvd(*(_pozycja(n) for n in numery))
    assert [p["cve"]["id"] for p in cve.podatnosci_z_pliku_nvd(plik)] == numery


def test_uciety_plik_nvd_to_blad_kanalu():
    caly = gzip.decompress(_plik_nvd(_pozycja("CVE-2024-0001"), _pozycja("CVE-2024-0002")))
    with pytest.raises(cve.BladKanalu):
        list(cve.podatnosci_z_pliku_nvd(gzip.compress(caly[: len(caly) // 2])))


def test_oceny_z_plikow_lat_i_pliku_zmian(serwer):
    s = serwer({
        "nvdcve-2.0-2024.json.gz": _plik_nvd(_pozycja("CVE-2024-0001"), _pozycja("CVE-2024-9999")),
        "nvdcve-2.0-2023.json.gz": _plik_nvd(_pozycja("CVE-2023-0001", ocena=None)),
        "nvdcve-2.0-modified.json.gz": _plik_nvd(_pozycja("CVE-2024-0001", ocena=9.8)),
    })
    flota = ["CVE-2024-0001", "CVE-2023-0001", "CVE-2024-0002"]
    with SessionLocal() as db:
        wynik = cve.oceny_z_plikow_nvd(db, flota)
        oceny = {o.cve: o for o in db.execute(select(CveScore)).scalars()}

    assert sorted(wynik["pliki"]) == ["2023", "2024", "modified"]
    # Plik zmian jest pozniej i podnosi ocene.
    assert oceny["CVE-2024-0001"].base_score == 9.8 and oceny["CVE-2024-0001"].found
    assert oceny["CVE-2024-0001"].summary == "opis CVE-2024-0001"
    assert not oceny["CVE-2023-0001"].found, "znany, ale jeszcze bez oceny"
    assert not oceny["CVE-2024-0002"].found, "brak w pliku swojego roku"
    assert "CVE-2024-9999" not in oceny, "spoza floty nie zapisujemy"

    # Drugi obieg: lata sa juz obsluzone, plik zmian sie nie zmienil.
    s.zapytania.clear()
    with SessionLocal() as db:
        wynik = cve.oceny_z_plikow_nvd(db, flota)
    assert wynik["pliki"] == [] and wynik["bez_zmian"] == ["modified"]
    assert [a for a, _ in s.zapytania] == [cve.ADRES_NVD_PLIK.format(nazwa="modified")]


def test_bez_plikow_nvd_oceny_z_api(monkeypatch):
    def padnij(db, cves, postep=None):
        raise cve.BladKanalu("nvd.nist.gov nie odpowiada")

    api = []
    monkeypatch.setattr(cve, "oceny_z_plikow_nvd", padnij)
    monkeypatch.setattr(cve, "pobierz_oceny",
                        lambda db, cves, klucz_api="", postep=None: api.append(cves) or {"pobrane": 0})
    with SessionLocal() as db:
        cve.oceny_nvd(db, ["CVE-2024-0001"], klucz_api="k")
    assert api == [["CVE-2024-0001"]]
