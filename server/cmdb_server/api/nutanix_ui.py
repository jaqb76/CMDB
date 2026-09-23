"""Panel: konfiguracja odczytu Nutanix Prism Central i widok Wirtualizacja."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ZRODLO_AGENT, Asset, NutanixUstawienia, PortalUser, utcnow
from ..services import nutanix, sekrety
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import _require_write, render, resolve_tenant

router = APIRouter(tags=["nutanix"])


# Komunikat jedzie w adresie jako KOD, a nie tekst: dowolny napis z URL-a
# wyswietlony w panelu pozwalalby spreparowac link z "komunikatem systemu".
KOMUNIKATY = {
    "adres": "Niepoprawny adres Prism Central — podaj https://nazwa[:port], bez ścieżki.",
    "ca": "Certyfikat CA musi być w formacie PEM (-----BEGIN CERTIFICATE-----).",
    "uzytkownik": "Podaj użytkownika Prism Central.",
    "haslo": "Podaj hasło do Prism Central.",
    "niepelna": "Najpierw zapisz adres, użytkownika i hasło.",
    "wylaczona": "Odczyt jest wyłączony — zaznacz „Odczytuj Prism Central z tej maszyny” i zapisz.",
}


def _karta(asset_id: str, kod: str = "") -> str:
    dopisek = f"&nutanix_komunikat={kod}" if kod in KOMUNIKATY else ""
    return f"/assets/{asset_id}?funkcja=nutanix{dopisek}#agent"


def _maszyna(db: Session, ctx: TenantContext, asset_id: str) -> Asset:
    """Maszyna z agentem tej firmy, zablokowana na czas zapisu."""
    asset = db.execute(select(Asset).where(Asset.id == asset_id, Asset.tenant_id == ctx.tenant_id)
                       .with_for_update()).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "nie znaleziono maszyny")
    if asset.zrodlo != ZRODLO_AGENT or not asset.is_active:
        raise HTTPException(400, "odczyt Nutanix wymaga aktywnej maszyny z agentem")
    return asset


@router.post("/assets/{asset_id}/nutanix")
def zapisz(asset_id: str, request: Request,
           wlaczona: bool = Form(False),
           adres: str = Form("", max_length=255),
           uzytkownik: str = Form("", max_length=128),
           haslo: str = Form("", max_length=512),
           ca_pem: str = Form("", max_length=20_000),
           interwal_minut: int = Form(60),
           revision: str = Form(..., max_length=36),
           csrf_token: str = Form(""),
           user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
           db: Session = Depends(get_db)) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    row = nutanix.ustawienia(db, asset)
    if revision != nutanix.wersja(row):
        raise HTTPException(409, "konfiguracja zmienila sie w miedzyczasie; odswiez strone")
    if interwal_minut not in nutanix.INTERWALY_MINUT:
        raise HTTPException(422, "niedozwolony odstep odczytu")
    try:
        adres_ok = nutanix.normalizuj_adres(adres) if (adres.strip() or wlaczona) else ""
    except ValueError:
        return RedirectResponse(_karta(asset.id, "adres"), status_code=303)
    try:
        ca_ok = nutanix.sprawdz_ca(ca_pem)
    except ValueError:
        return RedirectResponse(_karta(asset.id, "ca"), status_code=303)
    uzytkownik = uzytkownik.strip()
    if wlaczona and not uzytkownik:
        return RedirectResponse(_karta(asset.id, "uzytkownik"), status_code=303)
    if row is None:
        row = NutanixUstawienia(asset_id=asset.id, tenant_id=ctx.tenant_id)
        db.add(row)
    haslo_zmienione = bool(haslo)
    if haslo_zmienione:
        row.haslo = sekrety.zaszyfruj(haslo)
    if wlaczona and not row.haslo:
        db.rollback()
        return RedirectResponse(_karta(asset.id, "haslo"), status_code=303)

    przed = {"wlaczona": row.wlaczona, "adres": row.adres, "uzytkownik": row.uzytkownik,
             "interwal_minut": row.interwal_minut, "wlasne_ca": bool(row.ca_pem)}
    row.wlaczona, row.adres, row.uzytkownik = wlaczona, adres_ok, uzytkownik
    row.ca_pem, row.interwal_minut = ca_ok, interwal_minut
    row.revision, row.updated_by, row.updated_at = str(uuid4()), user.email, utcnow()
    po = {"wlaczona": row.wlaczona, "adres": row.adres, "uzytkownik": row.uzytkownik,
          "interwal_minut": row.interwal_minut, "wlasne_ca": bool(row.ca_pem)}
    # Hasla nie ma w audycie w zadnej postaci - tylko fakt, ze sie zmienilo.
    audit(db, ctx, action="nutanix.config_changed", target=asset.id,
          detail={"before": przed, "after": po, "haslo_zmienione": haslo_zmienione,
                  "revision": row.revision}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(asset.id), status_code=303)


@router.post("/assets/{asset_id}/nutanix/test")
def zlec_test(asset_id: str, request: Request, csrf_token: str = Form(""),
              user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
              db: Session = Depends(get_db)) -> Response:
    """Zleca agentowi test polaczenia. Serwer nie laczy sie z Prism sam."""
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    row = nutanix.ustawienia(db, asset)
    if row is None or not row.adres or not row.uzytkownik or not row.haslo:
        return RedirectResponse(_karta(asset.id, "niepelna"), status_code=303)
    row.test_zlecony_o = utcnow()
    audit(db, ctx, action="nutanix.test_requested", target=asset.id, detail={}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(asset.id), status_code=303)


@router.post("/assets/{asset_id}/nutanix/odczyt")
def zlec_odczyt(asset_id: str, request: Request, csrf_token: str = Form(""),
                user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
                db: Session = Depends(get_db)) -> Response:
    """Pelny odczyt przy najblizszym sprawdzeniu konfiguracji (do 5 minut).

    Tak jak test: serwer nie wola agenta, tylko zostawia mu zlecenie.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    row = nutanix.ustawienia(db, asset)
    if row is None or not row.wlaczona:
        return RedirectResponse(_karta(asset.id, "wylaczona"), status_code=303)
    row.odczyt_zlecony_o = utcnow()
    audit(db, ctx, action="nutanix.read_requested", target=asset.id, detail={}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(asset.id), status_code=303)


@router.get("/wirtualizacja", response_class=HTMLResponse)
def wirtualizacja(request: Request, user: PortalUser = Depends(require_user),
                  ctx: TenantContext = Depends(resolve_tenant),
                  db: Session = Depends(get_db)) -> Response:
    czytniki = db.execute(
        select(NutanixUstawienia, Asset).join(Asset, Asset.id == NutanixUstawienia.asset_id)
        .where(NutanixUstawienia.tenant_id == ctx.tenant_id, Asset.tenant_id == ctx.tenant_id)
        .order_by(Asset.hostname)
    ).all()
    teraz = utcnow()
    return render(request, "wirtualizacja.html", user, ctx, db,
                  drzewo=nutanix.drzewo(db, ctx.tenant_id),
                  czytniki=[{"ustawienia": u, "asset": a, "milczy": nutanix.czytnik_milczy(u, teraz)}
                            for u, a in czytniki])
