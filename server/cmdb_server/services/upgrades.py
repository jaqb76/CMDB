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
from ..models import AgentRelease, AgentUpgradeLog, Asset, TenantAgentTarget, utcnow

log = logging.getLogger(__name__)


def wersja_docelowa(db: Session, asset: Asset) -> AgentRelease | None:
    """Wersja oczekiwana na tej maszynie, albo None.

    Kolejnosc: ustawienie maszyny ma pierwszenstwo przed ustawieniem firmy.
    Pozwala to wypchnac nowa wersje na kilka maszyn testowych, nie ruszajac
    reszty floty, a pozniej jednym ruchem objac cala firme.

    Niezaleznie od zrodla wydanie MUSI byc zbudowane dla systemu tej maszyny.
    Agent jest osobnym plikiem dla Windows i dla Linuksa - wyslanie nie tego
    co trzeba konczyloby sie plikiem, ktorego maszyna nie ma jak uruchomic.
    """
    system = (asset.os_family or "").lower()

    if asset.target_release_id:
        wydanie = db.get(AgentRelease, asset.target_release_id)
        if wydanie is None:
            log.warning("maszyna %s wskazuje nieistniejaca wersje agenta", asset.hostname)
        elif _pasuje(wydanie, system):
            return wydanie
        else:
            log.warning(
                "maszyna %s (%s) ma ustawiona wersje dla %s - pomijam",
                asset.hostname, system or "?", wydanie.os_family,
            )

    cel = db.execute(
        select(TenantAgentTarget).where(
            TenantAgentTarget.tenant_id == asset.tenant_id,
            TenantAgentTarget.os_family == system,
        )
    ).scalar_one_or_none()
    if cel is None:
        return None

    wydanie = db.get(AgentRelease, cel.release_id)
    if wydanie is not None and _pasuje(wydanie, system):
        return wydanie
    return None


def _pasuje(wydanie: AgentRelease, system: str) -> bool:
    """Ostatnia bariera przed wyslaniem pliku dla niewlasciwego systemu."""
    if not system:
        # Maszyna, ktora nie zglosila systemu, nie dostaje niczego.
        return False
    return (wydanie.os_family or "").lower() == system


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
