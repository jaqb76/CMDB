"""Panel raportow: ustawienia poczty firmy i definicje raportow cyklicznych.

Osobny modul, bo ui.py urosl juz do rozmiaru, w ktorym dokladanie kolejnych
tras utrudnia czytanie. Trasy sa te same co reszta panelu firmowego - dziela
kontekst dzierzawcy i sposob renderowania.
"""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DefinicjaRaportu, PortalUser, Tenant, UstawieniaPoczty
from ..services import poczta, raporty, sekrety
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import render, resolve_tenant

log = logging.getLogger(__name__)
router = APIRouter(tags=["raporty"])

SZYFROWANIA = {"starttls", "ssl", "brak"}


def _definicja(db: Session, ctx: TenantContext, raport_id: str) -> DefinicjaRaportu:
    """Definicja nalezaca do TEJ firmy - nigdy cudza.

    Warunek na tenant_id jest tu barierą izolacji, a nie optymalizacja:
    bez niego identyfikator z cudzego panelu wystarczylby, zeby wyslac albo
    skasowac raport innej organizacji.
    """
    definicja = db.execute(
        select(DefinicjaRaportu).where(
            DefinicjaRaportu.id == raport_id,
            DefinicjaRaportu.tenant_id == ctx.tenant_id,
        )
    ).scalar_one_or_none()
    if definicja is None:
        raise HTTPException(status_code=404, detail="nie znaleziono raportu")
    return definicja


def _wroc(komunikat: str = "") -> RedirectResponse:
    adres = "/raporty"
    if komunikat:
        adres += "?komunikat=" + urllib.parse.quote(komunikat)
    return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)


def _zapis_dozwolony(ctx: TenantContext) -> None:
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")


@router.get("/raporty", response_class=HTMLResponse)
def strona_raportow(
    request: Request,
    komunikat: str = "",
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    definicje = db.execute(
        select(DefinicjaRaportu)
        .where(DefinicjaRaportu.tenant_id == ctx.tenant_id)
        .order_by(DefinicjaRaportu.nazwa)
    ).scalars().all()

    return render(
        request, "raporty.html", user, ctx, db,
        poczta=db.get(UstawieniaPoczty, ctx.tenant_id),
        definicje=definicje,
        rodzaje=raporty.RODZAJE,
        czestotliwosci=list(raporty.CZESTOTLIWOSCI),
        komunikat=komunikat[:500],
    )


@router.post("/raporty/poczta")
def zapisz_poczte(
    request: Request,
    host: str = Form(...),
    nadawca: str = Form(...),
    port: int = Form(587),
    szyfrowanie: str = Form("starttls"),
    uzytkownik: str = Form(""),
    haslo: str = Form(""),
    nazwa_nadawcy: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Zapisuje poswiadczenia SMTP firmy."""
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)

    wpis = db.get(UstawieniaPoczty, ctx.tenant_id)
    if wpis is None:
        wpis = UstawieniaPoczty(tenant_id=ctx.tenant_id, host="", nadawca="")
        db.add(wpis)

    wpis.host = host.strip()
    wpis.port = port
    wpis.szyfrowanie = szyfrowanie if szyfrowanie in SZYFROWANIA else "starttls"
    wpis.uzytkownik = uzytkownik.strip() or None
    wpis.nadawca = nadawca.strip()
    wpis.nazwa_nadawcy = nazwa_nadawcy.strip() or None
    wpis.aktywne = True
    wpis.ostatni_blad = None
    # Puste pole hasla znaczy "zostaw poprzednie". Inaczej kazda zmiana portu
    # albo nazwy nadawcy wymagalaby wpisywania hasla od nowa, co konczy sie
    # trzymaniem go w notatniku obok.
    if haslo:
        wpis.haslo_szyfr = sekrety.zaszyfruj(haslo)

    audit(db, ctx, action="poczta.zapisana", target=wpis.host, ip=client_ip(request))
    db.commit()
    return _wroc("Ustawienia poczty zapisane.")


@router.post("/raporty/poczta/test")
def sprawdz_poczte(
    request: Request,
    adres: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Wysyla wiadomosc probna.

    Jedyny pewny sposob sprawdzenia ustawien: poprawnie wygladajaca
    konfiguracja i dzialajaca konfiguracja to dwie rozne rzeczy.
    """
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)

    wpis = db.get(UstawieniaPoczty, ctx.tenant_id)
    try:
        poczta.sprawdz(db, ctx.tenant_id, adres.strip())
        komunikat = f"Wiadomosc probna wyslana na {adres.strip()}."
        if wpis is not None:
            wpis.ostatni_blad = None
    except poczta.BladPoczty as exc:
        komunikat = f"Nie udalo sie wyslac: {exc}"
        if wpis is not None:
            wpis.ostatni_blad = str(exc)[:1000]
    db.commit()
    return _wroc(komunikat)


@router.post("/raporty/definicje")
def dodaj_raport(
    request: Request,
    nazwa: str = Form(...),
    rodzaj: str = Form(...),
    adresaci: str = Form(...),
    czestotliwosc: str = Form("tygodniowo"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)

    if rodzaj not in raporty.RODZAJE:
        raise HTTPException(status_code=400, detail="nieznany rodzaj raportu")
    if czestotliwosc not in raporty.CZESTOTLIWOSCI:
        raise HTTPException(status_code=400, detail="nieznana czestotliwosc")

    definicja = DefinicjaRaportu(
        tenant_id=ctx.tenant_id,
        nazwa=nazwa.strip()[:200],
        rodzaj=rodzaj,
        czestotliwosc=czestotliwosc,
        adresaci=adresaci.strip(),
        utworzyl=user.email,
    )
    if not raporty.adresaci(definicja):
        raise HTTPException(status_code=400, detail="nie podano poprawnego adresu")

    db.add(definicja)
    audit(db, ctx, action="raport.dodany", target=definicja.nazwa, ip=client_ip(request))
    db.commit()
    return _wroc(f"Dodano raport {definicja.nazwa}.")


@router.post("/raporty/definicje/{raport_id}/usun")
def usun_raport(
    request: Request,
    raport_id: str,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    definicja = _definicja(db, ctx, raport_id)
    nazwa = definicja.nazwa
    db.delete(definicja)
    audit(db, ctx, action="raport.usuniety", target=nazwa, ip=client_ip(request))
    db.commit()
    return _wroc(f"Usunieto raport {nazwa}.")


@router.post("/raporty/definicje/{raport_id}/wyslij")
def wyslij_teraz(
    request: Request,
    raport_id: str,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Wysylka na zadanie, bez czekania na termin z harmonogramu."""
    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    definicja = _definicja(db, ctx, raport_id)
    raporty.wyslij_raport(db, definicja)

    if definicja.ostatni_status == "ok":
        return _wroc(f"Raport {definicja.nazwa} wyslany.")
    return _wroc(f"Nie udalo sie wyslac: {definicja.ostatni_blad}")


@router.get("/raporty/podglad/{rodzaj}", response_class=HTMLResponse)
def podglad_raportu(
    rodzaj: str,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Podglad tresci raportu bez wysylania go komukolwiek.

    Ta sama tresc idzie potem poczta - podglad, ktory rozni sie od wysylki,
    bylby gorszy niz jego brak.
    """
    if rodzaj not in raporty.RODZAJE:
        raise HTTPException(status_code=404, detail="nieznany rodzaj raportu")
    tenant = db.get(Tenant, ctx.tenant_id)
    html, _ = raporty.renderuj(raporty.zbuduj(db, tenant, rodzaj))
    return HTMLResponse(html)


# --- dane zakupu i gwarancji ------------------------------------------------

@router.post("/assets/{asset_id}/zakup")
def zapisz_zakup(
    request: Request,
    asset_id: str,
    purchase_date: str = Form(""),
    warranty_until: str = Form(""),
    vendor: str = Form(""),
    purchase_price: str = Form(""),
    purchase_currency: str = Form(""),
    invoice_number: str = Form(""),
    support_contract: str = Form(""),
    purchase_notes: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Dane, ktorych agent nie ma skad znac.

    Data zakupu, gwarancja i numer faktury nie wynikaja z niczego, co da sie
    odczytac z maszyny - wpisuje je czlowiek. Data konca gwarancji jest
    najwazniejsza, bo na niej opiera sie raport o wygasajacym wsparciu.
    """
    from datetime import date as _date

    from ..models import Asset

    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)

    maszyna = db.execute(
        select(Asset).where(Asset.id == asset_id, Asset.tenant_id == ctx.tenant_id)
    ).scalar_one_or_none()
    if maszyna is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")

    def data(wartosc: str):
        wartosc = (wartosc or "").strip()
        if not wartosc:
            return None
        try:
            return _date.fromisoformat(wartosc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"niepoprawna data: {wartosc}") from exc

    def kwota(wartosc: str):
        wartosc = (wartosc or "").strip().replace(",", ".")
        if not wartosc:
            return None
        try:
            return float(wartosc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="niepoprawna kwota") from exc

    maszyna.purchase_date = data(purchase_date)
    maszyna.warranty_until = data(warranty_until)
    maszyna.vendor = vendor.strip()[:200] or None
    maszyna.purchase_price = kwota(purchase_price)
    maszyna.purchase_currency = purchase_currency.strip()[:8].upper() or None
    maszyna.invoice_number = invoice_number.strip()[:120] or None
    maszyna.support_contract = support_contract.strip()[:200] or None
    maszyna.purchase_notes = purchase_notes.strip() or None

    audit(db, ctx, action="asset.zakup", target=maszyna.hostname, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset_id}#overview",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("/raporty/widok/{rodzaj}", response_class=HTMLResponse)
def widok_raportu(
    request: Request,
    rodzaj: str,
    raport_id: str = "",
    kolumny: list[str] = Query(default=[]),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Pelny raport na stronie, wraz z wyborem kolumn.

    Ta sama tresc idzie potem poczta - widok rozniacy sie od wysylki bylby
    gorszy niz jego brak.

    Kolumny mozna zmieniac zawsze, takze bez wskazanego raportu: wybor
    z adresu dziala od razu na tym, co widac. Zapisanie go przy definicji
    jest osobnym krokiem - dopiero wtedy obowiazuje takze wysylke.
    """
    from ..services import kolumny as katalog

    if rodzaj not in raporty.RODZAJE:
        raise HTTPException(status_code=404, detail="nieznany rodzaj raportu")

    definicja = _definicja(db, ctx, raport_id) if raport_id else None
    # Wybor z adresu ma pierwszenstwo - to on jest tym, co uzytkownik wlasnie
    # kliknal. Zapis przy definicji sluzy za wartosc wyjsciowa.
    wybor = list(kolumny) if kolumny else (definicja.kolumny if definicja else None)

    tenant = db.get(Tenant, ctx.tenant_id)
    raport = raporty.zbuduj(db, tenant, rodzaj, wybor)
    tresc, _ = raporty.renderuj(raport)

    return render(
        request, "raport_widok.html", user, ctx, db,
        raport=raport,
        tresc=tresc,
        rodzaj=rodzaj,
        rodzaje=raporty.RODZAJE,
        definicja=definicja,
        definicje=db.execute(
            select(DefinicjaRaportu)
            .where(DefinicjaRaportu.tenant_id == ctx.tenant_id)
            .order_by(DefinicjaRaportu.nazwa)
        ).scalars().all(),
        grupy_kolumn=katalog.grupy(),
        wybrane_klucze={k["klucz"] for k in raport["kolumny"]},
    )


@router.post("/raporty/definicje/{raport_id}/kolumny")
def zapisz_kolumny(
    request: Request,
    raport_id: str,
    kolumny: list[str] = Form(default=[]),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Zapisuje wybor kolumn przy definicji raportu.

    Kolejnosc zaznaczenia nie ma znaczenia - zapisujemy w kolejnosci
    katalogu, zeby tabela zawsze wygladala tak samo niezaleznie od tego,
    w jakiej kolejnosci ktos klikal.
    """
    from ..services import kolumny as katalog

    _zapis_dozwolony(ctx)
    verify_csrf(request, user, csrf_token)
    definicja = _definicja(db, ctx, raport_id)

    wybrane = [k["klucz"] for k in katalog.KOLUMNY if k["klucz"] in set(kolumny)]
    definicja.kolumny = wybrane or None
    audit(db, ctx, action="raport.kolumny", target=definicja.nazwa, ip=client_ip(request))
    db.commit()

    return RedirectResponse(
        f"/raporty/widok/{definicja.rodzaj}?raport_id={definicja.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
