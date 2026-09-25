"""Strony logowania poza samym formularzem: kod TOTP, reset hasla, zaproszenia.

Wszystkie konczace sie zalogowaniem ida przez ``po_hasle``/``wydaj_sesje`` -
jedno miejsce, w ktorym powstaje sesja panelu.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    HASLO_NIEUZYWANE,
    LINK_RESET,
    LINK_ZAPROSZENIE,
    ZRODLO_LOKALNE,
    PortalUser,
    Tenant,
    TozsamoscZewnetrzna,
    utcnow,
)
from ..security import (
    hash_password,
    load_mfa,
    load_oauth,
    sign_mfa,
    sign_oauth,
    sign_session,
    verify_password,
)
from ..services import konta_lokalne, logowanie, qr, sekrety, zewnetrzne
from ..services.auth import client_ip, current_user, require_user, verify_csrf
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
                   min_dlugosc_hasla=MIN_DLUGOSC_HASLA, zewnetrzni=zewnetrzne.wlaczeni(),
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


# --- konta zewnetrzne (Google, Microsoft, GitHub) ---------------------------

CIASTECZKO_OAUTH = "cmdb_oauth"


def _adres_zwrotny(request: Request) -> str:
    return download.adres_publiczny(request) + "/login/zewn/callback"


def _blad_zewn(request: Request, komunikat: str, kod: int = 400) -> Response:
    odp = _strona(request, "login_zewn_blad.html", kod, komunikat=komunikat)
    odp.delete_cookie(CIASTECZKO_OAUTH, path="/")
    return odp


@router.get("/login/zewn/callback")
def powrot_od_dostawcy(request: Request, db: Session = Depends(get_db)) -> Response:
    stan = load_oauth(request.cookies.get(CIASTECZKO_OAUTH, ""))
    parametry = request.query_params
    if not stan or not parametry.get("state") or parametry.get("state") != stan.get("state"):
        return _blad_zewn(request, "Logowanie wygasło albo zostało otwarte w innej karcie. "
                                   "Zacznij od nowa.")
    if parametry.get("error"):
        return _blad_zewn(request, "Logowanie zostało przerwane u dostawcy.")
    try:
        tozs = zewnetrzne.tozsamosc_z_kodu(stan, parametry.get("code", ""), _adres_zwrotny(request))
    except zewnetrzne.BladZewnetrzny as blad:
        log.warning("logowanie %s nieudane: %s", stan.get("dostawca"), blad)
        return _blad_zewn(request, f"Nie udało się potwierdzić konta: {blad}")

    nazwa = zewnetrzne.DOSTAWCY[tozs.dostawca].nazwa
    opis = f"{nazwa} ({tozs.email})" if tozs.email else nazwa
    ip = client_ip(request)
    powiazanie = db.execute(select(TozsamoscZewnetrzna).where(
        TozsamoscZewnetrzna.dostawca == tozs.dostawca, TozsamoscZewnetrzna.sub == tozs.sub
    )).scalar_one_or_none()
    cel = stan.get("cel")

    if cel == "zaproszenie":
        link = konta_lokalne.znajdz_link(db, stan.get("token", ""), LINK_ZAPROSZENIE)
        if link is None:
            return _blad_zewn(request, "Zaproszenie jest nieważne albo zostało już użyte.")
        if powiazanie is not None:
            return _blad_zewn(request, f"Konto {opis} jest już powiązane z innym kontem CMDB.")
        try:
            konto = przyjmij_zaproszenie(db, link, HASLO_NIEUZYWANE)
        except ValueError as b:
            return _blad_zewn(request, str(b))
        db.add(TozsamoscZewnetrzna(user_id=konto.id, dostawca=tozs.dostawca, sub=tozs.sub,
                                   email=tozs.email, ostatnio_uzyta=utcnow()))
        if tozs.nazwa and not konto.full_name:
            konto.full_name = tozs.nazwa[:200]
        audit(db, None, action="zaproszenie.przyjete", target=konto.email,
              detail={"zakres": link.zakres, "rola": konto.role, "zaprosil": link.utworzyl,
                      "konto_zewnetrzne": opis}, ip=ip, actor=konto.email)
        return wydaj_sesje(request, db, konto, tozs.dostawca)

    if cel == "powiaz":
        user = current_user(request, db)
        if user is None or user.id != stan.get("uid"):
            return _blad_zewn(request, "Zaloguj się ponownie i powtórz dodawanie konta.")
        if powiazanie is not None:
            if powiazanie.user_id == user.id:
                return RedirectResponse("/konto?info=" + quote(f"Konto {opis} było już powiązane."),
                                        status_code=status.HTTP_303_SEE_OTHER)
            return _blad_zewn(request, f"Konto {opis} jest już powiązane z innym kontem CMDB.")
        db.add(TozsamoscZewnetrzna(user_id=user.id, dostawca=tozs.dostawca, sub=tozs.sub,
                                   email=tozs.email))
        audit(db, None, action="zewnetrzne.powiazane", target=user.email,
              detail={"konto": opis}, ip=ip, actor=user.email)
        db.commit()
        odp = RedirectResponse("/konto?info=" + quote(f"Możesz logować się kontem {opis}."),
                               status_code=status.HTTP_303_SEE_OTHER)
        odp.delete_cookie(CIASTECZKO_OAUTH, path="/")
        return odp

    # cel == "login" (i "mobile" - patrz api/mobile)
    konto = db.get(PortalUser, powiazanie.user_id) if powiazanie else None
    if konto is None:
        audit(db, None, action="login.zewn.nieznane", target=tozs.email, ip=ip,
              detail={"dostawca": tozs.dostawca}, actor=tozs.email or tozs.dostawca)
        db.commit()
        return _blad_zewn(request, f"Konto {opis} nie ma dostępu do CMDB. Poproś administratora "
                                   "swojej firmy o zaproszenie.", 403)
    if not konto.is_active:
        return _blad_zewn(request, "To konto CMDB jest wyłączone.", 403)
    if konto.tenant_id:
        firma = db.get(Tenant, konto.tenant_id)
        if firma is not None and not firma.logowanie_lokalne:
            return _blad_zewn(request, "Twoja firma loguje się wyłącznie kontem domenowym.", 403)
    powiazanie.ostatnio_uzyta = utcnow()
    if cel == "mobile":
        from .mobile import przekaz_do_aplikacji

        return przekaz_do_aplikacji(request, db, konto, tozs.dostawca, stan.get("wyzwanie"))
    return wydaj_sesje(request, db, konto, tozs.dostawca)


@router.get("/login/zewn/{klucz}")
def do_dostawcy(klucz: str, request: Request, zaproszenie: str = "", cel: str = "login",
                wyzwanie: str = "", db: Session = Depends(get_db)) -> Response:
    try:
        zewnetrzne.dostawca(klucz)
    except zewnetrzne.BladZewnetrzny as blad:
        return _blad_zewn(request, str(blad), 404)
    dodatkowe: dict = {}
    podpowiedz = None
    if zaproszenie:
        link = konta_lokalne.znajdz_link(db, zaproszenie, LINK_ZAPROSZENIE)
        if link is None:
            return _blad_zewn(request, "Zaproszenie jest nieważne albo zostało już użyte.", 404)
        cel, dodatkowe, podpowiedz = "zaproszenie", {"token": zaproszenie}, link.email
    elif cel == "powiaz":
        user = current_user(request, db)
        if user is None:
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        if user.zrodlo != ZRODLO_LOKALNE:
            return _blad_zewn(request, "Konto z AD loguje się kontem domenowym.")
        dodatkowe = {"uid": user.id}
    elif cel == "mobile":
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", wyzwanie or ""):
            return _blad_zewn(request, "Aplikacja nie przekazała wyzwania logowania.")
        dodatkowe = {"wyzwanie": wyzwanie}
    elif cel != "login":
        cel = "login"
    stan = zewnetrzne.nowy_stan(klucz, cel, **dodatkowe)
    odp = RedirectResponse(zewnetrzne.adres_autoryzacji(stan, _adres_zwrotny(request), podpowiedz),
                           status_code=status.HTTP_303_SEE_OTHER)
    odp.set_cookie(CIASTECZKO_OAUTH, sign_oauth(stan), max_age=600, httponly=True,
                   secure=get_settings().require_https, samesite="lax", path="/")
    return odp


@router.post("/konto/zewn/{powiazanie_id}/odlacz")
def odlacz_zewnetrzne(powiazanie_id: str, request: Request, csrf_token: str = Form(""),
                      user: PortalUser = Depends(require_user), db: Session = Depends(get_db)):
    verify_csrf(request, user, csrf_token)
    powiazanie = db.get(TozsamoscZewnetrzna, powiazanie_id)
    if powiazanie is None or powiazanie.user_id != user.id:
        raise HTTPException(status_code=404, detail="nie ma takiego powiązania")
    inne = db.execute(select(TozsamoscZewnetrzna).where(
        TozsamoscZewnetrzna.user_id == user.id, TozsamoscZewnetrzna.id != powiazanie.id
    )).scalars().first()
    if inne is None and user.password_hash == HASLO_NIEUZYWANE:
        return _wroc_konto(blad="To jedyny sposób logowania na to konto. Najpierw ustaw hasło "
                                "(„Nie pamiętasz hasła?”) albo dodaj inne konto.")
    db.delete(powiazanie)
    audit(db, None, action="zewnetrzne.odlaczone", target=user.email,
          detail={"dostawca": powiazanie.dostawca, "email": powiazanie.email},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/konto?info=" + quote("Konto odłączone."),
                            status_code=status.HTTP_303_SEE_OTHER)


def dane_zewnetrzne_konta(db: Session, user: PortalUser) -> dict:
    powiazane = db.execute(select(TozsamoscZewnetrzna).where(
        TozsamoscZewnetrzna.user_id == user.id).order_by(TozsamoscZewnetrzna.utworzono)
    ).scalars().all()
    return {"powiazane": [(p, zewnetrzne.DOSTAWCY.get(p.dostawca)) for p in powiazane],
            "dostepni": zewnetrzne.wlaczeni() if user.zrodlo == ZRODLO_LOKALNE else []}
