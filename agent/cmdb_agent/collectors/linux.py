"""Kolektor danych dla Linuksa.

Sluzy dwom celom: pozwala testowac caly lancuch agent -> serwer bez maszyny
Windows i pokazuje, ze dolozenie kolejnego systemu to jeden plik, bez zmian
w serwerze (schemat raportu jest wspolny).

Czyta przede wszystkim /proc i /sys, a menedzery pakietow wola tylko wtedy,
gdy sa obecne.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import platform
import shutil
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import BaseCollector, Step
from .common import CommandError, clean, percent, run_command, to_int

log = logging.getLogger(__name__)

DMI = Path("/sys/class/dmi/id")

CHASSIS_TYPES = {
    3: "Desktop", 4: "Low Profile Desktop", 6: "Mini Tower", 7: "Tower",
    8: "Portable", 9: "Laptop", 10: "Notebook", 13: "All in One",
    17: "Main System Chassis", 23: "Rack Mount Chassis", 30: "Tablet", 31: "Convertible",
}


DRZEWO = Path("/proc/device-tree")
# Separator lancuchow w drzewie urzadzen (bajt zerowy).
ZEROWY = chr(0)


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, PermissionError):
        return default


# Nazwy producentow z drzewa urzadzen sa skrotami ("brcm", "raspberrypi").
# Nie da sie ich rozwinac algorytmicznie - to po prostu ustalone identyfikatory.
PRODUCENCI_DT = {
    "raspberrypi": "Raspberry Pi",
    "brcm": "Broadcom",
    "rockchip": "Rockchip",
    "amlogic": "Amlogic",
    "allwinner": "Allwinner",
    "nvidia": "NVIDIA",
    "qcom": "Qualcomm",
    "ti": "Texas Instruments",
    "fsl": "NXP",
    "marvell": "Marvell",
    "samsung": "Samsung",
}


def czytaj_drzewo(nazwa: str) -> str | None:
    """Pojedyncza wartosc z drzewa urzadzen - tak identyfikuja sie plyty ARM.

    DMI (/sys/class/dmi/id) to firmware x86; na Raspberry Pi i innych plytach
    ARM nie istnieje, wiec producent, model i numer seryjny trzeba czytac
    stad. Lancuchy w drzewie urzadzen sa zakonczone bajtem zerowym.
    """
    wartosci = czytaj_drzewo_lista(nazwa)
    return wartosci[0] if wartosci else None


def czytaj_drzewo_lista(nazwa: str) -> list[str]:
    """Wartosci z wlasciwosci drzewa urzadzen bedacej lista lancuchow.

    Wlasciwosc "compatible" zawiera kilka nazw ROZDZIELONYCH bajtem zerowym,
    od najbardziej szczegolowej do najogolniejszej, np. "raspberrypi,5-model-b"
    i "brcm,bcm2712". Usuwanie tych bajtow zamiast dzielenia po nich sklejalo
    je w jeden nieczytelny ciag - w bazie ladowalo "raspberrypi,5-model-bbrcm,bcm2712".
    """
    wartosc = read_text(DRZEWO / nazwa)
    if not wartosc:
        return []
    czesci = (clean(czesc.strip()) for czesc in wartosc.split(ZEROWY))
    return [czesc for czesc in czesci if czesc]


def nazwa_producenta(identyfikator: str | None) -> str | None:
    """Producent z identyfikatora w postaci "producent,model"."""
    if not identyfikator or "," not in identyfikator:
        return None
    return PRODUCENCI_DT.get(identyfikator.split(",", 1)[0].lower())


def opis_ukladu(compatible: list[str]) -> str | None:
    """Czytelna nazwa ukladu SoC z listy "compatible".

    Ostatni wpis jest najogolniejszy i to on opisuje uklad, a nie plyte:
    dla Raspberry Pi 5 lista to ["raspberrypi,5-model-b", "brcm,bcm2712"].
    """
    if not compatible:
        return None
    uklad = compatible[-1]
    producent, _, model = uklad.partition(",")
    czytelny = PRODUCENCI_DT.get(producent.lower())
    if not czytelny or not model:
        return uklad
    return f"{czytelny} {model.upper()}"


def dane_cpuinfo() -> dict[str, str]:
    """Pola z /proc/cpuinfo wystepujace poza x86: Model, Hardware, Serial."""
    dane: dict[str, str] = {}
    for linia in read_text("/proc/cpuinfo").splitlines():
        if ":" not in linia:
            continue
        klucz, _, wartosc = linia.partition(":")
        klucz, wartosc = klucz.strip(), wartosc.strip()
        if klucz in {"Model", "Hardware", "Serial", "Revision", "model name"} and wartosc:
            dane.setdefault(klucz, wartosc)
    return dane


class LinuxCollector(BaseCollector):
    name = "linux"
    os_family = "linux"

    def machine_id(self) -> str:
        # /etc/machine-id jest stabilny i czytelny dla kazdego uzytkownika.
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            value = read_text(candidate)
            if value:
                return f"linux-mid-{value.lower()}"
        # product_uuid wymaga roota, ale identyfikuje sprzet.
        uuid = read_text(DMI / "product_uuid")
        if uuid:
            return f"linux-uuid-{uuid.lower()}"
        self.record_error(
            "identity.machine_id", "brak /etc/machine-id - uzyto nazwy maszyny"
        )
        return f"linux-host-{platform.node().lower()}"

    def identity(self) -> dict:
        hostname = platform.node() or socket.gethostname()
        fqdn = socket.getfqdn()
        domain = fqdn.split(".", 1)[1] if "." in fqdn and fqdn.startswith(hostname) else None
        return {
            "hostname": hostname,
            "fqdn": fqdn if "." in fqdn else None,
            "domain": domain,
            "os_family": self.os_family,
            # Agent dla x86-64 nie uruchomi sie na ARM i odwrotnie - serwer
            # musi wiedziec, ktory plik wolno tej maszynie zaproponowac.
            "arch": platform.machine().lower(),
        }

    def steps(self) -> list[Step]:
        steps: list[Step] = [
            ("hardware.system", self.collect_system),
            ("hardware.firmware", self.collect_firmware),
            ("hardware.cpu", self.collect_cpu),
            ("hardware.memory", self.collect_memory),
            ("hardware.storage", self.collect_storage),
            ("os", self.collect_os),
            ("network.interfaces", self.collect_network),
            ("software.packages", self.collect_packages),
            ("users.local_accounts", self.collect_local_accounts),
            ("users.administrators", self.collect_administrators),
            ("users.sessions", self.collect_sessions),
        ]
        if self.config.collect_services:
            steps.append(("software.services", self.collect_services))
        if self.config.collect_processes:
            steps.append(("software.processes", self.collect_processes))
        return steps

    def collect_system(self) -> dict:
        """Identyfikacja plyty.

        Kolejnosc zrodel wynika z tego, gdzie co istnieje: DMI na sprzecie
        x86, drzewo urzadzen na plytach ARM (Raspberry Pi), /proc/cpuinfo
        jako ostatnia deska ratunku - tam Pi podaje model i numer seryjny.
        """
        cpuinfo = dane_cpuinfo()

        vendor = clean(read_text(DMI / "sys_vendor"))
        product = clean(read_text(DMI / "product_name"))
        numer = clean(read_text(DMI / "product_serial"))

        model_drzewa = czytaj_drzewo("model")
        compatible = czytaj_drzewo_lista("compatible")
        if not product:
            # "Raspberry Pi 4 Model B Rev 1.4"
            product = model_drzewa or clean(cpuinfo.get("Model"))
        if not vendor:
            # Producent nie jest podawany osobno. Identyfikator z "compatible"
            # ("raspberrypi,5-model-b") jest wiarygodniejszy niz zgadywanie
            # z nazwy modelu - z "Raspberry Pi 5" pierwszy czlon to "Raspberry".
            vendor = nazwa_producenta(compatible[0] if compatible else None)
            if not vendor and model_drzewa:
                vendor = model_drzewa.split()[0] or None
        if not numer:
            numer = czytaj_drzewo("serial-number") or clean(cpuinfo.get("Serial"))

        return {
            "manufacturer": vendor,
            "model": product,
            "serial_number": numer,
            "uuid": clean(read_text(DMI / "product_uuid")),
            "chassis": CHASSIS_TYPES.get(to_int(read_text(DMI / "chassis_type"))),
            "system_type": platform.machine(),
            "board": clean(cpuinfo.get("Hardware")) or (", ".join(compatible) or None),
            "virtualization": self._detect_virtualization(vendor, product),
        }

    def _detect_virtualization(self, vendor: str | None, product: str | None) -> str | None:
        if shutil.which("systemd-detect-virt"):
            try:
                result = run_command(["systemd-detect-virt"], timeout=15, check=False).strip()
                if result and result != "none":
                    return result
            except CommandError:
                pass
        haystack = f"{vendor or ''} {product or ''}".lower()
        for needle, label in {
            "vmware": "VMware", "virtualbox": "VirtualBox", "kvm": "KVM",
            "qemu": "QEMU", "xen": "Xen", "amazon ec2": "Amazon EC2",
            "google": "Google Compute Engine", "microsoft corporation": "Hyper-V",
        }.items():
            if needle in haystack:
                return label
        return None

    def collect_firmware(self) -> dict:
        return {
            "vendor": clean(read_text(DMI / "bios_vendor")),
            "version": clean(read_text(DMI / "bios_version")),
            "release_date": clean(read_text(DMI / "bios_date")),
        }

    def collect_cpu(self) -> dict:
        model = None
        physical_ids: set[str] = set()
        core_ids: set[tuple[str, str]] = set()
        logical = 0
        max_mhz = None
        current_physical = ""

        for line in read_text("/proc/cpuinfo").splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "model name" and not model:
                model = value
            elif key == "processor":
                logical += 1
            elif key == "physical id":
                current_physical = value
                physical_ids.add(value)
            elif key == "core id":
                core_ids.add((current_physical, value))
            elif key == "cpu MHz" and max_mhz is None:
                max_mhz = to_int(float(value)) if value else None

        boost = read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        if boost:
            max_mhz = to_int(int(boost) // 1000)

        if not model:
            # Na aarch64 /proc/cpuinfo nie podaje "model name". Bierzemy to,
            # czym plyta sie przedstawia, a w ostatecznosci sama architekture.
            cpuinfo = dane_cpuinfo()
            model = (
                clean(cpuinfo.get("Hardware"))
                or opis_ukladu(czytaj_drzewo_lista("compatible"))
                or platform.machine()
            )

        return {
            "model": model,
            # Na ARM /proc/cpuinfo nie podaje "physical id" ani "core id", wiec
            # zbior jest pusty. Te rdzenie istnieja - po prostu nie ma tam
            # informacji o wielowatkowosci, a bez niej liczba logicznych jest
            # najlepszym dostepnym przyblizeniem.
            "physical_cores": len(core_ids) or logical or os.cpu_count(),
            "logical_cores": logical or os.cpu_count(),
            "max_clock_mhz": max_mhz,
            "architecture": platform.machine(),
            "sockets": len(physical_ids) or 1,
        }

    def collect_memory(self) -> dict:
        total = available = None
        for line in read_text("/proc/meminfo").splitlines():
            if line.startswith("MemTotal:"):
                total = to_int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available = to_int(line.split()[1])
        return {
            "total_bytes": total * 1024 if total else None,
            "available_bytes": available * 1024 if available else None,
            "modules": [],
        }

    def collect_storage(self) -> dict:
        physical = []
        for device in sorted(glob.glob("/sys/block/*")):
            name = os.path.basename(device)
            if name.startswith(("loop", "ram", "dm-", "sr")):
                continue
            sectors = to_int(read_text(f"{device}/size"), 0) or 0
            rotational = read_text(f"{device}/queue/rotational")
            physical.append(
                {
                    "model": clean(read_text(f"{device}/device/model")) or name,
                    "device": f"/dev/{name}",
                    "size_bytes": sectors * 512,
                    "media_type": {"0": "SSD", "1": "HDD"}.get(rotational),
                    "serial_number": clean(read_text(f"{device}/device/serial")),
                    "status": "OK",
                }
            )

        logical = []
        for line in self._tablica_montowan().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mount, filesystem = parts[0], parts[1], parts[2]
            if not device.startswith("/dev/") or filesystem in {"squashfs", "iso9660"}:
                continue
            try:
                stat = os.statvfs(mount)
            except OSError:
                continue
            size = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            if size == 0:
                continue
            logical.append(
                {
                    "mount": mount,
                    "device": device,
                    "filesystem": filesystem,
                    "size_bytes": size,
                    "free_bytes": free,
                    "used_percent": percent(size - free, size),
                }
            )
        return {"physical_disks": self.limit(physical), "logical_disks": self.limit(logical)}

    def _tablica_montowan(self) -> str:
        """Tablica montowan systemu, a nie ta widziana przez samego agenta.

        Usluga agenta dziala z PrivateTmp i ReadWritePaths, wiec we wlasnej
        przestrzeni nazw widzi dodatkowe montowania podpiete pod /tmp i pod
        swoj katalog danych. Trafialy one do inwentarza jako osobne wolumeny
        na tym samym dysku - czyli jako cos, czego na maszynie nie ma.

        /proc/1/mounts to tablica procesu init, czyli faktyczny obraz systemu.
        Gdy jest nieczytelna (agent bez roota, nietypowy kontener), zostaje
        widok wlasny - lepszy niz brak danych.
        """
        return read_text("/proc/1/mounts") or read_text("/proc/mounts")

    def collect_os(self) -> dict:
        release = {}
        for line in read_text("/etc/os-release").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                release[key.strip()] = value.strip().strip('"')

        uptime_raw = read_text("/proc/uptime").split()
        uptime = int(float(uptime_raw[0])) if uptime_raw else None
        last_boot = None
        if uptime is not None:
            last_boot = (datetime.now(timezone.utc) - timedelta(seconds=uptime)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        return {
            "name": release.get("PRETTY_NAME") or release.get("NAME"),
            "version": release.get("VERSION_ID"),
            "build": platform.release(),
            "edition": release.get("VARIANT"),
            "architecture": platform.machine(),
            "last_boot": last_boot,
            "uptime_seconds": uptime,
            "locale": os.environ.get("LANG"),
            "kernel": platform.release(),
        }

    def collect_network(self) -> list:
        interfaces = []
        if shutil.which("ip"):
            try:
                raw = run_command(["ip", "-j", "addr", "show"], timeout=30)
                for entry in json.loads(raw):
                    name = entry.get("ifname")
                    if name == "lo":
                        continue
                    interfaces.append(
                        {
                            "name": name,
                            "description": entry.get("link_type"),
                            "mac_address": entry.get("address"),
                            "ip_addresses": [
                                a.get("local") for a in entry.get("addr_info", []) if a.get("local")
                            ],
                            "gateways": [],
                            "dns_servers": self._resolvers(),
                            "dhcp_enabled": None,
                            "is_up": entry.get("operstate") == "UP",
                        }
                    )
                return self.limit(interfaces)
            except (CommandError, json.JSONDecodeError, TypeError) as exc:
                self.record_error("network.interfaces", f"ip -j addr: {exc}")

        # Awaryjnie (brak iproute2): interfejsy z /sys + adres glowny wykryty
        # przez utworzenie gniazda UDP - nie wysyla zadnego pakietu, a zwraca
        # adres, ktorego jadro uzyloby do wyjscia w swiat.
        default_iface, gateway = self._default_route()
        primary = self._primary_address()
        for path in sorted(glob.glob("/sys/class/net/*")):
            name = os.path.basename(path)
            if name == "lo":
                continue
            operstate = read_text(f"{path}/operstate")
            interfaces.append(
                {
                    "name": name,
                    "mac_address": clean(read_text(f"{path}/address")),
                    "ip_addresses": [primary] if name == default_iface and primary else [],
                    "gateways": [gateway] if name == default_iface and gateway else [],
                    "dns_servers": self._resolvers() if name == default_iface else [],
                    # "unknown" zglaszaja m.in. interfejsy kontenerowe - to nadal dziala.
                    "is_up": operstate in {"up", "unknown"},
                }
            )
        return self.limit(interfaces)

    def _default_route(self) -> tuple[str | None, str | None]:
        """Interfejs i brama trasy domyslnej z /proc/net/route (adresy little-endian hex)."""
        for line in read_text("/proc/net/route").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 3 or fields[1] != "00000000":
                continue
            raw = fields[2]
            try:
                octets = [int(raw[i : i + 2], 16) for i in (6, 4, 2, 0)]
            except ValueError:
                return fields[0], None
            return fields[0], ".".join(str(o) for o in octets)
        return None, None

    def _primary_address(self) -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # TEST-NET-1 (RFC 5737): gniazdo UDP nie nawiazuje polaczenia,
            # a jadro przypisuje lokalny adres wedlug tablicy routingu.
            sock.connect(("192.0.2.1", 53))
            return sock.getsockname()[0]
        except OSError:
            return None
        finally:
            sock.close()

    def _resolvers(self) -> list:
        return [
            line.split()[1]
            for line in read_text("/etc/resolv.conf").splitlines()
            if line.startswith("nameserver") and len(line.split()) > 1
        ]

    def collect_packages(self) -> list:
        managers = [
            (["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${Maintainer}\\n"], "dpkg"),
            (["rpm", "-qa", "--qf", "%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{VENDOR}\\n"], "rpm"),
            (["pacman", "-Q"], "pacman"),
        ]
        for command, source in managers:
            if not shutil.which(command[0]):
                continue
            try:
                output = run_command(command, timeout=180)
            except CommandError as exc:
                self.record_error(f"software.packages[{source}]", str(exc))
                continue
            packages = []
            for line in output.splitlines():
                fields = line.split("\t") if "\t" in line else line.split(None, 1)
                if not fields or not fields[0].strip():
                    continue
                packages.append(
                    {
                        "name": fields[0].strip(),
                        "version": fields[1].strip() if len(fields) > 1 else None,
                        "publisher": fields[2].strip() if len(fields) > 2 else None,
                        "source": source,
                    }
                )
            packages.sort(key=lambda p: p["name"].lower())
            return self.limit(packages)

        self.record_error("software.packages", "nie znaleziono obslugiwanego menedzera pakietow")
        return []

    def collect_services(self) -> list:
        if not shutil.which("systemctl"):
            return []
        try:
            output = run_command(
                ["systemctl", "list-units", "--type=service", "--all", "--no-pager",
                 "--no-legend", "--plain"],
                timeout=60,
                check=False,
            )
        except CommandError as exc:
            self.record_error("software.services", str(exc))
            return []

        services = []
        for line in output.splitlines():
            fields = line.split(None, 4)
            if len(fields) < 4 or not fields[0].endswith(".service"):
                continue
            services.append(
                {
                    "name": fields[0].removesuffix(".service"),
                    "display_name": fields[4].strip() if len(fields) > 4 else None,
                    "state": fields[3],
                    "start_mode": fields[1],
                    "account": None,
                }
            )
        return self.limit(services)

    def collect_processes(self) -> list:
        page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        processes = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                name = read_text(f"/proc/{entry.name}/comm")
                statm = read_text(f"/proc/{entry.name}/statm").split()
                uid = os.stat(f"/proc/{entry.name}").st_uid
            except (OSError, PermissionError):
                continue
            if not name:
                continue
            processes.append(
                {
                    "name": name,
                    "pid": int(entry.name),
                    "memory_bytes": int(statm[1]) * page_size if len(statm) > 1 else None,
                    "user": self._user_names().get(uid, str(uid)),
                }
            )
        processes.sort(key=lambda p: p["memory_bytes"] or 0, reverse=True)
        return self.limit(processes)

    def _user_names(self) -> dict:
        if not hasattr(self, "_uid_cache"):
            self._uid_cache = {
                int(fields[2]): fields[0]
                for fields in (line.split(":") for line in read_text("/etc/passwd").splitlines())
                if len(fields) > 2 and fields[2].isdigit()
            }
        return self._uid_cache

    def collect_local_accounts(self) -> list:
        shadow_status = self._shadow_status()
        accounts = []
        for line in read_text("/etc/passwd").splitlines():
            fields = line.split(":")
            if len(fields) < 7:
                continue
            name, _, uid, gid, gecos, home, shell = fields[:7]
            interactive = not shell.endswith(("nologin", "false"))
            accounts.append(
                {
                    "name": name,
                    "full_name": clean(gecos.split(",")[0]) if gecos else None,
                    "uid": to_int(uid),
                    "gid": to_int(gid),
                    "home": home,
                    "shell": shell,
                    "enabled": interactive and shadow_status.get(name, True),
                    "system_account": (to_int(uid, 0) or 0) < 1000,
                }
            )
        accounts.sort(key=lambda a: a["name"])
        return self.limit(accounts)

    def _shadow_status(self) -> dict:
        """Konto z '!' lub '*' w /etc/shadow jest zablokowane. Wymaga roota."""
        status = {}
        for line in read_text("/etc/shadow").splitlines():
            fields = line.split(":")
            if len(fields) < 2:
                continue
            status[fields[0]] = not fields[1].startswith(("!", "*"))
        return status

    def collect_administrators(self) -> list:
        admins = []
        for line in read_text("/etc/group").splitlines():
            fields = line.split(":")
            if len(fields) < 4 or fields[0] not in {"sudo", "wheel", "admin", "root"}:
                continue
            for member in filter(None, fields[3].split(",")):
                admins.append({"name": member, "type": "uzytkownik", "source": f"grupa {fields[0]}"})
        if not any(a["name"] == "root" for a in admins):
            admins.insert(0, {"name": "root", "type": "uzytkownik", "source": "uid 0"})
        return self.limit(admins)

    def collect_sessions(self) -> list:
        if not shutil.which("who"):
            return []
        try:
            output = run_command(["who"], timeout=20, check=False)
        except CommandError:
            return []
        sessions = []
        for line in output.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 2:
                continue
            sessions.append(
                {
                    "user": fields[0],
                    "session_type": fields[1],
                    "logon_time": fields[2].strip() if len(fields) > 2 else None,
                }
            )
        return self.limit(sessions)
