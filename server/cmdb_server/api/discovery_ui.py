"""Tenant-scoped discovery review and explicit inventory approval."""
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Asset, DiscoveryDevice, DiscoveryScanner, PortalUser, TYPY_SPRZETU
from ..services.auth import require_user, verify_csrf, client_ip
from ..services.scoping import TenantContext, audit, get_asset
from .ui import resolve_tenant, render, _require_write

router = APIRouter(tags=["wykrywanie"])


def _candidate(db, ctx, device_id, *, lock=False):
    query = select(DiscoveryDevice).where(DiscoveryDevice.id == device_id,
                                         DiscoveryDevice.tenant_id == ctx.tenant_id)
    if lock:
        query = query.with_for_update()
    row = db.execute(query).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "nie znaleziono wykrytego urzadzenia")
    return row


@router.get("/wykrywanie")
def discovery_page(request: Request, view: str = Query("pending", pattern="^(pending|linked|all)$"),
                   q: str = Query("", max_length=255), page: int = Query(1, ge=1),
                   user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
                   db: Session = Depends(get_db)):
    query = select(DiscoveryDevice, Asset.hostname).join(Asset, DiscoveryDevice.scanner_id == Asset.id).where(
        DiscoveryDevice.tenant_id == ctx.tenant_id, Asset.tenant_id == ctx.tenant_id)
    if view != "all":
        query = query.where(DiscoveryDevice.asset_id.is_(None) if view == "pending" else DiscoveryDevice.asset_id.is_not(None))
    if q:
        query = query.where(or_(DiscoveryDevice.ip.contains(q, autoescape=True),
                               DiscoveryDevice.hostname.icontains(q, autoescape=True)))
    rows = db.execute(query.order_by(DiscoveryDevice.last_seen.desc(), DiscoveryDevice.id)
                      .offset((page - 1) * 50).limit(51)).all()
    scanners = db.execute(select(DiscoveryScanner, Asset.hostname).join(Asset, DiscoveryScanner.asset_id == Asset.id)
                          .where(DiscoveryScanner.tenant_id == ctx.tenant_id, Asset.tenant_id == ctx.tenant_id)
                          .order_by(DiscoveryScanner.scanned_at.desc()).limit(50)).all()
    return render(request, "discovery.html", user, ctx, db, rows=rows[:50], scanners=scanners,
                  page=page, has_next=len(rows) > 50, view=view, q=q, types=TYPY_SPRZETU)


@router.get("/wykrywanie/{device_id}")
def discovery_detail(device_id: str, request: Request, user: PortalUser = Depends(require_user),
                     ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    device = _candidate(db, ctx, device_id)
    matches = db.execute(select(Asset).where(Asset.tenant_id == ctx.tenant_id,
        or_(Asset.primary_ip == device.ip, Asset.hostname == (device.hostname or device.ip)))
        .order_by(Asset.hostname).limit(30)).scalars().all()
    return render(request, "discovery_detail.html", user, ctx, db, device=device,
                  scanner=get_asset(db, ctx, device.scanner_id), matches=matches, types=TYPY_SPRZETU)


@router.post("/wykrywanie/{device_id}/adopt")
def adopt(device_id: str, request: Request, hostname: str = Form(..., max_length=255),
          typ: str = Form(..., max_length=32), asset_id: str = Form("", max_length=36),
          csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
          ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    device = _candidate(db, ctx, device_id, lock=True)
    if device.asset_id:
        return RedirectResponse(f"/assets/{device.asset_id}", status_code=303)
    if asset_id:
        asset = get_asset(db, ctx, asset_id)
        if asset is None:
            raise HTTPException(404, "nie znaleziono zasobu w tej firmie")
        # Association only: discovery must not overwrite agent or manual data.
        action = "discovery.linked"
    else:
        if typ not in {"komputer", "drukarka", "siec", "inne"} or not hostname.strip():
            raise HTTPException(422, "podaj nazwe i prawidlowy typ urzadzenia")
        asset = Asset(tenant_id=ctx.tenant_id, machine_id=f"reczne:{uuid4()}",
                      hostname=hostname.strip(), typ=typ, zrodlo="reczne", primary_ip=device.ip,
                      facts={"network_discovery": device.observation, "scanner_id": device.scanner_id})
        db.add(asset)
        db.flush()
        action = "discovery.adopted"
    device.asset_id = asset.id
    audit(db, ctx, action=action, target=device.id,
          detail={"asset_id": asset.id, "scanner_id": device.scanner_id, "ip": device.ip}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=303)
