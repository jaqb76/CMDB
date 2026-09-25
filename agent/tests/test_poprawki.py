"""Wykrywanie brakujacych aktualizacji.

Najwazniejsze rozroznienie w tym module: pusta lista brakow znaczy
"sprawdzilismy i nic nie brakuje", a stan "nieznany" - "sprawdzic sie nie
udalo". W narzedziu do oceny bezpieczenstwa zlanie ich w jedno uspokajaloby
zamiast ostrzegac, wiec pilnuje tego kilka testow.
"""
from __future__ import annotations

import pytest

from cmdb_agent.collectors import poprawki

# Prawdziwe wyjscie "apt-get -s dist-upgrade" z Ubuntu 22.04.
APT_WYJSCIE = """NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
Reading package lists...
Building dependency tree...
The following packages will be upgraded:
  libssl3 openssh-server vim-common
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libssl3 [3.0.2-0ubuntu1.10] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
Inst openssh-server [1:8.9p1-3] (1:8.9p1-3ubuntu0.6 Ubuntu:22.04/jammy-security [amd64])
Inst vim-common [2:8.2.3995-1ubuntu2] (2:8.2.3995-1ubuntu2.15 Ubuntu:22.04/jammy-updates [all])
Conf libssl3 (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
"""

# "dnf -q -C check-update" z Rocky Linux 9.
DNF_WYJSCIE = """
kernel.x86_64                     5.14.0-362.8.1.el9_3      baseos
openssl-libs.x86_64               1:3.0.7-24.el9            baseos
tzdata.noarch                     2023c-1.el9               baseos

Obsoleting Packages
grub2-tools.x86_64                1:2.06-70.el9             baseos
"""


# --- parsowanie apt ---------------------------------------------------------

def test_apt_znajduje_wszystkie_braki():
    braki = poprawki.parsuj_apt(APT_WYJSCIE)
    assert [b["id"] for b in braki] == ["libssl3", "openssh-server", "vim-common"]


def test_apt_rozpoznaje_poprawki_bezpieczenstwa():
    """Debian i Ubuntu wydaja je z osobnej kieszeni "-security"."""
    braki = {b["id"]: b["security"] for b in poprawki.parsuj_apt(APT_WYJSCIE)}
    assert braki["libssl3"] is True
    assert braki["openssh-server"] is True
    assert braki["vim-common"] is False, "jammy-updates to nie kieszen bezpieczenstwa"


def test_apt_podaje_wersje_obecna_i_nowa():
    libssl = poprawki.parsuj_apt(APT_WYJSCIE)[0]
    assert libssl["current_version"] == "3.0.2-0ubuntu1.10"
    assert libssl["new_version"] == "3.0.2-0ubuntu1.15"
    assert libssl["source_repo"] == "Ubuntu:22.04/jammy-security"


def test_apt_pomija_linie_conf():
    """Linie "Conf" opisuja te same pakiety - bez pominiecia kazdy brak
    liczylby sie dwa razy."""
    assert len(poprawki.parsuj_apt(APT_WYJSCIE)) == 3


def test_apt_bez_brakow_daje_pusta_liste():
    wyjscie = "Reading package lists...\n0 upgraded, 0 newly installed.\n"
    assert poprawki.parsuj_apt(wyjscie) == []


# --- parsowanie dnf ---------------------------------------------------------

def test_dnf_znajduje_braki_bez_naglowkow():
    braki = poprawki.parsuj_dnf(DNF_WYJSCIE)
    assert [b["id"] for b in braki] == ["kernel", "openssl-libs", "tzdata", "grub2-tools"]


def test_dnf_oznacza_bezpieczenstwo_z_osobnej_listy():
    """dnf podaje poprawki bezpieczenstwa osobnym wywolaniem --security."""
    braki = {b["id"]: b["security"] for b in poprawki.parsuj_dnf(DNF_WYJSCIE, {"openssl-libs"})}
    assert braki["openssl-libs"] is True
    assert braki["tzdata"] is False


def test_dnf_odcina_architekture_od_nazwy():
    assert poprawki.parsuj_dnf(DNF_WYJSCIE)[0]["id"] == "kernel"


# --- postac wyniku ----------------------------------------------------------

def test_brak_brakow_to_co_innego_niz_brak_wiedzy():
    """Sedno calego modulu."""
    sprawdzone = poprawki.wynik("apt", poprawki.STATUS_OK, entries=[])
    nieznane = poprawki.wynik("apt", poprawki.STATUS_NIEZNANY, "apt-get nie odpowiedzial")

    assert sprawdzone["count"] == 0
    assert sprawdzone["security_count"] == 0
    assert nieznane["count"] is None, "nie wiemy - nie wolno pokazac zera"
    assert nieznane["security_count"] is None
    assert nieznane["detail"]


def test_wynik_liczy_poprawki_bezpieczenstwa():
    w = poprawki.wynik("apt", entries=poprawki.parsuj_apt(APT_WYJSCIE))
    assert w["count"] == 3
    assert w["security_count"] == 2


def test_wiek_indeksu_gdy_nic_nie_istnieje():
    assert poprawki.wiek_indeksu(("/nie/ma/takiej/sciezki",)) is None


def test_wiek_indeksu_liczony_w_godzinach(tmp_path):
    import os
    import time

    plik = tmp_path / "stamp"
    plik.write_text("x", encoding="utf-8")
    os.utime(plik, (time.time() - 7200, time.time() - 7200))

    wiek = poprawki.wiek_indeksu((str(plik),))
    assert wiek == pytest.approx(2.0, abs=0.1)


# --- dobor narzedzia --------------------------------------------------------

def test_bez_znanego_menedzera_stan_jest_nieznany(monkeypatch):
    """Windows albo nietypowa dystrybucja. Nie wolno raportowac "0 brakow"."""
    monkeypatch.setattr(poprawki.shutil, "which", lambda _: None)
    w = poprawki.braki_linux()
    assert w["status"] == poprawki.STATUS_NIEZNANY
    assert w["count"] is None
    assert "menedzera pakietow" in w["detail"]


def test_apt_nie_odswieza_indeksu(monkeypatch):
    """apt update pobiera dane z sieci, bierze blokade i zmienia stan maszyny.
    Agent ma maszyne inwentaryzowac, a nie modyfikowac."""
    wywolania = []

    def podstawiony(polecenie, timeout=0):
        wywolania.append(polecenie)
        return APT_WYJSCIE, 0

    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", podstawiony)

    poprawki.braki_apt()

    assert len(wywolania) == 1
    polecenie = wywolania[0]
    assert "update" not in polecenie, f"agent nie moze odswiezac indeksu: {polecenie}"
    assert "-s" in polecenie, "brak symulacji - to by faktycznie instalowalo"
    assert "Debug::NoLocking=1" in polecenie


def test_apt_zwraca_stan_nieznany_gdy_polecenie_padnie(monkeypatch):
    def podstawiony(polecenie, timeout=0):
        raise poprawki.CommandError("przekroczono limit czasu")

    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", podstawiony)

    w = poprawki.braki_apt()
    assert w["status"] == poprawki.STATUS_NIEZNANY
    assert w["count"] is None
    assert "limit czasu" in w["detail"]


def test_dnf_kod_100_znaczy_sa_aktualizacje(monkeypatch):
    """dnf check-update zwraca 100, gdy cos jest do zainstalowania - to nie blad."""
    def podstawiony(polecenie, timeout=0):
        if "--security" in polecenie:
            return "openssl-libs.x86_64 1:3.0.7-24.el9 baseos\n", 100
        return DNF_WYJSCIE, 100

    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: f"/usr/bin/{nazwa}")
    monkeypatch.setattr(poprawki, "_uruchom", podstawiony)

    w = poprawki.braki_dnf()
    assert w["status"] == poprawki.STATUS_OK
    assert w["count"] == 4
    assert w["security_count"] == 1


def test_dnf_kod_zero_znaczy_maszyna_aktualna(monkeypatch):
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: f"/usr/bin/{nazwa}")
    monkeypatch.setattr(poprawki, "_uruchom", lambda polecenie, timeout=0: ("", 0))

    w = poprawki.braki_dnf()
    assert w["status"] == poprawki.STATUS_OK
    assert w["count"] == 0


def test_dnf_nie_siega_do_sieci(monkeypatch):
    """-C korzysta wylacznie z cache. Bez tego kazdy raport ciagnalby
    metadane repozytoriow."""
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: f"/usr/bin/{nazwa}")
    monkeypatch.setattr(
        poprawki, "_uruchom",
        lambda polecenie, timeout=0: (wywolania.append(polecenie), ("", 0))[1],
    )

    poprawki.braki_dnf()
    assert all("-C" in polecenie for polecenie in wywolania)


# --- wlasny indeks agenta ----------------------------------------------------

def _apt_podstawiony(wywolania, kod_update=0):
    def podstawiony(polecenie, timeout=0):
        wywolania.append(polecenie)
        if "update" in polecenie:
            return "E: nie mozna pobrac\n" if kod_update else "", kod_update
        return APT_WYJSCIE, 0
    return podstawiony


def test_apt_odswieza_wlasny_indeks_a_nie_systemowy(monkeypatch, tmp_path):
    """Na maszynach bez "apt update" systemowy indeks ma miesiace. Agent
    pobiera wlasny - do swojego katalogu, bez ruszania /var/lib/apt/lists."""
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", _apt_podstawiony(wywolania))

    w = poprawki.braki_apt(katalog=tmp_path)

    assert w["status"] == poprawki.STATUS_OK
    assert w["index_refresh"] == {"status": "ok", "detail": None}
    assert w["index_age_hours"] == 0.0
    aktualizacja, symulacja = wywolania
    konfiguracja = str(tmp_path / "apt.conf")
    assert aktualizacja[-1] == "update" and konfiguracja in aktualizacja
    assert "-s" in symulacja and konfiguracja in symulacja
    tresc = (tmp_path / "apt.conf").read_text()
    assert f'Dir::State::Lists "{tmp_path / "lists"}/"' in tresc
    # Skrypty po "apt update" pisza do systemu - maja byc wylaczone.
    assert "#clear APT::Update::Post-Invoke-Success;" in tresc
    assert 'Dir::Cache::pkgcache "";' in tresc


def test_apt_nie_odswieza_swiezego_indeksu(monkeypatch, tmp_path):
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", _apt_podstawiony(wywolania))

    poprawki.braki_apt(katalog=tmp_path)
    wywolania.clear()
    poprawki.braki_apt(katalog=tmp_path)

    assert len(wywolania) == 1 and "update" not in wywolania[0]


def test_apt_nieudane_odswiezenie_wraca_do_indeksu_systemowego(monkeypatch, tmp_path):
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", _apt_podstawiony(wywolania, kod_update=100))

    w = poprawki.braki_apt(katalog=tmp_path)

    assert w["status"] == poprawki.STATUS_OK, "lista brakow z indeksu systemowego zostaje"
    assert w["index_refresh"]["status"] == "blad"
    assert "nie mozna pobrac" in w["index_refresh"]["detail"]
    assert "-c" not in wywolania[-1], "bez udanego pobrania czytamy indeks systemowy"

    # Kolejny raport nie ponawia od razu - niedostepne lustro to minuty czekania.
    wywolania.clear()
    w = poprawki.braki_apt(katalog=tmp_path)
    assert all("update" not in p for p in wywolania)
    assert w["index_refresh"]["status"] == "blad"


def test_apt_ponawia_po_bledzie_po_przerwie(monkeypatch, tmp_path):
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", _apt_podstawiony(wywolania, kod_update=100))
    poprawki.braki_apt(katalog=tmp_path)

    stan = poprawki._stan_odswiezenia(tmp_path)
    stan["proba"] -= (poprawki.PONOW_PO_BLEDZIE_GODZIN + 1) * 3600
    poprawki._zapisz_stan_odswiezenia(tmp_path, stan)
    wywolania.clear()
    poprawki.braki_apt(katalog=tmp_path)
    assert any("update" in p for p in wywolania)


def test_dnf_z_katalogiem_uzywa_wlasnego_bufora(monkeypatch, tmp_path):
    wywolania = []
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: f"/usr/bin/{nazwa}")
    monkeypatch.setattr(
        poprawki, "_uruchom",
        lambda polecenie, timeout=0: (wywolania.append(polecenie), (DNF_WYJSCIE, 100))[1],
    )

    w = poprawki.braki_dnf(katalog=tmp_path, co_ile_godzin=12)

    assert w["status"] == poprawki.STATUS_OK
    assert w["index_refresh"]["status"] == "ok"
    for polecenie in wywolania:
        assert "-C" not in polecenie
        assert f"--setopt=cachedir={tmp_path / 'cache'}" in polecenie
        assert "--setopt=metadata_expire=43200" in polecenie


def test_dnf_nieudane_odswiezenie_wraca_do_bufora_systemowego(monkeypatch, tmp_path):
    wywolania = []

    def podstawiony(polecenie, timeout=0):
        wywolania.append(polecenie)
        if "-C" in polecenie:
            return DNF_WYJSCIE, 100
        return "Error: Failed to download metadata for repo 'baseos'\n", 1

    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: f"/usr/bin/{nazwa}")
    monkeypatch.setattr(poprawki, "_uruchom", podstawiony)

    w = poprawki.braki_dnf(katalog=tmp_path)

    assert w["status"] == poprawki.STATUS_OK
    assert w["count"] == 4
    assert w["index_refresh"]["status"] == "blad"
    assert "Failed to download metadata" in w["index_refresh"]["detail"]


def test_bez_katalogu_nie_ma_informacji_o_odswiezaniu(monkeypatch):
    monkeypatch.setattr(poprawki.shutil, "which", lambda nazwa: "/usr/bin/apt-get")
    monkeypatch.setattr(poprawki, "_uruchom", lambda polecenie, timeout=0: (APT_WYJSCIE, 0))
    assert poprawki.braki_apt()["index_refresh"] is None
