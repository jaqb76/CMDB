"""Panel superadmina: firmy, konta, tokeny i wersje agenta.

Odrebny router z jedna zaleznoscia wejsciowa (require_superadmin), zamiast
sprawdzania roli w kazdym widoku - zapomniana kontrola w jednym miejscu
otwieralaby caly panel globalny.

Widoki firmowe (api/ui.py) pokazuja zawsze jedna firme. Tutaj patrzymy na
wszystkie naraz, wiec zapytania swiadomie nie przechodza przez scoping -
kazde takie miejsce jest w tym pliku i tylko tutaj.
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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    AgentRelease,
    Asset,
    EnrollmentToken,
    Owner,
    PortalUser,
    Tenant,
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
MIN_DLUGOSC_HASLA = 12


def render_admin(request: Request, template: str, user: PortalUser, **extra) -> HTMLResponse:
    payload = {
        "request": request,
        "user": user,
        "ctx": None,
        "csrf_token": issue_csrf_token(user.id),
        "all_tenants": [],
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


# --- widok globalny ---------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def przeglad(
    request: Request,
    wydany_token: str = "",
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    settings = get_settings()
    prog_aktywnosci = utcnow() - timedelta(hours=settings.stale_after_hours)

    # Liczniki jednym zapytaniem na metryke. Petla z zapytaniem na kazda firme
    # bylaby zauwazalna juz przy kilkudziesieciu firmach.
    wszystkie = dict(
        db.execute(select(Asset.tenant_id, func.count(Asset.id)).group_by(Asset.tenant_id)).all()
    )
    aktywne = dict(
        db.execute(
            select(Asset.tenant_id, func.count(Asset.id))
            .where(Asset.last_seen >= prog_aktywnosci)
            .group_by(Asset.tenant_id)
        ).all()
    )
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
    konta = dict(
        db.execute(
            select(PortalUser.tenant_id, func.count(PortalUser.id))
            .where(PortalUser.tenant_id.is_not(None))
            .group_by(PortalUser.tenant_id)
        ).all()
    )

    wersje = {r.id: r for r in db.execute(select(AgentRelease)).scalars()}
    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()

    wiersze = [
        {
            "firma": firma,
            "maszyny": wszystkie.get(firma.id, 0),
            "aktywne": aktywne.get(firma.id, 0),
            "opiekunowie": opiekunowie.get(firma.id, 0),
            "tokeny": tokeny.get(firma.id, 0),
            "konta": konta.get(firma.id, 0),
            "wersja_docelowa": wersje.get(firma.target_release_id),
        }
        for firma in firmy
    ]

    # Wersje agenta faktycznie spotykane we flocie - widac, co wymaga aktualizacji.
    rozklad_wersji = db.execute(
        select(Asset.agent_version, func.count(Asset.id))
        .where(Asset.last_seen >= prog_aktywnosci)
        .group_by(Asset.agent_version)
        .order_by(func.count(Asset.id).desc())
    ).all()

    return render_admin(
        request,
        "admin.html",
        user,
        wiersze=wiersze,
        podsumowanie={
            "firmy": len(firmy),
            "firmy_aktywne": sum(1 for f in firmy if f.is_active),
            "maszyny": sum(wszystkie.values()),
            "aktywne": sum(aktywne.values()),
        },
        rozklad_wersji=rozklad_wersji,
        wydania=db.execute(select(AgentRelease).order_by(AgentRelease.created_at.desc()))
        .scalars()
        .all(),
        wydany_token=wydany_token,
        stale_after_hours=settings.stale_after_hours,
    )


# --- firmy ------------------------------------------------------------------

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
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


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
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


# --- konta logowania --------------------------------------------------------

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
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


# --- tokeny rejestracyjne ---------------------------------------------------

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
        f"/admin?wydany_token={token.plaintext}", status_code=status.HTTP_303_SEE_OTHER
    )


# --- wersje agenta ----------------------------------------------------------

@router.post("/releases")
async def wgraj_wersje(
    request: Request,
    version: str = Form(...),
    notes: str = Form(""),
    plik: UploadFile = File(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Wgrywa plik agenta i liczy jego skrot.

    Skrot jest sercem calego mechanizmu: agent porownuje go po pobraniu
    i odmawia podmiany, gdy sie nie zgadza. Liczymy go strumieniowo, zeby
    trzydziestomegabajtowy plik nie ladowal w calosci do pamieci.
    """
    sprawdz_csrf(user, csrf_token)
    settings = get_settings()

    numer = version.strip()
    if not WERSJA_RE.match(numer):
        raise HTTPException(status_code=400, detail="niepoprawny numer wersji")
    if db.execute(select(AgentRelease).where(AgentRelease.version == numer)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"wersja {numer} juz istnieje")

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
        # Agent chodzi na Windows - odrzucamy cokolwiek, co nie jest programem.
        with tymczasowy.open("rb") as sprawdzany:
            if sprawdzany.read(2) != b"MZ":
                raise HTTPException(
                    status_code=400,
                    detail="plik nie jest programem Windows (brak sygnatury MZ)",
                )

        odcisk = skrot.hexdigest()
        nazwa_w_magazynie = f"{odcisk}.exe"
        tymczasowy.replace(katalog / nazwa_w_magazynie)
    except Exception:
        tymczasowy.unlink(missing_ok=True)
        raise

    db.add(
        AgentRelease(
            version=numer,
            filename=plik.filename or f"cmdb-agent-{numer}.exe",
            storage_name=nazwa_w_magazynie,
            sha256=odcisk,
            size_bytes=rozmiar,
            notes=notes.strip() or None,
            created_by=user.email,
        )
    )
    audit(db, None, action="release.uploaded", target=numer,
          detail={"sha256": odcisk, "size": rozmiar}, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s wgral wersje agenta %s (%s)", user.email, numer, odcisk[:16])
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


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

    uzywana = db.execute(
        select(func.count(Tenant.id)).where(Tenant.target_release_id == release_id)
    ).scalar_one() + db.execute(
        select(func.count(Asset.id)).where(Asset.target_release_id == release_id)
    ).scalar_one()
    if uzywana:
        raise HTTPException(
            status_code=400,
            detail="wersja jest ustawiona jako docelowa - najpierw zmien cel aktualizacji",
        )

    (katalog_wersji() / wydanie.storage_name).unlink(missing_ok=True)
    db.delete(wydanie)
    audit(db, None, action="release.deleted", target=wydanie.version,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


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
        select(Asset).where(Asset.tenant_id == firma.id).order_by(Asset.hostname)
    ).scalars().all()
    wersje = {r.id: r for r in db.execute(select(AgentRelease)).scalars()}

    return render_admin(
        request,
        "admin_upgrade.html",
        user,
        firma=firma,
        maszyny=maszyny,
        wersje=wersje,
        wydania=db.execute(select(AgentRelease).order_by(AgentRelease.version.desc()))
        .scalars()
        .all(),
        wersja_firmy=wersje.get(firma.target_release_id),
        stale_after_hours=get_settings().stale_after_hours,
    )


@router.post("/tenants/{tenant_id}/upgrade")
async def zlec_aktualizacje(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Ustawia wersje docelowa dla calej firmy albo wybranych maszyn.

    Nie wysylamy niczego na maszyny - to agent przy swoim cyklicznym raporcie
    dowiaduje sie, ze oczekiwana jest inna wersja, i sam ja pobiera. Serwer
    nigdy nie inicjuje polaczenia do agenta.
    """
    formularz = await request.form()
    sprawdz_csrf(user, formularz.get("csrf_token"))

    firma = znajdz_firme(db, tenant_id)
    zakres = formularz.get("zakres", "wybrane")
    release_id = (formularz.get("release_id") or "").strip()

    wydanie = None
    if release_id:
        wydanie = db.get(AgentRelease, release_id)
        if wydanie is None:
            raise HTTPException(status_code=400, detail="nie znaleziono wskazanej wersji")

    if zakres == "firma":
        firma.target_release_id = wydanie.id if wydanie else None
        # Ustawienia poszczegolnych maszyn kasujemy, zeby nie przeslanialy
        # decyzji podjetej dla calej firmy.
        for maszyna in db.execute(
            select(Asset).where(Asset.tenant_id == firma.id)
        ).scalars():
            maszyna.target_release_id = None
            maszyna.upgrade_status = "zlecona" if wydanie else None
            maszyna.upgrade_detail = None
            maszyna.upgrade_updated_at = utcnow()
        objete = "wszystkie maszyny"
    else:
        wybrane = formularz.getlist("asset_id")
        if not wybrane:
            raise HTTPException(status_code=400, detail="nie wskazano zadnej maszyny")
        maszyny = db.execute(
            select(Asset).where(Asset.tenant_id == firma.id, Asset.id.in_(wybrane))
        ).scalars().all()
        if len(maszyny) != len(set(wybrane)):
            raise HTTPException(status_code=400, detail="maszyna spoza tej firmy")
        for maszyna in maszyny:
            maszyna.target_release_id = wydanie.id if wydanie else None
            maszyna.upgrade_status = "zlecona" if wydanie else None
            maszyna.upgrade_detail = None
            maszyna.upgrade_updated_at = utcnow()
        objete = f"{len(maszyny)} maszyn"

    audit(db, None, action="agent.upgrade_requested", target=firma.slug,
          detail={"wersja": wydanie.version if wydanie else None, "zakres": objete},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info(
        "superadmin %s zlecil wersje %s dla %s w firmie %s",
        user.email, wydanie.version if wydanie else "(brak)", objete, firma.slug,
    )
    return RedirectResponse(
        f"/admin/tenants/{firma.id}/upgrade", status_code=status.HTTP_303_SEE_OTHER
    )
