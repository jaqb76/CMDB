"""Panel glownego administratora: firmy, konta, tokeny i wersje agenta.

Odrebny router z jedna zaleznoscia wejsciowa (require_superadmin), zamiast
sprawdzania roli w kazdym widoku - zapomniana kontrola w jednym miejscu
otwieralaby caly panel globalny.

Widoki firmowe (api/ui.py) pokazuja zawsze jedna firme i przechodza przez
services/scoping. Tutaj patrzymy na wszystkie naraz, wiec zapytania swiadomie
tego filtra nie maja - kazde takie miejsce jest w tym pliku i tylko tutaj.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    LIFECYCLE_AKTYWNY,
    AgentRelease,
    Asset,
    AuditLog,
    EnrollmentToken,
    GlobalAgentTarget,
    Owner,
    PortalUser,
    Tenant,
    TenantAgentTarget,
    utcnow,
)
from ..security import check_csrf_token, generate_token, hash_password, issue_csrf_token
from ..services.auth import client_ip, require_superadmin
from ..services.scoping import audit
from .ui import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
WERSJA_RE = re.compile(r"^[0-9][0-9a-zA-Z._-]{0,31}$")

# Agent budowany jest osobno dla kazdego systemu. Sygnatura pliku pozwala
# odrzucic pomylke juz przy wgrywaniu, a nie dopiero na maszynie klienta.
# Bajt 0x7F zapisujemy przez fromhex, bo doslownie w zrodle jest niewidoczny.
SYSTEMY = {
    "windows": {"etykieta": "Windows", "sygnatura": b"MZ", "opis": "program Windows (.exe)"},
    "linux": {"etykieta": "Linux", "sygnatura": bytes.fromhex("7f") + b"ELF", "opis": "program ELF"},
}
MIN_DLUGOSC_HASLA = 12


def render_admin(request: Request, template: str, user: PortalUser, strona: str, **extra):
    payload = {
        "request": request,
        "user": user,
        "strona": strona,
        "csrf_token": issue_csrf_token(user.id),
        **extra,
    }
    return templates.TemplateResponse(request, template, payload)


def sprawdz_csrf(user: PortalUser, token: str | None) -> None:
    if not token or not check_csrf_token(token, user.id):
        raise HTTPException(status_code=403, detail="nieprawidlowy token CSRF")


def znajdz_firme(db: Session, tenant_id: str) -> Tenant:
    firma = db.get(Tenant, tenant_id)
    if firma is None:
        raise HTTPException(status_code=404, detail="nie znaleziono firmy")
    return firma


def katalog_wersji() -> Path:
    katalog = Path(get_settings().release_dir)
    katalog.mkdir(parents=True, exist_ok=True)
    return katalog


def _wydania(db: Session):
    """Wszystkie wydania, mapa po identyfikatorze i podzial na systemy."""
    wszystkie = db.execute(
        select(AgentRelease).order_by(AgentRelease.os_family, AgentRelease.version.desc())
    ).scalars().all()
    wedlug_systemu: dict[str, list[AgentRelease]] = {}
    for wydanie in wszystkie:
        wedlug_systemu.setdefault(wydanie.os_family, []).append(wydanie)
    return wszystkie, {w.id: w for w in wszystkie}, wedlug_systemu


def _statystyki_agentow(db: Session):
    """Per firma: rozklad wersji, liczba aktywnych i nieaktywnych agentow."""
    prog = utcnow() - timedelta(hours=get_settings().stale_after_hours)

    # Jedno zapytanie na komplet. Petla z zapytaniem na kazda firme bylaby
    # zauwazalna juz przy kilkudziesieciu firmach.
    wiersze = db.execute(
        select(
            Asset.tenant_id,
            Asset.os_family,
            Asset.agent_version,
            func.count(Asset.id),
            func.sum(case((Asset.last_seen >= prog, 1), else_=0)),
        )
        .where(Asset.lifecycle == LIFECYCLE_AKTYWNY)
        .group_by(Asset.tenant_id, Asset.os_family, Asset.agent_version)
    ).all()

    rozklad: dict[str, list[dict]] = {}
    aktywne: dict[str, int] = {}
    nieaktywne: dict[str, int] = {}
    for tenant_id, system, wersja, ile, ilu_aktywnych in wiersze:
        ilu_aktywnych = int(ilu_aktywnych or 0)
        rozklad.setdefault(tenant_id, []).append(
            {
                "os_family": system or "nieznany",
                "wersja": wersja or "nieznana",
                "ile": ile,
                "aktywne": ilu_aktywnych,
                "nieaktywne": ile - ilu_aktywnych,
            }
        )
        aktywne[tenant_id] = aktywne.get(tenant_id, 0) + ilu_aktywnych
        nieaktywne[tenant_id] = nieaktywne.get(tenant_id, 0) + (ile - ilu_aktywnych)
    for lista in rozklad.values():
        lista.sort(key=lambda p: (p["os_family"], p["wersja"]))
    return rozklad, aktywne, nieaktywne


def _oficjalne(db: Session, wersje: dict) -> dict:
    return {
        cel.os_family: wersje.get(cel.release_id)
        for cel in db.execute(select(GlobalAgentTarget)).scalars()
    }


# --- przeglad ---------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def przeglad(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    rozklad, aktywne, nieaktywne = _statystyki_agentow(db)
    _, wersje, _ = _wydania(db)

    cele: dict[str, dict[str, AgentRelease]] = {}
    for cel in db.execute(select(TenantAgentTarget)).scalars():
        wydanie = wersje.get(cel.release_id)
        if wydanie is not None:
            cele.setdefault(cel.tenant_id, {})[cel.os_family] = wydanie

    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    wiersze = [
        {
            "firma": firma,
            "aktywne": aktywne.get(firma.id, 0),
            "nieaktywne": nieaktywne.get(firma.id, 0),
            "agenci": rozklad.get(firma.id, []),
            "cele": cele.get(firma.id, {}),
        }
        for firma in firmy
    ]

    return render_admin(
        request, "admin_przeglad.html", user, "przeglad",
        wiersze=wiersze,
        podsumowanie={
            "firmy": len(firmy),
            "firmy_aktywne": sum(1 for f in firmy if f.is_active),
            "aktywne": sum(aktywne.values()),
            "nieaktywne": sum(nieaktywne.values()),
        },
        oficjalne=_oficjalne(db, wersje),
        systemy=SYSTEMY,
        stale_after_hours=get_settings().stale_after_hours,
    )


# --- firmy i konta ----------------------------------------------------------

@router.get("/firmy", response_class=HTMLResponse)
def widok_firm(
    request: Request,
    wydany_token: str = "",
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    opiekunowie = dict(
        db.execute(select(Owner.tenant_id, func.count(Owner.id)).group_by(Owner.tenant_id)).all()
    )
    tokeny = dict(
        db.execute(
            select(EnrollmentToken.tenant_id, func.count(EnrollmentToken.id))
            .where(EnrollmentToken.revoked_at.is_(None))
            .group_by(EnrollmentToken.tenant_id)
        ).all()
    )
    maszyny = dict(
        db.execute(
            select(Asset.tenant_id, func.count(Asset.id))
            .where(Asset.lifecycle == LIFECYCLE_AKTYWNY)
            .group_by(Asset.tenant_id)
        ).all()
    )
    konta_firm: dict[str, list[PortalUser]] = {}
    for konto in db.execute(
        select(PortalUser).where(PortalUser.tenant_id.is_not(None)).order_by(PortalUser.email)
    ).scalars():
        konta_firm.setdefault(konto.tenant_id, []).append(konto)

    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    return render_admin(
        request, "admin_firmy.html", user, "firmy",
        firmy=firmy,
        konta_firm=konta_firm,
        opiekunowie=opiekunowie,
        tokeny=tokeny,
        maszyny=maszyny,
        wydany_token=wydany_token,
    )


@router.post("/tenants")
def utworz_firme(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    identyfikator = slug.strip().lower()
    if not SLUG_RE.match(identyfikator):
        raise HTTPException(
            status_code=400,
            detail="identyfikator moze zawierac male litery, cyfry i myslnik (2-63 znaki)",
        )
    if db.execute(select(Tenant).where(Tenant.slug == identyfikator)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="firma o tym identyfikatorze juz istnieje")

    firma = Tenant(name=name.strip(), slug=identyfikator)
    db.add(firma)
    db.flush()
    audit(db, None, action="tenant.created", target=identyfikator,
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s utworzyl firme %s", user.email, identyfikator)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/active")
def przelacz_firme(
    tenant_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)
    firma.is_active = not firma.is_active
    audit(db, None, action="tenant.active_changed", target=firma.slug,
          detail={"is_active": firma.is_active}, ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/users")
def utworz_konto(
    tenant_id: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("admin"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)
    adres = email.strip().lower()

    if len(password) < MIN_DLUGOSC_HASLA:
        raise HTTPException(
            status_code=400,
            detail=f"haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow",
        )
    if role not in {"admin", "viewer"}:
        raise HTTPException(status_code=400, detail="nieznana rola")
    if db.execute(select(PortalUser).where(PortalUser.email == adres)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="konto o tym adresie juz istnieje")

    db.add(
        PortalUser(
            tenant_id=firma.id,
            email=adres,
            full_name=full_name.strip() or None,
            password_hash=hash_password(password),
            role=role,
        )
    )
    audit(db, None, action="user.created", target=adres,
          detail={"tenant": firma.slug, "role": role}, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s utworzyl konto %s w firmie %s", user.email, adres, firma.slug)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/active")
def przelacz_konto(
    user_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    konto = db.get(PortalUser, user_id)
    if konto is None or konto.is_superadmin:
        raise HTTPException(status_code=404, detail="nie znaleziono konta")
    konto.is_active = not konto.is_active
    audit(db, None, action="user.active_changed", target=konto.email,
          detail={"is_active": konto.is_active}, ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/tokens")
def wydaj_token(
    tenant_id: str,
    request: Request,
    name: str = Form("token rejestracyjny"),
    expires_days: int = Form(0),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)

    token = generate_token("ent")
    db.add(
        EnrollmentToken(
            tenant_id=firma.id,
            name=name.strip() or "token rejestracyjny",
            prefix=token.prefix,
            token_hash=token.token_hash,
            expires_at=utcnow() + timedelta(days=expires_days) if expires_days > 0 else None,
            created_by=user.email,
        )
    )
    audit(db, None, action="token.created", target=firma.slug,
          detail={"prefix": token.prefix}, ip=client_ip(request), actor=user.email)
    db.commit()
    # Wartosc jawna pokazujemy raz - w bazie zostaje wylacznie skrot.
    return RedirectResponse(
        f"/admin/firmy?wydany_token={token.plaintext}", status_code=status.HTTP_303_SEE_OTHER
    )


# --- wersje agenta ----------------------------------------------------------

@router.get("/wersje", response_class=HTMLResponse)
def widok_wersji(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    wszystkie, wersje, wedlug_systemu = _wydania(db)

    cele: dict[str, dict[str, AgentRelease]] = {}
    for cel in db.execute(select(TenantAgentTarget)).scalars():
        wydanie = wersje.get(cel.release_id)
        if wydanie is not None:
            cele.setdefault(cel.tenant_id, {})[cel.os_family] = wydanie

    rozklad, _, _ = _statystyki_agentow(db)
    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()

    # Systemy faktycznie obecne u kazdej firmy - nie ma sensu proponowac celu
    # dla Linuksa firmie, ktora ma same maszyny z Windows.
    systemy_firmy = {
        firma.id: sorted({p["os_family"] for p in rozklad.get(firma.id, [])})
        for firma in firmy
    }

    return render_admin(
        request, "admin_wersje.html", user, "wersje",
        wydania=wszystkie,
        wydania_wg_systemu=wedlug_systemu,
        oficjalne=_oficjalne(db, wersje),
        cele=cele,
        firmy=firmy,
        systemy_firmy=systemy_firmy,
        systemy=SYSTEMY,
    )


@router.post("/releases")
async def wgraj_wersje(
    request: Request,
    version: str = Form(...),
    os_family: str = Form("windows"),
    notes: str = Form(""),
    plik: UploadFile = File(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Wgrywa plik agenta dla wskazanego systemu i liczy jego skrot.

    Skrot jest sercem calego mechanizmu: agent porownuje go po pobraniu
    i odmawia podmiany, gdy sie nie zgadza. Liczymy go strumieniowo, zeby
    trzydziestomegabajtowy plik nie ladowal w calosci do pamieci.
    """
    sprawdz_csrf(user, csrf_token)
    settings = get_settings()

    system = os_family.strip().lower()
    if system not in SYSTEMY:
        raise HTTPException(status_code=400, detail="nieznany system operacyjny")

    numer = version.strip()
    if not WERSJA_RE.match(numer):
        raise HTTPException(status_code=400, detail="niepoprawny numer wersji")
    if db.execute(
        select(AgentRelease).where(
            AgentRelease.version == numer, AgentRelease.os_family == system
        )
    ).scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=400,
            detail=f"wersja {numer} dla {SYSTEMY[system]['etykieta']} juz istnieje",
        )

    katalog = katalog_wersji()
    tymczasowy = katalog / f".wgrywanie-{utcnow().timestamp()}"
    skrot = hashlib.sha256()
    rozmiar = 0

    try:
        with tymczasowy.open("wb") as wyjscie:
            while fragment := await plik.read(1024 * 1024):
                rozmiar += len(fragment)
                if rozmiar > settings.max_release_bytes:
                    raise HTTPException(status_code=413, detail="plik przekracza dozwolony rozmiar")
                skrot.update(fragment)
                wyjscie.write(fragment)

        if rozmiar == 0:
            raise HTTPException(status_code=400, detail="pusty plik")

        # Sygnatura odrzuca pomylke juz tutaj, a nie dopiero na maszynie klienta,
        # gdzie plik po prostu nie chcialby sie uruchomic.
        oczekiwana = SYSTEMY[system]["sygnatura"]
        with tymczasowy.open("rb") as sprawdzany:
            if sprawdzany.read(len(oczekiwana)) != oczekiwana:
                raise HTTPException(
                    status_code=400,
                    detail=f"plik nie jest programem dla {SYSTEMY[system]['etykieta']} "
                           f"({SYSTEMY[system]['opis']})",
                )

        odcisk = skrot.hexdigest()
        nazwa_w_magazynie = f"{system}-{odcisk}.bin"
        tymczasowy.replace(katalog / nazwa_w_magazynie)
    except Exception:
        tymczasowy.unlink(missing_ok=True)
        raise

    db.add(
        AgentRelease(
            version=numer,
            os_family=system,
            filename=plik.filename or f"cmdb-agent-{numer}",
            storage_name=nazwa_w_magazynie,
            sha256=odcisk,
            size_bytes=rozmiar,
            notes=notes.strip() or None,
            created_by=user.email,
        )
    )
    audit(db, None, action="release.uploaded", target=f"{numer} ({system})",
          detail={"sha256": odcisk, "size": rozmiar, "os_family": system},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s wgral agenta %s dla %s (%s)", user.email, numer, system, odcisk[:16])
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/releases/{release_id}/oficjalna")
def ustaw_oficjalna(
    release_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Oznacza wersje jako aktywna dla jej systemu.

    Firmy bez wlasnego ustawienia korzystaja wlasnie z niej. Tabela ma
    UNIQUE(os_family), wiec dwie wersje tego samego systemu nie moga byc
    oficjalne naraz - podmieniamy wskazanie, zamiast dokladac drugie.
    """
    sprawdz_csrf(user, csrf_token)
    wydanie = db.get(AgentRelease, release_id)
    if wydanie is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wersji")

    cel = db.execute(
        select(GlobalAgentTarget).where(GlobalAgentTarget.os_family == wydanie.os_family)
    ).scalar_one_or_none()
    if cel is None:
        db.add(
            GlobalAgentTarget(
                os_family=wydanie.os_family, release_id=wydanie.id, updated_by=user.email
            )
        )
    else:
        cel.release_id = wydanie.id
        cel.updated_at = utcnow()
        cel.updated_by = user.email

    audit(db, None, action="release.official", target=f"{wydanie.version} ({wydanie.os_family})",
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s uczynil %s oficjalna dla %s",
             user.email, wydanie.version, wydanie.os_family)
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/releases/{release_id}/delete")
def usun_wersje(
    release_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    wydanie = db.get(AgentRelease, release_id)
    if wydanie is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wersji")

    uzywana = (
        db.execute(
            select(func.count(GlobalAgentTarget.id)).where(
                GlobalAgentTarget.release_id == release_id
            )
        ).scalar_one()
        + db.execute(
            select(func.count(TenantAgentTarget.id)).where(
                TenantAgentTarget.release_id == release_id
            )
        ).scalar_one()
        + db.execute(
            select(func.count(Asset.id)).where(Asset.target_release_id == release_id)
        ).scalar_one()
    )
    if uzywana:
        raise HTTPException(
            status_code=400,
            detail="wersja jest gdzies ustawiona jako docelowa - najpierw zmien cel",
        )

    (katalog_wersji() / wydanie.storage_name).unlink(missing_ok=True)
    db.delete(wydanie)
    audit(db, None, action="release.deleted", target=f"{wydanie.version} ({wydanie.os_family})",
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


# --- zlecanie aktualizacji --------------------------------------------------

@router.get("/tenants/{tenant_id}/upgrade", response_class=HTMLResponse)
def widok_aktualizacji(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    firma = znajdz_firme(db, tenant_id)
    maszyny = db.execute(
        select(Asset)
        .where(Asset.tenant_id == firma.id, Asset.lifecycle == LIFECYCLE_AKTYWNY)
        .order_by(Asset.os_family, Asset.hostname)
    ).scalars().all()

    wszystkie, wersje, wedlug_systemu = _wydania(db)
    cele = {
        cel.os_family: wersje.get(cel.release_id)
        for cel in db.execute(
            select(TenantAgentTarget).where(TenantAgentTarget.tenant_id == firma.id)
        ).scalars()
    }

    obecne: list[str] = []
    for maszyna in maszyny:
        system = (maszyna.os_family or "nieznany").lower()
        if system not in obecne:
            obecne.append(system)

    return render_admin(
        request, "admin_upgrade.html", user, "wersje",
        firma=firma,
        maszyny=maszyny,
        wersje=wersje,
        wydania=wszystkie,
        wydania_wg_systemu=wedlug_systemu,
        cele=cele,
        oficjalne=_oficjalne(db, wersje),
        obecne_systemy=obecne,
        systemy=SYSTEMY,
    )


@router.post("/tenants/{tenant_id}/upgrade")
async def zlec_aktualizacje(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Ustawia wersje docelowa dla systemu w calej firmie albo dla wybranych maszyn.

    Nie wysylamy niczego na maszyny - to agent przy swoim cyklicznym przebiegu
    dowiaduje sie, ze oczekiwana jest inna wersja, i sam ja pobiera. Serwer
    nigdy nie inicjuje polaczenia do agenta.
    """
    formularz = await request.form()
    sprawdz_csrf(user, formularz.get("csrf_token"))

    firma = znajdz_firme(db, tenant_id)
    zakres = formularz.get("zakres", "wybrane")
    release_id = (formularz.get("release_id") or "").strip()
    powrot = formularz.get("powrot") or f"/admin/tenants/{firma.id}/upgrade"

    wydanie = None
    if release_id:
        wydanie = db.get(AgentRelease, release_id)
        if wydanie is None:
            raise HTTPException(status_code=400, detail="nie znaleziono wskazanej wersji")

    if zakres == "firma":
        system = (formularz.get("os_family") or "").strip().lower()
        if system not in SYSTEMY:
            raise HTTPException(status_code=400, detail="nieznany system operacyjny")
        if wydanie is not None and wydanie.os_family != system:
            raise HTTPException(
                status_code=400,
                detail=f"wersja {wydanie.version} jest dla {wydanie.os_family}, "
                       f"a cel dotyczy {system}",
            )
        objete = _ustaw_cel_firmy(db, firma, system, wydanie, user.email)
    else:
        objete = _ustaw_cel_maszyn(db, firma, formularz.getlist("asset_id"), wydanie)

    audit(db, None, action="agent.upgrade_requested", target=firma.slug,
          detail={"wersja": wydanie.version if wydanie else None, "zakres": objete},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info(
        "superadmin %s zlecil wersje %s dla %s w firmie %s",
        user.email, wydanie.version if wydanie else "(brak)", objete, firma.slug,
    )
    return RedirectResponse(powrot, status_code=status.HTTP_303_SEE_OTHER)


def _ustaw_cel_firmy(
    db: Session, firma: Tenant, system: str, wydanie: AgentRelease | None, kto: str
) -> str:
    """Cel dla wszystkich maszyn firmy dzialajacych na wskazanym systemie."""
    cel = db.execute(
        select(TenantAgentTarget).where(
            TenantAgentTarget.tenant_id == firma.id,
            TenantAgentTarget.os_family == system,
        )
    ).scalar_one_or_none()

    if wydanie is None:
        # Brak wlasnego ustawienia znaczy "korzystaj z wersji oficjalnej".
        if cel is not None:
            db.delete(cel)
    elif cel is None:
        db.add(
            TenantAgentTarget(
                tenant_id=firma.id, os_family=system, release_id=wydanie.id, updated_by=kto
            )
        )
    else:
        cel.release_id = wydanie.id
        cel.updated_at = utcnow()
        cel.updated_by = kto

    # Ustawienia poszczegolnych maszyn tego systemu kasujemy, zeby nie
    # przeslanialy decyzji podjetej dla calej firmy.
    maszyny = db.execute(
        select(Asset).where(Asset.tenant_id == firma.id, Asset.os_family == system)
    ).scalars().all()
    for maszyna in maszyny:
        maszyna.target_release_id = None
        maszyna.upgrade_status = "zlecona" if wydanie else None
        maszyna.upgrade_detail = None
        maszyna.upgrade_updated_at = utcnow()
    etykieta = SYSTEMY.get(system, {}).get("etykieta", system)
    return f"wszystkie maszyny {etykieta} ({len(maszyny)})"


def _ustaw_cel_maszyn(
    db: Session, firma: Tenant, wybrane: list[str], wydanie: AgentRelease | None
) -> str:
    if not wybrane:
        raise HTTPException(status_code=400, detail="nie wskazano zadnej maszyny")

    maszyny = db.execute(
        select(Asset).where(Asset.tenant_id == firma.id, Asset.id.in_(wybrane))
    ).scalars().all()
    if len(maszyny) != len(set(wybrane)):
        raise HTTPException(status_code=400, detail="maszyna spoza tej firmy")

    if wydanie is not None:
        niezgodne = [m.hostname for m in maszyny if (m.os_family or "") != wydanie.os_family]
        if niezgodne:
            raise HTTPException(
                status_code=400,
                detail=f"wersja jest dla {wydanie.os_family}, a wskazano maszyny "
                       f"innego systemu: {', '.join(niezgodne[:5])}",
            )

    for maszyna in maszyny:
        maszyna.target_release_id = wydanie.id if wydanie else None
        maszyna.upgrade_status = "zlecona" if wydanie else None
        maszyna.upgrade_detail = None
        maszyna.upgrade_updated_at = utcnow()
    return f"{len(maszyny)} maszyn"


# --- audyt globalny ---------------------------------------------------------

@router.get("/audyt", response_class=HTMLResponse)
def widok_audytu(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    wpisy = db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(300)
    ).scalars().all()
    nazwy_firm = {f.id: f.name for f in db.execute(select(Tenant)).scalars()}
    return render_admin(
        request, "admin_audyt.html", user, "audyt", wpisy=wpisy, nazwy_firm=nazwy_firm
    )
