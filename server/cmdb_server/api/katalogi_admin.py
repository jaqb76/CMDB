"""Panel superadmina: katalogi AD/LDAP i mapowanie grup na role.

Katalog firmy daje role wylacznie w tej firmie. Katalog operatora (bez firmy)
jest jedynym, ktory moze dac superadmina, audytora i technika helpdesku -
patrz services/tozsamosc. Te reguly sa sprawdzane tu, przy zapisie mapowania,
i drugi raz przy liczeniu uprawnien.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    ROLE_KATALOGU_FIRMY,
    ROLE_KATALOGU_OPERATORA,
    KatalogTozsamosci,
    MapowanieGrupy,
    PortalUser,
    Tenant,
)
from ..services import katalog as ldap
from ..services import sekrety, tozsamosc
from ..services.auth import client_ip, require_superadmin
from ..services.katalog import BladKatalogu, normalizuj_domene
from ..services.scoping import audit
from .admin import render_admin, sprawdz_csrf

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/katalogi", tags=["admin"])

NAZWY_ROL = {
    "admin": "Administrator firmy",
    "viewer": "Tylko odczyt",
    "superadmin": "Superadmin",
    "audytor": "Audytor wszystkich firm",
    "helpdesk": "Technik helpdesku",
}


def _katalog(db: Session, katalog_id: str) -> KatalogTozsamosci:
    kat = db.get(KatalogTozsamosci, katalog_id)
    if kat is None:
        raise HTTPException(status_code=404, detail="nie znaleziono katalogu")
    return kat


def _wroc(adres: str, komunikat: str = "", blad: str = "") -> RedirectResponse:
    parametry = []
    if komunikat:
        parametry.append("komunikat=" + quote(komunikat))
    if blad:
        parametry.append("blad=" + quote(blad))
    return RedirectResponse(adres + ("?" + "&".join(parametry) if parametry else ""),
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def lista(request: Request, komunikat: str = "", blad: str = "",
          user: PortalUser = Depends(require_superadmin), db: Session = Depends(get_db)):
    katalogi = db.execute(select(KatalogTozsamosci)).scalars().all()
    katalogi.sort(key=lambda k: (not k.operatora, k.tenant.name.lower() if k.tenant else ""))
    konta = dict(db.execute(
        select(PortalUser.katalog_id, func.count(PortalUser.id))
        .where(PortalUser.katalog_id.is_not(None), PortalUser.is_active.is_(True))
        .group_by(PortalUser.katalog_id)
    ).all())
    zajete = {k.tenant_id for k in katalogi}
    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    return render_admin(
        request, "admin_katalogi.html", user, "katalogi",
        katalogi=katalogi, konta=konta, komunikat=komunikat, blad=blad,
        firmy_bez_katalogu=[f for f in firmy if f.id not in zajete],
        jest_operatora=None in zajete,
        filtr_domyslny=ldap.FILTR_DOMYSLNY,
    )


def _zapisz_pola(db: Session, kat: KatalogTozsamosci, *, nazwa: str, serwery: str,
                 starttls: bool, certyfikat_ca: str, base_dn: str, bind_dn: str,
                 bind_haslo: str, filtr: str, domeny: str, bez_grupy: str) -> None:
    ldap.sprawdz_adresy(serwery, starttls)
    lista_domen = []
    for wpis in domeny.replace("\n", ",").split(","):
        wpis = normalizuj_domene(wpis)
        if wpis and wpis not in lista_domen:
            lista_domen.append(wpis)
    if not lista_domen:
        raise ValueError("podaj co najmniej jedną domenę loginu, np. abc.pl albo ABC\\")
    zajete = tozsamosc.domeny_zajete(db, pomijajac=kat.id)
    for d in lista_domen:
        if d in zajete:
            raise ValueError(f"domena {d} należy już do katalogu {zajete[d].nazwa}")
    if not base_dn.strip() or not bind_dn.strip():
        raise ValueError("podaj bazę wyszukiwania i konto serwisowe")
    if filtr.strip() and "{login}" not in filtr and "{upn}" not in filtr:
        raise ValueError("filtr musi zawierać {login} albo {upn}")
    if bez_grupy not in ("odmowa", "viewer"):
        raise ValueError("nieznane ustawienie dla osób spoza grup")
    if bez_grupy == "viewer" and kat.operatora:
        raise ValueError("katalog operatora nie może wpuszczać osób spoza grup")
    if not kat.bind_haslo_szyfr and not bind_haslo:
        raise ValueError("podaj hasło konta serwisowego")

    kat.nazwa = nazwa.strip()[:200] or kat.nazwa
    kat.serwery = serwery.strip()
    kat.starttls = starttls
    kat.certyfikat_ca = certyfikat_ca.strip() or None
    kat.base_dn = base_dn.strip()
    kat.bind_dn = bind_dn.strip()
    if bind_haslo:
        kat.bind_haslo_szyfr = sekrety.zaszyfruj(bind_haslo)
    kat.filtr_uzytkownika = filtr.strip() or None
    kat.domeny = lista_domen
    kat.bez_grupy = bez_grupy


@router.post("")
def utworz(
    request: Request,
    tenant_id: str = Form(""),
    nazwa: str = Form(""),
    serwery: str = Form(...),
    starttls: bool = Form(False),
    certyfikat_ca: str = Form(""),
    base_dn: str = Form(...),
    bind_dn: str = Form(...),
    bind_haslo: str = Form(""),
    filtr: str = Form(""),
    domeny: str = Form(...),
    bez_grupy: str = Form("odmowa"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = db.get(Tenant, tenant_id) if tenant_id else None
    if tenant_id and firma is None:
        raise HTTPException(status_code=400, detail="nie ma takiej firmy")
    istnieje = db.execute(select(KatalogTozsamosci).where(
        KatalogTozsamosci.tenant_id.is_(None) if firma is None
        else KatalogTozsamosci.tenant_id == firma.id
    )).scalar_one_or_none()
    if istnieje is not None:
        return _wroc("/admin/katalogi", blad="ta firma ma już katalog - edytuj istniejący")
    kat = KatalogTozsamosci(tenant_id=firma.id if firma else None,
                            nazwa=nazwa.strip() or (firma.name if firma else "Katalog operatora"),
                            serwery="", base_dn="", bind_dn="", domeny=[])
    try:
        _zapisz_pola(db, kat, nazwa=kat.nazwa, serwery=serwery, starttls=starttls,
                     certyfikat_ca=certyfikat_ca, base_dn=base_dn, bind_dn=bind_dn,
                     bind_haslo=bind_haslo, filtr=filtr, domeny=domeny, bez_grupy=bez_grupy)
    except ValueError as blad:
        return _wroc("/admin/katalogi", blad=str(blad))
    db.add(kat)
    db.flush()
    audit(db, None, action="katalog.utworzony", target=kat.nazwa,
          detail={"firma": firma.slug if firma else None, "domeny": kat.domeny},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(f"/admin/katalogi/{kat.id}",
                 "Katalog zapisany. Sprawdź połączenie i dodaj grupy, które dają dostęp.")


@router.get("/{katalog_id}", response_class=HTMLResponse)
def szczegoly(
    katalog_id: str,
    request: Request,
    komunikat: str = "",
    blad: str = "",
    szukaj: str = Query("", max_length=100),
    sprawdz: str = Query("", max_length=320),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
):
    kat = _katalog(db, katalog_id)
    grupy_znalezione = None
    blad_szukania = ""
    if szukaj.strip():
        try:
            grupy_znalezione = ldap.klient(kat).szukaj_grup(szukaj)
        except BladKatalogu as b:
            blad_szukania = str(b)

    wynik_sprawdzenia = None
    if sprawdz.strip():
        wynik_sprawdzenia = _sprawdz_uzytkownika(db, kat, sprawdz)

    konta = db.execute(
        select(PortalUser).where(PortalUser.katalog_id == kat.id).order_by(PortalUser.email)
    ).scalars().all()
    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    role = ROLE_KATALOGU_OPERATORA if kat.operatora else ROLE_KATALOGU_FIRMY
    return render_admin(
        request, "admin_katalog.html", user, "katalogi",
        kat=kat, konta=konta, firmy=firmy, komunikat=komunikat, blad=blad,
        role=[(r, NAZWY_ROL[r]) for r in role], nazwy_rol=NAZWY_ROL,
        szukaj=szukaj, grupy_znalezione=grupy_znalezione, blad_szukania=blad_szukania,
        sprawdz=sprawdz, wynik_sprawdzenia=wynik_sprawdzenia,
        filtr_domyslny=ldap.FILTR_DOMYSLNY,
        nazwy_firm={f.id: f.name for f in firmy},
    )


def _sprawdz_uzytkownika(db: Session, kat: KatalogTozsamosci, login: str) -> dict:
    """Co katalog wie o osobie i jaka role dostanie - bez logowania jej."""
    try:
        klient = ldap.klient(kat)
        wpis = klient.znajdz(login)
    except BladKatalogu as b:
        return {"blad": str(b)}
    if wpis is None:
        return {"brak": True, "login": login}
    nazwy: dict = {}
    sidy = [g.identyfikator for g in wpis.grupy if g.identyfikator.upper().startswith("S-1-")]
    if sidy and hasattr(klient, "nazwy_grup"):
        try:
            nazwy = klient.nazwy_grup(sidy)
        except BladKatalogu:
            nazwy = {}
    mapowane = {ldap.normalizuj_identyfikator(m.identyfikator): m for m in kat.mapowania}
    grupy = []
    widziane = set()
    for g in wpis.grupy:
        nazwa = nazwy.get(g.identyfikator, (g.nazwa, g.dn))[0]
        klucz = nazwa.lower()
        m = (mapowane.get(ldap.normalizuj_identyfikator(g.identyfikator))
             or (mapowane.get(ldap.normalizuj_identyfikator(g.dn)) if g.dn else None))
        if klucz in widziane and m is None:
            continue
        widziane.add(klucz)
        grupy.append({"nazwa": nazwa, "rola": NAZWY_ROL.get(m.rola) if m else None})
    grupy.sort(key=lambda g: (g["rola"] is None, g["nazwa"].lower()))
    upr = tozsamosc.ustal_uprawnienia(kat, wpis)
    if upr is None:
        wynik = "brak dostępu" if wpis.aktywne else "brak dostępu - konto wyłączone w AD"
    elif upr.superadmin:
        wynik = "Superadmin"
    elif upr.audytor:
        wynik = "Audytor wszystkich firm"
    elif upr.firmy_helpdesku:
        wynik = "Technik helpdesku: " + ", ".join(
            sorted(t.name for t in db.execute(
                select(Tenant).where(Tenant.id.in_(upr.firmy_helpdesku))).scalars()))
    else:
        wynik = NAZWY_ROL[upr.rola]
    return {"wpis": wpis, "grupy": grupy, "wynik": wynik, "dostep": upr is not None}


@router.post("/{katalog_id}")
def zapisz(
    katalog_id: str,
    request: Request,
    nazwa: str = Form(""),
    serwery: str = Form(...),
    starttls: bool = Form(False),
    certyfikat_ca: str = Form(""),
    base_dn: str = Form(...),
    bind_dn: str = Form(...),
    bind_haslo: str = Form(""),
    filtr: str = Form(""),
    domeny: str = Form(...),
    bez_grupy: str = Form("odmowa"),
    aktywny: bool = Form(False),
    logowanie_lokalne: bool = Form(False),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    try:
        _zapisz_pola(db, kat, nazwa=nazwa, serwery=serwery, starttls=starttls,
                     certyfikat_ca=certyfikat_ca, base_dn=base_dn, bind_dn=bind_dn,
                     bind_haslo=bind_haslo, filtr=filtr, domeny=domeny, bez_grupy=bez_grupy)
    except ValueError as blad:
        db.rollback()
        return _wroc(f"/admin/katalogi/{kat.id}", blad=str(blad))
    kat.aktywny = aktywny
    if kat.tenant is not None:
        kat.tenant.logowanie_lokalne = logowanie_lokalne
    audit(db, None, action="katalog.zmieniony", target=kat.nazwa,
          detail={"domeny": kat.domeny, "aktywny": kat.aktywny,
                  "logowanie_lokalne": kat.tenant.logowanie_lokalne if kat.tenant else None},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(f"/admin/katalogi/{kat.id}", "Zapisano ustawienia katalogu.")


@router.post("/{katalog_id}/sprawdz")
def sprawdz_polaczenie(katalog_id: str, csrf_token: str = Form(""),
                       user: PortalUser = Depends(require_superadmin),
                       db: Session = Depends(get_db)) -> Response:
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    try:
        return _wroc(f"/admin/katalogi/{kat.id}", ldap.klient(kat).sprawdz_polaczenie())
    except (BladKatalogu, ValueError) as blad:
        return _wroc(f"/admin/katalogi/{kat.id}", blad=f"Połączenie nieudane: {blad}")


@router.post("/{katalog_id}/synchronizuj")
def synchronizuj(katalog_id: str, request: Request, csrf_token: str = Form(""),
                 user: PortalUser = Depends(require_superadmin),
                 db: Session = Depends(get_db)) -> Response:
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    wynik = tozsamosc.synchronizuj(db, kat)
    if wynik["blad"]:
        return _wroc(f"/admin/katalogi/{kat.id}",
                     blad=f"Synchronizacja nieudana, konta bez zmian: {wynik['blad']}")
    audit(db, None, action="katalog.synchronizacja", target=kat.nazwa, detail=wynik,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(f"/admin/katalogi/{kat.id}",
                 f"Sprawdzono {wynik['sprawdzone']} kont: odebrano dostęp {wynik['odebrane']}, "
                 f"zmieniono uprawnienia {wynik['zmienione']}.")


@router.post("/{katalog_id}/usun")
def usun(katalog_id: str, request: Request, csrf_token: str = Form(""),
         user: PortalUser = Depends(require_superadmin), db: Session = Depends(get_db)) -> Response:
    """Usuwa katalog. Jego konta zostaja, ale sa wylaczone - nie maja hasla CMDB."""
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    for konto in db.execute(select(PortalUser).where(PortalUser.katalog_id == kat.id)).scalars():
        if konto.id == user.id:
            return _wroc(f"/admin/katalogi/{kat.id}",
                         blad="Nie usuniesz katalogu, z którego jesteś zalogowany.")
        konto.is_active = False
        konto.katalog_id = None
        konto.session_version = PortalUser.session_version + 1
    nazwa = kat.nazwa
    db.delete(kat)
    audit(db, None, action="katalog.usuniety", target=nazwa, ip=client_ip(request),
          actor=user.email)
    db.commit()
    return _wroc("/admin/katalogi", f"Katalog {nazwa} usunięty. Jego konta są wyłączone.")


@router.post("/{katalog_id}/mapowania")
def dodaj_mapowanie(
    katalog_id: str,
    request: Request,
    identyfikator: str = Form(...),
    nazwa: str = Form(""),
    dn: str = Form(""),
    rola: str = Form(...),
    tenant_id: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    identyfikator = identyfikator.strip()
    dozwolone = ROLE_KATALOGU_OPERATORA if kat.operatora else ROLE_KATALOGU_FIRMY
    if rola not in dozwolone:
        # Granica firmy: katalog klienta nie moze dac uprawnien ponad firma.
        return _wroc(f"/admin/katalogi/{kat.id}",
                     blad="Ta rola nie jest dostępna w tym katalogu. Superadmina, audytora "
                          "i techników nadaje tylko katalog operatora.")
    if not identyfikator:
        return _wroc(f"/admin/katalogi/{kat.id}", blad="Wskaż grupę.")
    firma = None
    if rola == "helpdesk":
        firma = db.get(Tenant, tenant_id) if tenant_id else None
        if firma is None:
            return _wroc(f"/admin/katalogi/{kat.id}", blad="Wskaż firmę, którą obsługuje technik.")
    warunek_firmy = (MapowanieGrupy.tenant_id == firma.id if firma
                     else MapowanieGrupy.tenant_id.is_(None))
    istnieje = db.execute(select(MapowanieGrupy).where(
        MapowanieGrupy.katalog_id == kat.id, MapowanieGrupy.identyfikator == identyfikator,
        MapowanieGrupy.rola == rola, warunek_firmy,
    )).scalar_one_or_none()
    if istnieje is not None:
        return _wroc(f"/admin/katalogi/{kat.id}", blad="Ta grupa ma już tę rolę.")
    m = MapowanieGrupy(katalog_id=kat.id, identyfikator=identyfikator,
                       nazwa=(nazwa.strip() or ldap.cn_z_dn(dn) or identyfikator)[:300],
                       dn=dn.strip() or None, rola=rola, tenant_id=firma.id if firma else None)
    db.add(m)
    audit(db, None, action="katalog.mapowanie_dodane", target=kat.nazwa,
          detail={"grupa": m.nazwa, "identyfikator": identyfikator, "rola": rola,
                  "firma": firma.slug if firma else None},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(f"/admin/katalogi/{kat.id}",
                 f"Grupa {m.nazwa} daje rolę: {NAZWY_ROL[rola]}. "
                 "Obowiązuje od następnego logowania lub synchronizacji.")


@router.post("/{katalog_id}/mapowania/{mapowanie_id}/usun")
def usun_mapowanie(katalog_id: str, mapowanie_id: str, request: Request,
                   csrf_token: str = Form(""),
                   user: PortalUser = Depends(require_superadmin),
                   db: Session = Depends(get_db)) -> Response:
    sprawdz_csrf(user, csrf_token)
    kat = _katalog(db, katalog_id)
    m = db.get(MapowanieGrupy, mapowanie_id)
    if m is None or m.katalog_id != kat.id:
        raise HTTPException(status_code=404, detail="nie znaleziono mapowania")
    nazwa = m.nazwa
    db.delete(m)
    audit(db, None, action="katalog.mapowanie_usuniete", target=kat.nazwa,
          detail={"grupa": nazwa, "rola": m.rola}, ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(f"/admin/katalogi/{kat.id}",
                 f"Usunięto grupę {nazwa}. Osoby, którym dawała dostęp, stracą go przy "
                 "najbliższej synchronizacji.")
