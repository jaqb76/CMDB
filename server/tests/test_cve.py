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
    # Po zgrupowaniu pozycja opisuje pakiet ZRODLOWY, a nazwy binarne, ktore
    # administrator widzi na maszynie, sa wymienione obok.
    assert znalezione["source_package"] == "curl"
    assert znalezione["packages"] == ["libcurl4"]


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
        cve.odswiez(db, {"debian": {"bookworm"}})
        wpisy = db.execute(select(CveEntry).where(CveEntry.source == "debian")).scalars().all()
        stan = db.execute(
            select(CveFeed).where(CveFeed.source == "debian")
        ).scalar_one()

    assert wpisy, "wpisy musza zostac"
    assert stan.status == "blad"
    assert "brak lacznosci" in stan.detail


# --- oceny CVSS -------------------------------------------------------------

ODPOWIEDZ_NVD = {
    "vulnerabilities": [{"cve": {
        "id": "CVE-2025-10148",
        "published": "2025-09-10T14:15:00.000",
        "metrics": {
            "cvssMetricV31": [{"cvssData": {
                "baseScore": 6.5, "baseSeverity": "MEDIUM",
                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N",
            }}],
            "cvssMetricV2": [{"cvssData": {"baseScore": 4.3, "vectorString": "AV:N/AC:M"}}],
        },
    }}]
}


def _podstaw_nvd(monkeypatch, odpowiedzi):
    """Podstawia NVD i usuwa odstepy - testy nie moga czekac po 6 sekund."""
    kolejka = list(odpowiedzi)

    class _Odpowiedz:
        def __init__(self, dane):
            self.dane = json.dumps(dane).encode()

        def read(self):
            return self.dane

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def otworz(zadanie, timeout=0):
        if not kolejka:
            raise OSError("brak dalszych odpowiedzi")
        return _Odpowiedz(kolejka.pop(0))

    monkeypatch.setattr(cve.urllib.request, "urlopen", otworz)
    monkeypatch.setattr(cve.time, "sleep", lambda _: None)


def test_ocena_pobrana_i_zapamietana(monkeypatch):
    from cmdb_server.models import CveScore

    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        podsumowanie = cve.pobierz_oceny(db, ["CVE-2025-10148"])
        ocena = db.get(CveScore, "CVE-2025-10148")

    assert podsumowanie["pobrane"] == 1
    assert ocena.base_score == 6.5
    assert ocena.severity == "MEDIUM"
    assert ocena.vector.startswith("CVSS:3.1/")


def test_bierzemy_najnowsza_wersje_cvss(monkeypatch):
    """Mieszanie CVSS 2.0 i 3.1 w jednej kolumnie dawaloby liczby
    nieporownywalne miedzy soba."""
    from cmdb_server.models import CveScore

    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        cve.pobierz_oceny(db, ["CVE-2025-10148"])
        assert db.get(CveScore, "CVE-2025-10148").base_score == 6.5


def test_brak_oceny_tez_jest_zapamietywany(monkeypatch):
    """Inaczej przy kazdym odswiezeniu pytalibysmy o te same, nieopisane
    jeszcze podatnosci - a limit NVD to 5 zapytan na 30 sekund."""
    from cmdb_server.models import CveScore

    _podstaw_nvd(monkeypatch, [{"vulnerabilities": []}])
    with SessionLocal() as db:
        podsumowanie = cve.pobierz_oceny(db, ["CVE-2099-0001"])
        ocena = db.get(CveScore, "CVE-2099-0001")

    assert podsumowanie["bez_oceny"] == 1
    assert ocena is not None and ocena.found is False
    assert ocena.base_score is None


def test_znane_oceny_nie_sa_pobierane_ponownie(monkeypatch):
    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        cve.pobierz_oceny(db, ["CVE-2025-10148"])

    # Druga proba bez zadnej odpowiedzi w kolejce - gdyby pytala, wybuchlaby.
    _podstaw_nvd(monkeypatch, [])
    with SessionLocal() as db:
        podsumowanie = cve.pobierz_oceny(db, ["CVE-2025-10148"])
    assert podsumowanie["pobrane"] == 0


def test_przebieg_jest_ograniczony(monkeypatch):
    """Bez limitu pierwsze uruchomienie na duzej flocie trwaloby godzine."""
    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD, ODPOWIEDZ_NVD, ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        podsumowanie = cve.pobierz_oceny(
            db, ["CVE-2025-0001", "CVE-2025-0002", "CVE-2025-0003"], limit=2
        )
    assert podsumowanie["pozostalo"] == 1


def test_seria_bledow_przerywa_pobieranie(monkeypatch):
    """Zwykle oznacza wyczerpany limit zapytan - dalsze proby tylko go pogleboia."""
    monkeypatch.setattr(cve.time, "sleep", lambda _: None)

    def padnij(zadanie, timeout=0):
        raise OSError("429 Too Many Requests")

    monkeypatch.setattr(cve.urllib.request, "urlopen", padnij)
    with SessionLocal() as db:
        podsumowanie = cve.pobierz_oceny(db, [f"CVE-2025-{n:04d}" for n in range(50)])
    assert podsumowanie["bledy"] == 5, "po piatym bledzie przerywamy"


# --- prezentacja ------------------------------------------------------------

def test_odnosnik_prowadzi_do_strony_dystrybucji():
    """Dystrybucja opisuje, co zrobila z konkretnym pakietem - to jest
    uzyteczniejsze niz sam opis luki."""
    assert cve.odnosnik("debian/bookworm", "CVE-2025-1") == \
        "https://security-tracker.debian.org/tracker/CVE-2025-1"
    assert cve.odnosnik("ubuntu/jammy", "CVE-2025-1") == \
        "https://ubuntu.com/security/CVE-2025-1"
    assert "nvd.nist.gov" in cve.odnosnik("cokolwiek", "CVE-2025-1")


def test_znaleziska_niosa_ocene_i_odnosnik(kanal_debian, monkeypatch):
    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        cve.pobierz_oceny(db, ["CVE-2025-10148"])
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u14")]))

    znalezione = wynik["entries"][0]
    assert znalezione["base_score"] == 6.5
    assert znalezione["cvss_severity"] == "MEDIUM"
    assert znalezione["link"].endswith("CVE-2025-10148")
    assert wynik["critical_count"] == 0, "6.5 to nie jest powazna wedlug progu 7.0"


def test_podatnosc_bez_oceny_nie_udaje_lagodnej(kanal_debian):
    """Sortujemy po wadze - brak oceny nie moze wypchnac podatnosci na koniec
    listy tak, jakby byla najlagodniejsza."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u14")]))

    znalezione = wynik["entries"][0]
    assert znalezione["base_score"] is None
    assert wynik["bez_oceny"] == 1


# --- jadro i grupowanie -----------------------------------------------------
#
# Oba bledy ujawnily sie na maszynie w AWS: 5505 znalezisk, z ktorych
# wiekszosc dotyczyla jadra juz niedzialajacego, a kazda podatnosc byla
# wypisana tyle razy, ile pakietow binarnych pochodzi z jednego zrodla.

JADRO = {
    "linux-aws": {
        "CVE-2025-54505": {
            "description": "luka w jadrze",
            "releases": {"resolute": {"status": "resolved",
                                      "fixed_version": "7.0.0-1008.8",
                                      "urgency": "high"}},
        }
    }
}

# Pakiety binarne jednego ABI jadra - wszystkie z tego samego zrodla.
PAKIETY_JADRA = [
    _pakiet(n, "7.0.0-1006.6", source_package="linux-aws",
            source_version="7.0.0-1006.6")
    for n in ("linux-aws-headers-7.0.0-1006", "linux-aws-tools-7.0.0-1006",
              "linux-headers-7.0.0-1006-aws", "linux-modules-7.0.0-1006-aws",
              "linux-tools-7.0.0-1006-aws")
]


def _raport_jadra(uruchomione: str):
    return {
        "os": {"distro_id": "ubuntu", "codename": "resolute", "kernel": uruchomione},
        "software": {"packages": PAKIETY_JADRA},
    }


@pytest.fixture()
def kanal_jadra():
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "ubuntu", "resolute",
                         cve.wpisy_debian(json.dumps(JADRO).encode(), {"resolute"}))
    yield


def test_podatnosc_liczona_raz_na_zrodlo(kanal_jadra):
    """Piec pakietow binarnych z jednego zrodla to jedna podatnosc, a nie piec.
    Bez grupowania lista rosla do tysiecy pozycji i przestawala byc czytana."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport_jadra("7.0.0-1006-aws"))

    assert len(wynik["entries"]) == 1
    znalezione = wynik["entries"][0]
    assert znalezione["package_count"] == 5
    assert "linux-modules-7.0.0-1006-aws" in znalezione["packages"]


def test_dzialajace_nowsze_jadro_nie_jest_podatne(kanal_jadra):
    """Sedno bledu z AWS: uname pokazywal 7.0.0-1011, czyli jadro nowsze niz
    poprawka, a pakiety wycofanego ABI 1006 nadal lezaly na dysku. Ubuntu nie
    usuwa starych jader od razu, wiec zglaszalismy podatnosc jadra, ktore
    juz nie dziala."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport_jadra("7.0.0-1011-aws"))

    assert wynik["fixable_count"] == 0, "maszyna dziala na jadrze z poprawka"
    assert wynik["stale_kernel_count"] == 1
    # Na liscie ich nie ma - jest ich tyle, ze zaslanialyby wszystko, na co
    # faktycznie da sie zareagowac. Zostaje liczba i wersje do sprzatniecia.
    assert wynik["entries"] == []
    assert wynik["stale_kernels"] == ["7.0.0-1006.6"]


def test_dzialajace_starsze_jadro_jest_podatne(kanal_jadra):
    """Gdy maszyna faktycznie dziala na podatnym jadrze, ma to byc zgloszone."""
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport_jadra("7.0.0-1006-aws"))

    assert wynik["fixable_count"] == 1
    assert wynik["stale_kernel_count"] == 0
    assert wynik["entries"][0]["inactive_kernel"] is False


def test_bez_informacji_o_jadrze_zglaszamy_podatnosc(kanal_jadra):
    """Starszy agent nie podaje wersji jadra. Lepiej zglosic za duzo niz
    przemilczec podatnosc jadra, ktore moze byc uruchomione."""
    raport = _raport_jadra("")
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, raport)

    assert wynik["fixable_count"] == 1
    assert wynik["entries"][0]["inactive_kernel"] is False


def test_nieaktywne_jadro_nie_wchodzi_do_powaznych(kanal_jadra, monkeypatch):
    """Kafelek "powazne" ma pokazywac to, co wymaga dzialania teraz."""
    from cmdb_server.models import CveScore

    with SessionLocal() as db:
        db.add(CveScore(cve="CVE-2025-54505", base_score=9.8, severity="CRITICAL"))
        db.commit()
        wynik = cve.dopasuj(db, _raport_jadra("7.0.0-1011-aws"))

    assert wynik["critical_count"] == 0, "jadro juz nie dziala, mimo oceny 9.8"
    assert wynik["entries"] == [], "ocena 9.8 nie przywraca pozycji na liste"


def test_zwykly_pakiet_nie_podlega_regule_jadra(kanal_debian):
    """Regula dotyczy wylacznie jadra - dla reszty liczy sie to, co jest
    zainstalowane, bo to ono jest uruchamiane."""
    raport = _raport([_pakiet("curl", "7.88.1-10+deb12u14")])
    raport["os"]["kernel"] = "9.9.9-9999-generic"
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, raport)

    assert wynik["fixable_count"] == 1
    assert wynik["entries"][0]["inactive_kernel"] is False


def test_pozycje_nieaktywnego_jadra_nie_zaslaniaja_reszty(kanal_jadra):
    """Sedno zmiany: na maszynie w AWS bylo ich kilka tysiecy i przykrywaly
    wszystko, na co faktycznie dalo sie zareagowac."""
    raport = _raport_jadra("7.0.0-1011-aws")
    # Obok jadra cos, co naprawde wymaga dzialania.
    with SessionLocal() as db:
        cve.zapisz_kanal(db, "ubuntu", "resolute",
                         cve.wpisy_debian(json.dumps(JADRO).encode(), {"resolute"})
                         + [{"package": "curl", "cve": "CVE-2025-99999",
                             "release": "resolute", "fixed_version": "9.9",
                             "status": "resolved", "severity": "high",
                             "no_fix_reason": None, "description": None}])
    raport["software"]["packages"] = PAKIETY_JADRA + [_pakiet("curl", "7.0")]

    with SessionLocal() as db:
        wynik = cve.dopasuj(db, raport)

    assert [z["cve"] for z in wynik["entries"]] == ["CVE-2025-99999"]
    assert wynik["fixable_count"] == 1
    assert wynik["stale_kernel_count"] == 1


# --- opisy i podsumowanie po pakietach -----------------------------------------

def test_opis_z_nvd_trafia_do_znaleziska(kanal_debian, monkeypatch):
    odpowiedz = json.loads(json.dumps(ODPOWIEDZ_NVD))
    odpowiedz["vulnerabilities"][0]["cve"]["descriptions"] = [
        {"lang": "es", "value": "no"}, {"lang": "en", "value": "curl  mishandles\ncookies"}]
    odpowiedz["vulnerabilities"][0]["cve"]["weaknesses"] = [
        {"description": [{"lang": "en", "value": "CWE-787"}]},
        {"description": [{"lang": "en", "value": "NVD-CWE-Other"}]}]
    _podstaw_nvd(monkeypatch, [odpowiedz])
    with SessionLocal() as db:
        cve.pobierz_oceny(db, ["CVE-2025-10148"])
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u14")]))
    pozycja = wynik["entries"][0]
    assert pozycja["summary"] == "curl mishandles cookies"
    from cmdb_server.models import CveScore
    with SessionLocal() as db:
        assert db.get(CveScore, "CVE-2025-10148").cwe == "CWE-787"
    assert pozycja["nvd_link"] == "https://nvd.nist.gov/vuln/detail/CVE-2025-10148"


def test_oceny_bez_opisu_sa_dobierane_ponownie(monkeypatch):
    from cmdb_server.models import CveScore

    with SessionLocal() as db:
        db.add(CveScore(cve="CVE-2025-10148", found=True, base_score=6.5))
        db.commit()
    _podstaw_nvd(monkeypatch, [ODPOWIEDZ_NVD])
    with SessionLocal() as db:
        wynik = cve.pobierz_oceny(db, ["CVE-2025-10148"])
        ocena = db.get(CveScore, "CVE-2025-10148")
        assert wynik["pobrane"] == 1
        assert ocena.summary is not None


def test_podsumowanie_liczy_luki_na_pakiet(kanal_debian):
    with SessionLocal() as db:
        wynik = cve.dopasuj(db, _raport([_pakiet("curl", "7.88.1-10+deb12u14")]))
    assert wynik["packages_summary"][0]["package"] == "curl"
    assert wynik["packages_summary"][0]["count"] == 1
    assert wynik["packages_summary"][0]["fixed_version"] == "7.88.1-10+deb12u15"
    assert wynik["packages_summary"][0]["kernel"] is False


def test_karta_maszyny_pokazuje_co_zaktualizowac(client, tenant_a, make_user, kanal_debian):
    from .factories import build_report
    from .test_agent_api import enroll
    from .test_tenant_isolation import _login

    make_user(tenant_a["id"], "admin@cve.pl", "haslo-do-testow-123")
    _login(client, "admin@cve.pl", "haslo-do-testow-123")
    token = enroll(client, tenant_a["token"], machine_id="cve-0001", hostname="CVE-01").json()[
        "agent_token"]
    raport = build_report(machine_id="cve-0001", hostname="CVE-01")
    raport["os"].update({"distro_id": "debian", "codename": "bookworm"})
    raport.setdefault("software", {})["packages"] = [_pakiet("curl", "7.88.1-10+deb12u14")]
    odpowiedz = client.post("/api/v1/inventory", headers={"Authorization": f"Bearer {token}"},
                            json=raport)
    strona = client.get(f"/assets/{odpowiedz.json()['asset_id']}").text
    assert "Co zaktualizować" in strona
    # Opis i odnosniki sa w oknie szczegolow - na liscie zostaje przycisk.
    assert "co to jest" not in strona
    assert "/podatnosci/cve/CVE-2025-10148?zrodlo=debian/bookworm" in strona


# --- oceny Ubuntu -------------------------------------------------------------

ODPOWIEDZ_UBUNTU = {
    "id": "CVE-2025-10148", "priority": "medium", "cvss3": 7.4,
    "description": "\ncurl mishandles\nsomething",
    "impact": {"baseMetricV3": {"cvssV3": {
        "baseScore": 7.4, "baseSeverity": "HIGH",
        "vectorString": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N"}}},
}


def test_ocena_ubuntu_gdy_nvd_jeszcze_nie_ocenil(kanal_ubuntu, monkeypatch):
    """NVD ocenia swieze CVE z opoznieniem tygodni - Ubuntu od razu."""
    cve_ubuntu = [e["cves"][0] for e in UBUNTU.values()][0]
    odpowiedz = dict(ODPOWIEDZ_UBUNTU, id=cve_ubuntu)
    _podstaw_nvd(monkeypatch, [odpowiedz])
    with SessionLocal() as db:
        wynik = cve.pobierz_oceny_ubuntu(db, [cve_ubuntu])
    assert wynik["pobrane"] == 1

    from cmdb_server.models import CveUbuntu
    with SessionLocal() as db:
        ocena = db.get(CveUbuntu, cve_ubuntu)
    assert ocena.priority == "medium"
    assert ocena.base_score == 7.4
    assert ocena.summary == "curl mishandles something"

    with SessionLocal() as db:
        dopasowanie = cve.dopasuj(db, _raport(
            [_pakiet("e2fsprogs", "1.46.5-2ubuntu1")], "ubuntu", "jammy"))
    pozycja = next(e for e in dopasowanie["entries"] if e["cve"] == cve_ubuntu)
    assert pozycja["base_score"] == 7.4
    assert pozycja["score_source"] == "Ubuntu"
    assert pozycja["ubuntu_priority"] == "medium"


def test_ubuntu_nie_zna_cve_zapamietujemy(monkeypatch):
    from cmdb_server.models import CveUbuntu

    def nie_ma(zadanie, timeout=0):
        raise cve.urllib.error.HTTPError(zadanie.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(cve.urllib.request, "urlopen", nie_ma)
    with SessionLocal() as db:
        cve.pobierz_oceny_ubuntu(db, ["CVE-2099-0001"])
        assert db.get(CveUbuntu, "CVE-2099-0001").priority == ""


WEKTOR_CVSS4 = ("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N/"
                "E:X/CR:X/IR:X/AR:X/MAV:X/MAC:X/MAT:X/MPR:X/MUI:X/MVC:X/MVI:X/MVA:X/"
                "MSC:X/MSI:X/MSA:X/S:X/AU:X/R:X/V:X/RE:X/U:X")


def test_dlugi_wektor_cvss4_nie_przerywa_przebiegu(monkeypatch):
    """Zgloszony blad: 'value too long for type character varying(128)'."""
    from cmdb_server.models import CveScore

    assert len(WEKTOR_CVSS4) > 128
    odpowiedz = {"vulnerabilities": [{"cve": {"id": "CVE-2026-81642", "metrics": {
        "cvssMetricV40": [{"cvssData": {"baseScore": 9.1, "baseSeverity": "CRITICAL",
                                        "vectorString": WEKTOR_CVSS4}}]}}}]}
    _podstaw_nvd(monkeypatch, [odpowiedz])
    with SessionLocal() as db:
        assert cve.pobierz_oceny(db, ["CVE-2026-81642"])["pobrane"] == 1
        assert db.get(CveScore, "CVE-2026-81642").vector == WEKTOR_CVSS4


def test_migracja_poszerza_kolumny_wektorow():
    from cmdb_server.db import _poszerz_wektory_cvss, engine
    from sqlalchemy import inspect, text

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE cve_scores ALTER COLUMN vector TYPE VARCHAR(128)"))
    _poszerz_wektory_cvss()
    dlugosc = {k["name"]: getattr(k["type"], "length", None)
               for k in inspect(engine).get_columns("cve_scores")}
    assert dlugosc["vector"] == 512
