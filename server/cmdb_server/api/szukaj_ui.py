"""Globalna wyszukiwarka: pole w gornym pasku, strona wynikow i podpowiedzi."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PortalUser
from ..services import rodzaje, szukaj
from ..services.auth import require_user
from ..services.scoping import TenantContext
from .ui import render, resolve_tenant

router = APIRouter(tags=["szukaj"])


@router.get("/szukaj", response_class=HTMLResponse)
def strona(request: Request, q: str = Query("", max_length=200),
           user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
           db: Session = Depends(get_db)) -> Response:
    return render(request, "szukaj.html", user, ctx, db, wynik=szukaj.wszystko(db, ctx, user, q),
                  min_dlugosc=szukaj.MIN_DLUGOSC, typy=rodzaje.etykiety(db, ctx))


@router.get("/szukaj.json")
def podpowiedzi(q: str = Query("", max_length=200),
                user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
                db: Session = Depends(get_db)) -> Response:
    # Wyniki zaleza od konta i firmy - nie do pamieci podrecznej po drodze.
    return JSONResponse({"q": q.strip(), "wyniki": szukaj.podpowiedzi(db, ctx, user, q)},
                        headers={"Cache-Control": "no-store"})
