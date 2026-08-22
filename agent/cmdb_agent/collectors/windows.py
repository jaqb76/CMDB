"""Kolektor danych dla Windows (PowerShell + CIM/WMI).

Decyzje, ktore warto znac przy modyfikacji:

* Lista oprogramowania czytana jest z kluczy rejestru "Uninstall", a NIE przez
  Win32_Product. Zapytanie do Win32_Product uruchamia na kazdym zainstalowanym
  pakiecie MSI operacje reconfigure - potrafi to trwac minutami, zasmieca dziennik
  zdarzen i realnie psuje instalacje. To najczestszy blad w skryptach inwentarzowych.
* Grupa administratorow wyszukiwana jest po znanym SID S-1-5-32-544, a nie po
  nazwie. Na polskim Windows grupa nazywa sie "Administratorzy" i skrypty
  szukajace "Administrators" zwracaja pusta liste.
* Kazde wywolanie PowerShella idzie przez -EncodedCommand (Base64/UTF-16LE),
  co eliminuje problemy z cudzyslowami i znakami narodowymi w argumentach.
* Wszystkie daty formatujemy jawnie do ISO 8601 UTC - domyslna serializacja
  DateTime przez ConvertTo-Json rozni sie miedzy PowerShell 5.1 a 7.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import socket

from .base import BaseCollector, Step
from . import poprawki
from .common import CommandError, clean, percent, run_command, to_bool, to_int

log = logging.getLogger(__name__)

# Wartosci UUID z SMBIOS, ktore niektorzy producenci wpisuja seryjnie wszystkim
# maszynom - nie nadaja sie na identyfikator.
BAD_UUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "03000200-0400-0500-0006-000700080009",
}

CHASSIS_TYPES = {
    1: "Inna", 2: "Nieznana", 3: "Desktop", 4: "Low Profile Desktop", 5: "Pizza Box",
    6: "Mini Tower", 7: "Tower", 8: "Portable", 9: "Laptop", 10: "Notebook",
    11: "Handheld", 12: "Docking Station", 13: "All in One", 14: "Sub Notebook",
    15: "Space-saving", 16: "Lunch Box", 17: "Main System Chassis", 18: "Expansion Chassis",
    21: "Peripheral Chassis", 22: "Storage Chassis", 23: "Rack Mount Chassis",
    24: "Sealed-case PC", 30: "Tablet", 31: "Convertible", 32: "Detachable",
}

MEMORY_TYPES = {
    20: "DDR", 21: "DDR2", 22: "DDR2 FB-DIMM", 24: "DDR3", 26: "DDR4", 34: "DDR5",
}

# Grupy, ktorych sklad ma znaczenie dla bezpieczenstwa - po SID, nie po nazwie.
SECURITY_GROUPS = {
    "S-1-5-32-544": "Administratorzy",
    "S-1-5-32-555": "Uzytkownicy pulpitu zdalnego",
    "S-1-5-32-551": "Operatorzy kopii zapasowych",
}

# Preambula wspolna dla kazdego skryptu.
# Wyszukiwanie brakujacych aktualizacji odpytuje Windows Update albo WSUS
# i bywa dlugie - kilkanascie sekund na maszynie po swiezej aktualizacji,
# kilka minut na zaniedbanej. Limit pozostalych krokow (180 s) ucinalby ten
# krok tam, gdzie jego wynik jest najbardziej potrzebny.
WINDOWS_UPDATE_TIMEOUT = 420

_PS_PREAMBLE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "function Fmt-Date($d){ if($d){ try{ ([datetime]$d).ToUniversalTime()"
    ".ToString('yyyy-MM-ddTHH:mm:ssZ') }catch{ $null } } else { $null } };"
)


def powershell_executable() -> str:
    """PowerShell 5.1 jest na kazdym Windows; pwsh tylko jesli ktos go doinstalowal."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return candidate if os.path.isfile(candidate) else "powershell.exe"


def run_powershell(script: str, timeout: int = 180):
    """Uruchamia skrypt i zwraca sparsowany JSON (dict albo lista).

    Wynik dekodujemy jako UTF-8. Preambula ustawia [Console]::OutputEncoding,
    co sprawdzono na Windows 11 z polska strona kodowa: nazwy kont z polskimi
    znakami ("Gosc", "Konto domyslne") wracaja nietkniete takze przy
    przekierowaniu stdout do potoku.
    """
    encoded = base64.b64encode((_PS_PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    stdout = run_command(
        [
            powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-OutputFormat", "Text",
            "-EncodedCommand", encoded,
        ],
        timeout=timeout,
    )
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"PowerShell zwrocil dane spoza JSON: {text[:200]}") from exc


def items_of(result) -> list:
    """Skrypty zwracaja listy opakowane w @{items=...} - ConvertTo-Json w PS 5.1
    zamienia jednoelementowa tablice w pojedynczy obiekt, wiec normalizujemy."""
    if isinstance(result, dict):
        value = result.get("items", [])
    else:
        value = result
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class WindowsCollector(BaseCollector):
    name = "windows"
    os_family = "windows"

    def __init__(self, config):
        super().__init__(config)
        self._base_info: dict | None = None

    # --- identyfikacja ----------------------------------------------------

    def _base(self) -> dict:
        if self._base_info is None:
            self._base_info = run_powershell(
                """
                $cs   = Get-CimInstance Win32_ComputerSystem;
                $os   = Get-CimInstance Win32_OperatingSystem;
                $bios = Get-CimInstance Win32_BIOS;
                $csp  = Get-CimInstance Win32_ComputerSystemProduct;
                $enc  = @(Get-CimInstance Win32_SystemEnclosure) | Select-Object -First 1;
                $tz   = Get-CimInstance Win32_TimeZone;
                $guid = $null;
                try { $guid = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Cryptography' -Name MachineGuid).MachineGuid } catch {}
                @{
                  computer_name = $cs.Name;
                  dns_hostname  = $cs.DNSHostName;
                  domain        = $cs.Domain;
                  part_of_domain= $cs.PartOfDomain;
                  workgroup     = $cs.Workgroup;
                  manufacturer  = $cs.Manufacturer;
                  model         = $cs.Model;
                  system_type   = $cs.SystemType;
                  total_memory  = [string]$cs.TotalPhysicalMemory;
                  logged_user   = $cs.UserName;
                  serial_number = $bios.SerialNumber;
                  smbios_uuid   = $csp.UUID;
                  machine_guid  = $guid;
                  chassis       = @($enc.ChassisTypes)[0];
                  bios_version  = $bios.SMBIOSBIOSVersion;
                  bios_vendor   = $bios.Manufacturer;
                  bios_date     = (Fmt-Date $bios.ReleaseDate);
                  os_caption    = $os.Caption;
                  os_version    = $os.Version;
                  os_build      = $os.BuildNumber;
                  os_arch       = $os.OSArchitecture;
                  os_install    = (Fmt-Date $os.InstallDate);
                  os_boot       = (Fmt-Date $os.LastBootUpTime);
                  os_locale     = $(try { (Get-Culture).Name } catch { $os.Locale });
                  os_language   = $os.OSLanguage;
                  os_serial     = $os.SerialNumber;
                  os_free_mem   = [string]$os.FreePhysicalMemory;
                  timezone      = $tz.Caption;
                } | ConvertTo-Json -Depth 4 -Compress
                """
            )
        return self._base_info

    def machine_id(self) -> str:
        """UUID z SMBIOS identyfikuje sprzet i przezywa reinstalacje systemu.
        Gdy producent nie wpisal sensownej wartosci, spadamy na MachineGuid."""
        try:
            info = self._base()
        except Exception as exc:
            self.record_error("identity.machine_id", f"{type(exc).__name__}: {exc}")
            info = {}

        uuid = clean(info.get("smbios_uuid"))
        if uuid and uuid.lower() not in BAD_UUIDS:
            return f"win-uuid-{uuid.lower()}"

        guid = clean(info.get("machine_guid"))
        if guid:
            return f"win-guid-{guid.lower()}"

        # Ostatecznosc: nazwa maszyny. Sygnalizujemy to jawnie w raporcie.
        self.record_error(
            "identity.machine_id",
            "brak wiarygodnego UUID/MachineGuid - uzyto nazwy maszyny jako identyfikatora",
        )
        return f"win-host-{platform.node().lower()}"

    def identity(self) -> dict:
        try:
            info = self._base()
        except Exception:
            info = {}
        hostname = clean(info.get("computer_name")) or platform.node()
        dns_name = clean(info.get("dns_hostname"))
        domain = clean(info.get("domain")) or clean(info.get("workgroup"))
        fqdn = f"{dns_name}.{domain}" if dns_name and info.get("part_of_domain") and domain else None
        if not fqdn:
            try:
                candidate = socket.getfqdn()
                fqdn = candidate if "." in candidate else None
            except OSError:
                fqdn = None
        return {
            "hostname": hostname,
            "fqdn": fqdn,
            "domain": domain,
            "os_family": self.os_family,
            # Agent dla x86-64 nie uruchomi sie na ARM i odwrotnie - serwer
            # musi wiedziec, ktory plik wolno tej maszynie zaproponowac.
            "arch": platform.machine().lower(),
        }

    # --- kroki ------------------------------------------------------------

    def steps(self) -> list[Step]:
        steps: list[Step] = [
            ("hardware.system", self.collect_system),
            ("hardware.firmware", self.collect_firmware),
            ("hardware.cpu", self.collect_cpu),
            ("hardware.memory", self.collect_memory),
            ("hardware.load", self.collect_load),
            ("hardware.storage", self.collect_storage),
            ("hardware.gpus", self.collect_gpus),
            ("os", self.collect_os),
            ("network.interfaces", self.collect_network),
            ("software.packages", self.collect_packages),
            ("users.local_accounts", self.collect_local_accounts),
            ("users.administrators", self.collect_administrators),
            ("users.groups", self.collect_groups),
            ("users.sessions", self.collect_sessions),
        ]
        if self.config.collect_services:
            steps.append(("software.services", self.collect_services))
        if self.config.collect_updates:
            steps.append(("software.updates", self.collect_updates))
        if self.config.collect_pending_updates:
            steps.append(("software.updates_pending", self.collect_pending_updates))
        if self.config.collect_processes:
            steps.append(("software.processes", self.collect_processes))
        return steps

    def collect_system(self) -> dict:
        info = self._base()
        manufacturer = clean(info.get("manufacturer"))
        model = clean(info.get("model"))
        return {
            "manufacturer": manufacturer,
            "model": model,
            "serial_number": clean(info.get("serial_number")),
            "uuid": clean(info.get("smbios_uuid")),
            "machine_guid": clean(info.get("machine_guid")),
            "chassis": CHASSIS_TYPES.get(to_int(info.get("chassis")), None),
            "system_type": clean(info.get("system_type")),
            "domain": clean(info.get("domain")),
            "part_of_domain": to_bool(info.get("part_of_domain"), False),
            "virtualization": detect_virtualization(manufacturer, model),
        }

    def collect_firmware(self) -> dict:
        info = self._base()
        return {
            "vendor": clean(info.get("bios_vendor")),
            "version": clean(info.get("bios_version")),
            "release_date": clean(info.get("bios_date")),
        }

    def collect_os(self) -> dict:
        info = self._base()
        boot = clean(info.get("os_boot"))
        # FreePhysicalMemory z WMI jest w kilobajtach.
        free_kb = to_int(info.get("os_free_mem"))
        return {
            "name": clean(info.get("os_caption")),
            "version": clean(info.get("os_version")),
            "build": clean(info.get("os_build")),
            "edition": _edition_from_caption(clean(info.get("os_caption"))),
            "architecture": clean(info.get("os_arch")),
            "install_date": clean(info.get("os_install")),
            "last_boot": boot,
            "locale": clean(info.get("os_locale")),
            "timezone": clean(info.get("timezone")),
            "free_physical_memory_bytes": free_kb * 1024 if free_kb else None,
        }

    def collect_cpu(self) -> dict:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_Processor | ForEach-Object { @{
                 name = $_.Name; cores = $_.NumberOfCores;
                 logical = $_.NumberOfLogicalProcessors; clock = $_.MaxClockSpeed;
                 socket = $_.SocketDesignation; arch = $_.AddressWidth;
                 id = $_.ProcessorId } }) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        processors = items_of(result)
        if not processors:
            return {}
        first = processors[0]
        return {
            "model": clean(first.get("name")),
            "physical_cores": sum(to_int(p.get("cores"), 0) for p in processors) or None,
            "logical_cores": sum(to_int(p.get("logical"), 0) for p in processors) or None,
            "max_clock_mhz": to_int(first.get("clock")),
            "architecture": f"x{to_int(first.get('arch'), 64)}",
            "sockets": len(processors),
        }

    def collect_memory(self) -> dict:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { @{
                 slot = $_.DeviceLocator; bank = $_.BankLabel;
                 capacity = [string]$_.Capacity; speed = $_.Speed;
                 manufacturer = $_.Manufacturer; serial = $_.SerialNumber;
                 part = $_.PartNumber; type = $_.SMBIOSMemoryType } }) } |
            ConvertTo-Json -Depth 4 -Compress
            """
        )
        modules = []
        for module in items_of(result):
            modules.append(
                {
                    "slot": clean(module.get("slot")) or clean(module.get("bank")),
                    "capacity_bytes": to_int(module.get("capacity")),
                    "speed_mhz": to_int(module.get("speed")),
                    "type": MEMORY_TYPES.get(to_int(module.get("type")), None),
                    "manufacturer": clean(module.get("manufacturer")),
                    "serial_number": clean(module.get("serial")),
                    "part_number": clean(module.get("part")),
                }
            )
        total = to_int(self._base().get("total_memory"))
        if total is None and modules:
            total = sum(m["capacity_bytes"] or 0 for m in modules) or None
        # Pamiec dostepna byla dotad wylacznie w sekcji systemu, przez co
        # wyliczenie zajetosci wymagalo siegania w dwa rozne miejsca zaleznie
        # od systemu. Podajemy ja tam, gdzie jej szukac naturalnie.
        # Win32_OperatingSystem podaje wolna pamiec w KILOBAJTACH - bez
        # przeliczenia zajetosc wychodzilaby bliska stu procent na kazdej
        # maszynie, co wygladaloby na awarie, a bylo by bledem jednostki.
        wolne_kb = to_int(self._base().get("os_free_mem"))
        dostepna = wolne_kb * 1024 if wolne_kb else None
        return {
            "total_bytes": total,
            "available_bytes": dostepna,
            "modules": self.limit(modules),
        }

    def collect_load(self) -> dict:
        """Obciazenie procesora w chwili raportu.

        Windows nie prowadzi odpowiednika sredniej obciazenia z Linuksa, wiec
        bierzemy trzy probki licznika wydajnosci. Jeden odczyt bywa
        przypadkowy, a dluzsze mierzenie opoznialoby kazdy raport.

        Czytamy licznik przez CIM, a nie przez Get-Counter ze sciezka
        opisana po angielsku. Nazwy licznikow wydajnosci sa TLUMACZONE,
        wiec angielska sciezka zawodzi na kazdym systemie
        w innym jezyku - na polskim Windows za kazdym razem.

        To nadal chwila, a nie srednia - i tak jest opisana w polu "source",
        zeby zestawienia nie sugerowaly wiecej, niz wiadomo.
        """
        wynik = run_powershell(
            """
            $wartosci = @();
            foreach ($i in 1..3) {
              $p = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor `
                     -Filter "Name='_Total'" -ErrorAction SilentlyContinue;
              if ($p) { $wartosci += [double]$p.PercentProcessorTime };
              if ($i -lt 3) { Start-Sleep -Seconds 1 }
            };
            @{ probki = $wartosci } | ConvertTo-Json -Depth 3 -Compress
            """,
            timeout=90,
        )
        probki = [p for p in (wynik or {}).get("probki", []) if isinstance(p, (int, float))]
        if not probki:
            return {}
        return {
            "percent": round(sum(probki) / len(probki), 1),
            "samples": len(probki),
            # Licznik zwraca juz procent calego procesora, wiec nie ma czego
            # dzielic przez rdzenie - inaczej niz przy sredniej z Linuksa.
            "source": "licznik-3s",
        }

    def collect_storage(self) -> dict:
        result = run_powershell(
            """
            $physical = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { @{
                 model = $_.Model; size = [string]$_.Size; interface = $_.InterfaceType;
                 serial = $_.SerialNumber; status = $_.Status; partitions = $_.Partitions } });
            $media = @{};
            try {
              foreach ($d in Get-PhysicalDisk) { $media[[string]$d.FriendlyName] = [string]$d.MediaType }
            } catch {}
            $logical = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { @{
                 mount = $_.DeviceID; label = $_.VolumeName; fs = $_.FileSystem;
                 size = [string]$_.Size; free = [string]$_.FreeSpace } });
            @{ physical = $physical; logical = $logical; media = $media } |
            ConvertTo-Json -Depth 5 -Compress
            """
        )
        media_map = result.get("media") or {} if isinstance(result, dict) else {}

        physical = []
        for disk in items_of(result.get("physical") if isinstance(result, dict) else []):
            model = clean(disk.get("model"))
            physical.append(
                {
                    "model": model,
                    "size_bytes": to_int(disk.get("size")),
                    "interface": clean(disk.get("interface")),
                    "serial_number": clean(disk.get("serial")),
                    "status": clean(disk.get("status")),
                    "partitions": to_int(disk.get("partitions")),
                    "media_type": clean(media_map.get(model)) if model else None,
                }
            )

        logical = []
        for volume in items_of(result.get("logical") if isinstance(result, dict) else []):
            size = to_int(volume.get("size"))
            free = to_int(volume.get("free"))
            used = size - free if size is not None and free is not None else None
            logical.append(
                {
                    "mount": clean(volume.get("mount")),
                    "label": clean(volume.get("label")),
                    "filesystem": clean(volume.get("fs")),
                    "size_bytes": size,
                    "free_bytes": free,
                    "used_percent": percent(used, size),
                }
            )
        return {"physical_disks": self.limit(physical), "logical_disks": self.limit(logical)}

    def collect_gpus(self) -> list:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_VideoController | ForEach-Object { @{
                 name = $_.Name; driver = $_.DriverVersion;
                 ram = [string]$_.AdapterRAM } }) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        return self.limit(
            [
                {
                    "name": clean(gpu.get("name")),
                    "driver_version": clean(gpu.get("driver")),
                    "memory_bytes": to_int(gpu.get("ram")),
                }
                for gpu in items_of(result)
            ]
        )

    def collect_network(self) -> list:
        result = run_powershell(
            """
            $adapters = @{};
            foreach ($a in Get-CimInstance Win32_NetworkAdapter) {
              if ($a.NetConnectionID) { $adapters[[string]$a.Index] = @{
                 name = $a.NetConnectionID; up = $a.NetEnabled; speed = [string]$a.Speed } }
            };
            @{ items = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' |
              ForEach-Object {
                $meta = $adapters[[string]$_.Index];
                @{ index = $_.Index; name = $(if($meta){$meta.name}else{$null});
                   description = $_.Description; mac = $_.MACAddress;
                   ips = @($_.IPAddress); gateways = @($_.DefaultIPGateway);
                   dns = @($_.DNSServerSearchOrder); dhcp = $_.DHCPEnabled;
                   dhcp_server = $_.DHCPServer;
                   up = $(if($meta){$meta.up}else{$true}) } }) } |
            ConvertTo-Json -Depth 5 -Compress
            """
        )
        interfaces = []
        for iface in items_of(result):
            interfaces.append(
                {
                    "name": clean(iface.get("name")) or clean(iface.get("description")),
                    "description": clean(iface.get("description")),
                    "mac_address": clean(iface.get("mac")),
                    "ip_addresses": [a for a in (iface.get("ips") or []) if a],
                    "gateways": [g for g in (iface.get("gateways") or []) if g],
                    "dns_servers": [d for d in (iface.get("dns") or []) if d],
                    "dhcp_enabled": to_bool(iface.get("dhcp"), False),
                    "dhcp_server": clean(iface.get("dhcp_server")),
                    "is_up": to_bool(iface.get("up"), True),
                }
            )
        return self.limit(interfaces)

    def collect_packages(self) -> list:
        """Lista z rejestru - swiadomie zamiast Win32_Product (patrz naglowek modulu)."""
        result = run_powershell(
            r"""
            $roots = @(
              'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
            );
            if (-not (Get-PSDrive -Name HKU -ErrorAction SilentlyContinue)) {
              New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS -Scope Script | Out-Null
            };
            foreach ($sid in (Get-ChildItem 'HKU:\' -ErrorAction SilentlyContinue |
                              Where-Object { $_.PSChildName -like 'S-1-5-21-*' -and
                                             $_.PSChildName -notlike '*_Classes' })) {
              $roots += "HKU:\$($sid.PSChildName)\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
            };
            $found = @();
            foreach ($root in $roots) {
              try { $entries = Get-ItemProperty $root -ErrorAction SilentlyContinue } catch { continue };
              foreach ($e in $entries) {
                if (-not $e.DisplayName) { continue };
                if ($e.SystemComponent -eq 1) { continue };
                if ($e.ParentKeyName) { continue };
                if ($e.ReleaseType -in @('Security Update','Update Rollup','Hotfix')) { continue };
                $scope = if ($root -like 'HKU:*') { 'user' }
                         elseif ($root -like '*WOW6432Node*') { 'machine-32' }
                         else { 'machine' };
                $found += @{ name = [string]$e.DisplayName; version = [string]$e.DisplayVersion;
                             publisher = [string]$e.Publisher; install_date = [string]$e.InstallDate;
                             install_location = [string]$e.InstallLocation; scope = $scope }
              }
            };
            @{ items = @($found) } | ConvertTo-Json -Depth 4 -Compress
            """,
            timeout=240,
        )

        packages = []
        seen = set()
        for entry in items_of(result):
            name = clean(entry.get("name"))
            if not name:
                continue
            version = clean(entry.get("version"))
            key = (name.lower(), version or "")
            if key in seen:
                continue
            seen.add(key)
            packages.append(
                {
                    "name": name,
                    "version": version,
                    "publisher": clean(entry.get("publisher")),
                    "install_date": _normalize_install_date(clean(entry.get("install_date"))),
                    "install_location": clean(entry.get("install_location")),
                    "scope": clean(entry.get("scope")),
                    "source": "registry",
                }
            )
        packages.sort(key=lambda p: p["name"].lower())
        return self.limit(packages)

    def collect_services(self) -> list:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_Service | ForEach-Object { @{
                 name = $_.Name; display = $_.DisplayName; state = $_.State;
                 start = $_.StartMode; account = $_.StartName; path = $_.PathName } }) } |
            ConvertTo-Json -Depth 4 -Compress
            """
        )
        services = [
            {
                "name": clean(service.get("name")),
                "display_name": clean(service.get("display")),
                "state": clean(service.get("state")),
                "start_mode": clean(service.get("start")),
                "account": clean(service.get("account")),
                "path": clean(service.get("path")),
            }
            for service in items_of(result)
            if clean(service.get("name"))
        ]
        services.sort(key=lambda s: s["name"].lower())
        return self.limit(services)

    def collect_updates(self) -> list:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_QuickFixEngineering | ForEach-Object { @{
                 id = $_.HotFixID; description = $_.Description;
                 installed = (Fmt-Date $_.InstalledOn) } }) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        updates = [
            {
                "id": clean(update.get("id")),
                "description": clean(update.get("description")),
                "installed_on": clean(update.get("installed")),
            }
            for update in items_of(result)
            if clean(update.get("id"))
        ]
        updates.sort(key=lambda u: u["installed_on"] or "", reverse=True)
        return self.limit(updates)

    def collect_pending_updates(self) -> dict:
        """Aktualizacje czekajace na instalacje, wedlug uslugi Windows Update.

        Win32_QuickFixEngineering mowi tylko, co JEST zainstalowane. Do oceny
        bezpieczenstwa potrzebne jest to, czego BRAKUJE, a to wie wylacznie
        agent Windows Update - ten sam, ktorego uzywa panel sterowania.

        Wyszukiwanie jest kosztowne: odpytuje Windows Update albo WSUS i
        potrafi trwac od kilkunastu sekund do kilku minut. Dlatego limit czasu
        jest tu znacznie wyzszy niz przy pozostalych krokach, a niepowodzenie
        nie psuje calego raportu - konczy sie stanem "nieznany".

        Rozroznienie miedzy pusta lista a stanem "nieznany" jest istotne:
        pierwsze znaczy "sprawdzone, nic nie brakuje", drugie "nie udalo sie
        sprawdzic". W narzedziu do oceny bezpieczenstwa zlanie ich w jedno
        uspokajaloby zamiast ostrzegac.
        """
        skrypt = """
            $wynik = @{ status = 'nieznany'; detail = $null; items = @() };
            try {
              $sesja = New-Object -ComObject Microsoft.Update.Session;
              $szukacz = $sesja.CreateUpdateSearcher();
              $znalezione = $szukacz.Search("IsInstalled=0 and IsHidden=0 and Type='Software'");
              $wynik.items = @($znalezione.Updates | ForEach-Object {
                @{ id = (@($_.KBArticleIDs) -join ',');
                   title = $_.Title;
                   severity = $_.MsrcSeverity;
                   categories = (@($_.Categories | ForEach-Object { $_.Name }) -join '|') } });
              $wynik.status = 'ok';
            } catch {
              $wynik.detail = $_.Exception.Message;
            }
            $wynik | ConvertTo-Json -Depth 5 -Compress
            """
        try:
            odpowiedz = run_powershell(skrypt, timeout=WINDOWS_UPDATE_TIMEOUT)
        except CommandError as exc:
            return poprawki.wynik(
                "windows-update", poprawki.STATUS_NIEZNANY,
                f"wyszukiwanie aktualizacji nie powiodlo sie: {exc}",
            )

        if not isinstance(odpowiedz, dict) or odpowiedz.get("status") != "ok":
            detal = None
            if isinstance(odpowiedz, dict):
                detal = clean(odpowiedz.get("detail"))
            return poprawki.wynik(
                "windows-update", poprawki.STATUS_NIEZNANY,
                detal or "usluga Windows Update nie zwrocila wyniku",
            )

        braki = []
        for pozycja in odpowiedz.get("items") or []:
            tytul = clean(pozycja.get("title"))
            if not tytul:
                continue
            kategorie = (clean(pozycja.get("categories")) or "").lower()
            waga = clean(pozycja.get("severity"))
            identyfikator = clean(pozycja.get("id"))
            braki.append(
                {
                    # MsrcSeverity wypelnia sie wylacznie dla biuletynow
                    # bezpieczenstwa, wiec sama jego obecnosc juz o tym mowi.
                    "id": f"KB{identyfikator}" if identyfikator else tytul,
                    "title": tytul,
                    "current_version": None,
                    "new_version": None,
                    "source_repo": None,
                    "security": bool(waga) or "security" in kategorie,
                    "severity": waga,
                }
            )
        braki.sort(key=lambda b: (not b["security"], b["title"].lower()))
        return poprawki.wynik("windows-update", poprawki.STATUS_OK,
                              entries=self.limit(braki))

    def collect_processes(self) -> list:
        result = run_powershell(
            """
            @{ items = @(Get-CimInstance Win32_Process | ForEach-Object { @{
                 name = $_.Name; pid = $_.ProcessId; memory = [string]$_.WorkingSetSize;
                 path = $_.ExecutablePath } }) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        processes = [
            {
                "name": clean(process.get("name")),
                "pid": to_int(process.get("pid")),
                "memory_bytes": to_int(process.get("memory")),
                "path": clean(process.get("path")),
            }
            for process in items_of(result)
            if clean(process.get("name"))
        ]
        processes.sort(key=lambda p: p["memory_bytes"] or 0, reverse=True)
        return self.limit(processes)

    def collect_local_accounts(self) -> list:
        result = run_powershell(
            """
            $profiles = @{};
            try {
              foreach ($p in Get-CimInstance Win32_NetworkLoginProfile) {
                $profiles[[string]$p.Name] = (Fmt-Date $p.LastLogon)
              }
            } catch {};
            @{ items = @(Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=True' |
              ForEach-Object {
                $full = "$($_.Domain)\\$($_.Name)";
                @{ name = $_.Name; full_name = $_.FullName; description = $_.Description;
                   sid = $_.SID; disabled = $_.Disabled; locked = $_.Lockout;
                   password_expires = $_.PasswordExpires;
                   password_changeable = $_.PasswordChangeable;
                   password_required = $_.PasswordRequired;
                   last_logon = $profiles[$full] } }) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        accounts = [
            {
                "name": clean(account.get("name")),
                "full_name": clean(account.get("full_name")),
                "description": clean(account.get("description")),
                "sid": clean(account.get("sid")),
                "enabled": not to_bool(account.get("disabled"), False),
                "locked": to_bool(account.get("locked"), False),
                "password_expires": to_bool(account.get("password_expires")),
                "password_required": to_bool(account.get("password_required")),
                "last_logon": clean(account.get("last_logon")),
            }
            for account in items_of(result)
            if clean(account.get("name"))
        ]
        accounts.sort(key=lambda a: a["name"].lower())
        return self.limit(accounts)

    def collect_administrators(self) -> list:
        """Grupa wskazana po SID S-1-5-32-544 - nazwa jest zalezna od jezyka systemu."""
        members = self._group_members("S-1-5-32-544")
        return self.limit(members or [])

    def collect_groups(self) -> list:
        groups = []
        for sid, label in SECURITY_GROUPS.items():
            if sid == "S-1-5-32-544":
                continue  # administratorzy maja wlasna sekcje
            try:
                members = self._group_members(sid)
            except Exception as exc:
                self.record_error(f"users.groups[{label}]", f"{type(exc).__name__}: {exc}")
                continue
            # None znaczy "grupy nie ma w tej edycji Windows" - to nie jest
            # blad, tylko opis maszyny. Pusta lista znaczy "grupa jest, ale
            # nikogo w niej nie ma" i tez nie ma czego pokazywac.
            if members:
                groups.append({"name": label, "sid": sid, "members": [m["name"] for m in members]})
        return groups

    def _group_members(self, group_sid: str) -> list | None:
        """Czlonkowie grupy wskazanej przez SID albo None, gdy grupy nie ma.

        Nazwa grupy zalezy od jezyka systemu, wiec wskazujemy ja przez SID
        i dopiero na maszynie tlumaczymy na nazwe. Czesc grup wbudowanych nie
        istnieje w kazdej edycji Windows - "Uzytkownicy pulpitu zdalnego"
        i "Operatorzy kopii zapasowych" sa nieobecne w wydaniu Home. Tlumaczenie
        ich SID-u konczy sie wtedy wyjatkiem, ktory trafial do raportu jako blad
        kolektora, choc opisuje wylacznie to, czego na tej maszynie nie ma.

        Rozroznienie jest istotne: brak grupy to fakt, a nieudany odczyt grupy
        istniejacej to blad wart zgloszenia. Zwracamy None w pierwszym
        przypadku, a wyjatek przepuszczamy w drugim.
        """
        skrypt = """
            $wynik = @{ istnieje = $false; group = $null; items = @() };
            try {
              $sid = New-Object System.Security.Principal.SecurityIdentifier('@@SID@@');
              $nazwa = $sid.Translate([System.Security.Principal.NTAccount]).Value.Split('@@UK@@')[-1];
            } catch {
              # Grupa nie istnieje w tej edycji Windows - nie ma czego czytac.
              $wynik | ConvertTo-Json -Depth 4 -Compress;
              exit 0
            }
            $grupa = [ADSI]"WinNT://./$nazwa,group";
            $out = @();
            foreach ($m in @($grupa.psbase.Invoke('Members'))) {
              $path = $m.GetType().InvokeMember('AdsPath','GetProperty',$null,$m,$null);
              $cls  = $m.GetType().InvokeMember('Class','GetProperty',$null,$m,$null);
              $nm   = $m.GetType().InvokeMember('Name','GetProperty',$null,$m,$null);
              $out += @{ name = [string]$nm; path = [string]$path; class = [string]$cls }
            };
            $wynik.istnieje = $true;
            $wynik.group = $nazwa;
            $wynik.items = @($out);
            $wynik | ConvertTo-Json -Depth 4 -Compress
            """.replace("@@SID@@", group_sid).replace("@@UK@@", chr(92))

        result = run_powershell(skrypt)
        if not isinstance(result, dict) or not to_bool(result.get("istnieje")):
            return None

        local_name = (clean(self._base().get("computer_name")) or "").upper()
        members = []
        for member in items_of(result):
            # AdsPath ma postac WinNT://DOMENA/konto albo WinNT://KOMPUTER/konto.
            parts = [p for p in clean(member.get("path")).replace("WinNT://", "").split("/") if p] \
                if clean(member.get("path")) else []
            scope = parts[-2].upper() if len(parts) >= 2 else ""
            account = parts[-1] if parts else clean(member.get("name"))
            members.append(
                {
                    "name": f"{scope}\\{account}" if scope else account,
                    "type": "grupa" if clean(member.get("class")) == "Group" else "uzytkownik",
                    "source": "lokalne" if scope == local_name or not scope else "domena",
                }
            )
        return members

    def collect_sessions(self) -> list:
        result = run_powershell(
            """
            $users = @();
            foreach ($l in Get-CimInstance Win32_LoggedOnUser) {
              $a = $l.Antecedent;
              if ($a) { $users += "$($a.Domain)\\$($a.Name)" }
            };
            @{ console = (Get-CimInstance Win32_ComputerSystem).UserName;
               items = @($users | Sort-Object -Unique) } | ConvertTo-Json -Depth 4 -Compress
            """
        )
        console_user = clean(result.get("console")) if isinstance(result, dict) else None
        console_key = console_user.lower() if console_user else None

        # Windows zwraca te sama nazwe rozna wielkoscia liter (NEWNODE\jaqb7
        # kontra NewNode\jaqb7) - bez porownania bez wzgledu na wielkosc
        # ten sam uzytkownik trafial na liste dwa razy.
        sessions = []
        seen = set()
        for user in items_of(result):
            name = clean(user)
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            sessions.append(
                {
                    "user": name,
                    "session_type": "konsola" if name.lower() == console_key else "sesja",
                }
            )
        if console_key and console_key not in seen:
            sessions.append({"user": console_user, "session_type": "konsola"})
        return self.limit(sessions)


def detect_virtualization(manufacturer: str | None, model: str | None) -> str | None:
    haystack = f"{manufacturer or ''} {model or ''}".lower()
    signatures = {
        "vmware": "VMware",
        "virtualbox": "VirtualBox",
        "kvm": "KVM",
        "qemu": "QEMU",
        "xen": "Xen",
        "virtual machine": "Hyper-V",
        "hvm domu": "Xen HVM",
        "amazon ec2": "Amazon EC2",
        "google compute engine": "Google Compute Engine",
        "microsoft corporation virtual": "Hyper-V",
        "parallels": "Parallels",
    }
    for needle, label in signatures.items():
        if needle in haystack:
            return label
    return None


def _edition_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    for edition in ("Enterprise", "Education", "Professional", "Pro", "Home", "Standard", "Datacenter"):
        if edition.lower() in caption.lower():
            return edition
    return None


def _normalize_install_date(value: str | None) -> str | None:
    """Rejestr trzyma InstallDate jako 'YYYYMMDD' - zamieniamy na ISO."""
    if not value:
        return None
    digits = value.strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return value
