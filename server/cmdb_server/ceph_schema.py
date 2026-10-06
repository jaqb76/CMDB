"""Wynik odczytu klastra Ceph (Ceph Dashboard) od agenta.

Jak przy wirtualizacji (nutanix_schema.py): agent sprowadza odpowiedzi API
do plaskiej postaci, a limity chronia baze przed raportem, ktory urosl
z bledu, a nie z wielkosci klastra.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_TEKST = 255
_FSID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HostCeph(_Model):
    nazwa: str = Field(min_length=1, max_length=_TEKST)
    adres: str | None = Field(default=None, max_length=64)
    role: list[str] = Field(default_factory=list, max_length=20)
    osd: int | None = Field(default=None, ge=0, le=10_000)
    wersja: str | None = Field(default=None, max_length=128)
    stan: str | None = Field(default=None, max_length=32)


class PulaCeph(_Model):
    nazwa: str = Field(min_length=1, max_length=_TEKST)
    typ: str | None = Field(default=None, max_length=32)
    rozmiar: int | None = Field(default=None, ge=0, le=100)
    min_rozmiar: int | None = Field(default=None, ge=0, le=100)
    pg: int | None = Field(default=None, ge=0)
    aplikacje: list[str] = Field(default_factory=list, max_length=10)
    zajete_bajty: int | None = Field(default=None, ge=0)
    dostepne_bajty: int | None = Field(default=None, ge=0)


class KlasterCeph(_Model):
    ext_id: str = Field(pattern=_FSID)  # fsid
    wersja: str | None = Field(default=None, max_length=128)
    zdrowie: str | None = Field(default=None, max_length=32)
    ostrzezenia: list[str] = Field(default_factory=list, max_length=50)
    pojemnosc_bajty: int | None = Field(default=None, ge=0)
    zajete_bajty: int | None = Field(default=None, ge=0)
    wolne_bajty: int | None = Field(default=None, ge=0)
    liczba_osd: int | None = Field(default=None, ge=0, le=100_000)
    osd_up: int | None = Field(default=None, ge=0, le=100_000)
    osd_in: int | None = Field(default=None, ge=0, le=100_000)
    liczba_mon: int | None = Field(default=None, ge=0, le=100)
    mon_kworum: int | None = Field(default=None, ge=0, le=100)
    liczba_hostow: int | None = Field(default=None, ge=0, le=10_000)
    hosty: list[HostCeph] = Field(default_factory=list, max_length=2000)
    pule: list[PulaCeph] = Field(default_factory=list, max_length=2000)


class WynikCeph(_Model):
    protocol: Literal[1]
    revision: str = Field(max_length=36)
    polaczenie_id: str | None = Field(default=None, max_length=36)
    rodzaj: Literal["test", "odczyt"]
    ok: bool
    blad: str | None = Field(default=None, max_length=2000)
    czas_ms: int | None = Field(default=None, ge=0)
    wersja_pc: str | None = Field(default=None, max_length=128)
    # Dashboard to jeden klaster - lista dla tej samej postaci co przy wirtualizacji.
    klastry: list[KlasterCeph] = Field(default_factory=list, max_length=1)
