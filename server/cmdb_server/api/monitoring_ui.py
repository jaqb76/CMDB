"""Panel monitorowania uslug i certyfikatow.

Osobny modul z tego samego powodu co panel raportow: ui.py urosl juz do
rozmiaru, w ktorym dokladanie kolejnych tras utrudnia czytanie. Trasy dziela
z reszta panelu kontekst dzierzawcy i sposob renderowania.

Kazde zapytanie o cel przechodzi przez ``_cel``, ktore wymusza tenant_id.
Nie ma tu sciezki czytajacej cel bez tego filtra - identyfikator z cudzego
panelu nie moze wystarczyc do obejrzenia ani skasowania cudzej uslugi.
"""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    PORTY_DOMYSLNE,
    PROTOKOLY_MONITORA,
    PROTOKOLY_Z_HTTP,
    PROTOKOLY_Z_TLS,
    Asset,
    MonitorUslugi,
    PomiarMonitora,
    PortalUser,
    UstawieniaPoczty,
)
from ..services import monitoring
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import render, resolve_tenant

log = logging.getLogger(__name__)
router = APIRouter(tags=["monitorowanie"])

# Ile ostatnich pomiarow pokazujemy na stronie celu. Historia siega dalej -
# to tylko tyle, ile da sie objac wzrokiem bez przewijania w nieskonczonosc.
POMIARY_NA_STRONIE = 50


def _cel(db: Session, ctx: TenantContext, monitor_id: str, *, blokuj: bool = False) -> MonitorUslugi:
    """Cel nalezacy do TEJ firmy - nigdy cudzy."""
    zapytanie = select(MonitorUslugi).where(
        MonitorUslugi.id == monitor_id, MonitorUslugi.tenant_id == ctx.tenant_id
    )
    if blokuj:
        # Sprawdzenie i zapis wyniku to odczyt-modyfikacja-zapis stanu; bez
        # blokady dwa rownolegle "sprawdz teraz" moglyby zgubic licznik prob.
        zapytanie = zapytanie.with_for_update()
    monitor = db.execute(zapytanie).scalar_one_or_none()
    if monitor is None:
        raise HTTPException(status_code=404, detail="nie znaleziono monitorowanej uslugi")
    return monitor


def _wroc(komunikat: str = "", adres: str = "/monitoring") -> RedirectResponse:
    if komunikat:
        adres += ("&" if "?" in adres else "?") + "komunikat=" + urllib.parse.quote(komunikat)
    return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)


def _zapis_dozwolony(ctx: TenantContext) -> None:
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")


def _z_formularza(dane: dict) -> dict:
    try:
        return monitoring.sprawdz_ustawienia(dane)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _wspolne(db: Session, ctx: TenantContext) -> dict:
    """Dane, ktorych potrzebuja obie strony panelu."""
    return {
        "protokoly": PROTOKOLY_MONITORA,
        "porty_domyslne": PORTY_DOMYSLNE,
        "protokoly_tls": list(PROTOKOLY_Z_TLS),
        "protokoly_http": list(PROTOKOLY_Z_HTTP),
        # Bez skonfigurowanej poczty powiadomienia nie maja jak wyjsc -
        # lepiej powiedziec to przy formularzu niz zostawic ciche milczenie.
        "poczta": db.get(UstawieniaPoczty, ctx.tenant_id),
    }


@router.get("/monitoring", response_class=HTMLResponse)
def strona_monitoringu(
    request: Request,
    komunikat: str = Query("", max_length=500),
    stan: str = Query("", max_length=16),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    cele = monitoring.cele_firmy(db, ctx.tenant_id)
    if stan:
        cele = [c for c in cele if c.stan == stan]
    # Dostepnosc liczymy dla widocznych celow, a nie dla wszystkich - to
    # zapytanie na cel, wiec filtr oszczedza je razem z wierszami tabeli.
    dostepnosci = {
        c.id: monitoring.dostepnosc(db, c.id, ctx.tenant_id, 24) for c in cele
    }
    maszyny = db.execute(
        select(Asset).where(Asset.tenant_id == ctx.tenant_id, Asset.is_active.is_(True))
        .order_by(Asset.hostname).limit(500)
    ).scalars().all()
    return render(
        request, "monitoring.html", user, ctx, db,
        cele=cele,
        dostepnosci=dostepnosci,
        podsumowanie=monitoring.podsumowanie(db, ctx.tenant_id),
        maszyny=maszyny,
        limit_osiagniety=monitoring.limit_osiagniety(db, ctx.tenant_id),
        dni_do_konca=monitoring.dni_do_konca,
        filtr_stanu=stan,
        komunikat=komunikat[:500],
        **_wspolne(db, ctx),
    )


@router.post("/monitoring")
def dodaj_cel(
    request: Request,
    nazwa: str = Form(..., max_length=200),
    host: str = Form(..., max_length=255),
    port: str = Form(""),
    protokol: str = Form("https"),
    sciezka: str = Form("/"),
    oczekiwany_kod: str = Form("0"),
    nazwa_tls: str = Form("", max_length=255),
    weryfikuj_lancuch: bool = Form(False),
    interwal_sekund: str = Form("300"),
    limit_sekund: str = Form("10"),
    liczba_prob: str = Form("2"),
    prog_ostrzezenia_dni: str = Form("30"),
    prog_alarmu_dni: str = Form("7"),
    powiadamiaj: bool = Form(False),
    adresaci: str = Form("", max_length=2000),
    asset_id: str = Form("", max_length=36),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    if monitoring.limit_osiagniety(db, ctx.tenant_id):
        raise HTTPException(status_code=400, detail="osiagnieto limit monitorowanych uslug")

    ustawienia = _z_formularza({
        "nazwa": nazwa, "host": host, "port": port or PORTY_DOMYSLNE.get(protokol, 443),
        "protokol": protokol, "sciezka": sciezka, "oczekiwany_kod": oczekiwany_kod,
        "nazwa_tls": nazwa_tls, "weryfikuj_lancuch": weryfikuj_lancuch,
        "interwal_sekund": interwal_sekund, "limit_sekund": limit_sekund,
        "liczba_prob": liczba_prob, "prog_ostrzezenia_dni": prog_ostrzezenia_dni,
        "prog_alarmu_dni": prog_alarmu_dni, "powiadamiaj": powiadamiaj,
        "adresaci": adresaci, "aktywny": True,
    })
    if db.execute(select(MonitorUslugi.id).where(
            MonitorUslugi.tenant_id == ctx.tenant_id,
            MonitorUslugi.nazwa == ustawienia["nazwa"])).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="cel o tej nazwie juz istnieje")

    monitor = MonitorUslugi(tenant_id=ctx.tenant_id, utworzyl=user.email, **ustawienia)
    monitor.asset_id = _powiazany_zasob(db, ctx, asset_id)
    db.add(monitor)
    db.flush()
    audit(db, ctx, action="monitoring.dodany", target=monitor.nazwa,
          detail={"host": monitor.host, "port": monitor.port, "protokol": monitor.protokol},
          ip=client_ip(request))
    db.commit()

    # Pierwsze sprawdzenie idzie od razu: cel dodany i milczacy przez pieć
    # minut nie mowi, czy w ogole zostal wpisany poprawnie.
    monitoring.sprawdz_teraz(db, monitor)
    return _wroc(f"Dodano cel {monitor.nazwa} - stan: {monitor.stan}.")


def _powiazany_zasob(db: Session, ctx: TenantContext, asset_id: str) -> str | None:
    """Zasob z TEJ firmy albo None. Powiazanie jest opcjonalne."""
    if not asset_id.strip():
        return None
    zasob = db.execute(select(Asset).where(
        Asset.id == asset_id.strip(), Asset.tenant_id == ctx.tenant_id)).scalar_one_or_none()
    if zasob is None:
        raise HTTPException(status_code=404, detail="nie znaleziono zasobu w tej firmie")
    return zasob.id


@router.get("/monitoring/{monitor_id}", response_class=HTMLResponse)
def strona_celu(
    monitor_id: str,
    request: Request,
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    monitor = _cel(db, ctx, monitor_id)
    pomiary = db.execute(
        select(PomiarMonitora)
        .where(PomiarMonitora.monitor_id == monitor.id,
               PomiarMonitora.tenant_id == ctx.tenant_id)
        .order_by(PomiarMonitora.sprawdzono.desc())
        .limit(POMIARY_NA_STRONIE)
    ).scalars().all()
    maszyny = db.execute(
        select(Asset).where(Asset.tenant_id == ctx.tenant_id, Asset.is_active.is_(True))
        .order_by(Asset.hostname).limit(500)
    ).scalars().all()
    return render(
        request, "monitoring_cel.html", user, ctx, db,
        monitor=monitor,
        pomiary=pomiary,
        maszyny=maszyny,
        dni=monitoring.dni_do_konca(monitor),
        okna=[monitoring.dostepnosc(db, monitor.id, ctx.tenant_id, godziny)
              for godziny in (24, 168, 720)],
        komunikat=komunikat[:500],
        **_wspolne(db, ctx),
    )


@router.post("/monitoring/{monitor_id}")
def zapisz_cel(
    monitor_id: str,
    request: Request,
    nazwa: str = Form(..., max_length=200),
    host: str = Form(..., max_length=255),
    port: str = Form(""),
    protokol: str = Form("https"),
    sciezka: str = Form("/"),
    oczekiwany_kod: str = Form("0"),
    nazwa_tls: str = Form("", max_length=255),
    weryfikuj_lancuch: bool = Form(False),
    interwal_sekund: str = Form("300"),
    limit_sekund: str = Form("10"),
    liczba_prob: str = Form("2"),
    prog_ostrzezenia_dni: str = Form("30"),
    prog_alarmu_dni: str = Form("7"),
    powiadamiaj: bool = Form(False),
    adresaci: str = Form("", max_length=2000),
    aktywny: bool = Form(False),
    asset_id: str = Form("", max_length=36),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    monitor = _cel(db, ctx, monitor_id, blokuj=True)

    ustawienia = _z_formularza({
        "nazwa": nazwa, "host": host, "port": port or PORTY_DOMYSLNE.get(protokol, 443),
        "protokol": protokol, "sciezka": sciezka, "oczekiwany_kod": oczekiwany_kod,
        "nazwa_tls": nazwa_tls, "weryfikuj_lancuch": weryfikuj_lancuch,
        "interwal_sekund": interwal_sekund, "limit_sekund": limit_sekund,
        "liczba_prob": liczba_prob, "prog_ostrzezenia_dni": prog_ostrzezenia_dni,
        "prog_alarmu_dni": prog_alarmu_dni, "powiadamiaj": powiadamiaj,
        "adresaci": adresaci, "aktywny": aktywny,
    })
    if db.execute(select(MonitorUslugi.id).where(
            MonitorUslugi.tenant_id == ctx.tenant_id,
            MonitorUslugi.nazwa == ustawienia["nazwa"],
            MonitorUslugi.id != monitor.id)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="cel o tej nazwie juz istnieje")

    przed = {k: getattr(monitor, k) for k in ustawienia}
    zmiana_celu = any(
        przed[k] != ustawienia[k] for k in ("host", "port", "protokol", "sciezka", "nazwa_tls")
    )
    for pole, wartosc in ustawienia.items():
        setattr(monitor, pole, wartosc)
    monitor.asset_id = _powiazany_zasob(db, ctx, asset_id)

    if zmiana_celu:
        # Zmieniony adres to inna usluga, choc pod ta sama nazwa. Stan i to,
        # o czym juz powiadomilismy, dotyczyly poprzedniej - zostawienie ich
        # oznaczaloby alarm o awarii czegos, czego juz nie monitorujemy.
        monitor.stan = monitor.stan_dostepnosci = monitor.stan_certyfikatu = "nieznany"
        monitor.kolejne_bledy = 0
        monitor.powiadomiona_dostepnosc = None
        monitor.powiadomiony_prog = None
        monitor.powiadomiony_stan_cert = None
        monitor.powiadomiony_odcisk = None
        monitor.cert_odcisk = monitor.cert_podmiot = monitor.cert_wystawca = None
        monitor.cert_od = monitor.cert_do = monitor.cert_nazwy = None
        monitor.cert_zaufany = monitor.cert_blad = None
        monitor.ostatnie_sprawdzenie = None

    audit(db, ctx, action="monitoring.zmieniony", target=monitor.nazwa,
          detail={"przed": {k: str(v) for k, v in przed.items() if przed[k] != ustawienia[k]},
                  "po": {k: str(v) for k, v in ustawienia.items() if przed[k] != v}},
          ip=client_ip(request))
    db.commit()
    return _wroc("Zapisano ustawienia celu.", f"/monitoring/{monitor.id}")


@router.post("/monitoring/{monitor_id}/sprawdz")
def sprawdz_cel(
    monitor_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Sprawdzenie na zadanie - bez czekania na harmonogram.

    Po zmianie ustawien albo po naprawie uslugi nikt nie chce czekac
    pieciu minut, zeby zobaczyc, czy poskutkowalo.
    """
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    monitor = _cel(db, ctx, monitor_id, blokuj=True)
    wynik = monitoring.sprawdz_teraz(db, monitor)
    komunikat = f"Sprawdzono {monitor.nazwa}: {wynik['stan']}."
    if monitor.ostatni_blad:
        komunikat += f" {monitor.ostatni_blad}"
    if wynik["powiadomienia"]:
        komunikat += " Wyslano powiadomienie: " + ", ".join(wynik["powiadomienia"]) + "."
    return _wroc(komunikat, f"/monitoring/{monitor.id}")


@router.post("/monitoring/{monitor_id}/przelacz")
def przelacz_cel(
    monitor_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Wlacza albo wylacza cel bez kasowania jego historii."""
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    monitor = _cel(db, ctx, monitor_id, blokuj=True)
    monitor.aktywny = not monitor.aktywny
    if not monitor.aktywny:
        # Wylaczony cel nie jest sprawny - jest niesprawdzany. Zostawienie
        # zielonego stanu klamaloby, ze ktos go nadal pilnuje.
        monitor.stan = monitor.stan_dostepnosci = monitor.stan_certyfikatu = "nieznany"
        monitor.powiadomiona_dostepnosc = None
        monitor.powiadomiony_stan_cert = None
    audit(db, ctx, action="monitoring.przelaczony", target=monitor.nazwa,
          detail={"aktywny": monitor.aktywny}, ip=client_ip(request))
    db.commit()
    return _wroc(
        f"Cel {monitor.nazwa} {'wlaczony' if monitor.aktywny else 'wylaczony'}.",
        f"/monitoring/{monitor.id}",
    )


@router.post("/monitoring/{monitor_id}/usun")
def usun_cel(
    monitor_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    monitor = _cel(db, ctx, monitor_id)
    nazwa = monitor.nazwa
    db.delete(monitor)
    audit(db, ctx, action="monitoring.usuniety", target=nazwa, ip=client_ip(request))
    db.commit()
    return _wroc(f"Usunieto cel {nazwa} wraz z jego historia.")
