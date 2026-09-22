"""Natywne API aplikacji Android.

Uzywa tych samych kont, zakresow firm, regul zapisu i audytu co panel WWW.
Token mobilny jest podpisana sesja uzytkownika przesylana w naglowku Bearer;
nie jest tokenem agenta i nie jest akceptowany przez endpointy agentow.
"""
from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Annotated
from types import SimpleNamespace

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    KATEGORIE_SLOWNIKA,
    LIFECYCLE_AKTYWNY,
    Asset,
    AssetChange,
    AuditLog,
    DefinicjaRaportu,
    PortalUser,
    Tenant,
    WpisSlownika,
    ZRODLO_AGENT,
    utcnow,
)
from ..security import load_session, sign_session
from ..services import logowanie, raporty, rodzaje, scoping, slowniki, ustawienia
from ..services.auth import (
    authenticate_user,
    client_ip,
    firmy_helpdesku,
    firmy_konta,
    tenant_context_for,
    widzi_wszystkie_firmy,
)
from ..services.scoping import TenantContext, audit

router = APIRouter(prefix="/api/v1/mobile", tags=["mobile"])


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class DictionaryBody(BaseModel):
    attributes: dict = Field(default_factory=dict)


class AssignmentBody(BaseModel):
    owner_id: str | None = None
    user_id: str | None = None
    location_id: str | None = None
    role_label: str | None = Field(default=None, max_length=200)
    place: str | None = Field(default=None, max_length=200)


class ReportBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=32)
    frequency: str = Field(default="tygodniowo", max_length=16)
    recipients: str = Field(min_length=1, max_length=4000)
    active: bool = True
    send_to_vendors: bool = False
    columns: list[str] = Field(default_factory=list, max_length=100)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _bearer(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(401, "brak tokenu aplikacji", headers={"WWW-Authenticate": "Bearer"})
    return value.strip()


def mobile_user(request: Request, db: Session = Depends(get_db)) -> PortalUser:
    data = load_session(_bearer(request))
    if not data or data.get("kind") != "mobile" or not data.get("uid"):
        raise HTTPException(401, "token aplikacji jest nieprawidlowy lub wygasl")
    user = db.get(PortalUser, data["uid"])
    if user is None or not user.is_active or data.get("sv") != user.session_version:
        raise HTTPException(401, "sesja zostala wycofana")
    return user


def kontekst_firmy(
    db: Session, user: PortalUser, tenant_slug: str | None
) -> TenantContext | None:
    """Firma, w ktorej pracuje to zapytanie - albo None, gdy nie da sie jej wskazac.

    Trzy przypadki, te same co w panelu (``ui.resolve_tenant``): konto globalne
    wskazuje firme naglowkiem, technik helpdesku wybiera z firm, ktore dostal,
    a zwykle konto ma swoja firme na sztywno. Naglowek spoza listy konta nie
    jest bledem, tylko wraca do pierwszej dozwolonej firmy - tak samo jak
    parametr w adresie panelu.

    Technik helpdesku czesto NIE NALEZY do zadnej firmy: jego uprawnienie to
    wpisy w helpdesk_dostepy. Bez tej sciezki aplikacja pokazywala mu pusta
    liste firm i nie dalo sie z niej wyjsc.
    """
    if widzi_wszystkie_firmy(user):
        if not tenant_slug:
            return None
        tenant = db.execute(
            select(Tenant).where(Tenant.slug == tenant_slug)
        ).scalar_one_or_none()
        if tenant is None or not tenant.is_active:
            return None
        return tenant_context_for(user, tenant)

    firmy = firmy_konta(db, user)
    if not firmy:
        return None
    wybrana = next((t for t in firmy if t.slug == tenant_slug), None) if tenant_slug else None
    wybrana = wybrana or firmy[0]
    return tenant_context_for(
        user, wybrana, helpdesk=wybrana.id in firmy_helpdesku(db, user)
    )


def mobile_context(
    user: PortalUser = Depends(mobile_user),
    tenant_slug: Annotated[str | None, Header(alias="X-CMDB-Tenant")] = None,
    db: Session = Depends(get_db),
) -> TenantContext:
    if widzi_wszystkie_firmy(user) and not tenant_slug:
        raise HTTPException(400, "konto globalne musi wskazac naglowek X-CMDB-Tenant")
    ctx = kontekst_firmy(db, user, tenant_slug)
    if ctx is not None:
        return ctx
    if not widzi_wszystkie_firmy(user) and not firmy_konta(db, user):
        raise HTTPException(403, "konto nie jest przypisane do zadnej firmy")
    raise HTTPException(404, "firma nie istnieje albo jest nieaktywna")


def require_write(ctx: TenantContext) -> None:
    if not ctx.can_write:
        raise HTTPException(403, "konto ma uprawnienia tylko do odczytu")


def dictionary_item(row: WpisSlownika | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "category": row.kategoria,
        "value": row.wartosc,
        "attributes": row.atrybuty or {},
    }


def asset_item(row: Asset) -> dict:
    return {
        "id": row.id,
        "hostname": row.hostname,
        "type": row.typ,
        "source": row.zrodlo,
        "os_family": row.os_family,
        "primary_ip": row.primary_ip,
        "last_seen": _iso(row.last_seen),
        "lifecycle": row.lifecycle,
        "owner": dictionary_item(row.owner),
        "user": dictionary_item(row.uzytkownik),
        "location": dictionary_item(row.lokalizacja),
        "role_label": row.role_label,
        "place": row.miejsce,
    }


@router.post("/auth/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    ip = client_ip(request)
    blocked_until = logowanie.zablokowane_do(db, body.email, ip)
    if blocked_until is not None:
        audit(db, None, action="mobile.login.blocked", target=body.email, ip=ip, actor=body.email)
        db.commit()
        seconds = max(1, ceil((blocked_until - utcnow()).total_seconds()))
        raise HTTPException(
            429,
            "Zbyt wiele błędnych prób. Logowanie jest czasowo zablokowane.",
            headers={"Retry-After": str(seconds)},
        )
    user = authenticate_user(db, body.email, body.password)
    if user is None:
        audit(db, None, action="mobile.login.failed", target=body.email, ip=ip, actor=body.email)
        db.commit()
        logowanie.odnotuj_niepowodzenie(
            db, body.email, ip, settings.login_max_failures, settings.login_lockout_hours
        )
        raise HTTPException(401, "nieprawidlowy e-mail lub haslo")
    logowanie.wyczysc(db, body.email, ip)
    user.last_login_at = utcnow()
    audit(db, None, action="mobile.login.ok", target=user.email, ip=ip, actor=user.email)
    db.commit()
    token = sign_session({"kind": "mobile", "uid": user.id, "sv": user.session_version})
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.session_max_age,
        "user": _user_item(user, tenant),
    }


def _user_item(user: PortalUser, tenant: Tenant | None, ctx: TenantContext | None = None) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "can_write": ctx.can_write if ctx else bool(user.is_superadmin or user.role == "admin"),
        "tenant": ({"id": tenant.id, "name": tenant.name, "slug": tenant.slug} if tenant else None),
    }


@router.get("/me")
def me(user: PortalUser = Depends(mobile_user), db: Session = Depends(get_db),
       tenant_slug: Annotated[str | None, Header(alias="X-CMDB-Tenant")] = None) -> dict:
    """Konto razem z firma, w ktorej wlasnie pracuje.

    Firme wyznacza ta sama droga co dla reszty API, wiec technik helpdesku
    dostaje tu firme, ktora wybral w aplikacji, a nie puste pole - i razem
    z nia prawo do zapisu wyliczone dla tej firmy.
    """
    ctx = kontekst_firmy(db, user, tenant_slug)
    tenant = db.get(Tenant, ctx.tenant_id) if ctx else None
    return _user_item(user, tenant, ctx)


@router.get("/tenants")
def tenants(user: PortalUser = Depends(mobile_user), db: Session = Depends(get_db)) -> list[dict]:
    """Firmy, miedzy ktorymi to konto moze sie przelaczac.

    Ta sama lista co w pasku panelu (``auth.firmy_konta``): wlasna firma konta
    ORAZ firmy nadane w helpdesku. Technik bez wlasnej firmy widzi tu te,
    ktore obsluguje - wczesniej dostawal pusta liste i utykal na wyborze firmy.

    Nieaktywne firmy odpadaja takze kontu globalnemu: w aplikacji lista sluzy
    do wyboru, a wybranie nieaktywnej firmy skonczyloby sie bledem.
    """
    return [{"id": row.id, "name": row.name, "slug": row.slug}
            for row in firmy_konta(db, user) if row.is_active]


@router.get("/dashboard")
def dashboard(ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    tenant = db.get(Tenant, ctx.tenant_id)
    stale_before = ustawienia.granica_aktywnosci(tenant)
    total = db.scalar(select(func.count(Asset.id)).where(Asset.tenant_id == ctx.tenant_id)) or 0
    stale = db.scalar(select(func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.zrodlo == ZRODLO_AGENT, Asset.last_seen < stale_before
    )) or 0
    unassigned = db.scalar(select(func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.owner_id.is_(None)
    )) or 0
    with_agent = db.scalar(select(func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.zrodlo == ZRODLO_AGENT
    )) or 0
    by_os = db.execute(select(Asset.os_family, func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.zrodlo == ZRODLO_AGENT
    ).group_by(Asset.os_family)).all()
    type_labels = rodzaje.etykiety(db, ctx)
    by_type = db.execute(select(Asset.typ, func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id
    ).group_by(Asset.typ).order_by(func.count(Asset.id).desc())).all()
    recent = db.execute(scoping.assets_query(ctx).where(Asset.zrodlo == ZRODLO_AGENT)
                        .order_by(Asset.last_seen.desc()).limit(10)).scalars().all()
    changed = db.execute(scoping.assets_query(ctx).where(Asset.last_change_at.is_not(None))
                         .order_by(Asset.last_change_at.desc()).limit(10)).scalars().all()
    return {
        "total": total, "stale": stale, "unassigned": unassigned, "with_agent": with_agent,
        "by_os": [{"key": key, "label": key or "Nieznany", "count": count} for key, count in by_os],
        "by_type": [{"key": key, "label": type_labels.get(key, key), "count": count} for key, count in by_type],
        "recent": [asset_item(row) for row in recent],
        "changed": [asset_item(row) for row in changed],
    }


@router.get("/assets")
def assets(
    q: str = Query("", max_length=200), page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    os_family: str = Query("", max_length=100),
    unassigned: bool = False,
    ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db),
) -> dict:
    stmt = scoping.assets_query(ctx).where(Asset.lifecycle == LIFECYCLE_AKTYWNY)
    if os_family:
        stmt = stmt.where(Asset.os_family == os_family)
    if unassigned:
        stmt = stmt.where(Asset.owner_id.is_(None))
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Asset.hostname.ilike(pattern), Asset.fqdn.ilike(pattern),
                              Asset.serial_number.ilike(pattern), Asset.primary_ip.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(Asset.hostname).offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {"items": [asset_item(row) for row in rows], "page": page, "page_size": page_size, "total": total}


@router.get("/assets/{asset_id}")
def asset_detail(asset_id: str, ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    row = scoping.get_asset(db, ctx, asset_id)
    if row is None:
        raise HTTPException(404, "nie znaleziono zasobu")
    reading = scoping.current_reading(db, ctx, asset_id)
    return {"asset": asset_item(row), "facts": row.facts or {}, "attributes": row.atrybuty or {},
            "current_report": reading.payload if reading else None}


@router.put("/assets/{asset_id}/assignment")
def update_assignment(body: AssignmentBody, asset_id: str, request: Request,
                      ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    row = scoping.get_asset(db, ctx, asset_id)
    if row is None:
        raise HTTPException(404, "nie znaleziono zasobu")
    def entry(entry_id: str | None, category: str) -> str | None:
        if not entry_id:
            return None
        value = db.execute(select(WpisSlownika).where(WpisSlownika.id == entry_id,
            WpisSlownika.tenant_id == ctx.tenant_id, WpisSlownika.kategoria == category)).scalar_one_or_none()
        if value is None:
            raise HTTPException(400, "wpis slownika nie nalezy do tej firmy")
        return value.id
    row.owner_id = entry(body.owner_id, "osoba")
    row.uzytkownik_id = entry(body.user_id, "osoba")
    row.lokalizacja_id = entry(body.location_id, "lokalizacja")
    row.role_label = body.role_label.strip() if body.role_label else None
    row.miejsce = body.place.strip() if body.place else None
    audit(db, ctx, "asset.mobile_assignment", row.hostname, ip=client_ip(request))
    db.commit(); db.refresh(row)
    return asset_item(row)


@router.get("/dictionaries/{category}")
def dictionary(category: str, ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> list[dict]:
    if category not in KATEGORIE_SLOWNIKA:
        raise HTTPException(404, "nieznana kategoria slownika")
    return [dictionary_item(row) for row in slowniki.wpisy(db, ctx, category)]


@router.get("/dictionaries")
def dictionary_categories(ctx: TenantContext = Depends(mobile_context)) -> list[dict]:
    """Kategorie dostępne w portalu; klient nie utrzymuje własnej stałej listy."""
    return [{"key": key, "label": label} for key, label in KATEGORIE_SLOWNIKA.items()]


@router.get("/dictionaries/{category}/schema")
def dictionary_schema(category: str, ctx: TenantContext = Depends(mobile_context),
                      db: Session = Depends(get_db)) -> dict:
    """Schemat formularza w stabilnym, mobilnym formacie JSON."""
    if category not in KATEGORIE_SLOWNIKA:
        raise HTTPException(404, "nieznana kategoria slownika")
    description = slowniki.schemat(db, ctx, category)
    return {
        "category": category,
        "label": KATEGORIE_SLOWNIKA[category],
        "version": description.wersja,
        "fields": [{
            "key": field.klucz,
            "label": field.etykieta,
            "type": field.typ,
            "required": field.wymagane,
            "group": field.grupa,
            "hint": field.podpowiedz,
            "options": field.opcje,
            "target": field.cel,
            "format": field.format,
            "min": field.min,
            "max": field.max,
        } for field in description.pola],
    }


@router.post("/dictionaries/{category}", status_code=201)
def dictionary_create(category: str, body: DictionaryBody, request: Request,
                      ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    if category not in KATEGORIE_SLOWNIKA:
        raise HTTPException(404, "nieznana kategoria slownika")
    from ..services import schemat as dictionary_schema
    row = slowniki.nowy_szkic(db, ctx, category)
    try:
        slowniki.zapisz_wpis(db, ctx, row, body.attributes)
    except dictionary_schema.BladPola as exc:
        db.rollback()
        raise HTTPException(400, detail=exc.bledy) from exc
    audit(db, ctx, "slownik.mobile_created", f"{category}:{row.wartosc}", ip=client_ip(request))
    db.commit(); db.refresh(row)
    return dictionary_item(row)


@router.put("/dictionaries/{category}/{entry_id}")
def dictionary_update(category: str, entry_id: str, body: DictionaryBody, request: Request,
                      ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    row = slowniki.wpis(db, ctx, entry_id)
    if row is None or row.kategoria != category:
        raise HTTPException(404, "nie znaleziono wpisu slownika")
    from ..services import schemat as dictionary_schema
    try:
        slowniki.zapisz_wpis(db, ctx, row, body.attributes)
    except dictionary_schema.BladPola as exc:
        db.rollback()
        raise HTTPException(400, detail=exc.bledy) from exc
    audit(db, ctx, "slownik.mobile_updated", f"{category}:{row.wartosc}", ip=client_ip(request))
    db.commit(); db.refresh(row)
    return dictionary_item(row)


@router.delete("/dictionaries/{category}/{entry_id}", status_code=204)
def dictionary_delete(category: str, entry_id: str, request: Request,
                      ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> None:
    require_write(ctx)
    row = slowniki.wpis(db, ctx, entry_id)
    if row is None or row.kategoria != category:
        raise HTTPException(404, "nie znaleziono wpisu slownika")
    slowniki.usun(db, ctx, entry_id)
    audit(db, ctx, "slownik.mobile_deleted", f"{category}:{row.wartosc}", ip=client_ip(request))
    db.commit()


@router.get("/changes")
def change_list(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(AssetChange, Asset.hostname).join(Asset, Asset.id == AssetChange.asset_id)
        .where(AssetChange.tenant_id == ctx.tenant_id).order_by(AssetChange.occurred_at.desc())
        .offset((page - 1) * page_size).limit(page_size)).all()
    return [{"id": c.id, "asset_id": c.asset_id, "hostname": hostname,
             "occurred_at": _iso(c.occurred_at), "category": c.category, "action": c.action,
             "path": c.path, "label": c.label, "old_value": c.old_value, "new_value": c.new_value}
            for c, hostname in rows]


@router.get("/audit")
def audit_list(user: PortalUser = Depends(mobile_user), ctx: TenantContext = Depends(mobile_context),
               db: Session = Depends(get_db)) -> list[dict]:
    # Te same zasady co w panelu (/audit): dziennik tylko dla administratora glownego.
    if not user.is_superadmin:
        raise HTTPException(403, "audyt jest dostepny tylko dla administratora glownego")
    rows = db.execute(select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id).order_by(AuditLog.created_at.desc()).limit(200)).scalars().all()
    return [{"id": row.id, "actor": row.actor, "action": row.action, "target": row.target,
             "detail": row.detail, "ip": row.ip, "created_at": _iso(row.created_at)} for row in rows]


def report_item(row: DefinicjaRaportu) -> dict:
    return {"id": row.id, "name": row.nazwa, "type": row.rodzaj, "frequency": row.czestotliwosc,
            "recipients": row.adresaci, "active": row.aktywny, "last_status": row.ostatni_status,
            "last_sent_at": _iso(row.ostatnia_wysylka), "send_to_vendors": row.do_dostawcow,
            "columns": row.kolumny or []}


def validate_report_body(body: ReportBody) -> None:
    from ..services import kolumny
    if not body.name.strip():
        raise HTTPException(400, "podaj nazwę raportu")
    if body.type not in raporty.RODZAJE or body.frequency not in raporty.CZESTOTLIWOSCI:
        raise HTTPException(400, "nieznany rodzaj lub czestotliwosc raportu")
    allowed = {item["klucz"] for item in kolumny.KOLUMNY}
    if any(key not in allowed for key in body.columns):
        raise HTTPException(400, "nieznana kolumna raportu")
    if not raporty.adresaci(SimpleNamespace(adresaci=body.recipients)):
        raise HTTPException(400, "nie podano poprawnego adresu")


@router.get("/reports/catalog")
def report_catalog(ctx: TenantContext = Depends(mobile_context)) -> dict:
    from ..services import kolumny
    return {
        "types": [{"key": key, "label": label} for key, label in raporty.RODZAJE.items()],
        "frequencies": [{"key": key, "label": key.capitalize()} for key in raporty.CZESTOTLIWOSCI],
        "columns": [{"key": item["klucz"], "label": item["etykieta"],
                     "group": item["grupa"], "default": item.get("domyslna", False)}
                    for item in kolumny.KOLUMNY],
    }


@router.get("/reports")
def report_list(ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(DefinicjaRaportu).where(DefinicjaRaportu.tenant_id == ctx.tenant_id)
                      .order_by(DefinicjaRaportu.nazwa)).scalars().all()
    return [report_item(row) for row in rows]


@router.post("/reports", status_code=201)
def report_create(body: ReportBody, request: Request, user: PortalUser = Depends(mobile_user),
                  ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    validate_report_body(body)
    row = DefinicjaRaportu(tenant_id=ctx.tenant_id, nazwa=body.name.strip(), rodzaj=body.type,
        czestotliwosc=body.frequency, adresaci=body.recipients.strip(), utworzyl=user.email,
        aktywny=body.active, do_dostawcow=body.send_to_vendors, kolumny=body.columns or None)
    if not raporty.adresaci(row):
        raise HTTPException(400, "nie podano poprawnego adresu")
    db.add(row); audit(db, ctx, "raport.mobile_created", row.nazwa, ip=client_ip(request))
    db.commit(); db.refresh(row)
    return report_item(row)


@router.put("/reports/{report_id}")
def report_update(report_id: str, body: ReportBody, request: Request,
                  ctx: TenantContext = Depends(mobile_context), db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    validate_report_body(body)
    row = _report(db, ctx, report_id)
    row.nazwa = body.name.strip(); row.rodzaj = body.type
    row.czestotliwosc = body.frequency; row.adresaci = body.recipients.strip()
    row.aktywny = body.active; row.do_dostawcow = body.send_to_vendors
    row.kolumny = body.columns or None
    if not raporty.adresaci(row):
        raise HTTPException(400, "nie podano poprawnego adresu")
    audit(db, ctx, "raport.mobile_updated", row.nazwa, ip=client_ip(request))
    db.commit(); db.refresh(row)
    return report_item(row)


def _report(db: Session, ctx: TenantContext, report_id: str) -> DefinicjaRaportu:
    row = db.execute(select(DefinicjaRaportu).where(DefinicjaRaportu.id == report_id,
        DefinicjaRaportu.tenant_id == ctx.tenant_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "nie znaleziono raportu")
    return row


@router.post("/reports/{report_id}/send")
def report_send(report_id: str, request: Request, ctx: TenantContext = Depends(mobile_context),
                db: Session = Depends(get_db)) -> dict:
    require_write(ctx)
    row = _report(db, ctx, report_id)
    raporty.wyslij_raport(db, row)
    audit(db, ctx, "raport.mobile_sent", row.nazwa, ip=client_ip(request)); db.commit()
    if row.ostatni_status != "ok":
        raise HTTPException(502, row.ostatni_blad or "wysylka raportu nie powiodla sie")
    return {"detail": "raport wyslany"}


@router.delete("/reports/{report_id}", status_code=204)
def report_delete(report_id: str, request: Request, ctx: TenantContext = Depends(mobile_context),
                  db: Session = Depends(get_db)) -> None:
    require_write(ctx)
    row = _report(db, ctx, report_id); name = row.nazwa; db.delete(row)
    audit(db, ctx, "raport.mobile_deleted", name, ip=client_ip(request)); db.commit()
