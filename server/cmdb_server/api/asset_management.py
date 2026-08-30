"""Relacje i jakosc danych; te same uprawnienia i CSRF co panel zasobow."""
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AssetRelation, PortalUser, RELATION_KINDS
from ..services import relations, scoping, quality
from ..services.auth import require_user, verify_csrf, client_ip
from ..services.scoping import TenantContext, audit
from .ui import resolve_tenant, render, _require_write

router = APIRouter(tags=["zasoby"])


@router.get("/relacje")
def relations_page(request: Request, asset_id: str = Query("", max_length=36),
                   page: int = Query(1, ge=1), user: PortalUser = Depends(require_user),
                   ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    if asset_id and scoping.get_asset(db, ctx, asset_id) is None:
        raise HTTPException(404, "nie znaleziono zasobu")
    rows = relations.list_relations(db, ctx, asset_id)
    assets = db.execute(scoping.assets_query(ctx)).scalars().all()
    return render(request, "relations.html", user, ctx, db, relations=rows[(page-1)*50:page*50],
                  assets=sorted(assets, key=lambda a: a.hostname), kinds=RELATION_KINDS,
                  asset_id=asset_id, page=page, has_next=len(rows) > page*50)


@router.post("/relacje")
def create_relation(request: Request, source_id: str = Form(..., max_length=36),
                    target_id: str = Form(..., max_length=36), kind: str = Form(...),
                    csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
                    ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    relation, created = relations.add_relation(db, ctx, source_id, target_id, kind)
    if created:
        audit(db, ctx, action="asset.relation_added", target=relation.id,
              detail={"source": source_id, "target": target_id, "kind": kind}, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/relacje", status_code=303)


@router.post("/relacje/{relation_id}/delete")
def delete_relation(relation_id: str, request: Request, csrf_token: str = Form(""),
                    user: PortalUser = Depends(require_user),
                    ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    row = db.execute(select(AssetRelation).where(AssetRelation.id == relation_id,
                     AssetRelation.tenant_id == ctx.tenant_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "nie znaleziono relacji")
    audit(db, ctx, action="asset.relation_deleted", target=row.id,
          detail={"source": row.source_id, "target": row.target_id, "kind": row.kind}, ip=client_ip(request))
    db.delete(row)
    db.commit()
    return RedirectResponse("/relacje", status_code=303)


@router.get("/jakosc")
def quality_page(request: Request, issue: str = Query(""), page: int = Query(1, ge=1),
                 user: PortalUser = Depends(require_user),
                 ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    if issue and issue not in quality.QUALITY_LABELS:
        raise HTTPException(422, "nieznany filtr")
    rows, counts, total = quality.quality_rows(db, ctx)
    filtered = [row for row in rows if not issue or issue in row["issues"]]
    return render(request, "quality.html", user, ctx, db, rows=filtered[(page-1)*50:page*50],
                  counts=counts, labels=quality.QUALITY_LABELS, total=total, affected=len(rows),
                  issue=issue, page=page, has_next=len(filtered) > page*50)
