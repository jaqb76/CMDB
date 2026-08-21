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


def test_producent_wywiedziony_z_modelu(raspberry):
    """Plyty ARM nie podaja producenta osobno."""
    assert raspberry.collect_system()["manufacturer"] == "Raspberry"


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
