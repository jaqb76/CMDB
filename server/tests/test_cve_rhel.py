"""Podatnosci na RHEL i pochodnych oraz samodzielne odswiezanie kanalow.

Fragment OVAL odwzorowuje strukture plikow OVAL v2 Red Hata: definicje RHSA
z kryteriami "pakiet is earlier than epoka:wersja-wydanie", testy podpisu
i warunki modulow.
"""
from __future__ import annotations

import bz2
from datetime import timedelta

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, CveEntry, CveFeed, InventorySnapshot, utcnow
from cmdb_server.services import cve, harmonogram
from cmdb_server.services.wersje_pakietow import (
    dzialajace_jadro_starsze_rpm,
    porownaj_rpm,
    starsza_niz_rpm,
)
from sqlalchemy import select

OVAL = """<?xml version="1.0" encoding="UTF-8"?>
<oval_definitions xmlns="http://oval.mitre.org/XMLSchema/oval-definitions-5"
    xmlns:red-def="http://oval.mitre.org/XMLSchema/oval-definitions-5#linux">
 <definitions>
  <definition class="patch" id="oval:com.redhat.rhsa:def:20243339" version="1">
   <metadata>
    <title>RHSA-2024:3339: openssl security update (Moderate)</title>
    <reference ref_id="RHSA-2024:3339" source="RHSA"/>
    <reference ref_id="CVE-2023-5678" source="CVE"/>
    <advisory from="secalert@redhat.com">
     <severity>Moderate</severity>
     <cve cvss3="5.3/CVSS:3.1/AV:N" impact="low" public="20231106">CVE-2023-5678</cve>
     <cve cvss3="5.5/CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H" public="20240125">CVE-2024-0727</cve>
    </advisory>
   </metadata>
   <criteria operator="OR">
    <criterion comment="Red Hat Enterprise Linux must be installed" test_ref="t1"/>
    <criteria operator="AND">
     <criterion comment="openssl is earlier than 1:3.0.7-27.el9" test_ref="t2"/>
     <criterion comment="openssl is signed with Red Hat redhatrelease2 key" test_ref="t3"/>
    </criteria>
    <criteria operator="AND">
     <criterion comment="openssl-libs is earlier than 1:3.0.7-27.el9" test_ref="t4"/>
    </criteria>
   </criteria>
  </definition>
  <definition class="patch" id="oval:com.redhat.rhsa:def:20240001" version="1">
   <metadata>
    <title>RHSA-2024:0001: nodejs:20 security update (Important)</title>
    <advisory><severity>Important</severity><cve>CVE-2024-1111</cve></advisory>
   </metadata>
   <criteria operator="AND">
    <criterion comment="Module nodejs:20 is enabled" test_ref="t5"/>
    <criterion comment="nodejs is earlier than 1:20.11.1-1.module+el9.3.0+21363+3b8c7a1b" test_ref="t6"/>
   </criteria>
  </definition>
  <definition class="patch" id="oval:com.redhat.rhsa:def:20240002" version="1">
   <metadata>
    <title>RHSA-2024:0002: kernel security update (Important)</title>
    <advisory><severity>Important</severity><cve>CVE-2024-2222</cve></advisory>
   </metadata>
   <criteria operator="OR">
    <criterion comment="kernel-core is earlier than 0:5.14.0-427.16.1.el9_4" test_ref="t7"/>
    <criterion comment="kernel-tools is earlier than 0:5.14.0-427.16.1.el9_4" test_ref="t8"/>
   </criteria>
  </definition>
  <definition class="patch" id="oval:com.redhat.rhsa:def:20240003" version="1">
   <metadata>
    <title>RHSA-2024:0003: php:8.3 security update (Important)</title>
    <advisory><severity>Important</severity>
     <cve cvss3="8.8/CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H" cwe="(CWE-787|CWE-20)">CVE-2024-11235</cve></advisory>
   </metadata>
   <criteria operator="OR">
    <criteria operator="AND">
     <criterion comment="Module php:8.3 is enabled" test_ref="t10"/>
     <criteria operator="OR">
      <criterion comment="php is earlier than 0:8.3.19-1.module+el9.6.0+23015+da8065b7" test_ref="t11"/>
      <criterion comment="php-pecl-apcu is earlier than 0:5.1.23-1.module+el9.6.0+22647+1741ae35" test_ref="t12"/>
     </criteria>
    </criteria>
    <criteria operator="AND">
     <criterion comment="Module php:8.2 is enabled" test_ref="t13"/>
     <criterion comment="php is earlier than 0:8.2.28-1.module+el9.6.0+23016+aaaaaaaa" test_ref="t14"/>
    </criteria>
   </criteria>
  </definition>
  <definition class="inventory" id="oval:com.redhat.rhba:def:1" version="1">
   <metadata><title>Red Hat Enterprise Linux 9 is installed</title></metadata>
   <criteria><criterion comment="redhat-release is earlier than 0:99" test_ref="t9"/></criteria>
  </definition>
 </definitions>
 <tests>
  <red-def:rpminfo_test check="at least one" id="t2" version="1">
   <red-def:object object_ref="o1"/><red-def:state state_ref="s1"/>
  </red-def:rpminfo_test>
 </tests>
 <objects><red-def:rpminfo_object id="o1" version="1"><red-def:name>openssl</red-def:name></red-def:rpminfo_object></objects>
 <states><red-def:rpminfo_state id="s1" version="1"><red-def:evr datatype="evr_string" operation="less than">1:3.0.7-27.el9</red-def:evr></red-def:rpminfo_state></states>
</oval_definitions>
"""


def _surowe() -> bytes:
    return bz2.compress(OVAL.encode())


@pytest.fixture()
def kanal_rhel():
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "rhel", "9", cve.wpisy_rhel(_surowe(), "9"))
    yield


def _pakiet(name, version, evr=None, source_package=None):
    return {"name": name, "version": version, "evr": evr,
            "source_package": source_package or name, "source_version": version}


def _raport(pakiety, distro_id="rhel", version="9.4", kernel=""):
    return {"os": {"distro_id": distro_id, "version": version, "kernel": kernel},
            "software": {"packages": pakiety}}


# --- porownywanie wersji RPM -------------------------------------------------

@pytest.mark.parametrize(
    "mniejsza, wieksza",
    [
        ("1.0", "1.1"),
        ("1.9", "1.10"),
        ("1.0~rc1", "1.0"),
        ("1.0", "1.0^git1"),
        ("1.0^git1", "1.0.1"),
        ("1.0a", "1.0.1"),          # segment liczbowy nowszy niz literowy
        ("3.0.7-24.el9", "3.0.7-27.el9"),
        ("3.0.7-27.el9", "3.0.7-27.el9_4"),
        ("0:9.9-1", "1:1.0-1"),
        ("5.14.0-427.13.1.el9_4", "5.14.0-427.16.1.el9_4"),
    ],
)
def test_kolejnosc_wersji_rpm(mniejsza, wieksza):
    assert porownaj_rpm(mniejsza, wieksza) == -1
    assert porownaj_rpm(wieksza, mniejsza) == 1


def test_separatory_rpm_sa_rownowazne():
    assert porownaj_rpm("1.0_1", "1.0.1") == 0
    assert porownaj_rpm("1:1.0-1", "1:1.0-1") == 0


def test_nieznana_epoka_nie_robi_falszywego_alarmu():
    """Stary agent nie podaje epoki - "3.0.7-27.el9" to NIE "0:3.0.7-27.el9"."""
    assert not starsza_niz_rpm("3.0.7-27.el9", "1:3.0.7-27.el9")
    assert starsza_niz_rpm("3.0.7-24.el9", "1:3.0.7-27.el9")
    # Znana epoka jest porownywana normalnie.
    assert starsza_niz_rpm("0:3.0.7-27.el9", "1:3.0.7-27.el9")


def test_dzialajace_jadro_rhel():
    assert dzialajace_jadro_starsze_rpm(
        "5.14.0-427.13.1.el9_4.x86_64", "0:5.14.0-427.16.1.el9_4") is True
    assert dzialajace_jadro_starsze_rpm(
        "5.14.0-427.18.1.el9_4.x86_64", "0:5.14.0-427.16.1.el9_4") is False
    assert dzialajace_jadro_starsze_rpm(
        "5.14.0-427.18.1.el9_4.x86_64+debug", "0:5.14.0-427.16.1.el9_4") is False
    assert dzialajace_jadro_starsze_rpm("", "0:5.14.0-1.el9") is None


# --- odczyt OVAL -------------------------------------------------------------

def test_oval_daje_wpisy_po_pakiecie_binarnym():
    wpisy = {(w["package"], w["cve"]): w for w in cve.wpisy_rhel(_surowe(), "9")}
    assert ("openssl", "CVE-2023-5678") in wpisy
    assert ("openssl-libs", "CVE-2024-0727") in wpisy
    wpis = wpisy[("openssl-libs", "CVE-2023-5678")]
    assert wpis["fixed_version"] == "1:3.0.7-27.el9"
    assert wpis["status"] == "resolved"
    assert wpis["release"] == "9"
    assert wpis["description"].startswith("RHSA-2024:3339")


def test_oval_waga_z_cve_ma_pierwszenstwo_przed_biuletynem():
    wpisy = {(w["package"], w["cve"]): w for w in cve.wpisy_rhel(_surowe(), "9")}
    assert wpisy[("openssl", "CVE-2023-5678")]["severity"] == "Low"
    assert wpisy[("openssl", "CVE-2024-0727")]["severity"] == "Moderate"


def test_oval_pomija_definicje_inne_niz_poprawki():
    pakiety = {w["package"] for w in cve.wpisy_rhel(_surowe(), "9")}
    assert "redhat-release" not in pakiety
    # Kryterium podpisu to nie pakiet.
    assert all(" " not in p for p in pakiety)


def test_uszkodzony_oval_to_blad_kanalu():
    with pytest.raises(cve.BladKanalu):
        cve.wpisy_rhel(bz2.compress(b"<oval_definitions><definitions>"), "9")
    with pytest.raises(cve.BladKanalu):
        cve.wpisy_rhel(b"to nie jest bz2", "9")


# --- rozpoznanie wydania -----------------------------------------------------

@pytest.mark.parametrize(
    "system, oczekiwane",
    [
        ({"distro_id": "rhel", "version": "9.4"}, ("rhel", "9")),
        ({"distro_id": "rocky", "version": "8.10"}, ("rhel", "8")),
        ({"distro_id": "almalinux", "version": "9.3"}, ("rhel", "9")),
        ({"distro_id": "centos", "version": "7"}, ("rhel", "7")),
        ({"name": "Red Hat Enterprise Linux 9.4 (Plow)", "version": "9.4"}, ("rhel", "9")),
        ({"distro_id": "ol", "version": "9.4"}, None),
        ({"distro_id": "fedora", "version": "40"}, None),
        ({"distro_id": "rhel", "version": ""}, None),
    ],
)
def test_rozpoznanie_rodziny_rhel(system, oczekiwane):
    assert cve.wydanie_maszyny({"os": system}) == oczekiwane


# --- dopasowanie -------------------------------------------------------------

def test_rhel_starsza_wersja_jest_podatna(kanal_rhel):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([
            _pakiet("openssl-libs", "3.0.7-24.el9", evr="1:3.0.7-24.el9",
                    source_package="openssl"),
            _pakiet("openssl", "3.0.7-24.el9", evr="1:3.0.7-24.el9"),
        ]))
    assert wynik["status"] == "ok"
    assert wynik["source"] == "rhel/9"
    # Dwa binaria z jednego zrodla - jedna pozycja na CVE.
    assert {e["cve"] for e in wynik["entries"]} == {"CVE-2023-5678", "CVE-2024-0727"}
    pozycja = wynik["entries"][0]
    assert pozycja["packages"] == ["openssl", "openssl-libs"]
    assert pozycja["link"].startswith("https://access.redhat.com/security/cve/")


def test_rhel_wersja_z_poprawka_nie_jest_podatna(kanal_rhel):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([
            _pakiet("openssl-libs", "3.0.7-27.el9_4", evr="1:3.0.7-27.el9_4",
                    source_package="openssl"),
        ]))
    assert wynik["fixable_count"] == 0


def test_rhel_stary_agent_bez_epoki(kanal_rhel):
    with SessionLocal() as db:
        aktualny = cve.dopasuj(db, _raport([_pakiet("openssl-libs", "3.0.7-27.el9")]))
        stary = cve.dopasuj(db, _raport([_pakiet("openssl-libs", "3.0.7-20.el9")]))
    assert aktualny["fixable_count"] == 0
    assert stary["fixable_count"] == 2


def test_rocky_korzysta_z_danych_rhel(kanal_rhel):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(
            [_pakiet("openssl-libs", "3.0.7-24.el9", evr="1:3.0.7-24.el9")],
            distro_id="rocky", version="9.4",
        ))
    assert wynik["fixable_count"] == 2


def test_poprawka_modulu_nie_dotyczy_innego_strumienia(kanal_rhel):
    with SessionLocal() as db:
        inny_strumien = cve.dopasuj(db, _raport([_pakiet(
            "nodejs", "18.19.0-1.module+el9.3.0+1+a",
            evr="1:18.19.0-1.module+el9.3.0+1+a")]))
        ten_strumien = cve.dopasuj(db, _raport([_pakiet(
            "nodejs", "20.10.0-1.module+el9.3.0+1+a",
            evr="1:20.10.0-1.module+el9.3.0+1+a")]))
        spoza_modulu = cve.dopasuj(db, _raport([_pakiet(
            "nodejs", "16.20.2-1.el9", evr="1:16.20.2-1.el9")]))
    assert inny_strumien["fixable_count"] == 0
    assert ten_strumien["fixable_count"] == 1
    assert spoza_modulu["fixable_count"] == 0


def test_rhel_stare_jadro_na_dysku_nie_jest_podatne(kanal_rhel):
    """dnf trzyma trzy jadra - stare kernel-core lezy na dysku, ale nie dziala."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(
            [
                _pakiet("kernel-core", "5.14.0-427.13.1.el9_4",
                        evr="0:5.14.0-427.13.1.el9_4", source_package="kernel"),
                _pakiet("kernel-core", "5.14.0-427.18.1.el9_4",
                        evr="0:5.14.0-427.18.1.el9_4", source_package="kernel"),
            ],
            kernel="5.14.0-427.18.1.el9_4.x86_64",
        ))
    assert wynik["fixable_count"] == 0
    assert wynik["stale_kernel_count"] == 1


def test_rhel_narzedzia_jadra_nie_podlegaja_regule_jadra(kanal_rhel):
    """kernel-tools jest w jednej wersji - stary znaczy nieaktualny."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(
            [_pakiet("kernel-tools", "5.14.0-427.13.1.el9_4",
                     evr="0:5.14.0-427.13.1.el9_4", source_package="kernel")],
            kernel="5.14.0-427.18.1.el9_4.x86_64",
        ))
    assert wynik["fixable_count"] == 1


# --- pobieranie i harmonogram ------------------------------------------------

def test_odswiez_pobiera_osobny_plik_na_wydanie_rhel(monkeypatch):
    adresy = []

    def pobierz(adres):
        adresy.append(adres)
        return _surowe()

    monkeypatch.setattr(cve, "_pobierz", pobierz)
    with SessionLocal() as db:
        podsumowanie = cve.odswiez(db, {"rhel": {"8", "9"}})
        stany = {k.release: k for k in db.execute(
            select(CveFeed).where(CveFeed.source == "rhel")).scalars()}
    assert adresy == [cve.ADRES_RHEL.format(wydanie="8"), cve.ADRES_RHEL.format(wydanie="9")]
    assert set(podsumowanie) == {"rhel/8", "rhel/9"}
    assert stany["9"].status == "ok" and stany["9"].entries > 0


def test_blad_jednego_wydania_rhel_nie_psuje_drugiego(monkeypatch):
    def pobierz(adres):
        if "RHEL8" in adres:
            raise cve.BladKanalu("brak lacznosci")
        return _surowe()

    monkeypatch.setattr(cve, "_pobierz", pobierz)
    with SessionLocal() as db:
        cve.odswiez(db, {"rhel": {"8", "9"}})
        stany = {k.release: k.status for k in db.execute(
            select(CveFeed).where(CveFeed.source == "rhel")).scalars()}
    assert stany == {"8": "blad", "9": "ok"}


def _maszyna(db, distro_id="rhel", version="9.4"):
    from cmdb_server.models import Tenant

    firma = db.execute(select(Tenant)).scalars().first()
    if firma is None:
        firma = Tenant(name="Firma", slug="firma")
        db.add(firma)
        db.flush()
    maszyna = Asset(tenant_id=firma.id, machine_id=f"id-{distro_id}",
                    hostname=f"srv-{distro_id}", os_family="linux")
    db.add(maszyna)
    db.flush()
    db.add(InventorySnapshot(
        tenant_id=firma.id, asset_id=maszyna.id, collected_at=utcnow(),
        payload_hash="0" * 64,
        payload={"os": {"distro_id": distro_id, "version": version}},
    ))
    db.commit()


def test_do_odswiezenia_trafiaja_tylko_stare_i_brakujace():
    with SessionLocal() as db:
        _maszyna(db, "rhel", "9.4")
        _maszyna(db, "rocky", "8.10")
        cve.zapisz_kanal(db, "rhel", "9", [])
        assert cve.kanaly_do_odswiezenia(db, 24) == {"rhel": {"8"}}

        stan = db.execute(select(CveFeed).where(CveFeed.release == "9")).scalar_one()
        stan.fetched_at = utcnow() - timedelta(hours=30)
        db.commit()
        assert cve.kanaly_do_odswiezenia(db, 24) == {"rhel": {"8", "9"}}


def test_harmonogram_sam_pobiera_nieaktualne_kanaly(monkeypatch):
    monkeypatch.setattr(cve, "odswiez_cwe", lambda db: None)
    wywolania = []
    monkeypatch.setattr(cve, "odswiez", lambda db, wydania: wywolania.append(wydania) or {})
    monkeypatch.setattr(cve, "cve_we_flocie", lambda db, source=None: [])
    with SessionLocal() as db:
        _maszyna(db, "rhel", "9.4")

    wynik = harmonogram.przebieg_podatnosci(24)
    assert wywolania == [{"rhel": {"9"}}]
    assert wynik["oceny"] is None

    # Swieze dane - drugi obieg niczego nie pobiera.
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "rhel", "9", [])
    wywolania.clear()
    harmonogram.przebieg_podatnosci(24)
    assert wywolania == []


def test_harmonogram_dobiera_oceny(monkeypatch):
    monkeypatch.setattr(cve, "odswiez_cwe", lambda db: None)
    oceny = []
    monkeypatch.setattr(cve, "kanaly_do_odswiezenia", lambda db, h: {})
    monkeypatch.setattr(cve, "cve_we_flocie",
                        lambda db, source=None: [] if source else ["CVE-2024-0727"])
    monkeypatch.setattr(cve, "pobierz_oceny",
                        lambda db, cves, klucz_api="", **_: oceny.append((cves, klucz_api)) or {"pobrane": 1})
    wynik = harmonogram.przebieg_podatnosci(24, "klucz")
    assert oceny == [(["CVE-2024-0727"], "klucz")]
    assert wynik["oceny"] == {"pobrane": 1}


def test_zapis_rhel_trafia_do_bazy(kanal_rhel):
    with SessionLocal() as db:
        liczba = len(db.execute(
            select(CveEntry).where(CveEntry.source == "rhel")).scalars().all())
    assert liczba == 10


def test_ocena_red_hat_gdy_nvd_nie_ocenil(kanal_rhel):
    """Red Hat podaje CVSS w OVAL - nie trzeba czekac na NVD."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([
            _pakiet("openssl-libs", "3.0.7-24.el9", evr="1:3.0.7-24.el9", source_package="openssl")]))
    pozycja = next(e for e in wynik["entries"] if e["cve"] == "CVE-2024-0727")
    assert pozycja["base_score"] == 5.5
    assert pozycja["score_source"] == "Red Hat"
    assert pozycja["priority_label"] == "Red Hat"
    assert pozycja["priority"] == "Moderate"


def test_poprawka_dla_php_83_nie_dotyczy_php_82(kanal_rhel):
    """Zgloszony przypadek: php 8.2 z modulu porownywany z poprawka dla 8.3."""
    pakiety = [
        _pakiet("php", "8.2.30-1.module+el9.7.0+23849+7b831f17",
                evr="0:8.2.30-1.module+el9.7.0+23849+7b831f17"),
        _pakiet("php-pecl-apcu", "5.1.23-1.module+el9.4.0+20748+b46899d2",
                evr="0:5.1.23-1.module+el9.4.0+20748+b46899d2"),
    ]
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(pakiety))
    assert wynik["fixable_count"] == 0


def test_poprawka_modulu_dotyczy_tego_samego_strumienia(kanal_rhel):
    pakiety = [
        _pakiet("php", "8.3.10-1.module+el9.5.0+1+a", evr="0:8.3.10-1.module+el9.5.0+1+a"),
        _pakiet("php-pecl-apcu", "5.1.22-1.module+el9.5.0+1+a",
                evr="0:5.1.22-1.module+el9.5.0+1+a"),
    ]
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport(pakiety))
    pozycje = [e for e in wynik["entries"] if e["cve"] == "CVE-2024-11235"]
    assert {n for e in pozycje for n in e["packages"]} == {"php", "php-pecl-apcu"}
    assert pozycje[0]["base_score"] == 8.8


def test_przebieg_zapisuje_stan_dla_panelu(monkeypatch):
    from cmdb_server.models import CveSyncStatus

    monkeypatch.setattr(cve, "odswiez_cwe", lambda db: None)
    monkeypatch.setattr(cve, "kanaly_do_odswiezenia", lambda db, h: {})
    monkeypatch.setattr(cve, "cve_we_flocie", lambda db, source=None: [] if source else ["CVE-1"])

    def oceny(db, cves, klucz_api="", postep=None, **_):
        postep(1, 2)
        with SessionLocal() as inna:
            stan = inna.get(CveSyncStatus, 1)
            assert (stan.state, stan.phase, stan.done, stan.total) == ("running", "oceny NVD", 1, 2)
        return {"pobrane": 1, "bez_oceny": 0, "bledy": 0, "pozostalo": 5}

    monkeypatch.setattr(cve, "pobierz_oceny", oceny)
    harmonogram.przebieg_podatnosci(24)
    with SessionLocal() as db:
        stan = db.get(CveSyncStatus, 1)
    assert stan.state == "ok"
    assert "zostało 5" in stan.detail


def test_przycisk_ocen_nie_czeka_na_nvd(client, make_user, monkeypatch):
    """Pobieranie ocen trwa do kilkudziesieciu minut - nginx przerywal je 504."""
    from .test_admin import _superadmin

    uruchomione = []
    monkeypatch.setattr(harmonogram, "uruchom_w_tle",
                        lambda klucz, kanaly, oceny: uruchomione.append((kanaly, oceny)))
    csrf = _superadmin(client, make_user)
    odpowiedz = client.post("/admin/podatnosci/oceny", data={"csrf_token": csrf},
                            follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert uruchomione == [(False, True)]
    assert "Pobieranie w tle" in client.get("/admin/podatnosci").text


def test_strona_pokazuje_postep_i_odswieza_sie(client, make_user):
    from cmdb_server.models import CveSyncStatus
    from .test_admin import _superadmin

    _superadmin(client, make_user)
    with SessionLocal() as db:
        db.add(CveSyncStatus(id=1, state="running", phase="oceny NVD", done=40, total=200,
                             started_at=utcnow()))
        db.commit()
    strona = client.get("/admin/podatnosci").text
    assert '<progress value="40" max="200">' in strona
    assert 'http-equiv="refresh"' in strona


KATALOG_CWE = """<?xml version="1.0" encoding="UTF-8"?>
<Weakness_Catalog xmlns="http://cwe.mitre.org/cwe-7" Name="CWE" Version="4.18">
 <Weaknesses>
  <Weakness ID="787" Name="Out-of-bounds Write" Abstraction="Base">
   <Description>The product writes data past the end, or before the beginning, of the intended buffer.</Description>
   <Common_Consequences>
    <Consequence><Scope>Integrity</Scope><Impact>Modify Memory</Impact>
     <Impact>Execute Unauthorized Code or Commands</Impact>
     <Note>Write operations could cause memory corruption.</Note></Consequence>
    <Consequence><Scope>Availability</Scope><Impact>DoS: Crash, Exit, or Restart</Impact></Consequence>
   </Common_Consequences>
  </Weakness>
 </Weaknesses>
</Weakness_Catalog>
"""


def _katalog_zip() -> bytes:
    import io
    import zipfile

    bufor = io.BytesIO()
    with zipfile.ZipFile(bufor, "w") as archiwum:
        archiwum.writestr("cwec_v4.18.xml", KATALOG_CWE)
    return bufor.getvalue()


def test_katalog_cwe_czyta_skutki():
    wpisy = cve.wpisy_cwe(_katalog_zip())
    assert wpisy == [{
        "id": "787", "name": "Out-of-bounds Write",
        "description": "The product writes data past the end, or before the beginning, of the intended buffer.",
        "consequences": [
            {"scopes": ["Integrity"], "impacts": ["Modify Memory", "Execute Unauthorized Code or Commands"],
             "note": "Write operations could cause memory corruption."},
            {"scopes": ["Availability"], "impacts": ["DoS: Crash, Exit, or Restart"], "note": None},
        ],
    }]


def test_katalog_cwe_pobierany_raz_na_miesiac(monkeypatch):
    pobrania = []
    monkeypatch.setattr(cve, "_pobierz", lambda adres: pobrania.append(adres) or _katalog_zip())
    with SessionLocal() as db:
        assert cve.odswiez_cwe(db) == "1 slabosci"
        assert cve.odswiez_cwe(db) is None
    assert pobrania == [cve.ADRES_CWE]


def test_okno_szczegolow_cve(client, make_user, kanal_rhel, monkeypatch):
    from cmdb_server.models import CveScore
    from .test_admin import _superadmin

    monkeypatch.setattr(cve, "_pobierz", lambda adres: _katalog_zip())
    with SessionLocal() as db:
        cve.odswiez_cwe(db)
        db.add(CveScore(cve="CVE-2024-11235", found=False, summary="PHP out-of-bounds write in X."))
        db.commit()
    _superadmin(client, make_user)

    fragment = client.get("/podatnosci/cve/CVE-2024-11235?zrodlo=rhel/9",
                          headers={"X-Fragment": "1"}).text
    assert "<html" not in fragment
    assert "PHP out-of-bounds write in X." in fragment
    assert "8.8" in fragment and "Red Hat" in fragment and "Important" in fragment
    assert "Out-of-bounds Write" in fragment and "Execute Unauthorized Code" in fragment
    assert "https://access.redhat.com/security/cve/CVE-2024-11235" in fragment
    assert "https://cwe.mitre.org/data/definitions/20.html" in fragment

    strona = client.get("/podatnosci/cve/CVE-2024-11235?zrodlo=rhel/9")
    assert strona.status_code == 200 and "<html" in strona.text
    assert client.get("/podatnosci/cve/nie-cve").status_code == 404
