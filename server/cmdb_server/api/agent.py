"""API dla agentow: rejestracja maszyny i przyjmowanie raportow.

Tenant NIGDY nie pochodzi z ciala zadania - wynika wylacznie z tokenu.
Agent nie moze wiec zaraportowac maszyny do cudzej firmy.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AgentCredential, Asset, EnrollmentToken, Tenant, utcnow
from ..schemas import EnrollRequest, EnrollResponse, InventoryReport, InventoryResponse
from ..security import generate_token
from ..services.auth import client_ip, require_agent, require_enrollment_token
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
        report_interval_seconds=settings.report_interval_seconds,
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
        report_interval_seconds=settings.report_interval_seconds,
    )
