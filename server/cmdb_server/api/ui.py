"""Panel WWW: logowanie, lista maszyn, szczegoly, opiekunowie, tokeny.

Widoki renderowane po stronie serwera (Jinja2) - bez SPA i bez publicznego
API przegladarkowego, dzieki czemu powierzchnia ataku jest minimalna.
Kazde zapytanie o dane przechodzi przez services.scoping - nie ma sciezki,
ktora czytalaby maszyny bez filtra tenant_id.
"""
from __future__ import annotations

import hashlib
import json
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
    Owner,
    PortalUser,
    Tenant,
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
    cve, duplicates, logowanie, pakiet, scoping, slowniki, upgrades, ustawienia,
)
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


def _fmt_dt(value) -> str:
    if _missing(value):
        return "-"
    value = naive_utc(_na_date(value))
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


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
        sign_session({"uid": user.id}),
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
    by_os = db.execute(
        select(Asset.os_family, func.count(Asset.id))
        .where(Asset.tenant_id == ctx.tenant_id)
        .group_by(Asset.os_family)
    ).all()

    recent = db.execute(
        scoping.assets_query(ctx).order_by(Asset.last_seen.desc()).limit(10)
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
        recent=recent,
        changed=changed,
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
                Asset.lokalizacja.ilike(pattern),
            )
        )
    if os_family:
        stmt = stmt.where(Asset.os_family == os_family)
    if typ in TYPY_SPRZETU:
        stmt = stmt.where(Asset.typ == typ)
    if lokalizacja:
        stmt = stmt.where(Asset.lokalizacja == lokalizacja)
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
        typy=TYPY_SPRZETU,
        lokalizacje=slowniki.wartosci(db, ctx, "lokalizacja"),
        filters={"q": q, "os_family": os_family, "owner": owner,
                 "state": state, "lifecycle": lifecycle,
                 "typ": typ, "lokalizacja": lokalizacja},
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
        typy=TYPY_SPRZETU,
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
    lokalizacja: str = Form(""),
    rola: str = Form(""),
    owner_id: str = Form(""),
    uzytkownik_id: str = Form(""),
    dostawca: str = Form(""),
    uwagi: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    if typ not in TYPY_SPRZETU:
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
    sprzet = Asset(
        tenant_id=ctx.tenant_id,
        machine_id=f"reczne:{uuid4()}",
        typ=typ,
        zrodlo=ZRODLO_RECZNE,
        owner_id=osoba(owner_id),
        uzytkownik_id=osoba(uzytkownik_id),
        lokalizacja=slowniki.zapewnij(db, ctx, "lokalizacja", lokalizacja),
        vendor=slowniki.zapewnij(db, ctx, "dostawca", dostawca),
        **pola,
    )
    db.add(sprzet)
    db.flush()
    audit(db, ctx, action="asset.dodany_recznie", target=sprzet.hostname,
          detail={"typ": typ}, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{sprzet.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assets/{asset_id}/dane")
def zapisz_dane_sprzetu(
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
    if typ not in TYPY_SPRZETU:
        raise HTTPException(status_code=400, detail="nieznany rodzaj sprzetu")

    pola = _dane_recznego_sprzetu(
        {"nazwa": nazwa, "producent": producent, "model": model,
         "numer_seryjny": numer_seryjny, "ip": ip, "rola": rola, "uwagi": uwagi}
    )
    if not pola["hostname"]:
        raise HTTPException(status_code=400, detail="nazwa jest wymagana")
    for klucz, wartosc in pola.items():
        setattr(sprzet, klucz, wartosc)
    sprzet.typ = typ

    audit(db, ctx, action="asset.dane_zmienione", target=sprzet.hostname,
          ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/assets/{sprzet.id}", status_code=status.HTTP_303_SEE_OTHER)


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
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    asset = scoping.get_asset(db, ctx, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="nie znaleziono maszyny")

    if snapshot:
        current = scoping.get_snapshot(db, ctx, snapshot)
        if current is None or current.asset_id != asset.id:
            raise HTTPException(status_code=404, detail="nie znaleziono snapshotu")
    else:
        current = scoping.latest_snapshot(db, ctx, asset.id)

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

    zmiany = db.execute(
        select(AssetChange)
        .where(AssetChange.tenant_id == ctx.tenant_id, AssetChange.asset_id == asset.id)
        .order_by(AssetChange.occurred_at.desc())
        .limit(200)
    ).scalars().all()

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
        typy=TYPY_SPRZETU,
        podpowiedzi=slowniki.podpowiedzi(db, ctx),
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
    lokalizacja: str = Form(""),
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

    def osoba(identyfikator: str) -> Owner | None:
        if not identyfikator:
            return None
        znaleziona = scoping.get_owner(db, ctx, identyfikator)
        if znaleziona is None:
            raise HTTPException(status_code=400, detail="osoba spoza tej firmy")
        return znaleziona

    new_owner = osoba(owner_id)
    new_user = osoba(uzytkownik_id)

    previous = asset.owner.full_name if asset.owner else None
    poprzedni_uzytkownik = asset.uzytkownik.full_name if asset.uzytkownik else None
    asset.owner_id = new_owner.id if new_owner else None
    asset.uzytkownik_id = new_user.id if new_user else None
    asset.role_label = role_label.strip() or None
    # Nowa lokalizacja od razu trafia do slownika firmy - inaczej kazdy
    # wpisywalby ja po swojemu i podpowiedzi nigdy by nie powstaly.
    asset.lokalizacja = slowniki.zapewnij(db, ctx, "lokalizacja", lokalizacja)

    audit(
        db,
        ctx,
        action="asset.owner_changed",
        target=asset.hostname,
        detail={
            "from": previous,
            "to": new_owner.full_name if new_owner else None,
            "uzytkownik_z": poprzedni_uzytkownik,
            "uzytkownik_na": new_user.full_name if new_user else None,
            "lokalizacja": asset.lokalizacja,
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
        else scoping.latest_snapshot(db, ctx, asset.id)
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
    stmt = (
        select(AssetChange, Asset)
        .join(Asset, Asset.id == AssetChange.asset_id)
        .where(AssetChange.tenant_id == ctx.tenant_id, AssetChange.occurred_at >= od)
    )
    if kategoria:
        stmt = stmt.where(AssetChange.category == kategoria)

    wiersze = db.execute(stmt.order_by(AssetChange.occurred_at.desc()).limit(500)).all()

    kategorie = db.execute(
        select(AssetChange.category, func.count(AssetChange.id))
        .where(AssetChange.tenant_id == ctx.tenant_id, AssetChange.occurred_at >= od)
        .group_by(AssetChange.category)
        .order_by(func.count(AssetChange.id).desc())
    ).all()

    return render(
        request, "zmiany.html", user, ctx, db,
        wiersze=wiersze,
        kategorie=kategorie,
        filtry={"kategoria": kategoria, "dni": dni},
    )


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
    )


# --- opiekunowie ------------------------------------------------------------

@router.get("/owners", response_class=HTMLResponse)
def owner_list(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    owners = db.execute(scoping.owners_query(ctx)).scalars().all()
    counts = dict(
        db.execute(
            select(Asset.owner_id, func.count(Asset.id))
            .where(Asset.tenant_id == ctx.tenant_id)
            .group_by(Asset.owner_id)
        ).all()
    )
    return render(
        request, "owners.html", user, ctx, db,
        owners=owners, counts=counts,
        dzialy=slowniki.wartosci(db, ctx, "dzial"),
    )


@router.post("/owners")
def owner_create(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    department: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    email_normalized = email.strip().lower()
    existing = db.execute(
        scoping.owners_query(ctx).where(Owner.email == email_normalized)
    ).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse("/owners?error=duplikat", status_code=status.HTTP_303_SEE_OTHER)

    owner = Owner(
        tenant_id=ctx.tenant_id,
        full_name=full_name.strip(),
        email=email_normalized,
        phone=phone.strip() or None,
        # Nowy dzial od razu zasila slownik firmy - dzieki temu przy nastepnej
        # osobie podpowie sie ta sama nazwa, zamiast powstac jej drugi wariant.
        department=slowniki.zapewnij(db, ctx, "dzial", department),
        notes=notes.strip() or None,
    )
    db.add(owner)
    audit(db, ctx, action="owner.created", target=owner.email, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/owners", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/owners/{owner_id}/delete")
def owner_delete(
    owner_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    owner = scoping.get_owner(db, ctx, owner_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="nie znaleziono opiekuna")
    db.delete(owner)  # maszyny zostaja, owner_id -> NULL
    audit(db, ctx, action="owner.deleted", target=owner.email, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/owners", status_code=status.HTTP_303_SEE_OTHER)


# --- slowniki firmowe -------------------------------------------------------

@router.get("/slowniki", response_class=HTMLResponse)
def widok_slownikow(
    request: Request,
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Dzialy, lokalizacje i dostawcy uzywane w tej firmie.

    Slownik zapelnia sie sam - kazda nowa wartosc wpisana w formularzu trafia
    tu od razu. Ta strona sluzy do posprzatania go: usuniecia literowki albo
    dopisania wartosci z wyprzedzeniem, zanim pojawi sie pierwszy sprzet.
    """
    return render(
        request,
        "slowniki.html",
        user,
        ctx,
        db,
        kategorie=KATEGORIE_SLOWNIKA,
        wpisy=slowniki.wpisy(db, ctx),
        uzycia=uzycia_slownika(db, ctx),
    )


def uzycia_slownika(db: Session, ctx: TenantContext) -> dict[str, dict[str, int]]:
    """Ile razy kazda wartosc jest faktycznie uzyta.

    Bez tej liczby usuwanie ze slownika byloby zgadywaniem: nie widac, czy
    kasuje sie literowke uzyta raz, czy nazwe lokalizacji polowy floty.
    """
    def zlicz(kolumna) -> dict[str, int]:
        wiersze = db.execute(
            select(kolumna, func.count(Asset.id))
            .where(Asset.tenant_id == ctx.tenant_id, kolumna.is_not(None))
            .group_by(kolumna)
        ).all()
        return {wartosc: liczba for wartosc, liczba in wiersze}

    dzialy = db.execute(
        select(Owner.department, func.count(Owner.id))
        .where(Owner.tenant_id == ctx.tenant_id, Owner.department.is_not(None))
        .group_by(Owner.department)
    ).all()
    return {
        "lokalizacja": zlicz(Asset.lokalizacja),
        "dostawca": zlicz(Asset.vendor),
        "dzial": {wartosc: liczba for wartosc, liczba in dzialy},
    }


@router.post("/slowniki")
def dodaj_do_slownika(
    request: Request,
    kategoria: str = Form(...),
    wartosc: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    verify_csrf(request, user, csrf_token)
    _require_write(ctx)

    if kategoria not in KATEGORIE_SLOWNIKA:
        raise HTTPException(status_code=400, detail="nieznana kategoria slownika")
    dodana = slowniki.zapewnij(db, ctx, kategoria, wartosc)
    if dodana is None:
        raise HTTPException(status_code=400, detail="wartosc nie moze byc pusta")
    audit(db, ctx, action="slownik.dodany", target=f"{kategoria}:{dodana}",
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki", status_code=status.HTTP_303_SEE_OTHER)


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
    audit(db, ctx, action="slownik.usuniety", target=f"{wpis.kategoria}:{wpis.wartosc}",
          ip=client_ip(request))
    db.commit()
    return RedirectResponse("/slowniki", status_code=status.HTTP_303_SEE_OTHER)


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

    user.password_hash = hash_password(nowe)
    audit(db, None, action="haslo.zmienione", target=user.email,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/konto?zmienione=1", status_code=status.HTTP_303_SEE_OTHER)


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
