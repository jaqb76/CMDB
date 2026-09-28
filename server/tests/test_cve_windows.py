"""Podatnosci Windows z biuletynow MSRC (format CVRF)."""
from __future__ import annotations

import json

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetCurrentReport, CveEntry, CveFeed, InventorySnapshot, utcnow
from cmdb_server.services import cve, cve_windows

from .test_tenant_isolation import _login

# Okrojony biuletyn w ksztalcie api.msrc.microsoft.com/cvrf/v3.0/cvrf/{id}.
BIULETYN = {
    "DocumentTracking": {"Identification": {"ID": {"Value": "2026-Sep"}}},
    "ProductTree": {"FullProductName": [
        {"ProductID": "11923", "Value": "Windows Server 2022"},
        {"ProductID": "11924", "Value": "Windows Server 2022 (Server Core installation)"},
        {"ProductID": "12242", "Value": "Windows 11 Version 23H2 for x64-based Systems"},
        {"ProductID": "11954", "Value": ".NET Framework 4.8 on Windows Server 2022"},
    ]},
    "Vulnerability": [
        {
            "CVE": "CVE-2026-1001",
            "Title": {"Value": "Windows Kernel Elevation of Privilege Vulnerability"},
            "Threats": [{"Type": 3, "Description": {"Value": "Important"},
                         "ProductID": ["11923", "11924", "12242"]}],
            "CVSSScoreSets": [{"BaseScore": 7.8, "Vector": "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
                               "ProductID": ["11923", "11924", "12242"]}],
            "Remediations": [
                {"Type": 2, "Description": {"Value": "5042881"}, "FixedBuild": "10.0.20348.2700",
                 "ProductID": ["11923", "11924"]},
                {"Type": 2, "Description": {"Value": "5043076"}, "FixedBuild": "10.0.22631.4169",
                 "ProductID": ["12242"]},
                # Obejscie, a nie poprawka - nie liczy sie.
                {"Type": 0, "Description": {"Value": "Wylacz usluge"}, "ProductID": ["11923"]},
            ],
        },
        {
            "CVE": "CVE-2026-1002",
            "Title": {"Value": ".NET Remote Code Execution Vulnerability"},
            "Threats": [{"Type": 3, "Description": {"Value": "Critical"}, "ProductID": ["11954"]}],
            "CVSSScoreSets": [{"BaseScore": 9.8, "ProductID": ["11954"]}],
            "Remediations": [
                # .NET ma wlasne numery wersji - to nie jest kompilacja systemu.
                {"Type": 2, "Description": {"Value": "5044020"}, "FixedBuild": "4.8.4739.0",
                 "ProductID": ["11954"]},
            ],
        },
        {
            "CVE": "CVE-2026-1003",
            "Title": {"Value": "Windows TCP/IP Remote Code Execution Vulnerability"},
            "Threats": [{"Type": 3, "Description": {"Value": "Critical"}, "ProductID": ["11923"]}],
            "CVSSScoreSets": [{"BaseScore": 9.8, "ProductID": ["11923"]}],
            "Remediations": [
                # Zwykla aktualizacja i hotpatch - wystarczy dogonic ktorakolwiek.
                {"Type": 2, "Description": {"Value": "5042881"}, "FixedBuild": "10.0.20348.2700",
                 "ProductID": ["11923"]},
                {"Type": 2, "Description": {"Value": "5042999"}, "FixedBuild": "10.0.20348.2655",
                 "ProductID": ["11923"]},
            ],
        },
    ],
}

LISTA = {"value": [{"ID": "2026-Sep", "InitialReleaseDate": "2026-09-08T07:00:00Z",
                    "CurrentReleaseDate": "2026-09-08T07:00:00Z",
                    "CvrfUrl": "https://api.msrc.microsoft.com/cvrf/v3.0/cvrf/2026-Sep"}]}


class Siec:
    """Zamiast api.msrc.microsoft.com - liczy, co pobrano."""

    def __init__(self):
        self.adresy = []

    def __call__(self, adres, naglowki=None):
        self.adresy.append(adres)
        return json.dumps(LISTA if adres == cve_windows.ADRES_LISTY else BIULETYN).encode()


def _raport(kompilacja="20348", poprawka="2600", nazwa="Microsoft Windows Server 2022 Standard"):
    system = {"name": nazwa, "version": f"10.0.{kompilacja}", "build": kompilacja}
    if poprawka is not None:
        system["build_revision"] = poprawka
    return {"os": system, "software": {"packages": []}}


def _pobierz_kanal():
    siec = Siec()
    with SessionLocal() as db:
        cve_windows.odswiez(db, siec)
    return siec


def test_biuletyn_opisuje_tylko_system():
    wpisy = cve_windows.wpisy_biuletynu(BIULETYN)
    assert {w["cve"] for w in wpisy} == {"CVE-2026-1001", "CVE-2026-1003"}
    assert {w["release"] for w in wpisy} == {"20348-server", "22631-client"}
    serwer = next(w for w in wpisy if w["release"] == "20348-server" and w["cve"] == "CVE-2026-1001")
    assert serwer["package"] == "KB5042881"
    assert serwer["fixed_version"] == "10.0.20348.2700"
    assert serwer["severity"] == "Important"
    assert serwer["cvss_score"] == 7.8


def test_wydanie_i_kompilacja_maszyny():
    assert cve_windows.wydanie_maszyny(_raport()) == "20348-server"
    assert cve_windows.wydanie_maszyny(_raport("22631", nazwa="Microsoft Windows 11 Pro")) == "22631-client"
    assert cve_windows.kompilacja_maszyny(_raport()) == "10.0.20348.2600"
    assert cve_windows.kompilacja_maszyny(_raport(poprawka=None)) is None
    assert cve_windows.wydanie_maszyny({"os": {"name": "Ubuntu 24.04"}}) is None


def test_nieaktualna_maszyna_jest_podatna():
    _pobierz_kanal()
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(poprawka="2600"))
    assert wynik["status"] == cve.STATUS_OK
    assert wynik["source"] == "msrc/20348-server"
    assert {p["cve"] for p in wynik["entries"]} == {"CVE-2026-1001", "CVE-2026-1003"}
    assert wynik["critical_count"] == 2
    najgorsza = wynik["entries"][0]
    assert najgorsza["cve"] == "CVE-2026-1003"
    assert najgorsza["base_score"] == 9.8
    assert najgorsza["score_source"] == "Microsoft"
    assert najgorsza["priority_label"] == "Microsoft"
    assert "msrc.microsoft.com" in najgorsza["link"]
    # Aktualizacje sa zbiorcze - jedna pozycja do zainstalowania.
    assert len(wynik["packages_summary"]) == 1
    assert wynik["packages_summary"][0]["fixed_version"] == "10.0.20348.2700"


def test_hotpatch_wystarczy_do_zamkniecia_luki():
    _pobierz_kanal()
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(poprawka="2655"))
    assert {p["cve"] for p in wynik["entries"]} == {"CVE-2026-1001"}


def test_aktualna_maszyna_nie_ma_podatnosci():
    _pobierz_kanal()
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(poprawka="2700"))
    assert wynik["status"] == cve.STATUS_OK
    assert wynik["count"] == 0


def test_bez_numeru_poprawki_wynik_jest_nieznany():
    _pobierz_kanal()
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(poprawka=None))
    assert wynik["status"] == cve.STATUS_NIEZNANY
    assert "UBR" in wynik["detail"]


def test_kompilacja_nieopisana_przez_microsoft_jest_nieznana():
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport("17763"))
    assert wynik["status"] == cve.STATUS_NIEZNANY


def test_niezmieniony_biuletyn_nie_jest_pobierany_ponownie():
    pierwsza = _pobierz_kanal()
    assert len(pierwsza.adresy) == 2
    druga = _pobierz_kanal()
    assert druga.adresy == [cve_windows.ADRES_LISTY]
    with SessionLocal() as db:
        assert db.execute(select(CveEntry).where(CveEntry.source == "msrc")).scalars().all()
        stan = db.execute(select(CveFeed).where(
            CveFeed.source == "msrc", CveFeed.release == "20348-server")).scalar_one()
        assert stan.entries == 3


def test_blad_sieci_zostawia_stare_dane():
    _pobierz_kanal()

    def awaria(adres, naglowki=None):
        raise cve.BladKanalu("brak sieci")

    with SessionLocal() as db:
        assert cve_windows.odswiez(db, awaria).startswith("blad")
        stan = db.execute(select(CveFeed).where(
            CveFeed.source == "msrc", CveFeed.release == "20348-server")).scalar_one()
        assert stan.status == "blad"
        assert stan.fetched_at is not None
        assert cve.dopasuj(db, _raport())["status"] == cve.STATUS_OK


def _maszyna_windows(tenant_id, poprawka="2600"):
    with SessionLocal() as db:
        maszyna = Asset(tenant_id=tenant_id, machine_id="win-1", hostname="srv-win",
                        os_family="windows", os_name="Microsoft Windows Server 2022 Standard")
        db.add(maszyna)
        db.flush()
        db.add(AssetCurrentReport(asset_id=maszyna.id, tenant_id=tenant_id, collected_at=utcnow(),
                                  payload=_raport(poprawka=poprawka), payload_hash="x"))
        db.add(InventorySnapshot(tenant_id=tenant_id, asset_id=maszyna.id, collected_at=utcnow(),
                                 payload_hash="0" * 64, payload=_raport(poprawka=poprawka)))
        db.commit()
        return maszyna.id


def test_flota_z_windows_zamawia_biuletyny(tenant_a):
    _maszyna_windows(tenant_a["id"])
    with SessionLocal() as db:
        assert cve.wydania_we_flocie(db).get("msrc") == {"20348-server"}
        assert "msrc" in cve.kanaly_do_odswiezenia(db, 24)


def test_karta_i_pulpit_pokazuja_podatnosci_windows(client, tenant_a, make_user):
    _pobierz_kanal()
    asset_id = _maszyna_windows(tenant_a["id"])
    make_user(tenant_a["id"], "win@firma.pl", "haslo-do-testow-123")
    _login(client, "win@firma.pl", "haslo-do-testow-123")

    karta = client.get(f"/assets/{asset_id}").text
    assert "CVE-2026-1003" in karta
    assert "Aktualizacja zbiorcza KB5042881" in karta

    pulpit = client.get("/?odswiez=1").text.split("Najbardziej podatne maszyny")[1]
    assert "srv-win" in pulpit


# --- kontekst liczby: okno danych i ostatnia aktualizacja zbiorcza ----------------

def test_maszyna_starsza_niz_dane_dostaje_ostrzezenie():
    """Zgloszenie: 2452 luki na maszynie z poprawkami z 2022, a dane siegaja
    dwoch lat - liczba jest zanizona i karta musi to powiedziec."""
    _pobierz_kanal()
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(poprawka="2600"))
    assert wynik["windows"]["poza_oknem"] is True
    assert wynik["windows"]["dane_od"].strftime("%Y-%m") == "2026-09"


def test_ostatnia_aktualizacja_zbiorcza_z_lista_poprawek():
    _pobierz_kanal()
    raport = _raport(poprawka="2655")
    raport["software"]["updates"] = [{"id": "KB5042999", "installed_on": "2026-09-10T00:00:00Z"}]
    with SessionLocal() as db:
        kontekst = cve.dopasuj(db, raport)["windows"]
    assert kontekst["poza_oknem"] is False
    assert kontekst["ostatnia_zbiorcza"] == "KB5042999"
    assert kontekst["zainstalowano"] == "2026-09-10T00:00:00Z"


def test_karta_ostrzega_o_zanizonej_liczbie(client, tenant_a, make_user):
    _pobierz_kanal()
    asset_id = _maszyna_windows(tenant_a["id"], poprawka="2600")
    make_user(tenant_a["id"], "okno@firma.pl", "haslo-do-testow-123")
    _login(client, "okno@firma.pl", "haslo-do-testow-123")
    assert "Liczba luk jest zaniżona" in client.get(f"/assets/{asset_id}").text
