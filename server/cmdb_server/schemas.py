"""Kontrakt API pomiedzy agentem a serwerem (schema_version = 1).

Raport przyjmujemy "liberalnie": walidujemy naglowek i identyfikacje maszyny,
a sekcje danych (hardware/software/users) przepuszczamy jako dowolny JSON.
Dzieki temu nowsza wersja agenta moze dorzucic pola bez zmiany serwera,
a starszy agent nadal dziala.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1


class MachineIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    hostname: str = Field(min_length=1, max_length=255)
    fqdn: str | None = Field(default=None, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    os_family: str = Field(max_length=32)

    @field_validator("hostname")
    @classmethod
    def _strip_hostname(cls, v: str) -> str:
        return v.strip()


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
    machine_id: str = Field(min_length=8, max_length=128)
    agent: AgentInfo
    identity: MachineIdentity

    hardware: dict[str, Any] = Field(default_factory=dict)
    os: dict[str, Any] = Field(default_factory=dict)
    network: dict[str, Any] = Field(default_factory=dict)
    software: dict[str, Any] = Field(default_factory=dict)
    users: dict[str, Any] = Field(default_factory=dict)
    # Bledy czastkowe: jeden nieudany kolektor nie psuje calego raportu.
    errors: list[dict[str, Any]] = Field(default_factory=list)

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
