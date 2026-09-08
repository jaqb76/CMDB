"""Kontrakt raportu dostepnosci: agent -> serwer.

Sedno tego kontraktu jest w tym, czego w nim NIE MA. Agent sonduje co minute,
ale nie przysyla sondy - przysyla podsumowanie okresu ("od 12:30 do 12:45,
15 sond, 9 udanych") i to, czego brakowalo ("12:34:34 - 12:40:22, connection
refused"). Doba monitorowania co minute to 96 wierszy zamiast 1440, a procent
dostepnosci wychodzi z nich dokladnie ten sam.

Certyfikat jedzie caly wylacznie przy zmianie odcisku. Odcisk liczy agent
(hashlib wystarczy), a rozbiera go serwer, bo agent chodzi na samej
bibliotece standardowej, w ktorej nie ma parsera X.509.

Kazde pole jest ograniczone co do dlugosci i zakresu: raport przychodzi
z maszyny w sieci klienta i jest danymi, a nie prawda o swiecie.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Wersja kontraktu. Starszy agent i nowszy serwer musza sie rozpoznac.
MONITORING_PROTOCOL = 1

# Ile celow zmiesci sie w jednym raporcie. Ograniczenie nie jest teoretyczne:
# raport przychodzi z zewnatrz i musi miec skonczony rozmiar, zanim zaczniemy
# go rozbierac.
MAKS_CELOW = 500
MAKS_PRZERW = 200


class Przerwa(BaseModel):
    """Jedna przerwa w dzialaniu. Pusty ``do`` znaczy "trwa nadal"."""

    model_config = ConfigDict(extra="forbid")

    od: datetime
    do: datetime | None = None
    blad: str | None = Field(default=None, max_length=500)
    sond: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _kolejnosc(self):
        if self.do is not None and self.do < self.od:
            raise ValueError("przerwa nie moze konczyc sie przed swoim poczatkiem")
        return self


class Okno(BaseModel):
    """Podsumowanie okresu raportowania jednego celu."""

    model_config = ConfigDict(extra="forbid")

    od: datetime
    do: datetime
    sond: int = Field(ge=0, le=1_000_000)
    udanych: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _spojnosc(self):
        if self.do < self.od:
            raise ValueError("okno nie moze konczyc sie przed swoim poczatkiem")
        if self.udanych > self.sond:
            raise ValueError("udanych sond nie moze byc wiecej niz wszystkich")
        return self


class OstatniaSonda(BaseModel):
    """Wynik ostatniej sondy - to on trafia na karte celu w panelu."""

    model_config = ConfigDict(extra="forbid")

    kiedy: datetime
    dostepna: bool
    # Czy agent uznal awarie za potwierdzona (tyle nieudanych prob z rzedu,
    # ile ustawiono). Liczy to agent, bo to on zna kolejne proby - serwer
    # widzi wylacznie podsumowania.
    potwierdzona_awaria: bool = False
    czas_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    kod: int | None = Field(default=None, ge=100, le=599)
    adres: str | None = Field(default=None, max_length=64)
    blad: str | None = Field(default=None, max_length=500)


class StanTls(BaseModel):
    """Co agent zobaczyl przy uscisku dloni TLS.

    Obecnosc tego pola znaczy "TLS doszlo do skutku". Jego brak znaczy, ze
    do TLS w ogole nie doszlo - i wtedy serwer zostawia poprzednia ocene
    lancucha, zamiast oglaszac, ze certyfikat naprawil sie sam.
    """

    model_config = ConfigDict(extra="forbid")

    odcisk: str = Field(max_length=95)
    # None znaczy "nie sprawdzano" (cel ma wylaczona weryfikacje lancucha),
    # a nie "nie wiadomo".
    zaufany: bool | None = None
    blad: str | None = Field(default=None, max_length=500)
    # Caly certyfikat, wylacznie gdy odcisk rozni sie od znanego serwerowi.
    # 32 kB starcza na certyfikat z lancuchem posrednim; wiecej znaczy, ze
    # ktos probuje przeslac cos innego niz certyfikat.
    pem: str | None = Field(default=None, max_length=32768)


class WpisCelu(BaseModel):
    """Wszystko, co agent ma do powiedzenia o jednym celu."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=36)
    okno: Okno | None = None
    przerwy: list[Przerwa] = Field(default_factory=list, max_length=MAKS_PRZERW)
    ostatnia_sonda: OstatniaSonda | None = None
    tls: StanTls | None = None


class RaportDostepnosci(BaseModel):
    """Cala przesylka agenta."""

    model_config = ConfigDict(extra="forbid")

    protocol: int = MONITORING_PROTOCOL
    wyslano: datetime
    # Po co ten raport przyszedl. "okresowy" to podsumowanie kwadransa,
    # "zdarzenie" to awaria albo powrot zglaszany natychmiast - i tylko
    # dlatego alarm nie czeka na koniec okresu.
    powod: str = Field(default="okresowy", max_length=16)
    cele: list[WpisCelu] = Field(default_factory=list, max_length=MAKS_CELOW)

    @model_validator(mode="after")
    def _znany_protokol(self):
        if self.protocol != MONITORING_PROTOCOL:
            raise ValueError(f"nieobslugiwana wersja protokolu monitorowania: {self.protocol}")
        if self.powod not in {"okresowy", "zdarzenie", "start"}:
            raise ValueError("powod musi byc jednym z: okresowy, zdarzenie, start")
        return self


class OdpowiedzMonitorowania(BaseModel):
    przyjeto: int
    pominieto: int
    server_time: datetime
    interwal_raportu: int
