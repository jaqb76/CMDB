"""Wynik odczytu platformy wirtualizacji (Prism Central, vCenter) od agenta.

Agent sprowadza odpowiedzi API (Prism v4, vCenter REST) do tej plaskiej postaci, zanim cokolwiek
wysle: serwer nie musi znac ksztaltu API Nutanixa, a zmiana po stronie
Prism konczy sie poprawka jednego pliku w agencie. Limity chronia baze
przed raportem, ktory urosl z bledu, a nie z wielkosci instalacji.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_TEKST = 255


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Klaster(_Model):
    ext_id: str = Field(min_length=1, max_length=64)
    nazwa: str = Field(default="", max_length=_TEKST)
    wersja: str | None = Field(default=None, max_length=128)
    hipernadzorca: str | None = Field(default=None, max_length=128)
    liczba_hostow: int | None = Field(default=None, ge=0, le=10_000)


class Host(_Model):
    ext_id: str = Field(min_length=1, max_length=64)
    nazwa: str = Field(default="", max_length=_TEKST)
    klaster_id: str | None = Field(default=None, max_length=64)
    producent: str | None = Field(default=None, max_length=_TEKST)
    model: str | None = Field(default=None, max_length=_TEKST)
    numer_seryjny: str | None = Field(default=None, max_length=128)
    cpu_model: str | None = Field(default=None, max_length=_TEKST)
    gniazda: int | None = Field(default=None, ge=0, le=64)
    rdzenie: int | None = Field(default=None, ge=0, le=100_000)
    ram_bajty: int | None = Field(default=None, ge=0)
    hipernadzorca: str | None = Field(default=None, max_length=_TEKST)
    ip: str | None = Field(default=None, max_length=64)
    ipmi_ip: str | None = Field(default=None, max_length=64)


class Dysk(_Model):
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    magistrala: str | None = Field(default=None, max_length=32)
    kontener: str | None = Field(default=None, max_length=_TEKST)


class Karta(_Model):
    mac: str | None = Field(default=None, max_length=32)
    ip: list[str] = Field(default_factory=list, max_length=32)
    siec: str | None = Field(default=None, max_length=_TEKST)


class Vm(_Model):
    ext_id: str = Field(min_length=1, max_length=64)
    nazwa: str = Field(default="", max_length=_TEKST)
    klaster_id: str | None = Field(default=None, max_length=64)
    host_id: str | None = Field(default=None, max_length=64)
    stan: str | None = Field(default=None, max_length=32)
    gniazda: int | None = Field(default=None, ge=0, le=512)
    rdzenie_na_gniazdo: int | None = Field(default=None, ge=0, le=512)
    ram_bajty: int | None = Field(default=None, ge=0)
    dyski: list[Dysk] = Field(default_factory=list, max_length=64)
    karty: list[Karta] = Field(default_factory=list, max_length=32)
    ngt: bool | None = None
    system: str | None = Field(default=None, max_length=_TEKST)
    bios_uuid: str | None = Field(default=None, max_length=64)
    opis: str | None = Field(default=None, max_length=1000)


class WynikNutanix(_Model):
    protocol: Literal[1]
    revision: str = Field(max_length=36)
    # Od agenta z wieloma polaczeniami; starszy agent go nie wysyla.
    polaczenie_id: str | None = Field(default=None, max_length=36)
    rodzaj: Literal["test", "odczyt"]
    ok: bool
    blad: str | None = Field(default=None, max_length=2000)
    czas_ms: int | None = Field(default=None, ge=0)
    wersja_pc: str | None = Field(default=None, max_length=128)
    klastry: list[Klaster] = Field(default_factory=list, max_length=200)
    hosty: list[Host] = Field(default_factory=list, max_length=2000)
    vm: list[Vm] = Field(default_factory=list, max_length=20_000)
