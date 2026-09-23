"""Panel: konfiguracja odczytu Prism Central / vCenter i widok Wirtualizacja.

Obaj dostawcy maja te same trasy pod wlasnym przedrostkiem
(/assets/{id}/nutanix, /assets/{id}/vmware) - jawnie, bez parametru w
sciezce, ktory zlapalby tez /assets/{id}/owner i podobne.
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ZRODLO_AGENT, Asset, PortalUser, WirtualizacjaPolaczenie, utcnow
from ..services import nutanix, sekrety
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import _require_write, render, resolve_tenant

router = APIRouter(tags=["nutanix"])


# Komunikat jedzie w adresie jako KOD, a nie tekst: dowolny napis z URL-a
# wyswietlony w panelu pozwalalby spreparowac link z "komunikatem systemu".
KOMUNIKATY = {
    "adres": "Niepoprawny adres {pl} — podaj https://nazwa[:port], bez ścieżki.",
    "ca": "Certyfikat CA musi być w formacie PEM (-----BEGIN CERTIFICATE-----).",
    "uzytkownik": "Podaj użytkownika {pl}.",
    "haslo": "Podaj hasło do {pl}.",
    "niepelna": "Najpierw zapisz adres, użytkownika i hasło.",
    "wylaczona": "Odczyt jest wyłączony — zaznacz „Odczytuj {pl} z tej maszyny” i zapisz.",
}


def komunikat(kod: str, dostawca: str) -> str:
    opis = nutanix.DOSTAWCY.get(dostawca, nutanix.DOSTAWCY[nutanix.NUTANIX])
    return KOMUNIKATY[kod].format(pl=opis["platforma"]) if kod in KOMUNIKATY else ""


def _karta(dostawca: str, asset_id: str, kod: str = "") -> str:
    dopisek = f"&nutanix_komunikat={kod}" if kod in KOMUNIKATY else ""
    return f"/assets/{asset_id}?funkcja={dostawca}{dopisek}#agent"


def _maszyna(db: Session, ctx: TenantContext, asset_id: str) -> Asset:
    """Maszyna z agentem tej firmy, zablokowana na czas zapisu."""
    asset = db.execute(select(Asset).where(Asset.id == asset_id, Asset.tenant_id == ctx.tenant_id)
                       .with_for_update()).scalar_one_or_none()
    if asset is None:
        raise HTTPException(404, "nie znaleziono maszyny")
    if asset.zrodlo != ZRODLO_AGENT or not asset.is_active:
        raise HTTPException(400, "odczyt wirtualizacji wymaga aktywnej maszyny z agentem")
    return asset


def _wybrane(db: Session, asset: Asset, dostawca: str, polaczenie_id: str):
    """Polaczenie z formularza. Bez identyfikatora - jedyne polaczenie
    (formularze sprzed wielu polaczen); przy kilku trzeba wskazac."""
    if polaczenie_id:
        row = nutanix.polaczenie(db, asset, dostawca, polaczenie_id)
        if row is None:
            raise HTTPException(404, "nie ma takiego polaczenia")
        return row
    lista = nutanix.polaczenia(db, asset, dostawca)
    return lista[0] if len(lista) == 1 else None


def _zapisz(dostawca: str, asset_id: str, request: Request, polaczenie_id: str, nazwa: str,
            wlaczona: bool, adres: str, uzytkownik: str, haslo: str, ca_pem: str,
            interwal_minut: int, revision: str, csrf_token: str, user: PortalUser,
            ctx: TenantContext, db: Session) -> Response:
    verify_csrf(request, user, csrf_token)
    opis = nutanix.DOSTAWCY[dostawca]
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    if polaczenie_id:
        row = _wybrane(db, asset, dostawca, polaczenie_id)
    elif revision == "unassigned":
        row = None  # nowe polaczenie
    else:
        # Formularz bez identyfikatora, ale z rewizja - rewizja wskazuje polaczenie.
        row = next((r for r in nutanix.polaczenia(db, asset, dostawca) if r.revision == revision), None)
    if revision != nutanix.wersja(row):
        raise HTTPException(409, "konfiguracja zmienila sie w miedzyczasie; odswiez strone")
    if interwal_minut not in nutanix.INTERWALY_MINUT:
        raise HTTPException(422, "niedozwolony odstep odczytu")
    try:
        adres_ok = nutanix.normalizuj_adres(adres, opis["port"]) if (adres.strip() or wlaczona) else ""
    except ValueError:
        return RedirectResponse(_karta(dostawca, asset.id, "adres"), status_code=303)
    try:
        ca_ok = nutanix.sprawdz_ca(ca_pem)
    except ValueError:
        return RedirectResponse(_karta(dostawca, asset.id, "ca"), status_code=303)
    uzytkownik = uzytkownik.strip()
    if wlaczona and not uzytkownik:
        return RedirectResponse(_karta(dostawca, asset.id, "uzytkownik"), status_code=303)
    nowe = row is None
    if nowe:
        row = WirtualizacjaPolaczenie(asset_id=asset.id, tenant_id=ctx.tenant_id, dostawca=dostawca)
        db.add(row)
    haslo_zmienione = bool(haslo)
    if haslo_zmienione:
        row.haslo = sekrety.zaszyfruj(haslo)
    if wlaczona and not row.haslo:
        db.rollback()
        return RedirectResponse(_karta(dostawca, asset.id, "haslo"), status_code=303)

    def stan() -> dict:
        return {"nazwa": row.nazwa, "wlaczona": row.wlaczona, "adres": row.adres,
                "uzytkownik": row.uzytkownik, "interwal_minut": row.interwal_minut,
                "wlasne_ca": bool(row.ca_pem)}

    przed = {} if nowe else stan()
    row.nazwa = nazwa.strip()[:100]
    row.wlaczona, row.adres, row.uzytkownik = wlaczona, adres_ok, uzytkownik
    row.ca_pem, row.interwal_minut = ca_ok, interwal_minut
    row.revision, row.updated_by, row.updated_at = str(uuid4()), user.email, utcnow()
    db.flush()
    # Hasla nie ma w audycie w zadnej postaci - tylko fakt, ze sie zmienilo.
    audit(db, ctx, action=f"{dostawca}.config_changed", target=asset.id,
          detail={"polaczenie": row.id, "nowe": nowe, "before": przed, "after": stan(),
                  "haslo_zmienione": haslo_zmienione, "revision": row.revision}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(dostawca, asset.id), status_code=303)


def _zlec_test(dostawca: str, asset_id: str, request: Request, polaczenie_id: str, csrf_token: str,
               user: PortalUser, ctx: TenantContext, db: Session) -> Response:
    """Zleca agentowi test polaczenia. Serwer nie laczy sie z platforma sam."""
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    row = _wybrane(db, asset, dostawca, polaczenie_id)
    if row is None or not row.adres or not row.uzytkownik or not row.haslo:
        return RedirectResponse(_karta(dostawca, asset.id, "niepelna"), status_code=303)
    row.test_zlecony_o = utcnow()
    audit(db, ctx, action=f"{dostawca}.test_requested", target=asset.id,
          detail={"polaczenie": row.id}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(dostawca, asset.id), status_code=303)


def _zlec_odczyt(dostawca: str, asset_id: str, request: Request, polaczenie_id: str, csrf_token: str,
                 user: PortalUser, ctx: TenantContext, db: Session) -> Response:
    """Pelny odczyt przy najblizszym sprawdzeniu konfiguracji (do 5 minut).

    Tak jak test: serwer nie wola agenta, tylko zostawia mu zlecenie.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    row = _wybrane(db, asset, dostawca, polaczenie_id)
    if row is None or not row.wlaczona:
        return RedirectResponse(_karta(dostawca, asset.id, "wylaczona"), status_code=303)
    row.odczyt_zlecony_o = utcnow()
    audit(db, ctx, action=f"{dostawca}.read_requested", target=asset.id,
          detail={"polaczenie": row.id}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(dostawca, asset.id), status_code=303)


def _usun(dostawca: str, asset_id: str, request: Request, polaczenie_id: str, csrf_token: str,
          user: PortalUser, ctx: TenantContext, db: Session) -> Response:
    """Usuwa polaczenie; jego maszyny wypadaja z ewidencji jak po zniknieciu."""
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = _maszyna(db, ctx, asset_id)
    if not polaczenie_id:
        raise HTTPException(422, "wskaz polaczenie")
    row = _wybrane(db, asset, dostawca, polaczenie_id)
    # Ta sama blokada co przy przyjmowaniu odczytu - usuniecie nie moze
    # przeplesc sie z synchronizacja tych samych obiektow.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "nutanix:" + ctx.tenant_id})
    opis = {"polaczenie": row.id, "adres": row.adres, "nazwa": row.nazwa}
    opis["wycofane"] = nutanix.usun_polaczenie(db, asset, row)
    audit(db, ctx, action=f"{dostawca}.connection_deleted", target=asset.id, detail=opis,
          ip=client_ip(request))
    db.commit()
    return RedirectResponse(_karta(dostawca, asset.id), status_code=303)


@router.get("/wirtualizacja", response_class=HTMLResponse)
def wirtualizacja(request: Request, user: PortalUser = Depends(require_user),
                  ctx: TenantContext = Depends(resolve_tenant),
                  db: Session = Depends(get_db)) -> Response:
    teraz = utcnow()
    czytniki = []
    for row, a in db.execute(
        select(WirtualizacjaPolaczenie, Asset).join(Asset, Asset.id == WirtualizacjaPolaczenie.asset_id)
        .where(WirtualizacjaPolaczenie.tenant_id == ctx.tenant_id, Asset.tenant_id == ctx.tenant_id)
        .order_by(WirtualizacjaPolaczenie.dostawca, Asset.hostname, WirtualizacjaPolaczenie.utworzono)
    ).all():
        czytniki.append({"ustawienia": row, "asset": a, "dostawca": row.dostawca,
                         "opis": nutanix.DOSTAWCY.get(row.dostawca, {}),
                         "nazwa": nutanix.nazwa_polaczenia(row),
                         "milczy": nutanix.czytnik_milczy(row, teraz)})
    return render(request, "wirtualizacja.html", user, ctx, db,
                  drzewo=nutanix.drzewo(db, ctx.tenant_id), dostawcy=nutanix.DOSTAWCY,
                  czytniki=czytniki)


def _trasy(dostawca: str) -> None:
    """Trasy panelu dla jednego dostawcy. Polaczenie wskazuje pole formularza,
    nie sciezka - tak nic nie koliduje z innymi trasami /assets/{id}/..."""

    def zapisz(asset_id: str, request: Request,
               polaczenie_id: str = Form("", max_length=36),
               nazwa: str = Form("", max_length=100),
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
        return _zapisz(dostawca, asset_id, request, polaczenie_id, nazwa, wlaczona, adres, uzytkownik,
                       haslo, ca_pem, interwal_minut, revision, csrf_token, user, ctx, db)

    def akcja(funkcja):
        def obsluga(asset_id: str, request: Request,
                    polaczenie_id: str = Form("", max_length=36), csrf_token: str = Form(""),
                    user: PortalUser = Depends(require_user), ctx: TenantContext = Depends(resolve_tenant),
                    db: Session = Depends(get_db)) -> Response:
            return funkcja(dostawca, asset_id, request, polaczenie_id, csrf_token, user, ctx, db)
        return obsluga

    router.add_api_route(f"/assets/{{asset_id}}/{dostawca}", zapisz, methods=["POST"],
                         name=f"{dostawca}_zapisz")
    for koncowka, funkcja in (("test", _zlec_test), ("odczyt", _zlec_odczyt), ("usun", _usun)):
        router.add_api_route(f"/assets/{{asset_id}}/{dostawca}/{koncowka}", akcja(funkcja),
                             methods=["POST"], name=f"{dostawca}_{koncowka}")


for _dostawca in nutanix.DOSTAWCY:
    _trasy(_dostawca)
