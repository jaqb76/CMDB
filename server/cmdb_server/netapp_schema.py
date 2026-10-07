"""Wynik odczytu klastra NetApp ONTAP od agenta.

Jak przy wirtualizacji (nutanix_schema.py): agent sprowadza odpowiedzi API
do plaskiej postaci, a limity chronia baze przed raportem, ktory urosl
z bledu, a nie z wielkosci macierzy.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_T = 255


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WezelNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    model: str | None = Field(default=None, max_length=64)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    system_id: str | None = Field(default=None, max_length=32)
    wersja: str | None = Field(default=None, max_length=64)
    stan: str | None = Field(default=None, max_length=32)
    czas_pracy_s: int | None = Field(default=None, ge=0)
    partner_ha: str | None = Field(default=None, max_length=128)
    przejecie: str | None = Field(default=None, max_length=32)
    ip: str | None = Field(default=None, max_length=64)
    lokalizacja: str | None = Field(default=None, max_length=128)


class AgregatNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    wezel: str | None = Field(default=None, max_length=128)
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    zajete_bajty: int | None = Field(default=None, ge=0)
    dostepne_bajty: int | None = Field(default=None, ge=0)
    dyski: int | None = Field(default=None, ge=0)
    raid: str | None = Field(default=None, max_length=32)
    klasa: str | None = Field(default=None, max_length=32)
    stan: str | None = Field(default=None, max_length=32)


class SvmNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    stan: str | None = Field(default=None, max_length=32)
    podtyp: str | None = Field(default=None, max_length=32)
    protokoly: list[str] = Field(default_factory=list, max_length=10)
    ip: list[str] = Field(default_factory=list, max_length=32)


class WolumenNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    svm: str | None = Field(default=None, max_length=128)
    agregat: str | None = Field(default=None, max_length=_T)
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    zajete_bajty: int | None = Field(default=None, ge=0)
    typ: str | None = Field(default=None, max_length=16)
    styl: str | None = Field(default=None, max_length=32)
    sciezka: str | None = Field(default=None, max_length=_T)
    polityka_snapshotow: str | None = Field(default=None, max_length=64)
    root_svm: bool | None = None
    stan: str | None = Field(default=None, max_length=32)


class LunNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    svm: str | None = Field(default=None, max_length=128)
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    zajete_bajty: int | None = Field(default=None, ge=0)
    os: str | None = Field(default=None, max_length=32)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    zmapowany: bool | None = None
    stan: str | None = Field(default=None, max_length=32)


class DyskNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    producent: str | None = Field(default=None, max_length=64)
    typ: str | None = Field(default=None, max_length=16)
    klasa: str | None = Field(default=None, max_length=16)
    rola: str | None = Field(default=None, max_length=32)
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    wezel: str | None = Field(default=None, max_length=128)
    polka: str | None = Field(default=None, max_length=64)
    zatoka: int | None = Field(default=None, ge=0)
    firmware: str | None = Field(default=None, max_length=32)
    agregat: str | None = Field(default=None, max_length=_T)
    stan: str | None = Field(default=None, max_length=32)


class PolkaNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    model: str | None = Field(default=None, max_length=64)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    modul: str | None = Field(default=None, max_length=32)
    dyski: int | None = Field(default=None, ge=0)
    polaczenie: str | None = Field(default=None, max_length=32)
    stan: str | None = Field(default=None, max_length=32)


class InterfejsNetapp(_Model):
    nazwa: str = Field(min_length=1, max_length=_T)
    ip: str | None = Field(default=None, max_length=64)
    maska: str | None = Field(default=None, max_length=16)
    svm: str | None = Field(default=None, max_length=128)
    wezel: str | None = Field(default=None, max_length=128)
    port: str | None = Field(default=None, max_length=32)
    uslugi: str | None = Field(default=None, max_length=_T)
    stan: str | None = Field(default=None, max_length=32)


class SnapmirrorNetapp(_Model):
    zrodlo: str | None = Field(default=None, max_length=_T)
    cel: str | None = Field(default=None, max_length=_T)
    stan: str | None = Field(default=None, max_length=32)
    zdrowy: bool | None = None
    opoznienie: str | None = Field(default=None, max_length=32)
    polityka: str | None = Field(default=None, max_length=64)


class KlasterNetapp(_Model):
    ext_id: str = Field(min_length=1, max_length=64)  # UUID klastra
    nazwa: str = Field(default="", max_length=_T)
    wersja: str | None = Field(default=None, max_length=64)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    lokalizacja: str | None = Field(default=None, max_length=128)
    kontakt: str | None = Field(default=None, max_length=128)
    ip: str | None = Field(default=None, max_length=64)
    wezly: list[WezelNetapp] = Field(default_factory=list, max_length=24)
    agregaty: list[AgregatNetapp] = Field(default_factory=list, max_length=1000)
    svm: list[SvmNetapp] = Field(default_factory=list, max_length=1000)
    wolumeny: list[WolumenNetapp] = Field(default_factory=list, max_length=20_000)
    luny: list[LunNetapp] = Field(default_factory=list, max_length=20_000)
    dyski: list[DyskNetapp] = Field(default_factory=list, max_length=20_000)
    polki: list[PolkaNetapp] = Field(default_factory=list, max_length=1000)
    interfejsy: list[InterfejsNetapp] = Field(default_factory=list, max_length=5000)
    snapmirror: list[SnapmirrorNetapp] = Field(default_factory=list, max_length=20_000)


class WynikNetapp(_Model):
    protocol: Literal[1]
    revision: str = Field(max_length=36)
    polaczenie_id: str | None = Field(default=None, max_length=36)
    rodzaj: Literal["test", "odczyt"]
    ok: bool
    blad: str | None = Field(default=None, max_length=2000)
    czas_ms: int | None = Field(default=None, ge=0)
    wersja_pc: str | None = Field(default=None, max_length=128)
    klastry: list[KlasterNetapp] = Field(default_factory=list, max_length=1)
