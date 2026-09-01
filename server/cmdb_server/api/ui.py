"""Panel WWW: logowanie, lista maszyn, szczegoly, opiekunowie, tokeny.

Widoki renderowane po stronie serwera (Jinja2) - bez SPA i bez publicznego
API przegladarkowego, dzieki czemu powierzchnia ataku jest minimalna.
Kazde zapytanie o dane przechodzi przez services.scoping - nie ma sciezki,
ktora czytalaby maszyny bez filtra tenant_id.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse

from markupsafe import Markup
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChainableUndefined, Undefined
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    KATEGORIE_SLOWNIKA,
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    TYP_KOMPUTER,
    TYPY_SPRZETU,
    ZRODLO_AGENT,
    ZRODLO_RECZNE,
    AgentCredential,
    Asset,
    AssetChange,
    AuditLog,
    EnrollmentToken,
    InventorySnapshot,
    PortalUser,
    Tenant,
    WpisSlownika,
    utcnow,
)
from ..security import (
    generate_token,
    hash_password,
    issue_csrf_token,
    sign_session,
    verify_password,
)
from ..services import (
    changes, cve, duplicates, logowanie, pakiet, rodzaje, scoping, slowniki, upgrades,
    ustawienia,
)
from ..services import schemat as definicje_pol
from ..services.auth import (
    LoginRequired,
    authenticate_user,
    client_ip,
    current_user,
    naive_utc,
    require_user,
    tenant_context_for,
    verify_csrf,
    widzi_wszystkie_firmy,
)
from ..services.scoping import TenantContext, audit
from . import download

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_ODCISKI: dict[str, str] = {}
# ChainableUndefined: brak sekcji w raporcie (np. maszyna jeszcze nie raportowala)
# nie moze wywalac calego widoku - '{{ a.b.c }}' renderuje sie pusto zamiast rzucac.
templates.env.undefined = ChainableUndefined
router = APIRouter(tags=["ui"])


# --- filtry szablonow -------------------------------------------------------

def _missing(value) -> bool:
    return value is None or isinstance(value, Undefined)


def _na_date(value):
    """Daty z raportow agenta sa w JSON-ie napisami ISO, a nie datami.

    Filtry dostawaly wiec raz obiekt daty (kolumny bazy), a raz napis
    (tresc raportu) - i na napisie wywracaly sie bledem o brakujacym
    tzinfo. Rozpoznajemy oba przypadki tutaj, zamiast w kazdym szablonie.
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


def _fmt_dt(value):
    """Chwila jako element <time> - przegladarka przelicza ja na strefe czytajacego.

    Serwer liczy wszystko w UTC i tak to zapisuje, bo strefa serwera nie moze
    wplywac na dane. Ale czlowiek oglada raport w swojej strefie i "16:25 UTC"
    zmusza go do liczenia w pamieci - a przy sprawdzaniu, czy agent zglosil sie
    po ostatniej zmianie, latwo sie wtedy pomylic o dwie godziny.

    W atrybucie datetime zostaje zapis UTC z oznaczeniem strefy, wiec jest
    jednoznaczny; app.js podmienia sama tresc. Bez JavaScriptu widac nadal
    poprawna godzine UTC - gorzej, ale nie blednie.
    """
    if _missing(value):
        return "-"
    value = naive_utc(_na_date(value))
    if not value:
        return "-"
    znacznik = value.strftime("%Y-%m-%dT%H:%M:%SZ")
    widoczne = value.strftime("%Y-%m-%d %H:%M UTC")
    return Markup('<time datetime="{}" data-czas>{}</time>').format(znacznik, widoczne)


def _fmt_ago(value) -> str:
    if _missing(value):
        return "nigdy"
    value = naive_utc(_na_date(value))
    if not value:
        return "nigdy"
    delta = utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "przed chwila"
    if seconds < 3600:
        return f"{seconds // 60} min temu"
    if seconds < 86400:
        return f"{seconds // 3600} godz. temu"
    return f"{seconds // 86400} dni temu"


def _fmt_bytes(value) -> str:
    if _missing(value):
        return "-"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


def _pretty_json(value) -> str:
    if _missing(value):
        return "{}"
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)


def _odcisk(nazwa: str) -> str:
    """Adres pliku statycznego ze znacznikiem jego tresci.

    Bez tego przegladarka trzyma poprzedni arkusz stylow tak dlugo, jak uzna
    za stosowne - poprawka wygladu byla wdrozona na serwerze, a uzytkownik
    dalej ogladal stary uklad i nie mial powodu podejrzewac pamieci
    podrecznej. Znacznik zmienia sie razem z plikiem, wiec nowa wersja jest
    dla przegladarki innym adresem i pobiera ja natychmiast.

    Skrot liczymy raz, przy pierwszym uzyciu: pliki statyczne nie zmieniaja
    sie w trakcie pracy procesu.
    """
    if nazwa not in _ODCISKI:
        plik = STATIC_DIR / nazwa
        try:
            skrot = hashlib.sha256(plik.read_bytes()).hexdigest()[:10]
        except OSError:
            # Brak pliku nie jest powodem, zeby strona sie nie otworzyla.
            return f"/static/{nazwa}"
        _ODCISKI[nazwa] = f"/static/{nazwa}?v={skrot}"
    return _ODCISKI[nazwa]


templates.env.globals["zasob"] = _odcisk

templates.env.filters["dt"] = _fmt_dt
templates.env.filters["ago"] = _fmt_ago
templates.env.filters["bytes"] = _fmt_bytes
templates.env.filters["pretty_json"] = _pretty_json
templates.env.filters["zmiany"] = changes.odmiana_zmian


# --- kontekst tenanta -------------------------------------------------------

def resolve_tenant(
    request: Request,
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> TenantContext:
    """Zwykly uzytkownik ma tenant przypisany na sztywno.
    Superadmin i audytor globalny przelaczaja sie parametrem ?tenant=<slug>
    (zapamietanym w ciasteczku). Audytor dostaje kontekst bez prawa zapisu -
    decyduje o tym tenant_context_for, wiec nie da sie tego tu przeoczyc."""
    if widzi_wszystkie_firmy(user):
        slug = request.query_params.get("tenant") or request.cookies.get("cmdb_tenant")
        tenant = None
        if slug:
            tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
        if tenant is None:
            tenant = db.execute(
                select(Tenant).where(Tenant.is_active.is_(True)).order_by(Tenant.name)
            ).scalars().first()
        if tenant is None:
            raise HTTPException(status_code=404, detail="brak zdefiniowanych firm")
        return tenant_context_for(user, tenant)

    if not user.tenant_id:
        raise HTTPException(status_code=403, detail="konto nie jest przypisane do firmy")
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=403, detail="firma nieaktywna")
    return tenant_context_for(user, tenant)


def render(
    request: Request,
    template: str,
    user: PortalUser,
    ctx: TenantContext | None,
    db: Session,
    **extra,
) -> HTMLResponse:
    settings = get_settings()
    tenants = []
    if widzi_wszystkie_firmy(user):
        tenants = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    payload = {
        "request": request,
        "user": user,
        "ctx": ctx,
        "motyw": motyw_z_ciasteczka(request),
        "csrf_token": issue_csrf_token(user.id),
        "all_tenants": tenants,
        # Prog braku kontaktu moze byc ustawiony per firma - szablony maja
        # pokazywac te wartosc, ktora faktycznie obowiazuje.
        "stale_after_hours": ustawienia.prog_bez_kontaktu(
            db.get(Tenant, ctx.tenant_id) if ctx else None
        ),
        **extra,
    }
    return templates.TemplateResponse(request, template, payload)


@router.get("/pomoc", response_class=HTMLResponse)
def pomoc(request: Request, user: PortalUser = Depends(require_user)) -> Response:
    """Samodzielny podręcznik dostępny dopiero po zalogowaniu do portalu."""
    return templates.TemplateResponse(request, "pomoc.html", {"user": user})


# Minimalna dlugosc hasla panelu. Ta sama wartosc obowiazuje przy zakladaniu
# konta w administracji (api/admin.py importuje ja stad), zeby nie dalo sie
# ustawic hasla slabszego niz przy zalozeniu konta.
MIN_DLUGOSC_HASLA = 12

# Dozwolone wartosci ciasteczka motywu. Pusta - motyw z ustawien systemu.
MOTYWY = {"jasny", "ciemny"}


def motyw_z_ciasteczka(request: Request) -> str:
    """Wybrany motyw, wstawiany do atrybutu data-theme.

    Renderuje go serwer, a nie JavaScript: inaczej przy kazdym wejsciu
    mignelaby wersja jasna, zanim skrypt zdazy ustawic atrybut. Wartosc
    trafia wprost do HTML, wiec przepuszczamy wylacznie znane nazwy.
    """
    wybrany = (request.cookies.get("cmdb_motyw") or "").strip().lower()
    return wybrany if wybrany in MOTYWY else ""


def _wpis_id(wpis) -> str | None:
    """Identyfikator wpisu slownika albo None - formularze podaja nazwy."""
    return wpis.id if wpis is not None else None


def _require_write(ctx: TenantContext) -> None:
    if not ctx.can_write:
        raise HTTPException(status_code=403, detail="konto ma uprawnienia tylko do odczytu")


# --- logowanie --------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: PortalUser | None = Depends(current_user)) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html",
        {"request": request, "error": None, "motyw": motyw_z_ciasteczka(request)},
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    settings = get_settings()
    ip = client_ip(request)

    def odmow(komunikat: str, kod: int = status.HTTP_401_UNAUTHORIZED):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": komunikat, "motyw": motyw_z_ciasteczka(request)},
            status_code=kod,
        )

    do_kiedy = logowanie.zablokowane_do(db, email, ip)
    if do_kiedy is not None:
        # Nie sprawdzamy nawet hasla: inaczej blokada mowilaby, ktore haslo
        # jest poprawne, roznym czasem odpowiedzi.
        audit(db, None, action="login.blocked", target=email, ip=ip, actor=email)
        db.commit()
        return odmow(
            f"Logowanie zablokowane po nieudanych probach. Sprobuj po "
            f"{do_kiedy.strftime('%Y-%m-%d %H:%M')} UTC.",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = authenticate_user(db, email, password)
    if user is None:
        audit(db, None, action="login.failed", target=email, ip=ip, actor=email)
        db.commit()
        zalozona = logowanie.odnotuj_niepowodzenie(
            db, email, ip, settings.login_max_failures, settings.login_lockout_hours
        )
        if zalozona is not None:
            return odmow(
                f"Zbyt wiele nieudanych prob. Logowanie zablokowane do "
                f"{zalozona.strftime('%Y-%m-%d %H:%M')} UTC.",
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
        # Komunikat jest ten sam dla zlego adresu i zlego hasla - inaczej
        # dalby sie uzyc do sprawdzania, ktore konta istnieja.
        return odmow("Nieprawidlowy e-mail lub haslo.")

    logowanie.wyczysc(db, email, ip)
    user.last_login_at = utcnow()
    audit(
        db,
        None,
        action="login.ok",
        target=user.email,
        ip=client_ip(request),
        actor=user.email,
    )
    db.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie,
        sign_session({"uid": user.id, "sv": user.session_version}),
        max_age=settings.session_max_age,
        httponly=True,
        secure=settings.require_https,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    settings = get_settings()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


@router.post("/konto/wyloguj-wszystkie")
def logout_all(request: Request, csrf_token: str = Form(""),
               user: PortalUser = Depends(require_user), db: Session = Depends(get_db)) -> Response:
    verify_csrf(request, user, csrf_token)
    user.session_version = PortalUser.session_version + 1
    audit(db, None, action="session.revoke_all", actor=user.email, ip=client_ip(request))
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie, path="/")
    return response


@router.post("/assets/{asset_id}/enrollment/unblock")
def unblock_enrollment(asset_id: str, request: Request, csrf_token: str = Form(""),
                      user: PortalUser = Depends(require_user),
                      ctx: TenantContext = Depends(resolve_tenant),
                      db: Session = Depends(get_db)) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    asset = db.execute(scoping.assets_query(ctx).where(Asset.id == asset_id).with_for_update()).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")
    # Odblokowanie nie przywraca starych kluczy.
    for credential in db.execute(select(AgentCredential).where(
        AgentCredential.asset_id == asset.id, AgentCredential.revoked_at.is_(None)
    )).scalars():
        credential.revoked_at = utcnow()
    asset.enrollment_blocked = False
    audit(db, ctx, action="agent.enrollment_unblocked", target=asset.hostname, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=303)


@router.get("/switch-tenant")
def switch_tenant(
    slug: str = Query(...),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not widzi_wszystkie_firmy(user):
        raise HTTPException(status_code=403, detail="to konto widzi tylko swoja firme")
    tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="nieznana firma")
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("cmdb_tenant", tenant.slug, httponly=True, samesite="lax", path="/")
    return response


# --- dashboard --------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    stale_before = ustawienia.granica_aktywnosci(db.get(Tenant, ctx.tenant_id))

    total = db.execute(
        select(func.count(Asset.id)).where(Asset.tenant_id == ctx.tenant_id)
    ).scalar_one()
    # Wpisy reczne nie maja agenta - do liczby "bez kontaktu" nie wchodza.
    stale = db.execute(
        select(func.count(Asset.id)).where(
            Asset.tenant_id == ctx.tenant_id,
            Asset.zrodlo == ZRODLO_AGENT,
            Asset.last_seen < stale_before,
        )
    ).scalar_one()
    unassigned = db.execute(
        select(func.count(Asset.id)).where(
            Asset.tenant_id == ctx.tenant_id, Asset.owner_id.is_(None)
        )
    ).scalar_one()
    # Systemy operacyjne opisuja wylacznie sprzet z agentem. Monitor albo
    # przelacznik nie ma systemu i nie powinien powiekszac slupka "nieznany".
    by_os = db.execute(
        select(Asset.os_family, func.count(Asset.id))
        .where(Asset.tenant_id == ctx.tenant_id, Asset.zrodlo == ZRODLO_AGENT)
        .group_by(Asset.os_family)
    ).all()
    # Podzial na rodzaje odpowiada na pytanie "co my w ogole mamy" - przy
    # monitorach i sprzecie sieciowym sama liczba wpisow niczego nie mowi.
    by_typ = db.execute(
        select(Asset.typ, func.count(Asset.id))
        .where(Asset.tenant_id == ctx.tenant_id)
        .group_by(Asset.typ)
        .order_by(func.count(Asset.id).desc())
    ).all()

    # "Ostatni kontakt" to zgloszenia agenta. Wpis reczny nigdy sie nie
    # zglasza, wiec jego obecnosc tutaj sugerowala kontakt, ktorego nie bylo -
    # data przy nim to tylko chwila zalozenia wpisu.
    recent = db.execute(
        scoping.assets_query(ctx)
        .where(Asset.zrodlo == ZRODLO_AGENT)
        .order_by(Asset.last_seen.desc()).limit(10)
    ).scalars().all()
    # Wpisy reczne maja wlasna liste: ostatnio dodane albo poprawione.
    reczne = db.execute(
        scoping.assets_query(ctx)
        .where(Asset.zrodlo == ZRODLO_RECZNE)
        .order_by(Asset.last_seen.desc()).limit(10)
    ).scalars().all()
    changed = db.execute(
        scoping.assets_query(ctx)
        .where(Asset.last_change_at.is_not(None))
        .order_by(Asset.last_change_at.desc())
        .limit(10)
    ).scalars().all()

    return render(
        request,
        "dashboard.html",
        user,
        ctx,
        db,
        total=total,
        stale=stale,
        unassigned=unassigned,
        by_os=by_os,
        by_typ=by_typ,
        typy=rodzaje.etykiety(db, ctx),
        recent=recent,
        reczne=reczne,
        changed=changed,
        z_agentem=db.execute(
            select(func.count(Asset.id)).where(
                Asset.tenant_id == ctx.tenant_id, Asset.zrodlo == ZRODLO_AGENT)
        ).scalar_one(),
    )


# --- maszyny ----------------------------------------------------------------

@router.get("/assets", response_class=HTMLResponse)
def asset_list(
    request: Request,
    q: str = Query("", max_length=200),
    os_family: str = Query("", max_length=32),
    owner: str = Query("", max_length=36),
    state: str = Query("", max_length=16),
    lifecycle: str = Query("", max_length=16),
    typ: str = Query("", max_length=32),
    lokalizacja: str = Query("", max_length=200),
    zrodlo: str = Query("", max_length=16),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    granica = ustawienia.granica_aktywnosci(db.get(Tenant, ctx.tenant_id))
    stmt = scoping.assets_query(ctx)

    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Asset.hostname.ilike(pattern),
                Asset.fqdn.ilike(pattern),
                Asset.serial_number.ilike(pattern),
                Asset.primary_ip.ilike(pattern),
                Asset.role_label.ilike(pattern),
                Asset.lokalizacja.has(WpisSlownika.wartosc.ilike(pattern)),
            )
        )
    if os_family:
        stmt = stmt.where(Asset.os_family == os_family)
    if typ:
        stmt = stmt.where(Asset.typ == typ)
    if zrodlo in (ZRODLO_AGENT, ZRODLO_RECZNE):
        stmt = stmt.where(Asset.zrodlo == zrodlo)
    if lokalizacja:
        stmt = stmt.where(Asset.lokalizacja_id == lokalizacja)
    if owner == "none":
        stmt = stmt.where(Asset.owner_id.is_(None))
    elif owner:
        stmt = stmt.where(Asset.owner_id == owner)
    # Sprzet wpisany recznie nie ma agenta i nigdy sie nie odezwie - pytanie
    # o kontakt jest dla niego bez sensu, wiec nie trafia do zadnej z odpowiedzi.
    if state == "stale":
        stmt = stmt.where(Asset.zrodlo == ZRODLO_AGENT, Asset.last_seen < granica)
    elif state == "online":
        stmt = stmt.where(Asset.zrodlo == ZRODLO_AGENT, Asset.last_seen >= granica)

    # Wycofane maszyny znikaja z domyslnej listy, ale zostaja w bazie razem
    # z cala historia - pokazujemy je na zadanie.
    if lifecycle == "wycofany":
        stmt = stmt.where(Asset.lifecycle == LIFECYCLE_WYCOFANY)
    elif lifecycle != "wszystkie":
        stmt = stmt.where(Asset.lifecycle == LIFECYCLE_AKTYWNY)

    assets = db.execute(stmt.order_by(Asset.hostname)).scalars().all()
    owners = db.execute(scoping.owners_query(ctx)).scalars().all()
    families = db.execute(
        select(Asset.os_family)
        .where(Asset.tenant_id == ctx.tenant_id, Asset.os_family.is_not(None))
        .distinct()
    ).scalars().all()

    return render(
        request,
        "assets.html",
        user,
        ctx,
        db,
        assets=assets,
        owners=owners,
        families=families,
        typy=rodzaje.etykiety(db, ctx),
        lokalizacje=slowniki.wpisy(db, ctx, "lokalizacja"),
        filters={"q": q, "os_family": os_family, "owner": owner,
                 "state": state, "lifecycle": lifecycle,
                 "typ": typ, "lokalizacja": lokalizacja, "zrodlo": zrodlo},
        liczba_wycofanych=db.execute(
            select(func.count(Asset.id)).where(
                Asset.tenant_id == ctx.tenant_id, Asset.lifecycle == LIFECYCLE_WYCOFANY
            )
        ).scalar_one(),
    )


# --- sprzet wpisywany recznie -----------------------------------------------
# Trasa /assets/nowy musi byc zadeklarowana PRZED /assets/{asset_id}, bo
# inaczej "nowy" zostaloby potraktowane jako identyfikator maszyny.

def _dane_recznego_sprzetu(dane: dict) -> dict:
    """Pola wspolne dla dodawania i edycji - jedno miejsce na obcinanie dlugosci."""
    return {
        "hostname": (dane.get("nazwa") or "").strip()[:255],
        "manufacturer": (dane.get("producent") or "").strip()[:200] or None,
        "model": (dane.get("model") or "").strip()[:200] or None,
        "serial_number": (dane.get("numer_seryjny") or "").strip()[:128] or None,
        "primary_ip": (dane.get("ip") or "").strip()[:64] or None,
        "role_label": (dane.get("rola") or "").strip()[:200] or None,
        "purchase_notes": (dane.get("uwagi") or "").strip() or None,
    }


@router.get("/assets/nowy", response_class=HTMLResponse)
def formularz_nowego_sprzetu(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Sprzet, na ktorym agenta nie da sie zainstalowac.

    Drukarka, switch czy monitor nie zaraportuja sie same, a bez nich CMDB
    opisuje tylko czesc tego, co firma ma na stanie. Wpis reczny jest ubozszy
    (nie ma raportu, oprogramowania ani podatnosci), za to zna wszystko, co
    wpisal czlowiek: gdzie stoi, kto go uzywa i skad pochodzi.
    """
    _require_write(ctx)
    return render(
        request,
        "asset_nowy.html",
        user,
        ctx,
        db,
        owners=db.execute(scoping.owners_query(ctx)).scalars().all(),
        typy=rodzaje.etykiety(db, ctx),
        podpowiedzi=slowniki.podpowiedzi(db, ctx),
        domyslny_typ="siec",
    )


@router.post("/assets/nowy")
def utworz_sprzet(
    request: Request,
    nazwa: str = Form(...),
    typ: str = Form("siec"),
    producent: str = Form(""),
    model: str = Form(""),
    numer_seryjny: str = Form(""),
    ip: str = Form(""),
    lokalizacja_id: str = Form(""),
    rola: str = Form(""),
    owner_id: str = Form(""),
    uzytkownik_id: str = Form(""),
    dostawca_id: str = Form(""),
    uwagi: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    if not rodzaje.wpis_rodzaju(db, ctx, typ):
        raise HTTPException(status_code=400, detail="nieznany rodzaj sprzetu")
    pola = _dane_recznego_sprzetu(
        {"nazwa": nazwa, "producent": producent, "model": model,
         "numer_seryjny": numer_seryjny, "ip": ip, "rola": rola, "uwagi": uwagi}
    )
    if not pola["hostname"]:
        raise HTTPException(status_code=400, detail="nazwa jest wymagana")

    def osoba(identyfikator: str) -> str | None:
        if not identyfikator:
            return None
        znaleziona = scoping.get_owner(db, ctx, identyfikator)
        if znaleziona is None:
            raise HTTPException(status_code=400, detail="osoba spoza tej firmy")
        return znaleziona.id

    # Wpis reczny tez potrzebuje machine_id - kolumna jest wymagana i unikalna
    # w obrebie firmy. Przedrostek od razu mowi, ze to nie jest identyfikator
    # odczytany z plyty glownej, tylko klucz nadany przez system.
    try:
        lokalizacja = slowniki.wybierz(db, ctx, "lokalizacja", lokalizacja_id)
        dostawca = slowniki.wybierz(db, ctx, "dostawca", dostawca_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sprzet = Asset(
        tenant_id=ctx.tenant_id,
        machine_id=f"reczne:{uuid4()}",
        typ=typ,
        zrodlo=ZRODLO_RECZNE,
        owner_id=osoba(owner_id),
        uzytkownik_id=osoba(uzytkownik_id),
        lokalizacja_id=_wpis_id(lokalizacja),
        dostawca_id=_wpis_id(dostawca),
        **pola,
    )
    db.add(sprzet)
    db.flush()
    audit(db, ctx, action="asset.dodany_recznie", target=sprzet.hostname,
          detail={"typ": typ}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{sprzet.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assets/{asset_id}/dane")
async def zapisz_dane_sprzetu(
    asset_id: str,
    request: Request,
    nazwa: str = Form(...),
    typ: str = Form("siec"),
    producent: str = Form(""),
    model: str = Form(""),
    numer_seryjny: str = Form(""),
    ip: str = Form(""),
    rola: str = Form(""),
    uwagi: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Edycja danych sprzetu wpisanego recznie.

    Dla maszyn z agentem tych pol nie ruszamy: przy najblizszym raporcie i tak
    wrocilyby wartosci odczytane z maszyny, a rozjazd miedzy tym, co widac
    w panelu, a tym co jest na sprzecie, jest gorszy niz brak edycji.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    sprzet = scoping.get_asset(db, ctx, asset_id)
    if sprzet is None:
        raise HTTPException(status_code=404, detail="nie znaleziono sprzetu")
    if sprzet.zrodlo != ZRODLO_RECZNE:
        raise HTTPException(
            status_code=400,
            detail="dane maszyny z agentem pochodza z jej raportow i nie edytuje sie ich recznie",
        )
    if not rodzaje.wpis_rodzaju(db, ctx, typ):
        raise HTTPException(status_code=400, detail="nieznany rodzaj sprzetu")

    pola = _dane_recznego_sprzetu(
        {"nazwa": nazwa, "producent": producent, "model": model,
         "numer_seryjny": numer_seryjny, "ip": ip, "rola": rola, "uwagi": uwagi}
    )
    if not pola["hostname"]:
        raise HTTPException(status_code=400, detail="nazwa jest wymagana")

    # Zmiana rodzaju UKRYWA pola poprzedniego, nie kasuje ich. Ostrzezenie
    # musi powstac przed zapisem, bo potem nie da sie juz powiedziec, ile ich
    # bylo - a bez tego zmiana rodzaju wyglada jak utrata danych.
    ukryte = rodzaje.ukryte_przy_zmianie(db, ctx, sprzet, typ)

    for klucz, wartosc in pola.items():
        setattr(sprzet, klucz, wartosc)

    # Zmiana rodzaju NIE rusza wartosci: ani nie zapisuje nowych, ani nie kasuje
    # starych. Formularz zostal narysowany dla dotychczasowego rodzaju, wiec
    # jego pola nie opisuja juz tego, czym sprzet ma byc - a pola nowego rodzaju
    # jeszcze nie istnialy, gdy strona powstawala. Zapisujemy je dopiero przy
    # nastepnej edycji, kiedy czlowiek widzi wlasciwy formularz.
    if typ != sprzet.typ:
        sprzet.typ = typ
    else:
        formularz = {klucz[5:]: wartosc
                     for klucz, wartosc in (await request.form()).items()
                     if klucz.startswith("pole_")}
        try:
            rodzaje.zapisz_atrybuty(db, ctx, sprzet, formularz)
        except definicje_pol.BladPola as exc:
            db.rollback()
            adres = (f"/assets/{asset_id}?blad="
                     + urllib.parse.quote(json.dumps(exc.bledy, ensure_ascii=False)))
            return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)

    audit(db, ctx, action="asset.dane_zmienione", target=sprzet.hostname,
          detail={"rodzaj": typ, "ukryte_pola": ukryte} if ukryte else None,
          ip=client_ip(request))
    db.commit()
    adres = f"/assets/{sprzet.id}"
    if ukryte:
        adres += "?ukryte=" + urllib.parse.quote(", ".join(ukryte))
    return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assets/{asset_id}/usun")
def usun_sprzet(
    asset_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Kasuje wpis reczny. Maszyny z agentem sie wycofuje, a nie usuwa.

    Wpis reczny to sam tekst wpisany przez czlowieka - pomylke naprawia sie
    jego skasowaniem. Maszyna z agentem niesie historie raportow i zmian,
    ktorej nie wolno stracic, wiec ma cykl zycia (wycofana), a nie kasowanie.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    sprzet = scoping.get_asset(db, ctx, asset_id)
    if sprzet is None:
        raise HTTPException(status_code=404, detail="nie znaleziono sprzetu")
    if sprzet.zrodlo != ZRODLO_RECZNE:
        raise HTTPException(
            status_code=400,
            detail="maszyne z agentem mozna wycofac, ale nie usunac - historia raportow zostaje",
        )
    nazwa = sprzet.hostname
    db.delete(sprzet)
    audit(db, ctx, action="asset.usuniety", target=nazwa, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/assets", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_detail(
    asset_id: str,
    request: Request,
    snapshot: str = Query("", max_length=36),
    ukryte: str = Query("", max_length=500),
    blad: str = Query("", max_length=2000),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")
    try:
        bledy_pol = json.loads(blad) if blad else {}
    except json.JSONDecodeError:
        bledy_pol = {}
    if not isinstance(bledy_pol, dict):
        bledy_pol = {}

    if snapshot:
        current = scoping.get_snapshot(db, ctx, snapshot)
        if current is None or current.asset_id != asset.id:
            raise HTTPException(status_code=404, detail="nie znaleziono snapshotu")
    else:
        current = scoping.current_reading(db, ctx, asset.id)

    history = db.execute(
        select(InventorySnapshot)
        .where(InventorySnapshot.asset_id == asset.id)
        .order_by(InventorySnapshot.collected_at.desc())
        .limit(30)
    ).scalars().all()

    owners = db.execute(scoping.owners_query(ctx)).scalars().all()
    credentials = db.execute(
        select(AgentCredential)
        .where(AgentCredential.asset_id == asset.id)
        .order_by(AgentCredential.created_at.desc())
    ).scalars().all()

    klucze_zmian = changes.klucze_raportow(db, ctx.tenant_id, asset_id=asset.id)
    zmiany = changes.pogrupuj(
        changes.zmiany_raportow(db, ctx.tenant_id, klucze_zmian, asset_id=asset.id)
    )

    payload = current.payload if current else {}
    # Podatnosci liczymy przy wyswietleniu, a nie przy przyjeciu raportu:
    # dane o lukach zmieniaja sie niezaleznie od maszyny, wiec wynik zapisany
    # tydzien temu bylby nieaktualny mimo braku zmian na samej maszynie.
    podatnosci = cve.dopasuj(db, payload) if payload else None
    return render(
        request,
        "asset_detail.html",
        user,
        ctx,
        db,
        asset=asset,
        snapshot=current,
        podatnosci=podatnosci,
        dzisiaj=date.today(),
        history=history,
        owners=owners,
        typy=rodzaje.etykiety(db, ctx),
        podpowiedzi=slowniki.podpowiedzi(db, ctx),
        # Cztery podglady budowane tym samym sposobem - osoba przestala byc
        # przypadkiem szczegolnym, wiec nie ma powodu na osobna sciezke.
        # Pola wlasciwe dla rodzaju - tylko przy wpisie recznym. Maszyna
        # z agentem opisuje sie raportem i pola rodzaju jej nie dotycza.
        pola_rodzaju=(rodzaje.schemat_pol(db, ctx, asset.typ or "").pola
                      if asset.zrodlo == ZRODLO_RECZNE else []),
        wartosci_rodzaju=(rodzaje.widoczne(db, ctx, asset)
                          if asset.zrodlo == ZRODLO_RECZNE else []),
        rodzaje_listy=rodzaje.etykiety(db, ctx),
        formaty=definicje_pol.FORMATY,
        ukryte=ukryte,
        bledy_pol=bledy_pol,
        szczegoly_opiekuna=slowniki.szczegoly(db, ctx, asset.owner),
        szczegoly_uzytkownika=slowniki.szczegoly(db, ctx, asset.uzytkownik),
        szczegoly_lokalizacji=slowniki.szczegoly(db, ctx, asset.lokalizacja),
        szczegoly_dostawcy=slowniki.szczegoly(db, ctx, asset.dostawca),
        credentials=credentials,
        zmiany=zmiany,
        payload=payload,
        hardware=payload.get("hardware") or {},
        os_info=payload.get("os") or {},
        network=payload.get("network") or {},
        software=payload.get("software") or {},
        users_info=payload.get("users") or {},
        collector_errors=payload.get("errors") or [],
    )


@router.post("/assets/{asset_id}/owner")
def assign_owner(
    asset_id: str,
    request: Request,
    owner_id: str = Form(""),
    uzytkownik_id: str = Form(""),
    role_label: str = Form(""),
    lokalizacja_id: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Opiekun, uzytkownik, rola i lokalizacja - dane, ktorych agent nie zna.

    Opiekun odpowiada za sprzet, uzytkownik przy nim siedzi. Rozdzielone, bo
    to zwykle dwie rozne osoby i dwa rozne pytania: "kto to naprawi" i "komu
    to zabraknie". Obie wskazuja na te sama liste osob firmy.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")

    def osoba(identyfikator: str) -> WpisSlownika | None:
        if not identyfikator:
            return None
        znaleziona = scoping.get_owner(db, ctx, identyfikator)
        if znaleziona is None:
            raise HTTPException(status_code=400, detail="osoba spoza tej firmy")
        return znaleziona

    new_owner = osoba(owner_id)
    new_user = osoba(uzytkownik_id)

    previous = asset.owner.wartosc if asset.owner else None
    poprzedni_uzytkownik = asset.uzytkownik.wartosc if asset.uzytkownik else None
    asset.owner_id = new_owner.id if new_owner else None
    asset.uzytkownik_id = new_user.id if new_user else None
    asset.role_label = role_label.strip() or None
    # Nowa lokalizacja od razu trafia do slownika firmy - inaczej kazdy
    # wpisywalby ja po swojemu i podpowiedzi nigdy by nie powstaly.
    try:
        wpis_lokalizacji = slowniki.wybierz(db, ctx, "lokalizacja", lokalizacja_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    asset.lokalizacja_id = _wpis_id(wpis_lokalizacji)

    audit(
        db,
        ctx,
        action="asset.owner_changed",
        target=asset.hostname,
        detail={
            "from": previous,
            "to": new_owner.wartosc if new_owner else None,
            "uzytkownik_z": poprzedni_uzytkownik,
            "uzytkownik_na": new_user.wartosc if new_user else None,
            "lokalizacja": wpis_lokalizacji.wartosc if wpis_lokalizacji else None,
        },
        ip=client_ip(request),
    )
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/assets/{asset_id}/raw.json")
def asset_raw_json(
    asset_id: str,
    snapshot: str = Query("", max_length=36),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")
    current = (
        scoping.get_snapshot(db, ctx, snapshot)
        if snapshot
        else scoping.current_reading(db, ctx, asset.id)
    )
    if current is None or current.asset_id != asset.id:
        raise HTTPException(status_code=404, detail="brak snapshotu")
    return JSONResponse(current.payload)


@router.post("/assets/{asset_id}/credentials/{credential_id}/revoke")
def revoke_credential(
    asset_id: str,
    credential_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")
    credential = db.execute(
        select(AgentCredential).where(
            AgentCredential.id == credential_id,
            AgentCredential.asset_id == asset.id,
            AgentCredential.tenant_id == ctx.tenant_id,
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status_code=404, detail="nie znaleziono poswiadczenia")

    credential.revoked_at = utcnow()
    asset.enrollment_blocked = True
    audit(
        db,
        ctx,
        action="agent.credential_revoked",
        target=asset.hostname,
        detail={"prefix": credential.prefix},
        ip=client_ip(request),
    )
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assets/{asset_id}/lifecycle")
def zmien_cykl_zycia(
    asset_id: str,
    request: Request,
    akcja: str = Form(...),
    powod: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Wycofanie zasobu albo przywrocenie go do uzytku.

    Wycofanie nie kasuje niczego - maszyna znika z domyslnych list, ale
    zostaje w bazie razem z cala historia. Inwentarz ma pamietac, co bylo,
    a nie tylko co jest.
    """
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")

    if akcja == "wycofaj":
        asset.lifecycle = LIFECYCLE_WYCOFANY
        asset.retired_at = utcnow()
        asset.retired_by = user.email
        asset.retired_reason = powod.strip() or None
    elif akcja == "przywroc":
        asset.lifecycle = LIFECYCLE_AKTYWNY
        asset.retired_at = None
        asset.retired_by = None
        asset.retired_reason = None
    else:
        raise HTTPException(status_code=400, detail="nieznana akcja")

    audit(db, ctx, action=f"asset.{akcja}", target=asset.hostname,
          detail={"powod": powod.strip() or None}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{asset.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/zmiany", response_class=HTMLResponse)
def historia_zmian(
    request: Request,
    kategoria: str = Query("", max_length=20),
    dni: int = Query(30, ge=1, le=365),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Os czasu zmian w calej firmie.

    To jest wlasciwy powod istnienia CMDB: nie "co jest na maszynach",
    tylko "co sie zmienilo i kiedy".
    """
    od = utcnow() - timedelta(days=dni)
    # Najpierw raporty, potem ich zmiany. Odwrotna kolejnosc (pobrac N zmian
    # i pogrupowac) urywalaby ostatni raport w polowie i pokazywala przy nim
    # liczbe mniejsza, niz bylo naprawde.
    klucze = changes.klucze_raportow(db, ctx.tenant_id, od, kategoria)
    grupy = changes.pogrupuj(
        changes.zmiany_raportow(db, ctx.tenant_id, klucze, kategoria)
    )

    kategorie = db.execute(
        select(AssetChange.category, func.count(AssetChange.id))
        .where(AssetChange.tenant_id == ctx.tenant_id, AssetChange.occurred_at >= od)
        .group_by(AssetChange.category)
        .order_by(func.count(AssetChange.id).desc())
    ).all()

    return render(
        request, "zmiany.html", user, ctx, db,
        grupy=grupy,
        limit_raportow=changes.LIMIT_RAPORTOW,
        kategorie=kategorie,
        filtry={"kategoria": kategoria, "dni": dni},
    )


@router.post("/duplikaty/scal")
def scal_duplikaty(
    request: Request,
    docelowy_id: str = Form(...),
    zrodlowy_id: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Reczne polaczenie wpisu recznego z maszyna, ktora zglosil agent.

    Automat laczy tylko wtedy, gdy dowod jest jednoznaczny; tutaj decyduje
    czlowiek - miedzy innymi w przypadkach, w ktorych numer seryjny okazal sie
    wypelniaczem producenta albo pasowaly dwa wpisy naraz.
    """
    from ..services import scalanie

    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    docelowy = scoping.get_asset(db, ctx, docelowy_id)
    zrodlowy = scoping.get_asset(db, ctx, zrodlowy_id)
    if docelowy is None or zrodlowy is None:
        raise HTTPException(status_code=404, detail="nie znaleziono zasobu")
    try:
        scalanie.scal(db, ctx, docelowy, zrodlowy, sposob="recznie", ip=client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/assets/{docelowy.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/duplikaty", response_class=HTMLResponse)
def widok_duplikatow(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Zasoby, ktore wygladaja na te sama maszyne zgloszona dwa razy."""
    return render(
        request, "duplikaty.html", user, ctx, db,
        grupy=duplicates.znajdz_duplikaty(db, ctx.tenant_id),
        zrodlo_reczne=ZRODLO_RECZNE,
        zrodlo_agent=ZRODLO_AGENT,
    )


# --- opiekunowie ------------------------------------------------------------
#
# Osoby sa kategoria slownika, wiec maja te sama karte i te same pola opisane
# schematem co lokalizacje i dostawcy. Wlasna lista i wlasny formularz znikly:
# byly drugim miejscem, w ktorym te same pojecia dzialaly inaczej.

@router.get("/owners")
def owner_list(user: PortalUser = Depends(require_user)) -> Response:
    """Stare odnosniki prowadza do slownika osob.

    Zaleznosc require_user zostaje: bez niej niezalogowany dostawalby
    przekierowanie do slownika zamiast na strone logowania.
    """
    return RedirectResponse("/slowniki?kategoria=osoba",
                            status_code=status.HTTP_303_SEE_OTHER)


# --- slowniki firmowe -------------------------------------------------------

# Ile pol schematu trafia do tabeli slownika. Szerzej tabela przestaje sie
# miescic, a kazdy rekord ma i tak wlasna karte z kompletem danych.
KOLUMNY_SLOWNIKA = 6


@router.get("/slowniki", response_class=HTMLResponse)
def widok_slownikow(
    request: Request,
    kategoria: str = Query("", max_length=32),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Jeden slownik naraz, wybierany zakladka.

    Cztery kategorie zlozone jedna pod druga zmuszaly do przewijania przez
    trzy nieinteresujace, zeby dojsc do czwartej. Wybor trafia do adresu, wiec
    da sie go zapisac i wraca po zapisie rekordu.

    Kolumny tabeli biora sie ze SCHEMATU, a nie ze stalej listy: pokazujemy to,
    co ta firma u siebie prowadzi.
    """
    from ..services import schemat as definicje

    if kategoria not in KATEGORIE_SLOWNIKA:
        kategoria = next(iter(KATEGORIE_SLOWNIKA))
    schematy = {k: slowniki.schemat(db, ctx, k) for k in KATEGORIE_SLOWNIKA}
    rodzaje.zapewnij_startowe(db, ctx)
    opis = schematy[kategoria]
    pozycje = slowniki.wpisy(db, ctx)
    biezace = [w for w in pozycje if w.kategoria == kategoria]

    # SZESC PIERWSZYCH pol schematu, bez zadnego wlasnego doboru. Kolejnosc
    # w schemacie jest jedynym kryterium, wiec o tym, co widac w tabeli,
    # decyduje administrator - przestawiajac pola w edytorze albo usuwajac te,
    # ktorych nie potrzebuje. Wczesniej odsiewalismy pola etykiety i notatki
    # "madrzej"; skutek byl taki, ze przestawienie kolejnosci nie zmienialo
    # tabeli i nie dalo sie dojsc, dlaczego.
    kolumny = opis.pola[:KOLUMNY_SLOWNIKA]

    komorki = {}
    for w in biezace:
        czytelne = {p["klucz"]: p["wartosc"] for p in slowniki.szczegoly(db, ctx, w, z_kluczem=True)}
        komorki[w.id] = czytelne

    wynik = render(
        request, "slowniki.html", user, ctx, db,
        kategorie=KATEGORIE_SLOWNIKA, kategoria=kategoria, schemat=opis,
        kolumny=kolumny, komorki=komorki,
        wpisy={k: [w for w in pozycje if w.kategoria == k] for k in KATEGORIE_SLOWNIKA},
        braki={w.id: definicje.braki(schematy[w.kategoria], w.atrybuty) for w in pozycje},
        uzycia={w.id: slowniki.uzycie_wpisu(db, ctx, w) for w in biezace},
    )
    db.commit()          # schemat zalozony z wzorca przy pierwszym wejsciu
    return wynik


@router.post("/slowniki")
def dodaj_do_slownika(
    request: Request,
    kategoria: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise HTTPException(status_code=400, detail="nieznana kategoria slownika")
    dodana = slowniki.nowy_szkic(db, ctx, kategoria)
    cel = dodana.id
    audit(db, ctx, action="slownik.dodany", target=kategoria + ":" + dodana.wartosc,
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki/wpis/" + cel, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/slowniki/wpis/{wpis_id}", response_class=HTMLResponse)
def karta_wpisu(
    wpis_id: str,
    request: Request,
    blad: str = Query("", max_length=2000),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Karta wpisu z formularzem zbudowanym ze schematu firmy."""
    from ..services import schemat as definicje

    wpis = slowniki.wpis(db, ctx, wpis_id)
    if wpis is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wpisu slownika")
    opis = slowniki.schemat(db, ctx, wpis.kategoria)
    try:
        bledy = json.loads(blad) if blad else {}
    except json.JSONDecodeError:
        bledy = {}
    wynik = render(
        request, "slownik_wpis.html", user, ctx, db,
        wpis=wpis, schemat=opis, grupy=opis.grupy(), formaty=definicje.FORMATY,
        braki=definicje.braki(opis, wpis.atrybuty),
        uzycie=slowniki.uzycie_wpisu(db, ctx, wpis),
        osoby=db.execute(scoping.owners_query(ctx)).scalars().all(),
        powiazane={k: slowniki.wpisy(db, ctx, k) for k in KATEGORIE_SLOWNIKA},
        kategorie=KATEGORIE_SLOWNIKA,
        bledy=bledy if isinstance(bledy, dict) else {},
    )
    db.commit()
    return wynik


@router.post("/slowniki/wpis/{wpis_id}")
async def zapisz_wpis_slownika(
    wpis_id: str,
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Swiadoma edycja w karcie slownika - tu pola wymagane obowiazuja."""
    from ..services import schemat as definicje

    formularz = await request.form()
    verify_csrf(request, user, str(formularz.get("csrf_token", "")))
    _require_write(ctx)
    wpis = slowniki.wpis(db, ctx, wpis_id)
    if wpis is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wpisu slownika")
    dane = {klucz[5:]: wartosc for klucz, wartosc in formularz.items()
            if klucz.startswith("pole_")}
    try:
        slowniki.zapisz_wpis(db, ctx, wpis, dane)
    except definicje.BladPola as exc:
        db.rollback()
        # Bledy wracaja przy polach, ktorych dotycza: komunikat "cos jest nie
        # tak" nie mowi, ktore z jedenastu pol nalezy poprawic.
        adres = "/slowniki/wpis/" + wpis_id + "?blad=" + urllib.parse.quote(
            json.dumps(exc.bledy, ensure_ascii=False))
        return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)
    audit(db, ctx, action="slownik.zapisany", target=wpis.kategoria + ":" + wpis.wartosc,
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki/wpis/" + wpis_id, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/slowniki/{wpis_id}/usun")
def usun_ze_slownika(
    wpis_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    wpis = slowniki.usun(db, ctx, wpis_id)
    if wpis is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wpisu slownika")
    audit(db, ctx, action="slownik.usuniety", target=wpis.kategoria + ":" + wpis.wartosc,
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki", status_code=status.HTTP_303_SEE_OTHER)


# --- schemat slownika (tylko administrator firmy) ---------------------------

@router.get("/slowniki/{kategoria}/schemat", response_class=HTMLResponse)
def widok_schematu(
    kategoria: str,
    request: Request,
    blad: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Pola slownika. Schemat rzadzi tym, co widza wszyscy w firmie."""
    from ..services import schemat as definicje

    _require_write(ctx)
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise HTTPException(status_code=404, detail="nieznana kategoria slownika")
    opis = slowniki.schemat(db, ctx, kategoria)
    limity = {"pola": definicje.MAKS_POL, "opcje": definicje.MAKS_OPCJI}
    uzycia = {p.klucz: slowniki.uzycie_pola(db, ctx, kategoria, p.klucz) for p in opis.pola}
    wynik = render(
        request, "slownik_schemat.html", user, ctx, db,
        kategoria=kategoria, kategorie=KATEGORIE_SLOWNIKA, schemat=opis,
        definicja=json.dumps(opis.model_dump(exclude_none=True), ensure_ascii=False, indent=2),
        typy=definicje.TYPY, formaty=definicje.FORMATY, role=definicje.ROLE,
        limity=limity, uzycia=uzycia, blad=blad,
        # Slownik pojec dla edytora w przegladarce - te same typy, formaty
        # i role, ktore sprawdza serwer. Jedno zrodlo, dwa miejsca uzycia.
        slownik_edytora=json.dumps({
            "typy": list(definicje.TYPY),
            "formaty": {n: o["przyklad"] for n, o in definicje.FORMATY.items()},
            "role": definicje.ROLE,
            "cele": ["osoba"] + list(KATEGORIE_SLOWNIKA),
        }, ensure_ascii=False),
        uzycia_json=json.dumps(uzycia),
        limity_json=json.dumps(limity),
    )
    db.commit()
    return wynik


@router.post("/slowniki/{kategoria}/schemat")
def zapisz_schemat_slownika(
    kategoria: str,
    request: Request,
    definicja: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Podmiana calego schematu. Pola z wartosciami nie daja sie usunac."""
    from pydantic import ValidationError

    from ..services import schemat as definicje

    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise HTTPException(status_code=404, detail="nieznana kategoria slownika")

    poprzedni = slowniki.schemat(db, ctx, kategoria)
    try:
        tresc = json.loads(definicja)
        tresc["kategoria"] = kategoria
        nowy = definicje.Schemat.model_validate(tresc)
    except (json.JSONDecodeError, ValidationError, TypeError, AttributeError) as exc:
        db.rollback()
        return _blad_schematu(kategoria, str(exc)[:400])

    # Pole z wartosciami sie nie usuwa: kasowanie danych i kasowanie pola to
    # dwie osobne decyzje. Wgrany schemat nie moze byc droga na skroty do tej
    # pierwszej, bo formularz jej broni.
    znikaja = {p.klucz for p in poprzedni.pola} - {p.klucz for p in nowy.pola}
    for klucz in sorted(znikaja):
        ile = slowniki.uzycie_pola(db, ctx, kategoria, klucz)
        if ile:
            db.rollback()
            return _blad_schematu(
                kategoria,
                "pole '" + klucz + "' ma wartosci w " + str(ile)
                + " wpisach - najpierw je wyczysc")

    slowniki.zapisz_schemat(db, ctx, kategoria, nowy)
    audit(db, ctx, action="slownik.schemat", target=kategoria,
          detail={"pola": len(nowy.pola), "usuniete": sorted(znikaja)},
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki/" + kategoria + "/schemat",
                            status_code=status.HTTP_303_SEE_OTHER)


def _blad_schematu(kategoria: str, tresc: str) -> RedirectResponse:
    return RedirectResponse(
        "/slowniki/" + kategoria + "/schemat?blad=" + urllib.parse.quote(tresc),
        status_code=status.HTTP_303_SEE_OTHER)


@router.post("/slowniki/{kategoria}/schemat/wyczysc")
def wyczysc_pole_schematu(
    kategoria: str,
    request: Request,
    klucz: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Kasuje wartosci jednego pola - krok, ktory dopiero umozliwia jego usuniecie."""
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    if kategoria not in KATEGORIE_SLOWNIKA:
        raise HTTPException(status_code=404, detail="nieznana kategoria slownika")
    ile = slowniki.wyczysc_pole(db, ctx, kategoria, klucz)
    audit(db, ctx, action="slownik.pole_wyczyszczone", target=kategoria + ":" + klucz,
          detail={"wpisow": ile}, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki/" + kategoria + "/schemat",
                            status_code=status.HTTP_303_SEE_OTHER)


# --- pola wlasciwe dla rodzaju sprzetu --------------------------------------

@router.get("/rodzaje/{klucz}/pola", response_class=HTMLResponse)
def widok_pol_rodzaju(
    klucz: str,
    request: Request,
    blad: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Zestaw pol jednego rodzaju sprzetu - ten sam edytor co przy slownikach."""
    _require_write(ctx)
    wpis = rodzaje.wpis_rodzaju(db, ctx, klucz)
    if wpis is None:
        raise HTTPException(status_code=404, detail="nieznany rodzaj sprzetu")
    opis = rodzaje.schemat_pol(db, ctx, klucz)
    limity = {"pola": definicje_pol.MAKS_POL, "opcje": definicje_pol.MAKS_OPCJI}
    uzycia = {p.klucz: rodzaje.uzycie_pola(db, ctx, klucz, p.klucz) for p in opis.pola}
    wynik = render(
        request, "rodzaj_pola.html", user, ctx, db,
        rodzaj=wpis, klucz=klucz, schemat=opis, limity=limity, uzycia=uzycia,
        sprzetu=rodzaje.uzycie(db, ctx, klucz), blad=blad,
        definicja=json.dumps(opis.model_dump(exclude_none=True), ensure_ascii=False, indent=2),
        slownik_edytora=json.dumps({
            "typy": list(definicje_pol.TYPY),
            "formaty": {n: o["przyklad"] for n, o in definicje_pol.FORMATY.items()},
            "role": {},
            "cele": ["osoba"] + list(KATEGORIE_SLOWNIKA),
        }, ensure_ascii=False),
        uzycia_json=json.dumps(uzycia),
        limity_json=json.dumps(limity),
    )
    db.commit()
    return wynik


@router.post("/rodzaje/{klucz}/pola")
def zapisz_pola_rodzaju(
    klucz: str,
    request: Request,
    definicja: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Ta sama zasada co przy slownikach: pola z wartosciami sie nie usuwa."""
    from pydantic import ValidationError

    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    if rodzaje.wpis_rodzaju(db, ctx, klucz) is None:
        raise HTTPException(status_code=404, detail="nieznany rodzaj sprzetu")

    poprzedni = rodzaje.schemat_pol(db, ctx, klucz)
    try:
        tresc = json.loads(definicja)
        tresc["kategoria"] = definicje_pol.KATEGORIA_SPRZETU
        nowy = definicje_pol.Schemat.model_validate(tresc)
    except (json.JSONDecodeError, ValidationError, TypeError, AttributeError) as exc:
        db.rollback()
        return _blad_pol_rodzaju(klucz, str(exc)[:400])

    znikaja = {p.klucz for p in poprzedni.pola} - {p.klucz for p in nowy.pola}
    for pole in sorted(znikaja):
        ile = rodzaje.uzycie_pola(db, ctx, klucz, pole)
        if ile:
            db.rollback()
            return _blad_pol_rodzaju(
                klucz, "pole '" + pole + "' ma wartosci przy " + str(ile)
                + " sprzetach - najpierw je wyczysc")

    rodzaje.zapisz_schemat_pol(db, ctx, klucz, nowy)
    audit(db, ctx, action="rodzaj.pola", target=klucz,
          detail={"pola": len(nowy.pola), "usuniete": sorted(znikaja)},
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/rodzaje/" + klucz + "/pola",
                            status_code=status.HTTP_303_SEE_OTHER)


def _blad_pol_rodzaju(klucz: str, tresc: str) -> RedirectResponse:
    return RedirectResponse(
        "/rodzaje/" + klucz + "/pola?blad=" + urllib.parse.quote(tresc),
        status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rodzaje/{klucz}/pola/wyczysc")
def wyczysc_pole_rodzaju(
    klucz: str,
    request: Request,
    pole: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)
    if rodzaje.wpis_rodzaju(db, ctx, klucz) is None:
        raise HTTPException(status_code=404, detail="nieznany rodzaj sprzetu")
    ile = rodzaje.wyczysc_pole(db, ctx, klucz, pole)
    audit(db, ctx, action="rodzaj.pole_wyczyszczone", target=klucz + ":" + pole,
          detail={"sprzetu": ile}, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/rodzaje/" + klucz + "/pola",
                            status_code=status.HTTP_303_SEE_OTHER)


# --- wlasne konto -----------------------------------------------------------

@router.get("/konto", response_class=HTMLResponse)
def widok_konta(
    request: Request,
    zmienione: str = Query("", max_length=8),
    blad: str = Query("", max_length=200),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Wlasne konto - kazdy zalogowany, takze superadmin i audytor.

    Kontekst firmy jest tu opcjonalny: konto globalne zadnej firmy nie ma,
    a haslo zmienic musi. Dlatego widok nie zalezy od resolve_tenant.
    """
    ctx = None
    if user.tenant_id:
        tenant = db.get(Tenant, user.tenant_id)
        if tenant is not None:
            ctx = tenant_context_for(user, tenant)
    return render(
        request,
        "konto.html",
        user,
        ctx,
        db,
        zmienione=bool(zmienione),
        blad=blad,
        min_dlugosc_hasla=MIN_DLUGOSC_HASLA,
    )


@router.post("/konto/haslo")
def zmien_wlasne_haslo(
    request: Request,
    obecne: str = Form(...),
    nowe: str = Form(...),
    powtorzone: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Zmiana wlasnego hasla - po podaniu dotychczasowego.

    Stare haslo jest wymagane nawet przy zalogowanej sesji: bez tego
    pozostawiona bez opieki przegladarka wystarcza, zeby przejac konto
    na stale. Zaden administrator nie moze tego kroku ominac dla siebie.
    """
    verify_csrf(request, user, csrf_token)

    def odmow(komunikat: str) -> Response:
        return RedirectResponse(
            f"/konto?blad={quote(komunikat)}", status_code=status.HTTP_303_SEE_OTHER
        )

    if not verify_password(obecne, user.password_hash):
        audit(db, None, action="haslo.zmiana_odrzucona", target=user.email,
              ip=client_ip(request), actor=user.email)
        db.commit()
        return odmow("Dotychczasowe haslo jest nieprawidlowe.")
    if nowe != powtorzone:
        return odmow("Powtorzone haslo rozni sie od nowego.")
    if len(nowe) < MIN_DLUGOSC_HASLA:
        return odmow(f"Haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow.")
    if nowe == obecne:
        return odmow("Nowe haslo musi rozni sie od dotychczasowego.")

    user.session_version = PortalUser.session_version + 1
    user.password_hash = hash_password(nowe)
    audit(db, None, action="haslo.zmienione", target=user.email,
          ip=client_ip(request), actor=user.email)
    db.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(get_settings().session_cookie, path="/")
    return response


# --- tokeny rejestracyjne ---------------------------------------------------

@router.get("/pobierz", response_class=HTMLResponse)
def strona_pobierania(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Gotowe polecenia instalacyjne dla maszyn tej firmy.

    Tokenu nie da sie tu wypisac - w bazie sa wylacznie jego skroty, a wartosc
    jawna pokazujemy raz, przy wydaniu. Zamiast tego jest miejsce na wklejenie
    i odsylacz do strony tokenow.
    """
    return render(
        request, "pobierz.html", user, ctx, db,
        adres_serwera=download.adres_publiczny(request),
        paczka_linux=pakiet.opis(pakiet.katalog_paczki()),
        wydanie_windows=upgrades.wersja_dla_firmy(db, ctx.tenant_id, "windows"),
    )


@router.get("/tokens", response_class=HTMLResponse)
def token_list(
    request: Request,
    issued: str = Query("", max_length=200),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    tokens = db.execute(scoping.tokens_query(ctx)).scalars().all()
    agents = db.execute(
        select(AgentCredential, Asset)
        .join(Asset, Asset.id == AgentCredential.asset_id)
        .where(AgentCredential.tenant_id == ctx.tenant_id)
        .order_by(AgentCredential.created_at.desc())
        .limit(200)
    ).all()
    return render(
        request, "tokens.html", user, ctx, db, tokens=tokens, agents=agents, issued=issued
    )


@router.post("/tokens")
def token_create(
    request: Request,
    name: str = Form(...),
    expires_days: int = Form(0),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    token = generate_token("ent")
    expires_at = utcnow() + timedelta(days=expires_days) if expires_days > 0 else None
    db.add(
        EnrollmentToken(
            tenant_id=ctx.tenant_id,
            name=name.strip() or "token rejestracyjny",
            prefix=token.prefix,
            token_hash=token.token_hash,
            expires_at=expires_at,
            created_by=user.email,
        )
    )
    audit(
        db,
        ctx,
        action="token.created",
        target=name,
        detail={"prefix": token.prefix, "expires_days": expires_days},
        ip=client_ip(request),
    )
    db.commit()
    # Wartosc jawna pokazujemy jeden raz - w bazie jest tylko skrot.
    return RedirectResponse(
        f"/tokens?issued={token.plaintext}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/tokens/{token_id}/revoke")
def token_revoke(
    token_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    token = db.execute(
        scoping.tokens_query(ctx).where(EnrollmentToken.id == token_id)
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="nie znaleziono tokenu")
    token.revoked_at = utcnow()
    audit(
        db, ctx, action="token.revoked", target=token.name, detail={"prefix": token.prefix},
        ip=client_ip(request),
    )
    db.commit()
    return RedirectResponse("/tokens", status_code=status.HTTP_303_SEE_OTHER)


# --- audyt i eksport --------------------------------------------------------

@router.get("/audit", response_class=HTMLResponse)
def audit_view(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    entries = db.execute(
        select(AuditLog)
        .where(or_(AuditLog.tenant_id == ctx.tenant_id, AuditLog.tenant_id.is_(None)))
        .order_by(AuditLog.created_at.desc())
        .limit(200)
    ).scalars().all()
    return render(request, "audit.html", user, ctx, db, entries=entries)


@router.get("/export/assets.json")
def export_assets(
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    assets = db.execute(scoping.assets_query(ctx).order_by(Asset.hostname)).scalars().all()
    data = [
        {
            "id": a.id,
            "hostname": a.hostname,
            "fqdn": a.fqdn,
            "os_family": a.os_family,
            "os_name": a.os_name,
            "os_version": a.os_version,
            "manufacturer": a.manufacturer,
            "model": a.model,
            "serial_number": a.serial_number,
            "primary_ip": a.primary_ip,
            "role_label": a.role_label,
            "owner": {"name": a.owner.full_name, "email": a.owner.email} if a.owner else None,
            "first_seen": naive_utc(a.first_seen).isoformat() if a.first_seen else None,
            "last_seen": naive_utc(a.last_seen).isoformat() if a.last_seen else None,
            "facts": a.facts,
        }
        for a in assets
    ]
    return JSONResponse(
        {"tenant": ctx.tenant_slug, "generated_at": utcnow().isoformat(), "assets": data}
    )


__all__ = ["router", "LoginRequired"]
