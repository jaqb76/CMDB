"""Szkielet kolektora i logika parsowania danych z Windows.

Funkcje parsujace testujemy na kazdej platformie - nie wymagaja Windows,
bo dostaja gotowa strukture, taka jaka zwraca ConvertTo-Json.
"""
from __future__ import annotations

from cmdb_agent.collectors.base import BaseCollector, set_path
from cmdb_agent.collectors.common import clean, percent, to_bool, to_int
from cmdb_agent.collectors.windows import (
    _edition_from_caption,
    _normalize_install_date,
    detect_virtualization,
    items_of,
)
from cmdb_agent.config import AgentConfig


class _Collector(BaseCollector):
    name = "test"
    os_family = "test"

    def machine_id(self):
        return "test-machine-0001"

    def identity(self):
        return {"hostname": "TEST", "os_family": "test"}

    def steps(self):
        return [
            ("hardware.cpu", lambda: {"model": "Test CPU"}),
            ("software.packages", self._boom),
            ("users.local_accounts", lambda: [{"name": "test"}]),
        ]

    def _boom(self):
        raise PermissionError("odmowa dostepu")


def test_set_path_creates_nested_structure():
    target = {}
    set_path(target, "hardware.storage.logical_disks", [1, 2])
    assert target == {"hardware": {"storage": {"logical_disks": [1, 2]}}}


def test_failing_step_does_not_break_report():
    """Jeden kolektor bez uprawnien nie moze pozbawic nas calego raportu."""
    collector = _Collector(AgentConfig())
    sections, errors = collector.collect()

    assert sections["hardware"]["cpu"]["model"] == "Test CPU"
    assert sections["users"]["local_accounts"] == [{"name": "test"}]
    assert "packages" not in sections.get("software", {})
    assert len(errors) == 1
    assert errors[0]["collector"] == "software.packages"
    assert "odmowa dostepu" in errors[0]["message"]


def test_long_lists_are_capped():
    collector = _Collector(AgentConfig(max_items_per_section=10))
    assert len(collector.limit(list(range(500)))) == 10
    assert collector.errors[0]["collector"] == "limit"


def test_items_of_normalizes_powershell_output():
    """ConvertTo-Json w PS 5.1 zamienia jednoelementowa tablice w obiekt."""
    assert items_of({"items": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert items_of({"items": {"a": 1}}) == [{"a": 1}]      # pojedynczy element
    assert items_of({"items": None}) == []                   # pusta lista
    assert items_of({}) == []
    assert items_of([{"a": 1}]) == [{"a": 1}]


def test_detect_virtualization():
    assert detect_virtualization("VMware, Inc.", "VMware Virtual Platform") == "VMware"
    assert detect_virtualization("Microsoft Corporation", "Virtual Machine") == "Hyper-V"
    assert detect_virtualization("innotek GmbH", "VirtualBox") == "VirtualBox"
    assert detect_virtualization("Dell Inc.", "OptiPlex 7090") is None


def test_normalize_install_date():
    assert _normalize_install_date("20240215") == "2024-02-15"
    assert _normalize_install_date("2024-02-15") == "2024-02-15"
    assert _normalize_install_date(None) is None


def test_edition_from_caption():
    assert _edition_from_caption("Microsoft Windows 11 Pro") == "Pro"
    assert _edition_from_caption("Microsoft Windows Server 2022 Datacenter") == "Datacenter"
    assert _edition_from_caption(None) is None


def test_clean_removes_wmi_placeholders():
    """Producenci wpisuja w SMBIOS teksty zastepcze - nie moga trafiac do bazy."""
    assert clean("  Dell Inc.  ") == "Dell Inc."
    assert clean("To Be Filled By O.E.M.") is None
    assert clean("Default string") is None
    assert clean("System Serial Number") is None
    assert clean("") is None
    assert clean(None) is None


def test_value_conversions():
    assert to_int("42") == 42
    assert to_int("nie liczba", default=0) == 0
    assert to_bool("True") is True
    assert to_bool("false") is False
    assert to_bool(None, default=True) is True
    assert percent(50, 200) == 25.0
    assert percent(1, 0) is None


# --- tablica montowan a piaskownica systemd ---------------------------------

class _Statvfs:
    """Minimalny wynik os.statvfs - tylko pola uzywane przez kolektor."""

    f_blocks = 1000
    f_frsize = 4096
    f_bavail = 500

def test_wolumeny_czytane_z_tablicy_systemu(monkeypatch, tmp_path):
    """Usluga agenta dziala z PrivateTmp i ReadWritePaths, wiec we wlasnej
    przestrzeni nazw widzi montowania, ktorych na maszynie nie ma. Trafialy
    do inwentarza jako osobne wolumeny na tym samym dysku."""
    from cmdb_agent.collectors import linux as kolektor
    from cmdb_agent.config import AgentConfig

    WIDOK_SYSTEMU = "/dev/nvme0n1p2 / ext4 rw 0 0\n"
    WIDOK_AGENTA = (
        WIDOK_SYSTEMU
        + "/dev/nvme0n1p2 /tmp ext4 rw 0 0\n"
        + "/dev/nvme0n1p2 /var/lib/cmdb-agent ext4 rw 0 0\n"
    )

    def fake_read_text(sciezka, default=""):
        tekst = str(sciezka).replace("\\", "/")
        if tekst.endswith("/proc/1/mounts"):
            return WIDOK_SYSTEMU
        if tekst.endswith("/proc/mounts"):
            return WIDOK_AGENTA
        return default

    monkeypatch.setattr(kolektor, "read_text", fake_read_text)
    monkeypatch.setattr(kolektor.glob, "glob", lambda _: [])
    # os.statvfs nie istnieje na Windows, wiec podstawiamy wlasna atrape.
    monkeypatch.setattr(kolektor.os, "statvfs", lambda _: _Statvfs(), raising=False)

    wolumeny = kolektor.LinuxCollector(AgentConfig()).collect_storage()["logical_disks"]
    punkty = [w["mount"] for w in wolumeny]
    assert punkty == ["/"], f"montowania z piaskownicy trafily do inwentarza: {punkty}"


def test_bez_dostepu_do_tablicy_systemu_zostaje_widok_wlasny(monkeypatch):
    """Agent bez roota nie odczyta /proc/1/mounts. Lepiej pokazac widok
    wlasny niz nie pokazac nic."""
    from cmdb_agent.collectors import linux as kolektor
    from cmdb_agent.config import AgentConfig

    def fake_read_text(sciezka, default=""):
        tekst = str(sciezka).replace("\\", "/")
        if tekst.endswith("/proc/1/mounts"):
            return ""                      # brak uprawnien
        if tekst.endswith("/proc/mounts"):
            return "/dev/sda1 / ext4 rw 0 0\n"
        return default

    monkeypatch.setattr(kolektor, "read_text", fake_read_text)
    monkeypatch.setattr(kolektor.glob, "glob", lambda _: [])
    # os.statvfs nie istnieje na Windows, wiec podstawiamy wlasna atrape.
    monkeypatch.setattr(kolektor.os, "statvfs", lambda _: _Statvfs(), raising=False)

    wolumeny = kolektor.LinuxCollector(AgentConfig()).collect_storage()["logical_disks"]
    assert [w["mount"] for w in wolumeny] == ["/"]
