"""Kontrakt API pomiedzy agentem a serwerem (schema_version = 1).

Raport przyjmujemy "liberalnie": walidujemy naglowek i identyfikacje maszyny,
a sekcje danych (hardware/software/users) przepuszczamy jako dowolny JSON.
Dzieki temu nowsza wersja agenta moze dorzucic pola bez zmiany serwera,
a starszy agent nadal dziala.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .discovery_schema import NetworkDiscovery

SCHEMA_VERSION = 1


class MachineIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    hostname: str = Field(min_length=1, max_length=255)
    fqdn: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    os_family: str = Field(max_length=32)
    # Starsze agenty tego nie przysylaja - wtedy zostaje None i maszyna nie
    # dostaje propozycji aktualizacji, zamiast dostac plik nie do uruchomienia.
    arch: str | None = Field(default=None, max_length=16)

    @field_validator("hostname", mode="before")
    @classmethod
    def _strip_hostname(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class AgentInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = Field(max_length=32)
    collector: str = Field(max_length=64)
    collected_at: datetime
    duration_ms: int | None = None


class EnrollRequest(BaseModel):
    """Pierwszy kontakt agenta - wymienia token firmowy na wlasny."""

    model_config = ConfigDict(extra="forbid")

    machine_id: str = Field(min_length=8, max_length=128)
    identity: MachineIdentity
    agent_version: str = Field(max_length=32)


class EnrollResponse(BaseModel):
    asset_id: str
    tenant_slug: str
    agent_token: str
    server_time: datetime
    # Sugerowany odstep miedzy raportami (sekundy) - serwer steruje obciazeniem.
    report_interval_seconds: int


class InventoryReport(BaseModel):
    """Pelny raport inwentaryzacyjny."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    report_id: UUID | None = None
    machine_id: str = Field(min_length=8, max_length=128)
    agent: AgentInfo
    identity: MachineIdentity

    network_discovery: NetworkDiscovery | None = None

    hardware: dict[str, Any] = Field(default_factory=dict)
    os: dict[str, Any] = Field(default_factory=dict)
    network: dict[str, Any] = Field(default_factory=dict)
    software: dict[str, Any] = Field(default_factory=dict)
    users: dict[str, Any] = Field(default_factory=dict)
    # Bledy czastkowe: jeden nieudany kolektor nie psuje calego raportu.
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_known_sections(self):
        # Otwarte rozszerzenia pozostaja dozwolone, ale znane kontenery i
        # pola musza spelniac kontrakt funkcji summarize/changes i widokow.
        objects = {
            "hardware": ("system", "cpu", "memory", "storage", "firmware"),
            "software": ("updates_pending",),
        }
        lists = {
            "hardware.memory": ("modules",),
            "hardware.storage": ("physical_disks", "logical_disks"),
            "network": ("interfaces",),
            "software": ("packages", "services", "updates", "processes"),
            "users": ("local_accounts", "administrators", "sessions", "sensitive_groups", "groups"),
            "software.updates_pending": ("packages", "entries"),
        }
        def get(path):
            parts = path.split(".")
            value = getattr(self, parts[0])
            for part in parts[1:]:
                value = value.get(part) if isinstance(value, dict) else None
            return value
        for path, fields in objects.items():
            parent = get(path)
            for field in fields:
                value = parent.get(field)
                if value is not None and not isinstance(value, dict):
                    raise ValueError(f"{path}.{field}: oczekiwano obiektu")
        for path, fields in lists.items():
            parent = get(path)
            if parent is None:
                continue
            for field in fields:
                value = parent.get(field)
                if value is not None and (not isinstance(value, list) or any(not isinstance(v, dict) for v in value)):
                    raise ValueError(f"{path}.{field}: oczekiwano listy obiektow")
        # Znane pola tekstowe wykorzystywane m.in. jako klucze porownania.
        text_fields = {"name", "model", "serial_number", "slot", "mac_address", "version",
                       "display_name", "publisher", "manufacturer", "caption", "source_package",
                       "source_version", "distro_id", "codename", "kernel"}
        known_records = {
            "hardware.system", "hardware.cpu", "hardware.memory", "hardware.firmware",
            "hardware.memory.modules[]", "hardware.storage.physical_disks[]",
            "hardware.storage.logical_disks[]", "software.packages[]", "software.services[]",
            "software.updates[]", "software.processes[]", "software.updates_pending.packages[]",
            "network.interfaces[]", "users.local_accounts[]", "users.administrators[]",
            "users.sessions[]", "users.sensitive_groups[]", "users.groups[]",
            "software.updates_pending.entries[]", "os",
        }
        def check(node, path):
            if isinstance(node, list):
                for item in node:
                    check(item, path + "[]")
            elif isinstance(node, dict):
                for key, value in node.items():
                    if path == "network.interfaces[]" and key == "ip_addresses" and isinstance(value, list):
                        if any(isinstance(v, str) and len(v) > 64 for v in value):
                            raise ValueError("network.interfaces[].ip_addresses: adres zbyt dlugi")
                    if path in known_records and (key in text_fields or (path == "software.updates[]" and key == "id")) and value is not None and not isinstance(value, str):
                        raise ValueError(f"{path}.{key}: oczekiwano tekstu")
                    if path == "network.interfaces[]" and key in {"ip_addresses", "gateways", "dns_servers"} and value is not None:
                        if not isinstance(value, list) or any(not isinstance(v, str) or len(v) > 255 for v in value):
                            raise ValueError(f"{path}.{key}: oczekiwano listy tekstow")
                    check(value, path + "." + key)
        for section in ("hardware", "network", "software", "users", "os"):
            check(getattr(self, section), section)
        for path, limit in {"hardware.system.manufacturer":128, "hardware.system.model":128,
                            "hardware.system.serial_number":128, "os.name":200,
                            "os.caption":200, "os.version":100}.items():
            value = get(path)
            if isinstance(value, str) and len(value) > limit:
                raise ValueError(f"{path}: maksymalnie {limit} znakow")
        return self

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"nieobslugiwana wersja schematu: {v} (oczekiwano {SCHEMA_VERSION})")
        return v


class UpgradeOffer(BaseModel):
    """Informacja o wersji agenta oczekiwanej na tej maszynie.

    Swiadomie NIE ma tu adresu pobierania. Agent sklada go sam z wlasnej
    konfiguracji, wiec nawet podszycie sie pod serwer nie przekieruje go
    po plik na obcy host. Skrot jest obowiazkowy - agent odmawia podmiany,
    gdy pobrany plik sie z nim nie zgadza.
    """

    available: bool = False
    version: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    current_version: str | None = None
    # "plik" albo "zrodla". Instalacja ze zrodel to podmiana katalogu, a pliku
    # - podmiana jednego pliku; agent musi wiedziec, co pobiera, ZANIM zacznie.
    # Starsze agenty tego pola nie znaja i pomijaja je - a zrodel i tak nie
    # dostana, bo zglaszaja sie jako uruchomione z pliku.
    kind: str = "plik"


class UpgradeResult(BaseModel):
    """Wynik proby aktualizacji zglaszany przez agenta."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(max_length=32)
    status: str = Field(max_length=20)
    detail: str | None = Field(default=None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _znany_status(cls, v: str) -> str:
        dozwolone = {"ok", "blad", "pobrana", "odrzucona"}
        if v not in dozwolone:
            raise ValueError(f"status musi byc jednym z: {', '.join(sorted(dozwolone))}")
        return v


class InventoryResponse(BaseModel):
    asset_id: str
    snapshot_id: str | None
    changed: bool
    server_time: datetime
    report_interval_seconds: int
    upgrade: UpgradeOffer | None = None
