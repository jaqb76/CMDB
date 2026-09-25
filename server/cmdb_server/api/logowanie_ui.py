"""Strony logowania poza samym formularzem: kod TOTP, reset hasla, zaproszenia.

Wszystkie konczace sie zalogowaniem ida przez ``po_hasle``/``wydaj_sesje`` -
jedno miejsce, w ktorym powstaje sesja panelu.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    LINK_RESET,
    LINK_ZAPROSZENIE,
    ZRODLO_LOKALNE,
    PortalUser,
    Tenant,
    utcnow,
)
from ..security import (
    hash_password,
    load_mfa,
    sign_mfa,
    sign_session,
    verify_password,
)
from ..services import konta_lokalne, logowanie, qr, sekrety
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import audit
from . import download
from .ui import MIN_DLUGOSC_HASLA, motyw_z_ciasteczka, templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["logowanie"])

CIASTECZKO_MFA = "cmdb_mfa"


def _strona(request: Request, szablon: str, kod: int = 200, **dane) -> HTMLResponse:
    return templates.TemplateResponse(
        request, szablon,
        {"request": request, "motyw": motyw_z_ciasteczka(request), **dane},
        status_code=kod,
    )


# --- sesja ------------------------------------------------------------------

def wydaj_sesje(request: Request, db: Session, user: PortalUser, sposob: str,
                dokad: str = "/") -> Response:
    settings = get_settings()
    user.last_login_at = utcnow()
    audit(db, None, action="login.ok", target=user.email, detail={"sposob": sposob},
          ip=client_ip(request), actor=user.email)
    db.commit()
    response = RedirectResponse(dokad, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie,
        sign_session({"uid": user.id, "sv": user.session_version}),
        max_age=settings.session_max_age,
        httponly=True,
        secure=settings.require_https,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(CIASTECZKO_MFA, path="/")
    return response


def po_hasle(request: Request, db: Session, user: PortalUser, sposob: str) -> Response:
    """Haslo sprawdzone. Sesja od razu albo najpierw kod z aplikacji."""
    if not konta_lokalne.potrzebny_drugi_krok(db, user):
        return wydaj_sesje(request, db, user, sposob)
    db.commit()
    response = RedirectResponse("/login/kod", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        CIASTECZKO_MFA, sign_mfa(user.id, user.session_version, not user.totp_wlaczone),
        max_age=300, httponly=True, secure=get_settings().require_https, samesite="lax",
        path="/",
    )
    return response


def _konto_z_kroku(request: Request, db: Session) -> tuple[PortalUser, dict] | None:
    dane = load_mfa(request.cookies.get(CIASTECZKO_MFA, ""))
    if not dane:
        return None
    user = db.get(PortalUser, dane["uid"])
    if user is None or not user.is_active or user.session_version != dane.get("sv"):
        return None
    return user, dane


# --- drugi krok: kod TOTP ---------------------------------------------------

def _dane_konfiguracji(user: PortalUser) -> dict:
    sekret = konta_lokalne.sekret_konta(user)
    adres = konta_lokalne.adres_otpauth(sekret, user.email)
    return {"sekret": sekret, "kod_qr": Markup(qr.svg(adres))}


@router.get("/login/kod", response_class=HTMLResponse)
def formularz_kodu(request: Request, db: Session = Depends(get_db)) -> Response:
    krok = _konto_z_kroku(request, db)
    if krok is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    user, dane = krok
    konfiguracja = None
    if not user.totp_wlaczone:
        # Konto musi miec TOTP, a jeszcze go nie ma: ustawia je teraz.
        if not user.totp_szyfr:
            user.totp_szyfr = sekrety.zaszyfruj(konta_lokalne.nowy_sekret())
            db.commit()
        konfiguracja = _dane_konfiguracji(user)
    return _strona(request, "login_kod.html", email=user.email, konfiguracja=konfiguracja,
                   error=None)


@router.post("/login/kod")
def sprawdz_kod(request: Request, kod: str = Form(""), db: Session = Depends(get_db)) -> Response:
    krok = _konto_z_kroku(request, db)
    if krok is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    user, _ = krok
    settings = get_settings()
    ip = client_ip(request)
    klucz = "mfa:" + user.email
    if logowanie.zablokowane_do(db, klucz, ip) is not None:
        return _strona(request, "login_kod.html", 429, email=user.email, konfiguracja=None,
                       error="Zbyt wiele błędnych kodów. Logowanie jest czasowo zablokowane.")

    if user.totp_wlaczone:
        dobry = konta_lokalne.zweryfikuj_i_zapisz(user, kod)
    else:
        sekret = konta_lokalne.sekret_konta(user)
        krok_czasu = konta_lokalne.sprawdz_kod(sekret, kod) if sekret else None
        dobry = krok_czasu is not None
        if dobry:
            user.totp_wlaczone = True
            user.totp_ostatni_krok = krok_czasu
            audit(db, None, action="mfa.wlaczone", target=user.email, ip=ip, actor=user.email)

    if not dobry:
        audit(db, None, action="mfa.zly_kod", target=user.email, ip=ip, actor=user.email)
        db.commit()
        logowanie.odnotuj_niepowodzenie(db, klucz, ip, settings.login_max_failures + 2,
                                        settings.login_lockout_hours)
        konfiguracja = None if user.totp_wlaczone else _dane_konfiguracji(user)
        return _strona(request, "login_kod.html", 401, email=user.email,
                       konfiguracja=konfiguracja,
                       error="Kod się nie zgadza. Sprawdź, czy zegar telefonu jest dokładny.")
    logowanie.wyczysc(db, klucz, ip)
    return wydaj_sesje(request, db, user, "lokalne+totp")


# --- zapomniane haslo -------------------------------------------------------

@router.get("/haslo/zapomniane", response_class=HTMLResponse)
def formularz_resetu(request: Request) -> Response:
    return _strona(request, "haslo_zapomniane.html", wyslane=False)


@router.post("/haslo/zapomniane")
def zamow_reset(request: Request, email: str = Form(...), db: Session = Depends(get_db)) -> Response:
    """Wysyla link, jesli konto istnieje i ma skad go wyslac.

    Odpowiedz jest zawsze ta sama - inaczej formularz sluzylby do sprawdzania,
    kto ma konto. Konto z AD zmienia haslo w AD, nie tutaj.
    """
    adres = email.strip().lower()[:255]
    ip = client_ip(request)
    user = konta_lokalne.konto_do_resetu(db, adres)
    # Najwyzej trzy linki na godzine na adres - formularz nie moze sluzyc
    # do zasypywania czyjejs skrzynki.
    if user is not None and konta_lokalne.linkow_w_ostatniej_godzinie(db, adres) < 3:
        token = konta_lokalne.utworz_link(db, LINK_RESET, user.email, user=user,
                                          utworzyl=user.email)
        db.flush()
        link = konta_lokalne.adres_linku(download.adres_publiczny(request), LINK_RESET, token)
        wyslane = konta_lokalne.wyslij_link(db, user.tenant_id, user.email, LINK_RESET, link)
        audit(db, None, action="haslo.reset_zamowiony", target=user.email,
              detail={"wyslane": wyslane}, ip=ip, actor=user.email)
        db.commit()
    return _strona(request, "haslo_zapomniane.html", wyslane=True)


def _ustaw_haslo_formularz(request: Request, link, token: str, error: str | None = None,
                           kod: int = 200) -> Response:
    return _strona(request, "haslo_nowe.html", kod, link=link, token=token, error=error,
                   min_dlugosc_hasla=MIN_DLUGOSC_HASLA,
                   zaproszenie=link is not None and link.rodzaj == LINK_ZAPROSZENIE)


def _sprawdz_nowe(nowe: str, powtorzone: str) -> str | None:
    if nowe != powtorzone:
        return "Powtórzone hasło różni się od nowego."
    if len(nowe) < MIN_DLUGOSC_HASLA:
        return f"Hasło musi mieć co najmniej {MIN_DLUGOSC_HASLA} znaków."
    return None


@router.get("/haslo/nowe/{token}", response_class=HTMLResponse)
def formularz_nowego_hasla(token: str, request: Request, db: Session = Depends(get_db)):
    link = konta_lokalne.znajdz_link(db, token, LINK_RESET)
    return _ustaw_haslo_formularz(request, link, token, kod=200 if link else 404)


@router.post("/haslo/nowe/{token}")
def ustaw_nowe_haslo(token: str, request: Request, nowe: str = Form(...),
                     powtorzone: str = Form(...), db: Session = Depends(get_db)) -> Response:
    link = konta_lokalne.znajdz_link(db, token, LINK_RESET)
    if link is None:
        return _ustaw_haslo_formularz(request, None, token, kod=404)
    blad = _sprawdz_nowe(nowe, powtorzone)
    if blad:
        return _ustaw_haslo_formularz(request, link, token, blad, 400)
    user = db.get(PortalUser, link.user_id) if link.user_id else None
    if user is None or not user.is_active or user.zrodlo != ZRODLO_LOKALNE:
        return _ustaw_haslo_formularz(request, None, token, kod=404)
    user.password_hash = hash_password(nowe)
    # Nowe haslo wylogowuje wszedzie - takze tego, kto moze znac stare.
    user.session_version = PortalUser.session_version + 1
    link.uzyto = utcnow()
    logowanie.wyczysc(db, user.email, client_ip(request))
    audit(db, None, action="haslo.ustawione_z_linku", target=user.email,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/login?haslo=zmienione", status_code=status.HTTP_303_SEE_OTHER)


# --- zaproszenia ------------------------------------------------------------

@router.get("/zaproszenie/{token}", response_class=HTMLResponse)
def formularz_zaproszenia(token: str, request: Request, db: Session = Depends(get_db)):
    link = konta_lokalne.znajdz_link(db, token, LINK_ZAPROSZENIE)
    return _ustaw_haslo_formularz(request, link, token, kod=200 if link else 404)


def przyjmij_zaproszenie(db: Session, link, password_hash: str) -> PortalUser:
    """Zaklada konto opisane zaproszeniem. ValueError, gdy adres jest juz zajety."""
    if db.execute(select(PortalUser).where(PortalUser.email == link.email)).scalar_one_or_none():
        raise ValueError("Konto o tym adresie już istnieje. Zaloguj się albo użyj "
                         "„Nie pamiętasz hasła?”.")
    konto = PortalUser(
        email=link.email, full_name=link.full_name, password_hash=password_hash,
        role=link.rola or "viewer", tenant_id=link.tenant_id if link.zakres == "firma" else None,
        is_superadmin=link.zakres == "superadmin", is_global_viewer=link.zakres == "audytor",
        zrodlo=ZRODLO_LOKALNE,
    )
    if konto.is_global_viewer:
        konto.role = "viewer"
    db.add(konto)
    db.flush()
    link.uzyto = utcnow()
    return konto


@router.post("/zaproszenie/{token}")
def przyjmij(token: str, request: Request, nowe: str = Form(...), powtorzone: str = Form(...),
             db: Session = Depends(get_db)) -> Response:
    link = konta_lokalne.znajdz_link(db, token, LINK_ZAPROSZENIE)
    if link is None:
        return _ustaw_haslo_formularz(request, None, token, kod=404)
    blad = _sprawdz_nowe(nowe, powtorzone)
    if blad:
        return _ustaw_haslo_formularz(request, link, token, blad, 400)
    try:
        konto = przyjmij_zaproszenie(db, link, hash_password(nowe))
    except ValueError as b:
        return _ustaw_haslo_formularz(request, link, token, str(b), 400)
    audit(db, None, action="zaproszenie.przyjete", target=konto.email,
          detail={"zakres": link.zakres, "rola": konto.role, "zaprosil": link.utworzyl},
          ip=client_ip(request), actor=konto.email)
    db.commit()
    return po_hasle(request, db, konto, "zaproszenie")


# --- wlasne konto: weryfikacja dwuetapowa -----------------------------------

def _wroc_konto(komunikat: str = "", blad: str = "") -> RedirectResponse:
    parametr = f"?mfa={quote(komunikat)}" if komunikat else (f"?blad={quote(blad)}" if blad else "")
    return RedirectResponse("/konto" + parametr, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/konto/mfa/nowy")
def rozpocznij_mfa(request: Request, csrf_token: str = Form(""),
                   user: PortalUser = Depends(require_user), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    if user.zrodlo != ZRODLO_LOKALNE:
        raise HTTPException(status_code=400, detail="weryfikacja dwuetapowa dotyczy kont lokalnych")
    if user.totp_wlaczone:
        return _wroc_konto(blad="Weryfikacja dwuetapowa jest już włączona.")
    user.totp_szyfr = sekrety.zaszyfruj(konta_lokalne.nowy_sekret())
    db.commit()
    return RedirectResponse("/konto?mfa_konfiguracja=1#mfa", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/konto/mfa/potwierdz")
def potwierdz_mfa(request: Request, kod: str = Form(""), csrf_token: str = Form(""),
                  user: PortalUser = Depends(require_user), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    sekret = konta_lokalne.sekret_konta(user)
    krok = konta_lokalne.sprawdz_kod(sekret, kod) if sekret and not user.totp_wlaczone else None
    if krok is None:
        return RedirectResponse(
            "/konto?mfa_konfiguracja=1&blad=" + quote("Kod się nie zgadza - spróbuj ponownie."),
            status_code=status.HTTP_303_SEE_OTHER)
    user.totp_wlaczone = True
    user.totp_ostatni_krok = krok
    audit(db, None, action="mfa.wlaczone", target=user.email, ip=client_ip(request),
          actor=user.email)
    db.commit()
    return _wroc_konto("Weryfikacja dwuetapowa włączona. Przy logowaniu podasz kod z aplikacji.")


@router.post("/konto/mfa/wylacz")
def wylacz_mfa(request: Request, haslo: str = Form(""), kod: str = Form(""),
               csrf_token: str = Form(""), user: PortalUser = Depends(require_user),
               db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    if konta_lokalne.mfa_wymagane(db, user):
        return _wroc_konto(blad="Twoja firma wymaga weryfikacji dwuetapowej - nie można jej wyłączyć.")
    if not verify_password(haslo, user.password_hash) or not konta_lokalne.zweryfikuj_i_zapisz(user, kod):
        db.commit()
        return _wroc_konto(blad="Hasło albo kod się nie zgadza.")
    user.totp_wlaczone = False
    user.totp_szyfr = None
    user.totp_ostatni_krok = None
    audit(db, None, action="mfa.wylaczone", target=user.email, ip=client_ip(request),
          actor=user.email)
    db.commit()
    return _wroc_konto("Weryfikacja dwuetapowa wyłączona.")


def dane_mfa_konta(db: Session, user: PortalUser, konfiguracja: bool) -> dict:
    """Do szablonu konto.html."""
    dane = {"lokalne": user.zrodlo == ZRODLO_LOKALNE, "wlaczone": user.totp_wlaczone,
            "wymagane": konta_lokalne.mfa_wymagane(db, user), "konfiguracja": None}
    if konfiguracja and not user.totp_wlaczone and user.totp_szyfr:
        dane["konfiguracja"] = _dane_konfiguracji(user)
    return dane
