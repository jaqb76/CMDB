"""Zestawianie inwentarza z danymi o podatnosciach.

Fragmenty kanalow sa przepisane z prawdziwych danych Debiana i Ubuntu.
Najwazniejszy jest test o wersjach pakietow binarnych: na prawdziwych danych
ujawnil falszywe alarmy na pakietach, ktore byly aktualne.
"""
from __future__ import annotations

import bz2
import json

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import CveEntry, CveFeed, utcnow
from cmdb_server.services import cve
from sqlalchemy import select

# Fragment Debian Security Tracker, z zachowana struktura.
DEBIAN = {
    "curl": {
        "CVE-2025-10148": {
            "description": "curl mishandles something",
            "releases": {
                "bookworm": {
                    "status": "resolved",
                    "fixed_version": "7.88.1-10+deb12u15",
                    "urgency": "not yet assigned",
                },
                "bullseye": {
                    "status": "resolved",
                    "fixed_version": "7.74.0-1.3+deb11u14",
                    "urgency": "not yet assigned",
                },
            },
        }
    },
    "coreutils": {
        "CVE-2016-2781": {
            "description": "chroot issue",
            "releases": {
                "bookworm": {"status": "open", "urgency": "unimportant"},
            },
        }
    },
    "389-ds-base": {
        "CVE-2023-1055": {
            "description": "minor",
            "releases": {
                "bookworm": {"status": "open", "urgency": "low", "nodsa": "Minor issue"},
            },
        }
    },
    "nigdy-podatny": {
        "CVE-2020-0001": {
            "description": "nie dotyczy tego wydania",
            "releases": {"bookworm": {"status": "resolved", "fixed_version": "0"}},
        }
    },
    "niepewny": {
        "CVE-2021-0001": {
            "description": "nierozstrzygniete",
            "releases": {"bookworm": {"status": "undetermined", "urgency": "low"}},
        }
    },
}

# Fragment bazy USN. Sekcje "sources" i "allbinaries" celowo maja rozne
# schematy wersji - to wlasnie mieszanie ich dawalo falszywe alarmy.
UBUNTU = {
    "5555-1": {
        "cves": ["CVE-2022-1304"],
        "releases": {
            "jammy": {
                "sources": {"e2fsprogs": {"version": "1.46.5-2ubuntu1.1",
                                          "description": "narzedzia ext"}},
                "allbinaries": {
                    "e2fsprogs": {"version": "1.46.5-2ubuntu1.1", "source": "e2fsprogs"},
                    "comerr-dev": {"version": "2.1-1.46.5-2ubuntu1.1", "source": "e2fsprogs"},
                },
            }
        },
    },
    "6666-1": {
        "cves": ["CVE-2023-40184"],
        "releases": {
            "jammy": {
                "sources": {"xrdp": {"version": "0.9.17-2ubuntu3+esm2",
                                     "description": "serwer RDP"}},
                "allbinaries": {"xrdp": {"version": "0.9.17-2ubuntu3+esm2", "source": "xrdp"}},
            }
        },
    },
    # Ta sama luka w dwoch biuletynach - liczy sie nowsza poprawka.
    "6666-2": {
        "cves": ["CVE-2023-40184"],
        "releases": {
            "jammy": {
                "sources": {"xrdp": {"version": "0.9.17-2ubuntu3+esm1"}},
                "allbinaries": {},
            }
        },
    },
}


def _raport(pakiety, distro_id="debian", codename="bookworm"):
    return {
        "os": {"distro_id": distro_id, "codename": codename},
        "software": {"packages": pakiety},
    }


def _pakiet(name, version, source_package=None, source_version=None):
    return {
        "name": name,
        "version": version,
        "source_package": source_package or name,
        "source_version": source_version or version,
    }


@pytest.fixture()
def kanal_debian():
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "debian", "bookworm",
                         cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"}))
    yield


@pytest.fixture()
def kanal_ubuntu():
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "ubuntu", "jammy",
                         cve.wpisy_ubuntu(bz2.compress(json.dumps(UBUNTU).encode()), {"jammy"}))
    yield


# --- odczyt kanalu Debiana --------------------------------------------------

def test_debian_bierze_tylko_wskazane_wydanie():
    wpisy = cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"})
    assert all(w["release"] == "bookworm" for w in wpisy)
    assert not any(w["release"] == "bullseye" for w in wpisy)


def test_debian_pomija_wpisy_bez_znaczenia():
    """"unimportant" oznacza problem bez praktycznego znaczenia - zmieszany
    z reszta zaszumilby liste tak, ze przestanie byc czytana."""
    wpisy = {w["cve"] for w in cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"})}
    assert "CVE-2016-2781" not in wpisy


def test_debian_pomija_wydania_nigdy_nie_podatne():
    """fixed_version "0" to umowny zapis "to wydanie nigdy nie bylo podatne"."""
    wpisy = {w["cve"] for w in cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"})}
    assert "CVE-2020-0001" not in wpisy


def test_debian_pomija_nierozstrzygniete():
    """Skoro sam Debian nie wie, my tez nie zamieniamy tego na twierdzenie."""
    wpisy = {w["cve"] for w in cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"})}
    assert "CVE-2021-0001" not in wpisy


def test_debian_zapamietuje_powod_odstapienia_od_poprawki():
    wpisy = {w["cve"]: w for w in cve.wpisy_debian(json.dumps(DEBIAN).encode(), {"bookworm"})}
    assert wpisy["CVE-2023-1055"]["no_fix_reason"] == "Minor issue"
    assert wpisy["CVE-2025-10148"]["no_fix_reason"] is None


# --- odczyt kanalu Ubuntu ---------------------------------------------------

def test_ubuntu_uzywa_wersji_zrodlowej_a_nie_binarnej():
    """Sedno bledu wykrytego na prawdziwych danych.

    Z zrodla "e2fsprogs" powstaje binarny "comerr-dev" w wersji
    "2.1-1.46.5-...". Wziecie jej jako wersji poprawki dawalo poprawke
    rzekomo nowsza niz zainstalowana - czyli falszywy alarm na pakiecie,
    ktory byl aktualny.
    """
    wpisy = {w["cve"]: w for w in cve.wpisy_ubuntu(
        bz2.compress(json.dumps(UBUNTU).encode()), {"jammy"})}
    assert wpisy["CVE-2022-1304"]["fixed_version"] == "1.46.5-2ubuntu1.1"
    assert not wpisy["CVE-2022-1304"]["fixed_version"].startswith("2.1-")


def test_ubuntu_zostawia_najnowsza_poprawke():
    """Ta sama luka bywa w kilku biuletynach - starsza poprawka nie
    zamknelaby jej do konca."""
    wpisy = {w["cve"]: w for w in cve.wpisy_ubuntu(
        bz2.compress(json.dumps(UBUNTU).encode()), {"jammy"})}
    assert wpisy["CVE-2023-40184"]["fixed_version"] == "0.9.17-2ubuntu3+esm2"


# --- dopasowanie ------------------------------------------------------------

def test_starsza_wersja_jest_podatna(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u14")]))
    assert wynik["status"] == "ok"
    assert wynik["fixable_count"] == 1
    assert wynik["entries"][0]["cve"] == "CVE-2025-10148"


def test_wersja_z_poprawka_nie_jest_podatna(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u15")]))
    assert wynik["fixable_count"] == 0


def test_nowsza_wersja_nie_jest_podatna(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u16")]))
    assert wynik["fixable_count"] == 0


def test_dopasowanie_po_pakiecie_zrodlowym(kanal_debian):
    """Zainstalowany jest pakiet binarny "libcurl4", a dane sa indeksowane po
    zrodlowym "curl". Bez tego odwzorowania podatnosc nie zostalaby znaleziona."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([
            _pakiet("libcurl4", "7.88.1-10+deb12u14", source_package="curl")
        ]))
    assert wynik["fixable_count"] == 1
    znalezione = wynik["entries"][0]
    assert znalezione["package"] == "libcurl4", "pokazujemy nazwe, ktora widzi administrator"
    assert znalezione["source_package"] == "curl"


def test_porownujemy_wersje_zrodlowa(kanal_debian):
    """Binarna wersja pakietu bywa inna niz zrodlowa. Dane dystrybucji podaja
    zrodlowa, wiec to ja bierzemy do porownania."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([
            _pakiet("libcurl4", "7.88.1-10+deb12u15", source_package="curl",
                    source_version="7.88.1-10+deb12u14")
        ]))
    assert wynik["fixable_count"] == 1, "wersja zrodlowa jest starsza od poprawki"


def test_podatnosc_bez_poprawki_jest_zgloszona(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("389-ds-base", "2.3.1+dfsg1-1")]))
    assert wynik["minor_count"] == 1
    assert wynik["entries"][0]["no_fix_reason"] == "Minor issue"


def test_ubuntu_nie_daje_falszywego_alarmu(kanal_ubuntu):
    """Regresja: wersja 1.46.5-2ubuntu1.2 jest NOWSZA niz poprawka
    1.46.5-2ubuntu1.1, wiec nie moze byc zgloszona jako podatna."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(
            db,
            _raport([_pakiet("e2fsprogs", "1.46.5-2ubuntu1.2")],
                    distro_id="ubuntu", codename="jammy"),
        )
    assert wynik["fixable_count"] == 0, f"falszywy alarm: {wynik['entries']}"


# --- stan nieznany ----------------------------------------------------------

def test_nierozpoznana_dystrybucja_to_stan_nieznany():
    """Dystrybucja, dla ktorej nie mamy danych - zadnego zgadywania."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, {"os": {"name": "Fedora Linux 40"},
                                 "software": {"packages": [_pakiet("curl", "1.0")]}})
    assert wynik["status"] == "nieznany"
    assert wynik["count"] is None, "nie wiemy - nie wolno pokazac zera"
    assert "nie rozpoznaje dystrybucji" in wynik["detail"]


# --- rozpoznanie wydania ----------------------------------------------------
#
# Agenci starsi niz 0.5.4 nie przysylaja VERSION_CODENAME. Odczytujemy je
# wtedy z nazwy systemu, ale tylko tam, gdzie jest jednoznaczna - zla nazwa
# kodowa oznaczalaby porownanie z danymi innego wydania.

@pytest.mark.parametrize(
    "system, oczekiwane",
    [
        # Agent podaje wprost - to ma pierwszenstwo.
        ({"distro_id": "debian", "codename": "trixie", "name": "cokolwiek"},
         ("debian", "trixie")),
        # Debian ma nazwe kodowa wprost w nawiasie.
        ({"name": "Debian GNU/Linux 12 (bookworm)"}, ("debian", "bookworm")),
        # Wersja Ubuntu wyznacza nazwe przez stala tabele.
        ({"name": "Ubuntu 22.04.5 LTS", "version": "22.04"}, ("ubuntu", "jammy")),
        ({"name": "Ubuntu 24.04 LTS", "version": "24.04"}, ("ubuntu", "noble")),
        # Nieznane wydanie Ubuntu - lepiej nic niz zle dane.
        ({"name": "Ubuntu 30.10", "version": "30.10"}, None),
        # Debian bez nazwy kodowej w nazwie.
        ({"name": "Debian GNU/Linux"}, None),
        ({"name": "Fedora Linux 40"}, None),
        ({}, None),
    ],
)
def test_rozpoznanie_wydania(system, oczekiwane):
    assert cve.wydanie_maszyny({"os": system}) == oczekiwane


def test_brak_pobranych_danych_to_stan_nieznany():
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "1.0")],
                                        distro_id="debian", codename="trixie"))
    assert wynik["status"] == "nieznany"
    assert wynik["count"] is None


def test_raport_bez_pakietow_to_stan_nieznany(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([]))
    assert wynik["status"] == "nieznany"


def test_maszyna_bez_podatnosci_ma_stan_ok(kanal_debian):
    """Sprawdzone i czysto to co innego niz nie sprawdzone."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("cokolwiek-innego", "1.0")]))
    assert wynik["status"] == "ok"
    assert wynik["count"] == 0
    assert wynik["fixable_count"] == 0


# --- stan kanalu ------------------------------------------------------------

def test_zapis_kanalu_odnotowuje_stan(kanal_debian):
    with SessionLocal() as db:
        stan = db.execute(
            select(CveFeed).where(CveFeed.source == "debian", CveFeed.release == "bookworm")
        ).scalar_one()
    assert stan.status == "ok"
    assert stan.entries > 0
    assert stan.fetched_at is not None


def test_ponowny_zapis_podmienia_wpisy(kanal_debian):
    """Kanal jest zrodlem prawdy - stare wpisy nie moga zostac obok nowych."""
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "debian", "bookworm", [
            {"package": "curl", "cve": "CVE-2099-0001", "release": "bookworm",
             "fixed_version": "9.9", "status": "resolved", "severity": None,
             "no_fix_reason": None, "description": None},
        ])
        wpisy = db.execute(
            select(CveEntry.cve).where(CveEntry.source == "debian")
        ).scalars().all()
    assert wpisy == ["CVE-2099-0001"]


def test_nieudane_pobranie_nie_kasuje_starych_danych(kanal_debian, monkeypatch):
    """Lepiej dane sprzed tygodnia z widocznym wiekiem niz zadne."""
    def padnij(adres):
        raise cve.BladKanalu("brak lacznosci")

    monkeypatch.setattr(cve, "_pobierz", padnij)
    with SessionLocal() as db:
        cve.odswiez(db, wydania_debian={"bookworm"}, wydania_ubuntu=set())
        wpisy = db.execute(select(CveEntry).where(CveEntry.source == "debian")).scalars().all()
        stan = db.execute(
            select(CveFeed).where(CveFeed.source == "debian")
        ).scalar_one()

    assert wpisy, "wpisy musza zostac"
    assert stan.status == "blad"
    assert "brak lacznosci" in stan.detail
