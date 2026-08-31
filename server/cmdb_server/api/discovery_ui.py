"""Tenant-scoped discovery review and explicit inventory approval."""
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Asset, DiscoveryDevice, DiscoveryScanner, DiscoveryPolicy, PortalUser, TYPY_SPRZETU, utcnow
from ..discovery_policy import ScanPolicy
from pydantic import ValidationError
from ..services.auth import require_user, verify_csrf, client_ip
from ..services import dopasowanie
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
    agents = db.execute(select(Asset).where(Asset.tenant_id == ctx.tenant_id,
        Asset.zrodlo != "reczne", Asset.is_active.is_(True)).order_by(Asset.hostname).limit(200)).scalars().all()
    return render(request, "discovery.html", user, ctx, db, rows=rows[:50], scanners=scanners, agents=agents,
                  page=page, has_next=len(rows) > 50, view=view, q=q, types=TYPY_SPRZETU)


@router.get("/assets/{asset_id}/discovery-policy")
def policy_page(asset_id: str, request: Request, user: PortalUser = Depends(require_user),
                ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    asset = get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(404, "nie znaleziono maszyny")
    row = db.get(DiscoveryPolicy, asset.id)
    return render(request, "discovery_policy.html", user, ctx, db, asset=asset,
                  policy=ScanPolicy.model_validate(row.config) if row else ScanPolicy(),
                  revision=row.revision if row else "unassigned", policy_row=row)


@router.post("/assets/{asset_id}/discovery-policy")
def save_policy(asset_id: str, request: Request, enabled: bool = Form(False), auto_subnets: bool = Form(False),
                cidrs: str = Form("", max_length=2048), interval_seconds: int = Form(86400),
                max_hosts: int = Form(1024), rate: int = Form(32), budget_seconds: int = Form(300),
                revision: str = Form(..., max_length=36), csrf_token: str = Form(""),
                user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
                db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    # Same asset lock for first creation and edits; prevents lost policy updates.
    asset = db.execute(select(Asset).where(Asset.id == asset_id, Asset.tenant_id == ctx.tenant_id)
                       .with_for_update()).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "nie znaleziono maszyny")
    if asset.zrodlo == "reczne" or not asset.is_active:
        raise HTTPException(400, "polityka wymaga aktywnego zasobu z agentem")
    row = db.get(DiscoveryPolicy, asset.id)
    if revision != (row.revision if row else "unassigned"):
        raise HTTPException(409, "polityka zmienila sie; odswiez formularz")
    try:
        policy = ScanPolicy(enabled=enabled, auto_subnets=auto_subnets,
            cidrs=[v.strip() for v in cidrs.replace("\n", ",").split(",") if v.strip()],
            interval_seconds=interval_seconds, max_hosts=max_hosts, rate=rate, budget_seconds=budget_seconds)
    except ValidationError:
        raise HTTPException(422, "Niepoprawna polityka: sprawdz prywatne CIDR, limit hostow i limity czasu/tempa.")
    previous = row.config if row else ScanPolicy().model_dump()
    if row is None:
        row = DiscoveryPolicy(asset_id=asset.id, tenant_id=ctx.tenant_id)
        db.add(row)
    row.config, row.revision = policy.model_dump(), str(uuid4())
    row.updated_by, row.updated_at = user.email, utcnow()
    audit(db, ctx, action="discovery.policy_changed", target=asset.id,
          detail={"before": previous, "after": row.config, "revision": row.revision}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}/discovery-policy", status_code=303)


@router.get("/wykrywanie/{device_id}")
def discovery_detail(device_id: str, request: Request, user: PortalUser = Depends(require_user),
                     ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    device = _candidate(db, ctx, device_id)
    # Podpowiedzi z adresow zgloszonych przez same maszyny - maszyna z kilkoma
    # interfejsami ma kilka adresow, a pole primary_ip zna tylko jeden.
    katalog = dopasowanie.indeks(db, ctx.tenant_id)
    kandydaci = dopasowanie.podpowiedzi(katalog, device.observation.get("mac", ""), device.ip)
    matches = db.execute(select(Asset).where(Asset.tenant_id == ctx.tenant_id,
        or_(Asset.id.in_(kandydaci) if kandydaci else false(),
            Asset.primary_ip == device.ip,
            Asset.hostname == (device.hostname or device.ip)))
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
    device.link_mode = dopasowanie.RECZNIE
    device.link_reason = "potwierdzone przez " + (user.email or "uzytkownika panelu")
    audit(db, ctx, action=action, target=device.id,
          detail={"asset_id": asset.id, "scanner_id": device.scanner_id, "ip": device.ip}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=303)
