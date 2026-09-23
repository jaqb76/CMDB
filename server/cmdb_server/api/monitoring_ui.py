"""Panel monitorowania uslug i certyfikatow.

Osobny modul z tego samego powodu co panel raportow: ui.py urosl juz do
rozmiaru, w ktorym dokladanie kolejnych tras utrudnia czytanie. Trasy dziela
z reszta panelu kontekst dzierzawcy i sposob renderowania.

Kazde zapytanie o cel przechodzi przez ``_cel``, ktore wymusza tenant_id.
Nie ma tu sciezki czytajacej cel bez tego filtra - identyfikator z cudzego
panelu nie moze wystarczyc do obejrzenia ani skasowania cudzej uslugi.

Panel niczego nie sonduje: zapisuje ZAMIAR (co, jak czesto, z ktorej maszyny),
a sprawdza agent. "Sprawdz teraz" jest wiec zadaniem zlozonym agentowi,
a nie odpowiedzia - i tak jest opisane w interfejsie.
"""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    LIFECYCLE_AKTYWNY,
    PORTY_DOMYSLNE,
    PROTOKOLY_MONITORA,
    PROTOKOLY_Z_HTTP,
    PROTOKOLY_Z_TLS,
    ZRODLO_AGENT,
    Asset,
    MonitorUslugi,
    PortalUser,
    UstawieniaPoczty,
    utcnow,
)
from ..services import monitoring
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import render, resolve_tenant

log = logging.getLogger(__name__)
router = APIRouter(tags=["monitorowanie"])

# Ile ostatnich przerw pokazujemy na stronie celu. Historia siega dalej -
# to tylko tyle, ile da sie objac wzrokiem bez przewijania w nieskonczonosc.
PRZERW_NA_STRONIE = 50

# Pola formularza celu. Jedna lista, bo formularz dodawania i edycji jest ten
# sam - a ustawienie, ktorego nie da sie wpisac przy zakladaniu, nie moze
# wchodzic tylnymi drzwiami przy poprawianiu.
POLA_FORMULARZA = (
    "nazwa", "host", "port", "protokol", "sciezka", "oczekiwany_kod", "nazwa_tls",
    "weryfikuj_lancuch", "interwal_sekund", "interwal_certyfikatu", "limit_sekund",
    "liczba_prob", "prog_ostrzezenia_dni", "prog_alarmu_dni", "powiadamiaj", "adresaci",
)


def _cel(db: Session, ctx: TenantContext, monitor_id: str, *, blokuj: bool = False) -> MonitorUslugi:
    """Cel nalezacy do TEJ firmy - nigdy cudzy."""
    zapytanie = select(MonitorUslugi).where(
        MonitorUslugi.id == monitor_id, MonitorUslugi.tenant_id == ctx.tenant_id
    )
    if blokuj:
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


def _agenci(db: Session, ctx: TenantContext) -> list[Asset]:
    """Maszyny, ktore moga sondowac: aktywne, z agentem.

    Wpis reczny (drukarka, przelacznik) agenta nie ma i niczego nie sprawdzi -
    pokazanie go na liscie wykonawcow konczyloby sie celem, ktory nigdy nie
    zostanie zmierzony, a wygladalby na skonfigurowany.
    """
    return db.execute(
        select(Asset).where(
            Asset.tenant_id == ctx.tenant_id,
            Asset.zrodlo == ZRODLO_AGENT,
            Asset.is_active.is_(True),
            Asset.lifecycle == "aktywny",
        ).order_by(Asset.hostname).limit(500)
    ).scalars().all()


def _wykonawca(db: Session, ctx: TenantContext, wykonawca_id: str,
               pomijany: str | None = None) -> Asset:
    """Sprawdza, ze wskazana maszyna moze byc wykonawca - i ma jeszcze miejsce."""
    maszyna = db.execute(
        select(Asset).where(Asset.id == (wykonawca_id or "").strip(),
                            Asset.tenant_id == ctx.tenant_id)
    ).scalar_one_or_none()
    if maszyna is None:
        raise HTTPException(404, "nie znaleziono maszyny wskazanej jako wykonawca")
    if (maszyna.zrodlo != ZRODLO_AGENT or not maszyna.is_active
            or maszyna.lifecycle != LIFECYCLE_AKTYWNY):
        # Lista rozwijana i tak nie pokaze maszyny wycofanej, ale zadanie
        # da sie zlozyc z pominieciem formularza - a cel przypisany maszynie,
        # ktorej agent juz nie chodzi, nie bylby sprawdzany wcale.
        raise HTTPException(400, "sprawdzac moze tylko aktywna maszyna z agentem")
    zapytanie = select(func.count(MonitorUslugi.id)).where(
        MonitorUslugi.wykonawca_id == maszyna.id)
    if pomijany:
        zapytanie = zapytanie.where(MonitorUslugi.id != pomijany)
    if db.execute(zapytanie).scalar_one() >= get_settings().monitoring_max_per_agent:
        raise HTTPException(
            400, f"maszyna {maszyna.hostname} ma juz maksymalna liczbe przypisanych celow")
    return maszyna


def _zasob(db: Session, ctx: TenantContext, asset_id: str) -> str | None:
    """Zasob z TEJ firmy albo None. Powiazanie jest opcjonalne."""
    if not (asset_id or "").strip():
        return None
    zasob = db.execute(select(Asset).where(
        Asset.id == asset_id.strip(), Asset.tenant_id == ctx.tenant_id)).scalar_one_or_none()
    if zasob is None:
        raise HTTPException(status_code=404, detail="nie znaleziono zasobu w tej firmie")
    return zasob.id


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
        "agenci": _agenci(db, ctx),
        "interwal_raportu": get_settings().monitoring_report_seconds,
        # Bez skonfigurowanej poczty powiadomienia nie maja jak wyjsc -
        # lepiej powiedziec to przy formularzu niz zostawic ciche milczenie.
        "poczta": db.get(UstawieniaPoczty, ctx.tenant_id),
    }


@router.get("/monitoring", response_class=HTMLResponse)
def strona_monitoringu(
    request: Request,
    komunikat: str = Query("", max_length=500),
    stan: str = Query("", max_length=16),
    wykonawca: str = Query("", max_length=36),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    cele = monitoring.cele_firmy(db, ctx.tenant_id)
    if stan:
        cele = [c for c in cele if c.stan == stan]
    teraz = utcnow()
    return render(
        request, "monitoring.html", user, ctx, db,
        cele=cele,
        dostepnosci={c.id: monitoring.dostepnosc(db, c.id, ctx.tenant_id, 24) for c in cele},
        milczace={c.id: monitoring.milczy(c, teraz) for c in cele},
        osierocone={c.id: monitoring.bez_wykonawcy(c) for c in cele},
        podsumowanie=monitoring.podsumowanie(db, ctx.tenant_id),
        maszyny=_agenci(db, ctx),
        limit_osiagniety=monitoring.limit_osiagniety(db, ctx.tenant_id),
        dni_do_konca=monitoring.dni_do_konca,
        filtr_stanu=stan,
        # Wstepny wybor maszyny sprawdzajacej - przycisk z zakladki "Agent".
        # Tylko wybor na liscie: nieznany identyfikator nic nie zaznacza.
        wybrany_wykonawca=wykonawca,
        komunikat=komunikat[:500],
        **_wspolne(db, ctx),
    )


def _formularz(**pola) -> dict:
    """Sklada slownik ustawien z pol formularza, uzupelniajac port domyslny."""
    protokol = pola.get("protokol") or "https"
    if not (pola.get("port") or "").strip():
        pola["port"] = PORTY_DOMYSLNE.get(protokol, 443)
    return pola


@router.post("/monitoring")
def dodaj_cel(
    request: Request,
    nazwa: str = Form(..., max_length=200),
    host: str = Form(..., max_length=255),
    wykonawca_id: str = Form(..., max_length=36),
    port: str = Form(""),
    protokol: str = Form("https"),
    sciezka: str = Form("/"),
    oczekiwany_kod: str = Form("0"),
    nazwa_tls: str = Form("", max_length=255),
    weryfikuj_lancuch: bool = Form(False),
    interwal_sekund: str = Form("60"),
    interwal_certyfikatu: str = Form("86400"),
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

    ustawienia = _z_formularza(_formularz(
        nazwa=nazwa, host=host, port=port, protokol=protokol, sciezka=sciezka,
        oczekiwany_kod=oczekiwany_kod, nazwa_tls=nazwa_tls,
        weryfikuj_lancuch=weryfikuj_lancuch, interwal_sekund=interwal_sekund,
        interwal_certyfikatu=interwal_certyfikatu, limit_sekund=limit_sekund,
        liczba_prob=liczba_prob, prog_ostrzezenia_dni=prog_ostrzezenia_dni,
        prog_alarmu_dni=prog_alarmu_dni, powiadamiaj=powiadamiaj, adresaci=adresaci,
        aktywny=True))
    if db.execute(select(MonitorUslugi.id).where(
            MonitorUslugi.tenant_id == ctx.tenant_id,
            MonitorUslugi.nazwa == ustawienia["nazwa"])).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="cel o tej nazwie juz istnieje")

    maszyna = _wykonawca(db, ctx, wykonawca_id)
    monitor = MonitorUslugi(tenant_id=ctx.tenant_id, utworzyl=user.email,
                            wykonawca_id=maszyna.id, **ustawienia)
    monitor.asset_id = _zasob(db, ctx, asset_id)
    # Pierwsze sprawdzenie idzie poza kolejnoscia: cel dodany i milczacy do
    # konca pierwszego odstepu nie mowi, czy w ogole zostal wpisany poprawnie.
    monitor.wymuszone_o = utcnow()
    db.add(monitor)
    db.flush()
    audit(db, ctx, action="monitoring.dodany", target=monitor.nazwa,
          detail={"host": monitor.host, "port": monitor.port, "protokol": monitor.protokol,
                  "wykonawca": maszyna.hostname},
          ip=client_ip(request))
    db.commit()
    return _wroc(
        f"Dodano cel {monitor.nazwa}. Sprawdzi go {maszyna.hostname} przy najblizszym cyklu."
    )


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
    return render(
        request, "monitoring_cel.html", user, ctx, db,
        monitor=monitor,
        przerwy=monitoring.przerwy(db, monitor.id, ctx.tenant_id, PRZERW_NA_STRONIE),
        maszyny=_agenci(db, ctx),
        dni=monitoring.dni_do_konca(monitor),
        milczy=monitoring.milczy(monitor),
        osierocony=monitoring.bez_wykonawcy(monitor),
        oczekuje_sprawdzenia=bool(
            monitor.wymuszone_o is not None
            and (monitor.ostatnie_sprawdzenie is None
                 or monitor.ostatnie_sprawdzenie < monitor.wymuszone_o)
        ),
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
    wykonawca_id: str = Form(..., max_length=36),
    port: str = Form(""),
    protokol: str = Form("https"),
    sciezka: str = Form("/"),
    oczekiwany_kod: str = Form("0"),
    nazwa_tls: str = Form("", max_length=255),
    weryfikuj_lancuch: bool = Form(False),
    interwal_sekund: str = Form("60"),
    interwal_certyfikatu: str = Form("86400"),
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

    ustawienia = _z_formularza(_formularz(
        nazwa=nazwa, host=host, port=port, protokol=protokol, sciezka=sciezka,
        oczekiwany_kod=oczekiwany_kod, nazwa_tls=nazwa_tls,
        weryfikuj_lancuch=weryfikuj_lancuch, interwal_sekund=interwal_sekund,
        interwal_certyfikatu=interwal_certyfikatu, limit_sekund=limit_sekund,
        liczba_prob=liczba_prob, prog_ostrzezenia_dni=prog_ostrzezenia_dni,
        prog_alarmu_dni=prog_alarmu_dni, powiadamiaj=powiadamiaj, adresaci=adresaci,
        aktywny=aktywny))
    if db.execute(select(MonitorUslugi.id).where(
            MonitorUslugi.tenant_id == ctx.tenant_id,
            MonitorUslugi.nazwa == ustawienia["nazwa"],
            MonitorUslugi.id != monitor.id)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="cel o tej nazwie juz istnieje")

    maszyna = _wykonawca(db, ctx, wykonawca_id, pomijany=monitor.id)
    przed = {k: getattr(monitor, k) for k in ustawienia}
    zmiana_celu = any(
        przed[k] != ustawienia[k] for k in ("host", "port", "protokol", "sciezka", "nazwa_tls")
    ) or maszyna.id != monitor.wykonawca_id
    for pole, wartosc in ustawienia.items():
        setattr(monitor, pole, wartosc)
    monitor.wykonawca_id = maszyna.id
    monitor.asset_id = _zasob(db, ctx, asset_id)

    if zmiana_celu:
        # Zmieniony adres albo inny wykonawca to inny pomiar, choc pod ta sama
        # nazwa. Stan i to, o czym juz powiadomilismy, dotyczyly poprzedniego -
        # zostawienie ich oznaczaloby alarm o awarii czegos, czego juz nie
        # monitorujemy. Historia przerw zostaje: opisuje to, co bylo naprawde.
        _wyzeruj_stan(monitor)

    audit(db, ctx, action="monitoring.zmieniony", target=monitor.nazwa,
          detail={"przed": {k: str(v) for k, v in przed.items() if przed[k] != ustawienia[k]},
                  "po": {k: str(v) for k, v in ustawienia.items() if przed[k] != v},
                  "wykonawca": maszyna.hostname},
          ip=client_ip(request))
    db.commit()
    return _wroc("Zapisano ustawienia celu.", f"/monitoring/{monitor.id}")


def _wyzeruj_stan(monitor: MonitorUslugi) -> None:
    """Kasuje stan i historie powiadomien, zostawiajac historie przerw."""
    monitor.stan = monitor.stan_dostepnosci = monitor.stan_certyfikatu = "nieznany"
    monitor.powiadomiona_dostepnosc = None
    monitor.powiadomiony_prog = None
    monitor.powiadomiony_stan_cert = None
    monitor.powiadomiony_odcisk = None
    monitor.cert_odcisk = monitor.cert_podmiot = monitor.cert_wystawca = None
    monitor.cert_od = monitor.cert_do = monitor.cert_nazwy = None
    monitor.cert_zaufany = monitor.cert_blad = None
    monitor.ostatnie_sprawdzenie = monitor.ostatni_raport = None
    monitor.ostatni_blad = monitor.ostatni_adres = None
    monitor.czas_odpowiedzi_ms = monitor.ostatni_kod = None
    monitor.wymuszone_o = utcnow()


@router.post("/monitoring/{monitor_id}/sprawdz")
def sprawdz_cel(
    monitor_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Zleca agentowi sprawdzenie poza kolejnoscia.

    To ZLECENIE, nie odpowiedz: sonduje agent, wiec wynik przyjdzie dopiero,
    gdy agent po nie siegnie. Udawanie natychmiastowej odpowiedzi byloby
    najgorsza z opcji - ktos zobaczylby "sprawdzono" i staly stan sprzed
    zmiany, ktora wlasnie wprowadzil.
    """
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    monitor = _cel(db, ctx, monitor_id, blokuj=True)
    if not monitor.aktywny:
        raise HTTPException(400, "cel jest wylaczony - najpierw go wlacz")
    monitor.wymuszone_o = utcnow()
    audit(db, ctx, action="monitoring.wymuszone", target=monitor.nazwa, ip=client_ip(request))
    db.commit()
    wykonawca = monitor.wykonawca.hostname if monitor.wykonawca else "agent"
    return _wroc(f"Zlecono sprawdzenie - {wykonawca} wykona je przy najblizszym cyklu.",
                 f"/monitoring/{monitor.id}")


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
        # zielonego stanu klamaloby, ze ktos go nadal pilnuje. Agent przestanie
        # go dostawac przy najblizszym pobraniu polityki.
        monitor.stan = monitor.stan_dostepnosci = monitor.stan_certyfikatu = "nieznany"
        monitor.powiadomiona_dostepnosc = None
        monitor.powiadomiony_stan_cert = None
    else:
        monitor.wymuszone_o = utcnow()
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
