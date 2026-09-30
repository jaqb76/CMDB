"""Formularz, podglad i potwierdzenie przeniesienia zgloszenia."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CzasPracy, PortalUser, Tenant, WpisSlownika, WpisZgloszenia, ZalacznikWpisu
from ..services import helpdesk, helpdesk_przeniesienia as transfer, helpdesk_wysylka
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext
from .helpdesk_ui import _wroc, _zgloszenie
from .ui import render, resolve_tenant

router = APIRouter(tags=["helpdesk"])


def _blad(exc):
    return HTTPException(403 if isinstance(exc, transfer.BrakDostepu) else
                         409 if isinstance(exc, transfer.Konflikt) else 400, str(exc))


def _formularz(request, user, ctx, db, zgloszenie, dane=None, blad="", podglad=None):
    if not transfer.uprawniona_rola(user):
        raise HTTPException(403, "To konto nie może przenosić zgłoszeń.")
    dane = dane or {}
    firmy = transfer.firmy_docelowe(db, user, zgloszenie)
    opcje = {"contacts": [], "technicians": []}
    if dane.get("tenant_id") in {f.id for f in firmy}:
        opcje = transfer.opcje(db, user, zgloszenie, dane["tenant_id"])
    liczby = {"wpisy": db.scalar(select(func.count(WpisZgloszenia.id)).where(
        WpisZgloszenia.zgloszenie_id == zgloszenie.id)),
        "pliki": db.scalar(select(func.count(ZalacznikWpisu.id)).where(
            ZalacznikWpisu.zgloszenie_id == zgloszenie.id)),
        "minuty": db.scalar(select(func.coalesce(func.sum(CzasPracy.minuty), 0)).where(
            CzasPracy.zgloszenie_id == zgloszenie.id))}
    return render(request, "helpdesk_przenies.html", user, ctx, db,
                  zgloszenie=zgloszenie, firma=db.get(Tenant, zgloszenie.tenant_id),
                  firmy=firmy, opcje=opcje, dane=dane, blad=blad, podglad=podglad,
                  liczby=liczby, sprzet=helpdesk.sprzet_zgloszenia(db, zgloszenie.id))


@router.get("/helpdesk/zgloszenie/{zgloszenie_id}/przenies", response_class=HTMLResponse)
def formularz(zgloszenie_id: str, request: Request, tenant_id: str = Query("", max_length=36),
              user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
              db: Session = Depends(get_db)):
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    return _formularz(request, user, ctx, db, zgloszenie, {"tenant_id": tenant_id})


@router.get("/helpdesk/zgloszenie/{zgloszenie_id}/przenies/opcje")
def opcje(zgloszenie_id: str, tenant_id: str = Query(..., max_length=36),
          user: PortalUser = Depends(require_user), db: Session = Depends(get_db)):
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    try:
        return JSONResponse(transfer.opcje(db, user, zgloszenie, tenant_id),
                            headers={"Cache-Control": "no-store"})
    except helpdesk.BladHelpdesku as exc:
        raise _blad(exc) from exc


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/przenies/podglad", response_class=HTMLResponse)
def podglad(zgloszenie_id: str, request: Request,
            tenant_id: str = Form("", max_length=36), kontakt_id: str = Form("", max_length=36),
            technik_id: str = Form("", max_length=36), powod: str = Form("", max_length=1000),
            powiadom: bool = Form(False), wstecz: bool = Form(False),
            csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
            ctx: TenantContext = Depends(resolve_tenant), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    zgloszenie = _zgloszenie(db, user, zgloszenie_id, blokuj=True)
    dane = dict(tenant_id=tenant_id, kontakt_id=kontakt_id, technik_id=technik_id, powod=powod,
                powiadom=powiadom)
    if wstecz:
        return _formularz(request, user, ctx, db, zgloszenie, dane)
    try:
        wynik = transfer.przygotuj(db, user, zgloszenie, **dane)
    except transfer.BrakDostepu as exc:
        raise _blad(exc) from exc
    except helpdesk.BladHelpdesku as exc:
        return _formularz(request, user, ctx, db, zgloszenie, dane, blad=str(exc))
    return _formularz(request, user, ctx, db, zgloszenie, dane, podglad=wynik)


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/przenies")
def zatwierdz(zgloszenie_id: str, request: Request,
              potwierdzenie: str = Form("", max_length=12000), potwierdzam: bool = Form(False),
              csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
              db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    _zgloszenie(db, user, zgloszenie_id, blokuj=True)
    if not potwierdzam:
        raise HTTPException(400, "Potwierdź przekazanie treści i zmianę dostępu.")
    try:
        zgloszenie = transfer.zatwierdz(db, user, zgloszenie_id, potwierdzenie, client_ip(request))
        db.commit()
    except helpdesk.BladHelpdesku as exc:
        db.rollback()
        raise _blad(exc) from exc
    helpdesk_wysylka.wyslij_powiadomienie_o_przeniesieniu(db, zgloszenie)
    db.commit()
    return _wroc(f"/helpdesk/zgloszenie/{zgloszenie.id}",
                 f"Zgłoszenie {zgloszenie.numer_pelny} przeniesiono do nowej firmy.")


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/kontakt")
def ustaw_kontakt(zgloszenie_id: str, request: Request, kontakt_id: str = Form("", max_length=36),
                 csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
                 db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    zgloszenie = _zgloszenie(db, user, zgloszenie_id, blokuj=True)
    if not transfer.uprawniona_rola(user) or transfer.ostatnie(db, zgloszenie.id) is None:
        raise HTTPException(403, "Ta operacja dotyczy kontaktu przeniesionego zgłoszenia.")
    kontakt = db.scalar(select(WpisSlownika).where(
        WpisSlownika.id == kontakt_id, WpisSlownika.tenant_id == zgloszenie.tenant_id,
        WpisSlownika.kategoria == "osoba"))
    email = ((kontakt.atrybuty or {}).get("email") or "").strip().lower() if kontakt else ""
    if not email or "@" not in email or "." not in helpdesk.domena_adresu(email) or helpdesk.obca_firma_adresu(db, email, zgloszenie.tenant_id):
        raise HTTPException(400, "Wybierz kontakt z adresem e-mail należący do firmy zgłoszenia.")
    from ..models import utcnow
    from ..services.scoping import audit
    poprzedni = zgloszenie.zglaszajacy_email
    zgloszenie.zglaszajacy_email, zgloszenie.zglaszajacy_nazwa = email, kontakt.wartosc
    zgloszenie.ostatnia_aktywnosc = utcnow()
    helpdesk.zdarzenie(db, zgloszenie, f"kontakt do odpowiedzi: {email}",
                      autor=helpdesk.opis_osoby(user), autor_id=user.id)
    audit(db, None, "helpdesk.zgloszenie.kontakt", target=zgloszenie.id,
          detail={"przed": poprzedni, "po": email}, actor=user.email, ip=client_ip(request))
    db.commit()
    return _wroc(f"/helpdesk/zgloszenie/{zgloszenie.id}", "Kontakt do odpowiedzi został zapisany.")
