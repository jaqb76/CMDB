"""Ustalanie wersji agenta oczekiwanej na danej maszynie.

Zasada dzialania calego mechanizmu: komunikacja jest jednostronna. Serwer
nigdy nie laczy sie z agentem ani niczego mu nie wysyla. Agent przy swoim
cyklicznym przebiegu sam pyta, jaka wersja jest oczekiwana, i sam pobiera
plik po HTTPS - tym samym polaczeniem, ktorym raportuje.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AgentRelease,
    AgentUpgradeLog,
    Asset,
    GlobalAgentTarget,
    TenantAgentTarget,
    utcnow,
)
from . import architektura, pakiet

log = logging.getLogger(__name__)


def wersja_docelowa(db: Session, asset: Asset) -> AgentRelease | None:
    """Wersja oczekiwana na tej maszynie, albo None.

    Kolejnosc od najwezszego do najszerszego:
        1. ustawienie na tej maszynie      - wyjatek, np. jedna maszyna probna
        2. ustawienie dla firmy            - np. wersja probna w jednej firmie
        3. wersja oficjalna dla systemu    - wszystkie pozostale firmy

    Dzieki temu da sie trzymac wersje probna w jednej organizacji, podczas
    gdy reszta pracuje na wersji uznanej za oficjalna.

    Niezaleznie od zrodla wydanie MUSI byc zbudowane dla systemu tej maszyny.
    Agent jest osobnym plikiem dla Windows i dla Linuksa - wyslanie nie tego
    co trzeba konczyloby sie plikiem, ktorego maszyna nie ma jak uruchomic.
    """
    system = (asset.os_family or "").lower()

    if asset.target_release_id:
        wydanie = db.get(AgentRelease, asset.target_release_id)
        if wydanie is None:
            log.warning("maszyna %s wskazuje nieistniejaca wersje agenta", asset.hostname)
        elif _pasuje(wydanie, asset):
            return wydanie
        else:
            log.warning(
                "maszyna %s (%s/%s) ma ustawiona wersje dla %s/%s - pomijam",
                asset.hostname, system or "?", asset.arch or "?",
                wydanie.os_family, wydanie.arch,
            )

    cel = db.execute(
        select(TenantAgentTarget).where(
            TenantAgentTarget.tenant_id == asset.tenant_id,
            TenantAgentTarget.os_family == system,
        )
    ).scalar_one_or_none()
    if cel is not None:
        wydanie = db.get(AgentRelease, cel.release_id)
        if wydanie is not None and _pasuje(wydanie, asset):
            return wydanie

    # Firma bez wlasnego ustawienia korzysta z wersji uznanej za oficjalna.
    # To ten poziom sprawia, ze mozna trzymac wersje probna w jednej firmie,
    # nie ruszajac pozostalych.
    return wersja_oficjalna(db, asset)


def wersja_oficjalna(db: Session, asset: Asset) -> AgentRelease | None:
    """Wersja oznaczona jako aktywna dla systemu tej maszyny."""
    system = (asset.os_family or "").lower()
    if not system:
        return None
    cel = db.execute(
        select(GlobalAgentTarget).where(GlobalAgentTarget.os_family == system)
    ).scalar_one_or_none()
    if cel is None:
        return None
    wydanie = db.get(AgentRelease, cel.release_id)
    return wydanie if wydanie is not None and _pasuje(wydanie, asset) else None


def wersja_dla_firmy(
    db: Session, tenant_id: str, os_family: str, arch: str = "x86_64"
) -> AgentRelease | None:
    """Wersja obowiazujaca w firmie dla danego systemu - bez konkretnej maszyny.

    Uzywane przy pobieraniu instalatora: maszyny jeszcze nie ma w bazie, wiec
    nie da sie odpytac o wersje docelowa dla niej. Zostaja dwa szersze poziomy:
    ustawienie firmy, a w razie jego braku wersja oficjalna.
    """
    system = (os_family or "").lower()
    if not system:
        return None

    cel = db.execute(
        select(TenantAgentTarget).where(
            TenantAgentTarget.tenant_id == tenant_id,
            TenantAgentTarget.os_family == system,
        )
    ).scalar_one_or_none()
    if cel is not None:
        wydanie = db.get(AgentRelease, cel.release_id)
        if wydanie is not None and _zgodne(wydanie, system, arch):
            return wydanie

    cel = db.execute(
        select(GlobalAgentTarget).where(GlobalAgentTarget.os_family == system)
    ).scalar_one_or_none()
    if cel is None:
        return None
    wydanie = db.get(AgentRelease, cel.release_id)
    return wydanie if wydanie is not None and _zgodne(wydanie, system, arch) else None


def _zgodne(wydanie: AgentRelease, os_family: str, arch: str | None) -> bool:
    """Czy wydanie pasuje do pary system + architektura."""
    system = (os_family or "").lower()
    if not system or (wydanie.os_family or "").lower() != system:
        return False

    # Paczka zrodel nie jest zbudowana pod zadna architekture - agent stoi na
    # samej bibliotece standardowej, wiec ta sama paczka dziala na Raspberry Pi
    # i na serwerze x86. Wymaganie zgodnosci architektury odcinaloby ja od
    # wszystkich maszyn.
    if (wydanie.arch or "") == pakiet.ARCH_ZRODLA:
        return True

    # Wariant z ikona w zasobniku nie jest architektura procesora, wiec nie
    # przechodzi przez normalizacje. Pasuje wylacznie wtedy, gdy pytamy o niego
    # wprost - maszyna zglasza sie jako x86_64 i nigdy go nie dostanie jako
    # aktualizacji agenta.
    if (wydanie.arch or "") == architektura.ARCH_TRAY:
        return arch == architektura.ARCH_TRAY

    znormalizowana = architektura.normalizuj(arch)
    if not znormalizowana:
        return False
    return (wydanie.arch or "") == znormalizowana


def czy_zrodla(wydanie: AgentRelease | None) -> bool:
    """Czy wydanie jest paczka zrodel, a nie plikiem wykonywalnym.

    Agent musi to wiedziec, zanim cokolwiek pobierze: instalacja ze zrodel
    to podmiana katalogu, a instalacja pliku - podmiana jednego pliku.
    Pomylenie tych dwoch rzeczy konczy sie maszyna z agentem nie do
    uruchomienia.
    """
    return wydanie is not None and (wydanie.arch or "") == pakiet.ARCH_ZRODLA


def _pasuje(wydanie: AgentRelease, asset: Asset) -> bool:
    """Ostatnia bariera przed wyslaniem pliku, ktorego maszyna nie uruchomi.

    Sprawdzamy system ORAZ architekture. Sama rodzina systemu nie wystarcza:
    ELF dla x86-64 i dla ARM64 to oba "linux", a Raspberry Pi nie uruchomi
    pliku zbudowanego na serwerze x86.

    Maszyna, ktora nie zglosila systemu albo architektury, nie dostaje nic -
    starszy agent lepiej niech zostanie na swojej wersji, niz ma pobrac plik,
    ktorego nie da sie wykonac.
    """
    return _zgodne(wydanie, asset.os_family or "", asset.arch)


def czy_wymaga_aktualizacji(asset: Asset, wydanie: AgentRelease | None) -> bool:
    """Porownujemy dokladna wersje, a nie kolejnosc.

    Celowo: pozwala to takze cofnac flote do wersji wczesniejszej, gdy nowa
    okaze sie wadliwa. Numer wersji agenta nie jest tu porownywany
    semantycznie, bo wystarczy odpowiedz na pytanie "czy to ta, ktora ma byc".
    """
    if wydanie is None:
        return False
    return (asset.agent_version or "") != wydanie.version


def sciezka_pliku(wydanie: AgentRelease) -> Path:
    return Path(get_settings().release_dir) / wydanie.storage_name


def zapisz_wynik(
    db: Session,
    asset: Asset,
    wydanie: AgentRelease | None,
    status: str,
    detail: str | None = None,
) -> None:
    """Odnotowuje przebieg aktualizacji przy maszynie i w dzienniku."""
    asset.upgrade_status = status
    asset.upgrade_detail = (detail or "")[:1000] or None
    asset.upgrade_updated_at = utcnow()

    db.add(
        AgentUpgradeLog(
            tenant_id=asset.tenant_id,
            asset_id=asset.id,
            release_id=wydanie.id if wydanie else None,
            from_version=asset.agent_version,
            to_version=wydanie.version if wydanie else None,
            status=status,
            detail=(detail or "")[:1000] or None,
        )
    )


def wersja_ikony(db: Session, tenant_id: str) -> AgentRelease | None:
    """Wariant z ikona w zasobniku dla tej firmy.

    Ikona NIE podlega polityce wersji i nie da sie jej wskazac jako aktywnej:
    tabela wersji oficjalnych ma UNIQUE(os_family), a to miejsce nalezy do
    agenta - on jest rozsylany samoaktualizacja, ikona nie. Proba szukania
    ikony przez ten sam mechanizm konczyla sie trafieniem na wpis wskazujacy
    agenta i odmowa, bo architektura sie nie zgadzala.

    Bierzemy wiec ikone w TEJ SAMEJ wersji co agent obowiazujacy firme, zeby
    obie czesci pochodzily z jednego budowania. Gdy takiej nie ma - najnowsza
    dostepna, bo stara ikona jest uzyteczniejsza niz zadna: czyta plik statusu,
    ktorego format jest stabilny.
    """
    zapytanie = select(AgentRelease).where(
        AgentRelease.os_family == "windows",
        AgentRelease.arch == architektura.ARCH_TRAY,
    )

    agent = wersja_dla_firmy(db, tenant_id, "windows")
    if agent is not None:
        zgodna = db.execute(
            zapytanie.where(AgentRelease.version == agent.version)
        ).scalars().first()
        if zgodna is not None:
            return zgodna
        log.info(
            "brak ikony w wersji %s - wydaje najnowsza dostepna", agent.version
        )

    return db.execute(
        zapytanie.order_by(AgentRelease.created_at.desc())
    ).scalars().first()
