"""Generator przykladowych raportow agenta."""
from __future__ import annotations

from datetime import datetime, timezone


def build_report(
    machine_id: str = "win-machine-0001",
    hostname: str = "WS-KSIEGOWOSC-01",
    packages: list[dict] | None = None,
    uptime: int = 1000,
) -> dict:
    if packages is None:
        packages = [
            {"name": "7-Zip 23.01", "version": "23.01", "publisher": "Igor Pavlov", "source": "registry"},
            {"name": "Mozilla Firefox", "version": "128.0", "publisher": "Mozilla", "source": "registry"},
        ]
    return {
        "schema_version": 1,
        "machine_id": machine_id,
        "agent": {
            "version": "0.1.0",
            "collector": "windows",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 812,
        },
        "identity": {
            "hostname": hostname,
            "fqdn": f"{hostname.lower()}.firma.local",
            "domain": "firma.local",
            "os_family": "windows",
            "arch": "amd64",
        },
        "hardware": {
            "system": {
                "manufacturer": "Dell Inc.",
                "model": "OptiPlex 7090",
                "serial_number": "SN-ABC-123",
                "chassis": "Desktop",
                "virtualization": None,
            },
            "cpu": {
                "model": "Intel Core i5-11500",
                "physical_cores": 6,
                "logical_cores": 12,
                "max_clock_mhz": 2700,
                "architecture": "x64",
                "sockets": 1,
            },
            "memory": {
                "total_bytes": 17179869184,
                "available_bytes": 8000000000,
                "modules": [
                    {"slot": "DIMM A", "capacity_bytes": 8589934592, "type": "DDR4", "speed_mhz": 3200}
                ],
            },
            "storage": {
                "physical_disks": [
                    {"model": "Samsung SSD 980", "size_bytes": 512110190592, "media_type": "SSD"}
                ],
                "logical_disks": [
                    {
                        "mount": "C:",
                        "label": "System",
                        "filesystem": "NTFS",
                        "size_bytes": 511000000000,
                        "free_bytes": 210000000000,
                        "used_percent": 58.9,
                    }
                ],
            },
            "firmware": {"version": "1.14.0", "release_date": "2024-02-11"},
        },
        "os": {
            "name": "Microsoft Windows 11 Pro",
            "version": "10.0.22631",
            "build": "22631",
            "edition": "Professional",
            "architecture": "64-bit",
            "install_date": "2023-05-04T10:11:00Z",
            "last_boot": "2026-08-18T06:00:00Z",
            "uptime_seconds": uptime,
            "locale": "pl-PL",
            "timezone": "Central European Time",
        },
        "network": {
            "interfaces": [
                {
                    "name": "Ethernet",
                    "description": "Intel I219-LM",
                    "mac_address": "00:11:22:33:44:55",
                    "ip_addresses": ["10.10.5.21"],
                    "gateways": ["10.10.5.1"],
                    "dns_servers": ["10.10.5.10"],
                    "dhcp_enabled": True,
                    "is_up": True,
                }
            ]
        },
        "software": {
            "packages": packages,
            "services": [
                {"name": "Spooler", "display_name": "Print Spooler", "state": "Running", "start_mode": "Auto"},
                {"name": "wuauserv", "display_name": "Windows Update", "state": "Stopped", "start_mode": "Manual"},
            ],
            "updates": [{"id": "KB5034123", "installed_on": "2026-07-10"}],
            "processes": [{"name": "explorer.exe", "pid": 4321, "memory_bytes": 120000000}],
        },
        "users": {
            "local_accounts": [
                {"name": "Administrator", "enabled": False, "sid": "S-1-5-21-...-500"},
                {"name": "jkowalski", "enabled": True, "full_name": "Jan Kowalski", "sid": "S-1-5-21-...-1001"},
            ],
            "administrators": [{"name": "FIRMA\\Domain Admins", "type": "group"}],
            "sessions": [{"user": "FIRMA\\jkowalski", "session_type": "console"}],
        },
        "errors": [],
    }
