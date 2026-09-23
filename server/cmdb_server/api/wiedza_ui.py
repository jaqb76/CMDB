"""Panel bazy wiedzy.

Osobny modul z tego samego powodu co panel monitorowania i helpdesku: ui.py
jest juz duzy. Trasy dziela z reszta panelu kontekst firmy i renderowanie.

Uprawnienia sa sprawdzane na KAZDEJ trasie zapisu (``_zapis``), a nie tylko
w menu - w poprzednim systemie menu chowalo modul, ale trasy przyjmowaly
zapis od kazdego zalogowanego. Konto tylko do odczytu (viewer, audytor
globalny) dostaje 403.
"""
from __future__ import annotations

import difflib
import urllib.parse
from datetime import date

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import KATEGORIE_WIEDZY, SLOWNIK_WIEDZY_SYSTEM, SLOWNIK_WIEDZY_TAG, Asset, PortalUser, WiedzaArtykul
from ..services import rodzaje, scoping, slowniki, wiedza
from ..services import wiedza_dopasowanie as dopasowanie
from ..services import wiedza_tresc as tresc
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.helpdesk_poczta import do_podgladu
from ..services.scoping import TenantContext, audit
from .ui import render, resolve_tenant, templates

router = APIRouter(tags=["baza wiedzy"])

# Pomocnicze dla szablonow bazy wiedzy - makra importowane bez kontekstu
# widza tylko globalne nazwy srodowiska.
templates.env.globals["dzisiaj"] = date.today
templates.env.globals["etykieta_warunku"] = lambda pole: dopasowanie.POLA_DOTYCZY.get(pole, pole)

# Ile pustych wierszy warunku dokladamy w formularzu. Bez skryptu to jedyny
# sposob na dopisanie warunku; ze skryptem przycisk dodaje kolejne.
PUSTE_WARUNKI = 2


def _zapis(request: Request, user: PortalUser, ctx: TenantContext, csrf_token: str) -> None:
    verify_csrf(request, user, csrf_token)
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")


def _wroc(adres: str, komunikat: str = "") -> RedirectResponse:
    if komunikat:
        adres += ("&" if "?" in adres else "?") + "komunikat=" + urllib.parse.quote(komunikat)
    return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)


def _artykul(db: Session, ctx: TenantContext, artykul_id: str, **opcje) -> WiedzaArtykul:
    art = wiedza.artykul(db, ctx, artykul_id, **opcje)
    if art is None:
        raise HTTPException(status_code=404, detail="nie znaleziono artykułu")
    return art


def _maszyny(db: Session, ctx: TenantContext, ids: list[str]) -> dict[str, Asset]:
    if not ids:
        return {}
    return {a.id: a for a in db.execute(select(Asset).where(
        Asset.tenant_id == ctx.tenant_id, Asset.id.in_(ids))).scalars()}


def powody_opis(powody: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(dopasowanie.ETYKIETY_POWODOW.get(p, p), w) for p, w in powody]


# --- start --------------------------------------------------------------------------

@router.get("/wiedza", response_class=HTMLResponse)
def start(
    request: Request,
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    liczby = dict(db.execute(
        select(WiedzaArtykul.przestrzen_id, func.count(WiedzaArtykul.id))
        .where(WiedzaArtykul.tenant_id == ctx.tenant_id, WiedzaArtykul.usuniety_o.is_(None))
        .group_by(WiedzaArtykul.przestrzen_id)
    ).all())
    objete = dopasowanie.liczniki(db, ctx.tenant_id)
    aktywne = db.execute(select(func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.lifecycle == "aktywny")).scalar_one()
    aktywne_objete = db.execute(select(func.count(Asset.id)).where(
        Asset.tenant_id == ctx.tenant_id, Asset.lifecycle == "aktywny",
        Asset.id.in_(list(objete) or [""]))).scalar_one()
    return render(
        request, "wiedza_start.html", user, ctx, db,
        przestrzenie=wiedza.przestrzenie(db, ctx),
        liczby=liczby,
        razem=sum(liczby.values()),
        na_dyzur=wiedza.artykuly(db, ctx, na_dyzur=True),
        ostatnie=wiedza.artykuly(db, ctx, kolejnosc="zmiana", limit=10),
        do_przegladu=wiedza.do_przegladu(db, ctx),
        maszyny_objete=aktywne_objete, maszyny_aktywne=aktywne,
        w_koszu=db.execute(select(func.count(WiedzaArtykul.id)).where(
            WiedzaArtykul.tenant_id == ctx.tenant_id,
            WiedzaArtykul.usuniety_o.is_not(None))).scalar_one(),
        komunikat=komunikat,
    )


# --- lista i wyszukiwanie ----------------------------------------------------------------

@router.get("/wiedza/artykuly", response_class=HTMLResponse)
def lista(
    request: Request,
    q: str = Query("", max_length=200),
    kategoria: str = Query("", max_length=32),
    tag: str = Query("", max_length=wiedza.MAKS_TAG),
    system: str = Query("", max_length=wiedza.MAKS_SYSTEM),
    przestrzen: str = Query("", max_length=36),
    maszyna: str = Query("", max_length=36),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    filtr = wiedza.Filtr(q=q.strip(), kategoria=kategoria, tag=tag.strip(), system=system.strip(),
                         przestrzen_id=przestrzen, asset_id=maszyna)
    wyniki = wiedza.szukaj(db, ctx, filtr)
    wybrana_maszyna = scoping.get_asset(db, ctx, maszyna) if maszyna else None
    return render(
        request, "wiedza_lista.html", user, ctx, db,
        wyniki=wyniki,
        fragmenty={a.id: tresc.fragment(a.tresc, filtr.q) for a in wyniki} if filtr.q else {},
        filtr=filtr,
        wybrana_maszyna=wybrana_maszyna,
        przestrzenie=wiedza.przestrzenie(db, ctx),
        kategorie=KATEGORIE_WIEDZY,
        tagi=wiedza.podpowiedzi(db, ctx, SLOWNIK_WIEDZY_TAG, limit=500),
        systemy=wiedza.podpowiedzi(db, ctx, SLOWNIK_WIEDZY_SYSTEM, limit=500),
    )


@router.get("/wiedza/kosz", response_class=HTMLResponse)
def kosz(
    request: Request,
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    return render(request, "wiedza_kosz.html", user, ctx, db,
                  usuniete=wiedza.artykuly(db, ctx, usuniete=True, kolejnosc="usuniecie"),
                  komunikat=komunikat)


# --- przestrzenie ------------------------------------------------------------------------

@router.get("/wiedza/przestrzenie", response_class=HTMLResponse)
def przestrzenie(
    request: Request,
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    liczby = dict(db.execute(
        select(WiedzaArtykul.przestrzen_id, func.count(WiedzaArtykul.id))
        .where(WiedzaArtykul.tenant_id == ctx.tenant_id).group_by(WiedzaArtykul.przestrzen_id)
    ).all())
    return render(request, "wiedza_przestrzenie.html", user, ctx, db,
                  przestrzenie=wiedza.przestrzenie(db, ctx), liczby=liczby, komunikat=komunikat)


@router.post("/wiedza/przestrzenie")
def dodaj_przestrzen(
    request: Request,
    nazwa: str = Form("", max_length=200),
    opis: str = Form("", max_length=400),
    skrot: str = Form("", max_length=10),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    try:
        nowa = wiedza.zapisz_przestrzen(db, ctx, nazwa, opis, skrot)
    except ValueError as blad:
        return _wroc("/wiedza/przestrzenie", f"Nie zapisano: {blad}")
    audit(db, ctx, "wiedza.przestrzen.dodana", nowa.nazwa, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/p/{nowa.id}", f"Dodano przestrzeń {nowa.nazwa}")


@router.get("/wiedza/p/{przestrzen_id}", response_class=HTMLResponse)
def przestrzen(
    przestrzen_id: str,
    request: Request,
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    cel = wiedza.przestrzen(db, ctx, przestrzen_id)
    if cel is None:
        raise HTTPException(status_code=404, detail="nie znaleziono przestrzeni")
    lista_art = wiedza.artykuly(db, ctx, przestrzen_id=cel.id)
    return render(request, "wiedza_przestrzen.html", user, ctx, db,
                  przestrzen=cel, drzewo=wiedza.drzewo(lista_art), liczba=len(lista_art),
                  przestrzenie=wiedza.przestrzenie(db, ctx), komunikat=komunikat)


@router.post("/wiedza/p/{przestrzen_id}")
def zmien_przestrzen(
    przestrzen_id: str,
    request: Request,
    nazwa: str = Form("", max_length=200),
    opis: str = Form("", max_length=400),
    skrot: str = Form("", max_length=10),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    cel = wiedza.przestrzen(db, ctx, przestrzen_id)
    if cel is None:
        raise HTTPException(status_code=404, detail="nie znaleziono przestrzeni")
    try:
        wiedza.zapisz_przestrzen(db, ctx, nazwa, opis, skrot, istniejaca=cel)
    except ValueError as blad:
        return _wroc("/wiedza/przestrzenie", f"Nie zapisano: {blad}")
    audit(db, ctx, "wiedza.przestrzen.zmieniona", cel.nazwa, ip=client_ip(request))
    db.commit()
    return _wroc("/wiedza/przestrzenie", f"Zapisano przestrzeń {cel.nazwa}")


@router.post("/wiedza/p/{przestrzen_id}/usun")
def usun_przestrzen(
    przestrzen_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    cel = wiedza.przestrzen(db, ctx, przestrzen_id)
    if cel is None:
        raise HTTPException(status_code=404, detail="nie znaleziono przestrzeni")
    try:
        wiedza.usun_przestrzen(db, ctx, cel)
    except ValueError as blad:
        return _wroc("/wiedza/przestrzenie", f"Nie usunięto: {blad}")
    audit(db, ctx, "wiedza.przestrzen.usunieta", cel.nazwa, ip=client_ip(request))
    db.commit()
    return _wroc("/wiedza/przestrzenie", f"Usunięto przestrzeń {cel.nazwa}")


# --- formularz artykulu ---------------------------------------------------------------------

def _formularz(request: Request, user: PortalUser, ctx: TenantContext, db: Session, *,
               art: WiedzaArtykul | None, pola: dict, blad: str = "",
               status_code: int = 200) -> HTMLResponse:
    przestrzen_id = pola.get("przestrzen_id") or ""
    wykluczone = ({art.id} | wiedza.potomkowie(db, ctx, art.id)) if art else set()
    strony = [a for a in wiedza.artykuly(db, ctx, przestrzen_id=przestrzen_id or None)
              if a.id not in wykluczone] if przestrzen_id else []
    warunki = list(pola.get("dotyczy") or []) + [{"pole": "host", "wartosc": ""}] * PUSTE_WARUNKI
    wlasciciele = wiedza.wlasciciele(db, ctx)
    if pola.get("wlasciciel") and pola["wlasciciel"] not in wlasciciele:
        wlasciciele.append(pola["wlasciciel"])
    odpowiedz = render(
        request, "wiedza_edycja.html", user, ctx, db,
        art=art, pola=pola, blad=blad, warunki=warunki,
        przestrzenie=wiedza.przestrzenie(db, ctx), strony=strony,
        kategorie=KATEGORIE_WIEDZY, pola_dotyczy=dopasowanie.POLA_DOTYCZY,
        rodzaje_sprzetu=rodzaje.etykiety(db, ctx),
        lokalizacje=[w.wartosc for w in slowniki.wpisy(db, ctx, "lokalizacja")],
        wlasciciele=wlasciciele, przeglady=wiedza.PRZEGLADY,
        tagi=wiedza.podpowiedzi(db, ctx, SLOWNIK_WIEDZY_TAG, limit=500),
        systemy=wiedza.podpowiedzi(db, ctx, SLOWNIK_WIEDZY_SYSTEM, limit=500),
        pola_maszyny=tresc.POLA_MASZYNY, panele=tresc.PANELE,
    )
    odpowiedz.status_code = status_code
    return odpowiedz


def _pola_z_artykulu(art: WiedzaArtykul) -> dict:
    dane = wiedza.dane_z_artykulu(art)
    return {**dane.__dict__, "tagi": ", ".join(dane.tagi), "systemy": ", ".join(dane.systemy),
            "wersja": art.wersja}


@router.get("/wiedza/nowy", response_class=HTMLResponse)
def nowy(
    request: Request,
    przestrzen: str = Query("", max_length=36),
    rodzic: str = Query("", max_length=36),
    maszyna: str = Query("", max_length=36),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")
    wybrana = wiedza.przestrzen(db, ctx, przestrzen) if przestrzen else None
    rodzic_art = wiedza.artykul(db, ctx, rodzic) if rodzic else None
    if rodzic_art is not None:
        wybrana = wiedza.przestrzen(db, ctx, rodzic_art.przestrzen_id)
    if wybrana is None:
        istniejace = wiedza.przestrzenie(db, ctx)
        wybrana = istniejace[0] if istniejace else None
    pola: dict = {
        "przestrzen_id": wybrana.id if wybrana else "", "rodzic_id": rodzic_art.id if rodzic_art else "",
        "kategoria": "procedures", "przeglad_co_mies": 6, "wlasciciel": user.email,
        "tagi": "", "systemy": "", "dotyczy": [],
        "tresc": "## Kiedy\n\n\n## Kroki\n\n1. \n",
    }
    # "Artykul dla tej maszyny" z karty zasobu: system = jej nazwa, czyli
    # artykul od razu pojawi sie na jej karcie.
    zasob = scoping.get_asset(db, ctx, maszyna) if maszyna else None
    if zasob is not None:
        pola["systemy"] = zasob.fqdn or zasob.hostname
        pola["tytul"] = f"{zasob.hostname} — "
    return _formularz(request, user, ctx, db, art=None, pola=pola)


def _rodzaje_na_klucze(db: Session, ctx: TenantContext, pola: dict) -> dict:
    """Warunek "rodzaj sprzetu" porownuje klucz rodzaju - ale czlowiek wpisze
    raczej etykiete ("Drukarka / skaner"). Zamieniamy ja na klucz."""
    etykiety = {wiedza.klucz(e): k for k, e in rodzaje.etykiety(db, ctx).items()}
    for warunek in pola["dotyczy"]:
        if warunek["pole"] == "rodzaj":
            warunek["wartosc"] = etykiety.get(wiedza.klucz(warunek["wartosc"]), warunek["wartosc"])
    return pola


def _pola_formularza(tytul, streszczenie, tresc_, kategoria, przestrzen_id, rodzic_id, tagi,
                     systemy, warunek_pole, warunek_wartosc, notatki_zalacznikow,
                     notatki_diagramu, wlasciciel, przeglad_co_mies, na_dyzur) -> dict:
    return {
        "tytul": tytul, "streszczenie": streszczenie, "tresc": tresc_, "kategoria": kategoria,
        "przestrzen_id": przestrzen_id, "rodzic_id": rodzic_id, "tagi": tagi, "systemy": systemy,
        "dotyczy": [{"pole": p, "wartosc": w} for p, w in zip(warunek_pole, warunek_wartosc)
                    if (w or "").strip()],
        "notatki_zalacznikow": notatki_zalacznikow, "notatki_diagramu": notatki_diagramu,
        "wlasciciel": wlasciciel, "przeglad_co_mies": przeglad_co_mies, "na_dyzur": na_dyzur,
    }


@router.post("/wiedza/nowy")
def utworz(
    request: Request,
    tytul: str = Form("", max_length=400),
    streszczenie: str = Form("", max_length=1000),
    tresc_: str = Form("", alias="tresc", max_length=wiedza.MAKS_TRESC + 1000),
    kategoria: str = Form("", max_length=32),
    przestrzen_id: str = Form("", max_length=36),
    rodzic_id: str = Form("", max_length=36),
    tagi: str = Form("", max_length=4000),
    systemy: str = Form("", max_length=6000),
    warunek_pole: list[str] = Form([]),
    warunek_wartosc: list[str] = Form([]),
    notatki_zalacznikow: str = Form("", max_length=wiedza.MAKS_NOTATKI + 100),
    notatki_diagramu: str = Form("", max_length=wiedza.MAKS_NOTATKI + 100),
    wlasciciel: str = Form("", max_length=255),
    przeglad_co_mies: str = Form("6", max_length=4),
    na_dyzur: str = Form(""),
    opis_zmiany: str = Form("", max_length=300),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    pola = _pola_formularza(tytul, streszczenie, tresc_, kategoria, przestrzen_id, rodzic_id, tagi,
                            systemy, warunek_pole, warunek_wartosc, notatki_zalacznikow,
                            notatki_diagramu, wlasciciel, przeglad_co_mies, na_dyzur)
    if not przestrzen_id:
        pola["przestrzen_id"] = wiedza.zapewnij_przestrzen(db, ctx).id
    _rodzaje_na_klucze(db, ctx, pola)
    try:
        art, _ = wiedza.zapisz(db, ctx, wiedza.sprawdz_dane(pola), opis_zmiany=opis_zmiany)
    except ValueError as blad:
        db.rollback()
        return _formularz(request, user, ctx, db, art=None, pola=pola, blad=str(blad), status_code=400)
    audit(db, ctx, "wiedza.artykul.utworzony", art.tytul, {"id": art.id}, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}", "Opublikowano artykuł")


@router.get("/wiedza/a/{artykul_id}/edycja", response_class=HTMLResponse)
def edycja(
    artykul_id: str,
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")
    art = _artykul(db, ctx, artykul_id)
    return _formularz(request, user, ctx, db, art=art, pola=_pola_z_artykulu(art))


@router.post("/wiedza/a/{artykul_id}")
def zapisz(
    artykul_id: str,
    request: Request,
    tytul: str = Form("", max_length=400),
    streszczenie: str = Form("", max_length=1000),
    tresc_: str = Form("", alias="tresc", max_length=wiedza.MAKS_TRESC + 1000),
    kategoria: str = Form("", max_length=32),
    przestrzen_id: str = Form("", max_length=36),
    rodzic_id: str = Form("", max_length=36),
    tagi: str = Form("", max_length=4000),
    systemy: str = Form("", max_length=6000),
    warunek_pole: list[str] = Form([]),
    warunek_wartosc: list[str] = Form([]),
    notatki_zalacznikow: str = Form("", max_length=wiedza.MAKS_NOTATKI + 100),
    notatki_diagramu: str = Form("", max_length=wiedza.MAKS_NOTATKI + 100),
    wlasciciel: str = Form("", max_length=255),
    przeglad_co_mies: str = Form("6", max_length=4),
    na_dyzur: str = Form(""),
    opis_zmiany: str = Form("", max_length=300),
    wersja: int = Form(0),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    pola = _pola_formularza(tytul, streszczenie, tresc_, kategoria, przestrzen_id, rodzic_id, tagi,
                            systemy, warunek_pole, warunek_wartosc, notatki_zalacznikow,
                            notatki_diagramu, wlasciciel, przeglad_co_mies, na_dyzur)
    pola["wersja"] = wersja
    _rodzaje_na_klucze(db, ctx, pola)
    if wersja and wersja != art.wersja:
        # Ktos zapisal artykul, kiedy ten formularz byl otwarty. Nadpisanie
        # po cichu zgubiloby jego zmiany - pokazujemy formularz z nasza trescia
        # i numerem wersji, ktora trzeba najpierw obejrzec.
        pola["wersja"] = art.wersja
        return _formularz(
            request, user, ctx, db, art=art, pola=pola, status_code=409,
            blad=(f"W międzyczasie {art.zmienil or 'ktoś'} zapisał wersję {art.wersja}. "
                  "Twoja treść jest poniżej - sprawdź historię i zapisz ponownie."))
    try:
        _, nowa = wiedza.zapisz(db, ctx, wiedza.sprawdz_dane(pola), art, opis_zmiany)
    except ValueError as blad:
        db.rollback()
        return _formularz(request, user, ctx, db, art=art, pola=pola, blad=str(blad), status_code=400)
    if nowa is None:
        db.rollback()
        return _wroc(f"/wiedza/a/{art.id}", "Bez zmian - nowa wersja nie powstała")
    audit(db, ctx, "wiedza.artykul.zmieniony", art.tytul,
          {"id": art.id, "wersja": art.wersja, "pola": sorted(nowa.zmiany)}, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}", f"Zapisano wersję {art.wersja}")


# --- strona artykulu --------------------------------------------------------------------------

@router.get("/wiedza/a/{artykul_id}", response_class=HTMLResponse)
def artykul(
    artykul_id: str,
    request: Request,
    maszyna: str = Query("", max_length=36),
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    art = _artykul(db, ctx, artykul_id, takze_usuniety=True)
    trafienia = dopasowanie.trafienia(db, ctx.tenant_id, artykul_id=art.id)
    maszyny = _maszyny(db, ctx, [t.asset_id for t in trafienia])
    objete = sorted(
        ((maszyny[t.asset_id], powody_opis(t.powody)) for t in trafienia if t.asset_id in maszyny),
        key=lambda para: para[0].hostname.lower(),
    )

    # Kontekst maszyny: pola {hostname} itd. wypelniaja sie jej danymi.
    # Tylko maszyna z tej firmy - identyfikator z cudzego panelu nic nie da.
    kontekst_maszyna = scoping.get_asset(db, ctx, maszyna) if maszyna else None
    kontekst = None
    if kontekst_maszyna is not None:
        odczyt = scoping.current_reading(db, ctx, kontekst_maszyna.id)
        kontekst = tresc.kontekst_maszyny(kontekst_maszyna, odczyt.payload if odczyt else None)

    lista_przestrzeni = wiedza.artykuly(db, ctx, przestrzen_id=art.przestrzen_id)
    return render(
        request, "wiedza_artykul.html", user, ctx, db,
        art=art, html=tresc.renderuj(art.tresc, kontekst),
        przestrzen=wiedza.przestrzen(db, ctx, art.przestrzen_id),
        # Nie "sciezka" - pod ta nazwa szablon bazowy trzyma adres strony.
        okruszki=wiedza.sciezka(db, ctx, art),
        drzewo=wiedza.drzewo(lista_przestrzeni),
        dzieci=[a for a in lista_przestrzeni if a.rodzic_id == art.id],
        objete=objete, kontekst_maszyna=kontekst_maszyna,
        zalaczniki=wiedza.zalaczniki(db, art),
        limit_mb=wiedza.limit_zalacznika() // (1024 * 1024),
        ostatnia_wersja=next(iter(wiedza.wersje(db, art)), None),
        komunikat=komunikat,
    )


@router.post("/wiedza/a/{artykul_id}/usun")
def usun(
    artykul_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    wiedza.usun(db, ctx, art)
    audit(db, ctx, "wiedza.artykul.usuniety", art.tytul, {"id": art.id}, ip=client_ip(request))
    db.commit()
    return _wroc("/wiedza/kosz", f"Artykuł „{art.tytul}” jest w koszu - można go przywrócić")


@router.post("/wiedza/a/{artykul_id}/przywroc")
def przywroc(
    artykul_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, takze_usuniety=True, blokuj=True)
    if art.usuniety_o is None:
        return _wroc(f"/wiedza/a/{art.id}")
    wiedza.przywroc(db, ctx, art)
    audit(db, ctx, "wiedza.artykul.przywrocony", art.tytul, {"id": art.id}, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}", "Przywrócono artykuł z kosza")


@router.post("/wiedza/a/{artykul_id}/przejrzany")
def przejrzany(
    artykul_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    if wiedza.oznacz_przejrzany(db, ctx, art) is None:
        return _wroc(f"/wiedza/a/{art.id}", "Termin przeglądu bez zmian")
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}", f"Następny przegląd: {art.przeglad_do:%d.%m.%Y}")


# --- historia ---------------------------------------------------------------------------------

def _diff(stara: str, nowa: str) -> list[tuple[str, str]]:
    """Porownanie linia po linii: (rodzaj, linia), rodzaj: dod / usu / kon / sep."""
    wynik: list[tuple[str, str]] = []
    for linia in difflib.unified_diff(stara.splitlines(), nowa.splitlines(), lineterm="", n=2):
        if linia.startswith(("---", "+++")):
            continue
        if linia.startswith("@@"):
            wynik.append(("sep", "…"))
        elif linia.startswith("+"):
            wynik.append(("dod", linia[1:]))
        elif linia.startswith("-"):
            wynik.append(("usu", linia[1:]))
        else:
            wynik.append(("kon", linia[1:]))
    return wynik


@router.get("/wiedza/a/{artykul_id}/historia", response_class=HTMLResponse)
def historia(
    artykul_id: str,
    request: Request,
    od: int = Query(0, ge=0),
    do: int = Query(0, ge=0),
    komunikat: str = Query("", max_length=300),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    art = _artykul(db, ctx, artykul_id, takze_usuniety=True)
    lista_wersji = wiedza.wersje(db, art)
    do = do if 0 < do <= art.wersja else art.wersja
    od = od if 0 < od < do else max(1, do - 1)
    porownanie = _diff(wiedza.tresc_w_wersji(db, art, od), wiedza.tresc_w_wersji(db, art, do)) \
        if od < do else []
    return render(
        request, "wiedza_historia.html", user, ctx, db,
        art=art, wersje=lista_wersji, od=od, do=do, porownanie=porownanie,
        etykiety_pol=wiedza.ETYKIETY_POL, kategorie=KATEGORIE_WIEDZY,
        pola_dotyczy=dopasowanie.POLA_DOTYCZY, komunikat=komunikat,
    )


@router.post("/wiedza/a/{artykul_id}/historia/{numer}/przywroc")
def przywroc_wersje(
    artykul_id: str,
    numer: int,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    if not 1 <= numer <= art.wersja:
        raise HTTPException(status_code=404, detail="nie ma takiej wersji")
    if wiedza.przywroc_tresc(db, ctx, art, numer) is None:
        db.rollback()
        return _wroc(f"/wiedza/a/{art.id}/historia", f"Treść wersji {numer} jest taka sama jak bieżąca")
    audit(db, ctx, "wiedza.artykul.przywrocona_wersja", art.tytul,
          {"id": art.id, "z_wersji": numer}, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}", f"Przywrócono treść wersji {numer} jako wersję {art.wersja}")


# --- zalaczniki --------------------------------------------------------------------------------

@router.post("/wiedza/a/{artykul_id}/zalaczniki")
async def dodaj_zalaczniki(
    artykul_id: str,
    request: Request,
    pliki: list[UploadFile] = File(default=[]),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    limit = wiedza.limit_zalacznika()
    wczytane: list[wiedza.Plik] = []
    for plik in pliki[:20]:
        # Czytamy o bajt wiecej niz limit - wiecej nie trzeba, zeby wiedziec,
        # ze plik jest za duzy, a caly moglby zajac pamiec serwera.
        dane = await plik.read(limit + 1)
        wczytane.append(wiedza.Plik(plik.filename or "", plik.content_type, dane))
    try:
        wersja = wiedza.dodaj_zalaczniki(db, ctx, art, wczytane)
    except ValueError as blad:
        db.rollback()
        return _wroc(f"/wiedza/a/{art.id}#zalaczniki", f"Nie dodano: {blad}")
    if wersja is None:
        return _wroc(f"/wiedza/a/{art.id}#zalaczniki", "Nie wybrano pliku")
    audit(db, ctx, "wiedza.zalacznik.dodany", art.tytul,
          {"id": art.id, "pliki": [p.nazwa for p in wczytane]}, ip=client_ip(request))
    db.commit()
    return _wroc(f"/wiedza/a/{art.id}#zalaczniki", "Dodano załączniki")


@router.post("/wiedza/a/{artykul_id}/zalaczniki/{zalacznik_id}/usun")
def usun_zalacznik(
    artykul_id: str,
    zalacznik_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _zapis(request, user, ctx, csrf_token)
    art = _artykul(db, ctx, artykul_id, blokuj=True)
    zal = wiedza.zalacznik(db, ctx, zalacznik_id)
    if zal is None or zal.artykul_id != art.id:
        raise HTTPException(status_code=404, detail="nie znaleziono załącznika")
    nazwa = zal.nazwa
    plik = wiedza.usun_zalacznik(db, ctx, art, zal)
    audit(db, ctx, "wiedza.zalacznik.usuniety", art.tytul, {"id": art.id, "plik": nazwa},
          ip=client_ip(request))
    db.commit()
    # Plik kasujemy dopiero po zatwierdzeniu zapisu - patrz usun_zalacznik.
    if plik is not None:
        plik.unlink(missing_ok=True)
    return _wroc(f"/wiedza/a/{art.id}#zalaczniki", f"Usunięto załącznik {nazwa}")


def _plik(db: Session, ctx: TenantContext, zalacznik_id: str):
    zal = wiedza.zalacznik(db, ctx, zalacznik_id)
    if zal is None:
        raise HTTPException(status_code=404, detail="nie znaleziono załącznika")
    sciezka_pliku = wiedza.plik_zalacznika(zal)
    if sciezka_pliku is None or not sciezka_pliku.is_file():
        raise HTTPException(status_code=404, detail="pliku nie ma już na dysku")
    return zal, sciezka_pliku


@router.get("/wiedza/zalacznik/{zalacznik_id}")
def pobierz_zalacznik(
    zalacznik_id: str,
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    zal, sciezka_pliku = _plik(db, ctx, zalacznik_id)
    # Zawsze jako plik do pobrania: HTML wgrany jako zalacznik nie moze sie
    # otworzyc w przegladarce w domenie panelu.
    return FileResponse(sciezka_pliku, filename=zal.nazwa, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/wiedza/zalacznik/{zalacznik_id}/podglad")
def podglad_zalacznika(
    zalacznik_id: str,
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Obrazek do pokazania w artykule. Typ musi sie zgadzac z poczatkiem pliku."""
    zal, sciezka_pliku = _plik(db, ctx, zalacznik_id)
    with sciezka_pliku.open("rb") as plik:
        typ = do_podgladu(zal.typ_mime, plik.read(16))
    if typ is None:
        raise HTTPException(status_code=404, detail="ten plik nie ma podglądu")
    return FileResponse(sciezka_pliku, media_type=typ,
                        headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


# --- pomocnicze dla edytora (JSON) ----------------------------------------------------------------

@router.get("/wiedza/podpowiedzi")
def podpowiedzi(
    rodzaj: str = Query(..., pattern="^(tag|system)$"),
    q: str = Query("", max_length=100),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(wiedza.podpowiedzi(db, ctx, rodzaj, q))


@router.get("/wiedza/dopasowanie")
def podglad_dopasowania(
    pole: list[str] = Query([]),
    wartosc: list[str] = Query([]),
    systemy: str = Query("", max_length=6000),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Maszyny pasujace do niezapisanych warunkow - licznik w edytorze."""
    try:
        warunki = dopasowanie.normalizuj_warunki(
            [{"pole": p, "wartosc": w} for p, w in zip(pole, wartosc)])
        nazwy = wiedza.lista_nazw(systemy, wiedza.MAKS_SYSTEM, "system")
    except ValueError as blad:
        return JSONResponse({"blad": str(blad)}, status_code=400)
    liczba, maszyny = dopasowanie.podglad(db, ctx.tenant_id, warunki, nazwy)
    return JSONResponse({
        "liczba": liczba,
        "maszyny": [{"id": a, "nazwa": h,
                     "powody": [f"{e}: {w}" for e, w in powody_opis(p)]} for a, h, p in maszyny],
    })


@router.post("/wiedza/podglad", response_class=HTMLResponse)
def podglad_tresci(
    request: Request,
    tresc_: str = Form("", alias="tresc", max_length=wiedza.MAKS_TRESC + 1000),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
) -> HTMLResponse:
    """Podglad tresci w edytorze - renderuje serwer, tym samym kodem co strona
    artykulu, wiec podglad nie rozni sie od tego, co zobaczy czytelnik."""
    verify_csrf(request, user, csrf_token)
    return HTMLResponse(str(tresc.renderuj(tresc_)))


def _linki(db: Session, ctx: TenantContext, nazwy: list[str]) -> dict[str, str]:
    nazwy = [n for n in nazwy if isinstance(n, str)][:500]
    trafione = dopasowanie.nazwy_z_artykulami(db, ctx.tenant_id, nazwy)
    return {n: "/wiedza/artykuly?system=" + urllib.parse.quote(n)
            for n in nazwy if wiedza.klucz(n) in trafione}


@router.get("/wiedza/linki")
def linki(
    nazwa: list[str] = Query([]),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Nazwy hostow/systemow -> adres listy artykulow, tylko dla nazw z artykulem.

    Kontrakt z poprzedniego systemu (get-system-links): modul z lista obiektow
    wysyla nazwy z biezacej strony i dokleja ikone tylko przy tych, ktore
    wrocily. Jedno zapytanie do bazy bez wzgledu na liczbe nazw.
    """
    return JSONResponse(_linki(db, ctx, nazwa))


@router.post("/wiedza/linki")
def linki_post(
    dane: dict = Body(...),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """To samo co GET, dla dlugich list - ``{"nazwy": [...]}`` albo ``{"systems": [...]}``."""
    nazwy = dane.get("nazwy") or dane.get("systems") or []
    if not isinstance(nazwy, list):
        raise HTTPException(status_code=400, detail="oczekiwano listy nazw")
    return JSONResponse(_linki(db, ctx, nazwy))


# --- dla karty maszyny i listy zasobow ------------------------------------------------------------

def artykuly_maszyny(db: Session, ctx: TenantContext, asset: Asset) -> list[tuple[WiedzaArtykul, list]]:
    """Artykuly dotyczace maszyny razem z powodem - zakladka "Wiedza"."""
    trafienia = {t.artykul_id: t for t in dopasowanie.trafienia(db, ctx.tenant_id, asset_ids=[asset.id])}
    if not trafienia:
        return []
    return [(a, powody_opis(trafienia[a.id].powody))
            for a in wiedza.artykuly(db, ctx, ids=list(trafienia))]

