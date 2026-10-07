"""Wynik odczytu Dell OpenManage Enterprise (serwery) od agenta.

Jak przy wirtualizacji (nutanix_schema.py): agent sprowadza odpowiedzi API
do plaskiej postaci, a limity chronia baze przed raportem, ktory urosl
z bledu, a nie z wielkosci instalacji.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_TEKST = 255


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DyskOme(_Model):
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    typ: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    producent: str | None = Field(default=None, max_length=64)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    # Stan z OME, gdy inny niz OK (np. "Critical") - do wyroznienia na karcie.
    stan: str | None = Field(default=None, max_length=32)
    miejsce: str | None = Field(default=None, max_length=128)


class PamiecOme(_Model):
    slot: str | None = Field(default=None, max_length=64)
    rozmiar_bajty: int | None = Field(default=None, ge=0)
    typ: str | None = Field(default=None, max_length=32)
    predkosc: int | None = Field(default=None, ge=0)
    predkosc_robocza: int | None = Field(default=None, ge=0)
    producent: str | None = Field(default=None, max_length=64)
    part: str | None = Field(default=None, max_length=64)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    ranga: str | None = Field(default=None, max_length=32)
    stan: str | None = Field(default=None, max_length=32)


class PortOme(_Model):
    port: str | None = Field(default=None, max_length=64)
    karta: str | None = Field(default=None, max_length=64)
    producent: str | None = Field(default=None, max_length=64)
    opis: str | None = Field(default=None, max_length=_TEKST)
    mac: str | None = Field(default=None, max_length=32)
    lacze: str | None = Field(default=None, max_length=16)
    predkosc_mbps: int | None = Field(default=None, ge=0)


class KontrolerOme(_Model):
    nazwa: str | None = Field(default=None, max_length=128)
    opis: str | None = Field(default=None, max_length=128)
    firmware: str | None = Field(default=None, max_length=64)
    cache_mb: int | None = Field(default=None, ge=0)
    stan: str | None = Field(default=None, max_length=32)
    dyski_wirtualne: int | None = Field(default=None, ge=0)
    uklady: str | None = Field(default=None, max_length=_TEKST)
    wirtualne_z_bledem: list[str] = Field(default_factory=list, max_length=32)


class ZasilaczOme(_Model):
    nazwa: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    moc_w: int | None = Field(default=None, ge=0)
    firmware: str | None = Field(default=None, max_length=32)
    numer_seryjny: str | None = Field(default=None, max_length=64)
    napiecie: int | None = Field(default=None, ge=0)
    stan: str | None = Field(default=None, max_length=32)


class KartaPcieOme(_Model):
    slot: str | None = Field(default=None, max_length=64)
    producent: str | None = Field(default=None, max_length=128)
    opis: str | None = Field(default=None, max_length=_TEKST)


class LicencjaOme(_Model):
    opis: str | None = Field(default=None, max_length=128)
    typ: str | None = Field(default=None, max_length=32)
    stan: str | None = Field(default=None, max_length=32)


class SerwerOme(_Model):
    ext_id: str = Field(min_length=1, max_length=64)  # Service Tag
    nazwa: str = Field(default="", max_length=_TEKST)
    model: str | None = Field(default=None, max_length=_TEKST)
    zdrowie: str | None = Field(default=None, max_length=32)
    zasilanie: str | None = Field(default=None, max_length=32)
    polaczony: bool | None = None
    idrac_ip: str | None = Field(default=None, max_length=64)
    idrac_wersja: str | None = Field(default=None, max_length=64)
    bios_wersja: str | None = Field(default=None, max_length=64)
    cpu_model: str | None = Field(default=None, max_length=_TEKST)
    gniazda: int | None = Field(default=None, ge=0, le=64)
    rdzenie: int | None = Field(default=None, ge=0, le=100_000)
    watki: int | None = Field(default=None, ge=0, le=100_000)
    ram_bajty: int | None = Field(default=None, ge=0)
    dyski: list[DyskOme] = Field(default_factory=list, max_length=64)
    mac: list[str] = Field(default_factory=list, max_length=32)
    system: str | None = Field(default=None, max_length=_TEKST)
    hostname_os: str | None = Field(default=None, max_length=_TEKST)
    obudowa: str | None = Field(default=None, max_length=64)
    inwentaryzacja_o: str | None = Field(default=None, max_length=64)
    # Szczegoly do zakladki "Sprzet (OME)" na karcie.
    pamiec: list[PamiecOme] = Field(default_factory=list, max_length=128)
    porty: list[PortOme] = Field(default_factory=list, max_length=64)
    kontrolery: list[KontrolerOme] = Field(default_factory=list, max_length=16)
    zasilacze: list[ZasilaczOme] = Field(default_factory=list, max_length=16)
    karty_pcie: list[KartaPcieOme] = Field(default_factory=list, max_length=64)
    licencje: list[LicencjaOme] = Field(default_factory=list, max_length=8)


class WynikOme(_Model):
    protocol: Literal[1]
    revision: str = Field(max_length=36)
    polaczenie_id: str | None = Field(default=None, max_length=36)
    rodzaj: Literal["test", "odczyt"]
    ok: bool
    blad: str | None = Field(default=None, max_length=2000)
    czas_ms: int | None = Field(default=None, ge=0)
    wersja_pc: str | None = Field(default=None, max_length=128)
    urzadzenia: list[SerwerOme] = Field(default_factory=list, max_length=5000)

    @property
    def klastry(self) -> list:
        """Opis testu (services/nutanix._opis_testu) liczy "klastry" - tu serwery."""
        return self.urzadzenia
