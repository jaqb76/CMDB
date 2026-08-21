"""API dla agentow: rejestracja maszyny i przyjmowanie raportow.

Tenant NIGDY nie pochodzi z ciala zadania - wynika wylacznie z tokenu.
Agent nie moze wiec zaraportowac maszyny do cudzej firmy.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AgentCredential, Asset, EnrollmentToken, Tenant, utcnow
from ..schemas import (
    EnrollRequest,
    EnrollResponse,
    InventoryReport,
    InventoryResponse,
    UpgradeOffer,
    UpgradeResult,
)
from ..security import generate_token
from ..services.auth import client_ip, require_agent, require_enrollment_token
from ..services import architektura, upgrades, ustawienia
from ..services.inventory import store_report
from ..services.scoping import TenantContext, audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "schema_version": 1, "server_time": utcnow().isoformat()}


@router.post("/agents/enroll", response_model=EnrollResponse, status_code=status.HTTP_201_CREATED)
def enroll(
    payload: EnrollRequest,
    request: Request,
    db: Session = Depends(get_db),
    auth: tuple[EnrollmentToken, Tenant] = Depends(require_enrollment_token),
) -> EnrollResponse:
    """Wymienia firmowy token rejestracyjny na indywidualne poswiadczenie agenta."""
    enrollment_token, tenant = auth
    settings = get_settings()
    ip = client_ip(request)

    ctx = TenantContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        actor=f"enroll:{enrollment_token.prefix}",
        can_write=True,
    )

    asset = db.execute(
        select(Asset).where(Asset.tenant_id == tenant.id, Asset.machine_id == payload.machine_id)
    ).scalar_one_or_none()

    created = asset is None
    if asset is None:
        asset = Asset(
            tenant_id=tenant.id,
            machine_id=payload.machine_id,
            hostname=payload.identity.hostname,
            fqdn=payload.identity.fqdn,
            domain=payload.identity.domain,
            os_family=payload.identity.os_family,
            arch=architektura.normalizuj(payload.identity.arch),
            agent_version=payload.agent_version,
            tags=[],
            facts={},
        )
        db.add(asset)
        db.flush()
    else:
        asset.hostname = payload.identity.hostname
        asset.agent_version = payload.agent_version
        asset.is_active = True
        # Ponowna rejestracja tej samej maszyny uniewaznia poprzednie poswiadczenia,
        # zeby nie zostawiac dzialajacych kluczy po reinstalacji agenta.
        previous = db.execute(
            select(AgentCredential).where(
                AgentCredential.asset_id == asset.id, AgentCredential.revoked_at.is_(None)
            )
        ).scalars().all()
        for cred in previous:
            cred.revoked_at = utcnow()

    token = generate_token("agt")
    db.add(
        AgentCredential(
            tenant_id=tenant.id,
            asset_id=asset.id,
            enrollment_token_id=enrollment_token.id,
            prefix=token.prefix,
            token_hash=token.token_hash,
        )
    )
    audit(
        db,
        ctx,
        action="agent.enroll",
        target=asset.hostname,
        detail={
            "asset_id": asset.id,
            "machine_id": payload.machine_id,
            "new_asset": created,
            "credential_prefix": token.prefix,
        },
        ip=ip,
    )
    db.commit()

    log.info(
        "enrollment tenant=%s asset=%s hostname=%s new=%s",
        tenant.slug,
        asset.id,
        asset.hostname,
        created,
    )
    return EnrollResponse(
        asset_id=asset.id,
        tenant_slug=tenant.slug,
        agent_token=token.plaintext,
        server_time=utcnow(),
        report_interval_seconds=ustawienia.interwal_raportowania(tenant),
    )


@router.post("/inventory", response_model=InventoryResponse)
def submit_inventory(
    report: InventoryReport,
    request: Request,
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> InventoryResponse:
    """Przyjmuje pelny raport inwentaryzacyjny i zapisuje go jako snapshot JSON."""
    credential, ctx = auth
    settings = get_settings()

    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    # Poswiadczenie jest przypisane do konkretnej maszyny - raport musi sie zgadzac.
    if report.machine_id != asset.machine_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="machine_id nie zgadza sie z poswiadczeniem - wymagana ponowna rejestracja",
        )

    snapshot, changed = store_report(db, ctx, asset, report)
    db.commit()

    if report.errors:
        log.warning(
            "agent zglosil %d bledow kolektorow asset=%s", len(report.errors), asset.hostname
        )

    return InventoryResponse(
        asset_id=asset.id,
        snapshot_id=snapshot.id if snapshot else None,
        changed=changed,
        server_time=utcnow(),
        report_interval_seconds=ustawienia.interwal_raportowania(
            db.get(Tenant, ctx.tenant_id)
        ),
        upgrade=_oferta_aktualizacji(db, asset),
    )


# --- aktualizacja agenta ----------------------------------------------------
#
# Komunikacja pozostaje jednostronna: serwer nigdy nie laczy sie z maszyna.
# Agent sam pyta o oczekiwana wersje i sam pobiera plik tym samym polaczeniem
# HTTPS, ktorym raportuje.

def _oferta_aktualizacji(db: Session, asset: Asset) -> UpgradeOffer:
    wydanie = upgrades.wersja_docelowa(db, asset)
    if not upgrades.czy_wymaga_aktualizacji(asset, wydanie):
        return UpgradeOffer(available=False, current_version=asset.agent_version)
    return UpgradeOffer(
        available=True,
        version=wydanie.version,
        sha256=wydanie.sha256,
        size_bytes=wydanie.size_bytes,
        current_version=asset.agent_version,
        kind="zrodla" if upgrades.czy_zrodla(wydanie) else "plik",
    )


@router.get("/agent/version", response_model=UpgradeOffer)
def sprawdz_wersje(
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> UpgradeOffer:
    """Odpytywane przez agenta przed zaplanowana synchronizacja.

    Dzieki temu raport powstaje juz z wersji, ktora ma byc na maszynie,
    zamiast czekac na kolejny cykl.
    """
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")
    oferta = _oferta_aktualizacji(db, asset)
    db.commit()
    return oferta


@router.get("/agent/release")
def pobierz_wersje(
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> FileResponse:
    """Wydaje plik wersji oczekiwanej na TEJ maszynie.

    Agent nie podaje, co chce pobrac - serwer wydaje dokladnie to, co sam
    wskazal jako wersje docelowa. Nie ma tu wiec parametru, ktorym dalo by
    sie wyciagnac dowolny plik z magazynu.
    """
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    wydanie = upgrades.wersja_docelowa(db, asset)
    if wydanie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dla tej maszyny nie ustawiono wersji docelowej",
        )

    sciezka = upgrades.sciezka_pliku(wydanie)
    if not sciezka.is_file():
        log.error("brak pliku wersji %s w magazynie (%s)", wydanie.version, sciezka)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plik wersji jest niedostepny na serwerze",
        )

    upgrades.zapisz_wynik(db, asset, wydanie, "pobrana")
    db.commit()
    log.info("maszyna %s pobiera wersje agenta %s", asset.hostname, wydanie.version)

    return FileResponse(
        path=sciezka,
        media_type="application/octet-stream",
        filename=wydanie.filename,
        headers={"X-CMDB-SHA256": wydanie.sha256, "X-CMDB-Version": wydanie.version},
    )


@router.post("/agent/upgrade-result")
def zglos_wynik_aktualizacji(
    wynik: UpgradeResult,
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> dict:
    """Agent zglasza, co sie stalo - dzieki temu w panelu widac nieudane proby."""
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    wydanie = upgrades.wersja_docelowa(db, asset)
    upgrades.zapisz_wynik(db, asset, wydanie, wynik.status, wynik.detail)
    db.commit()

    if wynik.status == "blad":
        log.warning(
            "aktualizacja agenta na %s nie powiodla sie (%s): %s",
            asset.hostname, wynik.version, wynik.detail,
        )
    return {"status": "przyjeto"}
