"""Kolektor Linux na plytach ARM (Raspberry Pi).

DMI (/sys/class/dmi/id) to firmware x86 - na plytach ARM nie istnieje.
Bez siegania do drzewa urzadzen i /proc/cpuinfo producent, model i numer
seryjny Raspberry Pi wychodzilyby puste.

Testy podstawiaja tresc plikow, wiec dzialaja na kazdym systemie.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cmdb_agent.collectors import linux as kolektor
from cmdb_agent.config import AgentConfig

# Prawdziwa tresc z Raspberry Pi 4 z 64-bitowym Ubuntu.
CPUINFO_PI = """processor\t: 0
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU part\t: 0xd08

processor\t: 1
BogoMIPS\t: 108.00

Hardware\t: BCM2835
Revision\t: c03114
Serial\t\t: 100000003d1f9c21
Model\t\t: Raspberry Pi 4 Model B Rev 1.4
"""

# Pliki drzewa urzadzen koncza sie bajtem zerowym.
DRZEWO_PI = {
    "model": "Raspberry Pi 4 Model B Rev 1.4" + chr(0),
    "serial-number": "100000003d1f9c21" + chr(0),
    "compatible": "raspberrypi,4-model-b" + chr(0),
}


@pytest.fixture
def raspberry(monkeypatch):
    """Maszyna bez DMI, z drzewem urzadzen - czyli Raspberry Pi."""
    def fake_read_text(sciezka, default=""):
        tekst = str(sciezka).replace("\\", "/")
        if "/sys/class/dmi/" in tekst:
            return default            # na ARM tego katalogu nie ma
        if "/proc/cpuinfo" in tekst:
            return CPUINFO_PI
        for nazwa, wartosc in DRZEWO_PI.items():
            if tekst.endswith(f"device-tree/{nazwa}"):
                return wartosc
        return default

    monkeypatch.setattr(kolektor, "read_text", fake_read_text)
    monkeypatch.setattr(kolektor.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(kolektor.shutil, "which", lambda _: None)
    return kolektor.LinuxCollector(AgentConfig())


def test_model_czytany_z_drzewa_urzadzen(raspberry):
    system = raspberry.collect_system()
    assert system["model"] == "Raspberry Pi 4 Model B Rev 1.4"


def test_numer_seryjny_z_drzewa_urzadzen(raspberry):
    assert raspberry.collect_system()["serial_number"] == "100000003d1f9c21"


def test_producent_wywiedziony_z_identyfikatora(raspberry):
    """Plyty ARM nie podaja producenta osobno.

    Bierzemy go z identyfikatora "raspberrypi,4-model-b", a nie z nazwy
    modelu - z "Raspberry Pi 4 Model B" pierwszy czlon to samo "Raspberry".
    """
    assert raspberry.collect_system()["manufacturer"] == "Raspberry Pi"


def test_plyta_rozpoznana(raspberry):
    assert raspberry.collect_system()["board"] == "BCM2835"


def test_architektura_trafia_do_identyfikacji(raspberry):
    """Serwer musi wiedziec, ktory plik wolno tej maszynie zaproponowac -
    agent dla x86-64 nie uruchomi sie na ARM."""
    assert raspberry.identity()["arch"] == "aarch64"
    assert raspberry.identity()["os_family"] == "linux"


def test_procesor_gdy_brak_model_name(raspberry):
    """Na aarch64 /proc/cpuinfo nie podaje "model name"."""
    cpu = raspberry.collect_cpu()
    assert cpu["model"] == "BCM2835"
    assert cpu["architecture"] == "aarch64"
    assert cpu["logical_cores"] == 2


def test_bajt_zerowy_jest_obcinany(raspberry):
    """Pliki drzewa urzadzen koncza sie bajtem zerowym - gdyby zostal,
    trafilby do bazy i do interfejsu."""
    for wartosc in raspberry.collect_system().values():
        if isinstance(wartosc, str):
            assert chr(0) not in wartosc


def test_maszyna_x86_nadal_uzywa_dmi(monkeypatch):
    """Poprawka dla ARM nie moze zepsuc odczytu na zwyklym sprzecie."""
    def fake_read_text(sciezka, default=""):
        tekst = str(sciezka).replace("\\", "/")
        odpowiedzi = {
            "sys_vendor": "Dell Inc.",
            "product_name": "OptiPlex 7090",
            "product_serial": "SN-DELL-1",
            "chassis_type": "3",
        }
        for nazwa, wartosc in odpowiedzi.items():
            if tekst.endswith(f"dmi/id/{nazwa}"):
                return wartosc
        if "/proc/cpuinfo" in tekst:
            return "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i5-11500\n"
        return default

    monkeypatch.setattr(kolektor, "read_text", fake_read_text)
    monkeypatch.setattr(kolektor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(kolektor.shutil, "which", lambda _: None)

    system = kolektor.LinuxCollector(AgentConfig()).collect_system()
    assert system["manufacturer"] == "Dell Inc."
    assert system["model"] == "OptiPlex 7090"
    assert system["serial_number"] == "SN-DELL-1"
    assert system["chassis"] == "Desktop"


# --- Raspberry Pi 5: wlasciwosc "compatible" z kilkoma wpisami --------------
#
# Prawdziwe dane z Pi 5. Wlasciwosc "compatible" zawiera kilka nazw
# ROZDZIELONYCH bajtem zerowym, od najbardziej szczegolowej do najogolniejszej.
# Usuwanie tych bajtow zamiast dzielenia po nich sklejalo je w jeden ciag
# i do bazy trafialo "raspberrypi,5-model-bbrcm,bcm2712".

CPUINFO_PI5 = """processor\t: 0
BogoMIPS\t: 108.00
CPU implementer\t: 0x41
CPU part\t: 0xd0b

processor\t: 1
processor\t: 2
processor\t: 3

Revision\t: d04170
Serial\t\t: 05a350ebd1b50841
Model\t\t: Raspberry Pi 5 Model B Rev 1.0
"""

DRZEWO_PI5 = {
    "model": "Raspberry Pi 5 Model B Rev 1.0" + chr(0),
    "serial-number": "05a350ebd1b50841" + chr(0),
    "compatible": "raspberrypi,5-model-b" + chr(0) + "brcm,bcm2712" + chr(0),
}


@pytest.fixture
def raspberry5(monkeypatch):
    """Pi 5: brak DMI, brak pola Hardware w cpuinfo, lista w "compatible"."""
    def fake_read_text(sciezka, default=""):
        tekst = str(sciezka).replace("\\", "/")
        if "/sys/class/dmi/" in tekst:
            return default
        if "/proc/cpuinfo" in tekst:
            return CPUINFO_PI5
        for nazwa, wartosc in DRZEWO_PI5.items():
            if tekst.endswith(f"device-tree/{nazwa}"):
                return wartosc
        return default

    monkeypatch.setattr(kolektor, "read_text", fake_read_text)
    monkeypatch.setattr(kolektor.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(kolektor.shutil, "which", lambda _: None)
    return kolektor.LinuxCollector(AgentConfig())


def test_lista_compatible_nie_jest_sklejana(raspberry5):
    """Sedno bledu: wpisy byly laczone w jeden nieczytelny ciag."""
    assert kolektor.czytaj_drzewo_lista("compatible") == [
        "raspberrypi,5-model-b",
        "brcm,bcm2712",
    ]


def test_plyta_wymienia_oba_wpisy(raspberry5):
    board = raspberry5.collect_system()["board"]
    assert board == "raspberrypi,5-model-b, brcm,bcm2712"
    assert "bbrcm" not in board, "wpisy zostaly sklejone"


def test_procesor_opisany_ukladem_a_nie_plyta(raspberry5):
    """Ostatni wpis "compatible" opisuje uklad, nie plyte."""
    assert raspberry5.collect_cpu()["model"] == "Broadcom BCM2712"


def test_producent_pi5(raspberry5):
    assert raspberry5.collect_system()["manufacturer"] == "Raspberry Pi"


def test_rdzenie_fizyczne_nie_sa_puste_na_arm(raspberry5):
    """Na ARM cpuinfo nie ma "physical id" ani "core id". Te rdzenie istnieja -
    zwracanie None sugerowaloby maszyne bez procesora."""
    cpu = raspberry5.collect_cpu()
    assert cpu["logical_cores"] == 4
    assert cpu["physical_cores"] == 4


def test_zaden_bajt_zerowy_nie_przechodzi(raspberry5):
    for wartosc in raspberry5.collect_system().values():
        if isinstance(wartosc, str):
            assert chr(0) not in wartosc


def test_rpm_podaje_wersje_z_epoka(monkeypatch):
    """Red Hat opisuje poprawki wersja z epoka ("1:3.0.7-27.el9"). Bez epoki
    porownanie z jego danymi bywa bledne, wiec agent podaje ja osobno."""
    wyjscie = (
        "openssl-libs\t3.0.7-27.el9\tRed Hat, Inc.\topenssl-3.0.7-27.el9.src.rpm\t1:3.0.7-27.el9\n"
        "bash\t5.1.8-9.el9\tRed Hat, Inc.\tbash-5.1.8-9.el9.src.rpm\t0:5.1.8-9.el9\n"
    )
    monkeypatch.setattr(kolektor.shutil, "which",
                        lambda nazwa: "/usr/bin/rpm" if nazwa == "rpm" else None)
    polecenia = []
    monkeypatch.setattr(kolektor, "run_command",
                        lambda polecenie, timeout=0: (polecenia.append(polecenie), wyjscie)[1])

    pakiety = {p["name"]: p for p in kolektor.LinuxCollector(AgentConfig()).collect_packages()}

    assert "%|EPOCH?{%{EPOCH}}:{0}|" in polecenia[0][-1]
    assert pakiety["openssl-libs"]["evr"] == "1:3.0.7-27.el9"
    assert pakiety["openssl-libs"]["source_package"] == "openssl"
    # Wersja zrodlowa to nadal wersja-wydanie - epoka nie przecieka tam.
    assert pakiety["openssl-libs"]["source_version"] == "3.0.7-27.el9"
    assert pakiety["bash"]["evr"] == "0:5.1.8-9.el9"
