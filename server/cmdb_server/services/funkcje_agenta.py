"""Dodatkowe funkcjonalnosci agenta: katalog, stan i slad odbioru konfiguracji.

Agent robi zawsze to samo (inwentaryzacja), a ponad to - tylko to, co mu
wlaczono na jego karcie: skaner sieci, monitorowanie uslug, a docelowo
kolejne integracje. Kazda funkcja ma wlasna konfiguracje i wlasny adres,
spod ktorego agent ja pobiera; ten modul niczego tu nie ujednolica poza
jednym - odpowiedzia na pytanie, co jest wlaczone i czy agent juz o tym wie.

Katalog jest w kodzie, a nie w bazie: funkcja to kod po stronie agenta
i serwera, wiec nie da sie jej "dodac" formularzem.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..discovery_policy import ScanPolicy
from ..models import (
    Asset, DiscoveryPolicy, MonitorUslugi, NutanixUstawienia, OdbiorFunkcjiAgenta, utcnow,
)

SKANER = "skaner"
MONITOROWANIE = "monitorowanie"
NUTANIX = "nutanix"


@dataclass(frozen=True)
class Funkcja:
    klucz: str
    nazwa: str
    grupa: str
    opis: str


# Kolejnosc tu jest kolejnoscia w panelu - grupy wystepuja w kolejnosci
# pierwszego pojawienia sie.
KATALOG: tuple[Funkcja, ...] = (
    Funkcja(SKANER, "Skaner sieci", "Sieć",
            "Wykrywa urządzenia w podsieciach widzianych z tej maszyny."),
    Funkcja(MONITOROWANIE, "Monitorowanie usług", "Sieć",
            "Sprawdza z tej maszyny dostępność usług i ważność certyfikatów."),
    Funkcja(NUTANIX, "Nutanix Prism Central", "Wirtualizacja",
            "Odczytuje z Prism Central klastry, hosty i maszyny wirtualne (tylko odczyt, API v4)."),
)
KLUCZE = {f.klucz for f in KATALOG}


def funkcja(klucz: str) -> Funkcja | None:
    return next((f for f in KATALOG if f.klucz == klucz), None)


def grupy() -> list[tuple[str, list[Funkcja]]]:
    wynik: dict[str, list[Funkcja]] = {}
    for f in KATALOG:
        wynik.setdefault(f.grupa, []).append(f)
    return list(wynik.items())


# --- wersja wydanej konfiguracji --------------------------------------------

def wersja_skanera(row: DiscoveryPolicy | None) -> str:
    """Ta sama rewizja, ktora agent dostaje w odpowiedzi."""
    return row.revision if row else "unassigned"


# Pola odpowiedzi, ktore zmieniaja sie bez zmiany konfiguracji: znacznik
# czasu wydania, znany odcisk certyfikatu (serwer uczy sie go z wynikow)
# i jednorazowe "sprawdz teraz". Liczone do skrotu dawalyby "czeka na
# odebranie" po kazdym raporcie, choc administrator niczego nie zmienial.
_ULOTNE_CELU = ("znany_odcisk", "wymus")


def wersja_monitorowania(polityka: dict) -> str:
    """Skrot tresci polityki monitorowania - ona sama nie ma rewizji."""
    tresc = {
        "enabled": polityka.get("enabled", False),
        "interwal_raportu": polityka.get("interwal_raportu"),
        "cele": [
            {k: v for k, v in cel.items() if k not in _ULOTNE_CELU}
            for cel in polityka.get("cele", [])
        ],
    }
    surowe = json.dumps(tresc, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(surowe.encode()).hexdigest()[:32]


def polityka_monitorowania(db: Session, asset: Asset) -> dict:
    """Dokladnie to, co dostanie agent - wspolne dla endpointu i panelu.

    Gdyby panel liczyl wersje z czego innego niz endpoint, "odebrana" nigdy
    nie zgadzalaby sie z "biezaca" dla maszyny wylaczonej albo przy
    monitorowaniu wylaczonym w calej instalacji.
    """
    from ..config import get_settings
    from . import monitoring

    if not asset.is_active or asset.enrollment_blocked or not get_settings().monitoring_enabled:
        return {"enabled": False, "interwal_raportu": get_settings().monitoring_report_seconds,
                "cele": [], "wydano": utcnow().isoformat()}
    return monitoring.polityka_dla_agenta(db, asset)


def zapisz_odbior(db: Session, asset: Asset, klucz: str, wersja: str) -> None:
    """Zapamietuje, ze agent pobral te wersje. Wola to endpoint agenta.

    Upsert, bo agent przychodzi po konfiguracje wielokrotnie, a dwa procesy
    serwera moga obsluzyc dwa zapytania tej samej maszyny naraz.
    """
    teraz = utcnow()
    db.execute(
        insert(OdbiorFunkcjiAgenta)
        .values(asset_id=asset.id, funkcja=klucz, tenant_id=asset.tenant_id,
                wersja=wersja, odebrano=teraz)
        .on_conflict_do_update(
            index_elements=["asset_id", "funkcja"],
            set_={"wersja": wersja, "odebrano": teraz, "tenant_id": asset.tenant_id},
        )
    )


# --- stan na karcie maszyny -------------------------------------------------

@dataclass
class StanFunkcji:
    funkcja: Funkcja
    wlaczona: bool
    podsumowanie: str
    wersja: str
    odebrana_wersja: str | None
    odebrano: datetime | None

    @property
    def czeka(self) -> bool:
        """Konfiguracja zmieniona, a agent jeszcze jej nie pobral.

        Funkcja nigdy niewlaczona i nigdy niepobrana nie "czeka" - nie ma
        na co. Wylaczenie czeka tak samo jak wlaczenie: do chwili odbioru
        agent pracuje na poprzednim ustawieniu.
        """
        if self.odebrana_wersja is None:
            return self.wlaczona
        return self.odebrana_wersja != self.wersja


def stan(db: Session, asset: Asset) -> list[StanFunkcji]:
    odbiory = {
        o.funkcja: o for o in db.execute(
            select(OdbiorFunkcjiAgenta).where(OdbiorFunkcjiAgenta.asset_id == asset.id)
        ).scalars()
    }

    def _odb(klucz: str) -> tuple[str | None, datetime | None]:
        o = odbiory.get(klucz)
        return (o.wersja, o.odebrano) if o else (None, None)

    wynik: list[StanFunkcji] = []

    row = db.get(DiscoveryPolicy, asset.id)
    polityka = ScanPolicy.model_validate(row.config) if row else ScanPolicy()
    if polityka.enabled:
        zakres = ["automatyczne podsieci"] if polityka.auto_subnets else []
        zakres += polityka.cidrs
        podsumowanie = ", ".join(zakres) or "brak zakresu"
    else:
        # Plakietka "wylaczona" mowi to samo - drugi napis bylby echem.
        podsumowanie = ""
    wynik.append(StanFunkcji(funkcja(SKANER), polityka.enabled, podsumowanie,
                             wersja_skanera(row), *_odb(SKANER)))

    pol = polityka_monitorowania(db, asset)
    liczba = len(pol["cele"])
    wynik.append(StanFunkcji(
        funkcja(MONITOROWANIE), bool(pol["enabled"]),
        f"{liczba} {_cele(liczba)}" if liczba else "brak celów",
        wersja_monitorowania(pol), *_odb(MONITOROWANIE),
    ))

    from . import nutanix
    nx = nutanix.ustawienia(db, asset)
    if nx is not None and nx.wlaczona:
        liczby = nx.odczyt_liczby or {}
        if nx.odczyt_ok and liczby:
            podsumowanie = (f"{nx.adres} · klastry {liczby.get('klastry', 0)}, "
                            f"hosty {liczby.get('hosty', 0)}, VM {liczby.get('vm', 0)}")
        else:
            podsumowanie = nx.adres
    else:
        podsumowanie = ""
    wynik.append(StanFunkcji(funkcja(NUTANIX), bool(nx and nx.wlaczona), podsumowanie,
                             nutanix.wersja(nx), *_odb(NUTANIX)))
    return wynik


def _cele(n: int) -> str:
    if n == 1:
        return "cel"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "cele"
    return "celów"


# --- filtr listy sprzetu ----------------------------------------------------

def warunek_filtra(klucz: str):
    """Warunek SQL "na tej maszynie wlaczono funkcje" dla listy sprzetu."""
    if klucz == SKANER:
        return exists().where(
            DiscoveryPolicy.asset_id == Asset.id,
            DiscoveryPolicy.config["enabled"].as_boolean().is_(True),
        )
    if klucz == MONITOROWANIE:
        return exists().where(
            MonitorUslugi.wykonawca_id == Asset.id,
            MonitorUslugi.aktywny.is_(True),
        )
    if klucz == NUTANIX:
        return exists().where(
            NutanixUstawienia.asset_id == Asset.id,
            NutanixUstawienia.wlaczona.is_(True),
        )
    return None
