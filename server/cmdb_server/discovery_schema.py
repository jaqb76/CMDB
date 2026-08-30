"""Bounded, untrusted network observations supplied by an enrolled scanner."""
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, ip_network
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRIVATE = tuple(ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ShortText = Annotated[str, Field(max_length=240)]


class DiscoveredDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: IPv4Address
    mac: str = Field(default="", pattern=r"^(?:|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})$")
    hostname: str = Field(default="", max_length=255)
    ports: list[Annotated[int, Field(strict=True, ge=1, le=65535)]] = Field(default_factory=list, max_length=32)
    device_type: Literal["komputer", "drukarka", "siec", "inne"] = "inne"
    os_hint: str = Field(default="", max_length=200)
    manufacturer: str = Field(default="", max_length=200)
    confidence: Literal["unknown", "low", "medium"] = "unknown"
    evidence: list[ShortText] = Field(default_factory=list, max_length=32)

    @field_validator("ip")
    @classmethod
    def private_ip(cls, value):
        if not any(value in n for n in PRIVATE):
            raise ValueError("discovery: tylko prywatne IPv4")
        return value


class NetworkDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scanned_at: datetime
    ranges: list[Annotated[str, Field(max_length=32)]] = Field(max_length=32)
    devices: list[DiscoveredDevice] = Field(max_length=4096)
    errors: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=32)
    complete: bool
    attempted_hosts: int = Field(ge=0, le=4096)
    total_hosts: int = Field(ge=0, le=4096)

    @model_validator(mode="after")
    def consistent(self):
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=timezone.utc)
        if self.scanned_at > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError("discovery: data skanowania z przyszlosci")
        networks = [ip_network(c, strict=True) for c in self.ranges]
        if any(n.version != 4 or not any(n.subnet_of(p) for p in PRIVATE) for n in networks):
            raise ValueError("discovery: tylko prywatne zakresy IPv4")
        total = sum(n.num_addresses if n.prefixlen >= 31 else n.num_addresses - 2 for n in networks)
        if total > 4096 or self.total_hosts != total or self.attempted_hosts > total:
            raise ValueError("discovery: niespojny zakres lub licznik")
        if self.complete and self.attempted_hosts != total:
            raise ValueError("discovery: niepelny skan oznaczony jako pelny")
        if len({d.ip for d in self.devices}) != len(self.devices):
            raise ValueError("discovery: powtorzony adres IP")
        if len(self.devices) > total or any(not any(d.ip in n for n in networks) for d in self.devices):
            raise ValueError("discovery: urzadzenie poza zakresem")
        return self
