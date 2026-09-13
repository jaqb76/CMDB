"""Uwierzytelnianie agentow i uzytkownikow panelu."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (Asset, AgentCredential, EnrollmentToken, HelpdeskDostep, PortalUser,
                      Tenant, as_utc, utcnow)
from ..security import (
    check_csrf_token,
    hash_password,
    load_session,
    parse_token,
    verify_password,
    verify_token,
)
from .scoping import TenantContext

# --- prosty throttle nieudanych prob (per IP) -------------------------------
# Do jednej instancji wystarcza; przy wielu replikach przenies do Redis.
_FAIL_WINDOW = 300.0
_FAIL_LIMIT = 20
_failures: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Uvicorn interpretuje proxy headers tylko od zaufanych proxy.
    # Nigdy nie czytamy naglowka dostarczonego bezposrednio przez klienta.
    return request.client.host if request.client else "unknown"


def _record_failure(ip: str) -> None:
    now = time.monotonic()
    bucket = _failures[ip]
    bucket.append(now)
    while bucket and now - bucket[0] > _FAIL_WINDOW:
        bucket.popleft()


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    bucket = _failures[ip]
    while bucket and now - bucket[0] > _FAIL_WINDOW:
        bucket.popleft()
    return len(bucket) >= _FAIL_LIMIT


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="brak naglowka Authorization: Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return value.strip()


def _reject(ip: str, detail: str = "nieprawidlowy token") -> HTTPException:
    _record_failure(ip)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _guard_throttle(ip: str) -> None:
    if _throttled(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="zbyt wiele nieudanych prob uwierzytelnienia",
        )


def _sprawdz_token_firmowy(
    request: Request, db: Session
) -> tuple[EnrollmentToken, Tenant]:
    """Sam kontrola tokenu firmowego, bez odnotowywania uzycia."""
    ip = client_ip(request)
    _guard_throttle(ip)
    token = _bearer(request)

    parsed = parse_token(token)
    if parsed is None or parsed[0] != "ent":
        raise _reject(ip)
    _, prefix = parsed

    row = db.execute(
        select(EnrollmentToken).where(EnrollmentToken.prefix == prefix)
    ).scalar_one_or_none()
    if row is None or not verify_token(token, row.token_hash):
        raise _reject(ip)
    if not row.is_usable:
        raise _reject(ip, "token wycofany lub wygasl")

    tenant = db.get(Tenant, row.tenant_id)
    if tenant is None or not tenant.is_active:
        raise _reject(ip, "firma nieaktywna")

    return row, tenant


def require_enrollment_token(
    request: Request, db: Session = Depends(get_db)
) -> tuple[EnrollmentToken, Tenant]:
    """Token firmowy - akceptowany wylacznie na endpoincie enrollmentu."""
    row, tenant = _sprawdz_token_firmowy(request, db)
    row.last_used_at = utcnow()
    row.use_count += 1
    return row, tenant


def require_enrollment_token_do_pobrania(
    request: Request, db: Session = Depends(get_db)
) -> tuple[EnrollmentToken, Tenant]:
    """Token firmowy przy pobieraniu instalatora.

    Ten sam token, ale licznika uzyc nie ruszamy: liczy on zarejestrowane
    maszyny, a pobranie pliku maszyna moze powtorzyc kilka razy, zanim
    instalacja sie powiedzie. Wliczanie tego zafalszowaloby obraz floty.
    """
    return _sprawdz_token_firmowy(request, db)


def require_agent(
    request: Request, db: Session = Depends(get_db)
) -> tuple[AgentCredential, TenantContext]:
    """Indywidualne poswiadczenie agenta - uzywane przy kazdym raporcie."""
    ip = client_ip(request)
    _guard_throttle(ip)
    token = _bearer(request)

    parsed = parse_token(token)
    if parsed is None or parsed[0] != "agt":
        raise _reject(ip)
    _, prefix = parsed

    cred = db.execute(
        select(AgentCredential).where(AgentCredential.prefix == prefix)
    ).scalar_one_or_none()
    if cred is None or not verify_token(token, cred.token_hash):
        raise _reject(ip)
    asset = db.get(Asset, cred.asset_id)
    if asset is None or asset.enrollment_blocked:
        raise _reject(ip, "maszyna zablokowana przez administratora")
    if not cred.is_usable:
        raise _reject(ip, "poswiadczenie agenta wycofane")

    tenant = db.get(Tenant, cred.tenant_id)
    if tenant is None or not tenant.is_active:
        raise _reject(ip, "firma nieaktywna")

    cred.last_used_at = utcnow()
    cred.last_used_ip = ip
    ctx = TenantContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        actor=f"agent:{cred.prefix}",
        can_write=True,
    )
    return cred, ctx


# --- panel WWW --------------------------------------------------------------

@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Hash-atrapa do wyrownania czasu logowania dla nieistniejacych kont."""
    return hash_password("nieistniejace-konto")


def authenticate_user(db: Session, email: str, password: str) -> PortalUser | None:
    user = db.execute(
        select(PortalUser).where(PortalUser.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        # Liczymy hash mimo braku uzytkownika, zeby czas odpowiedzi nie zdradzal,
        # czy konto istnieje.
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> PortalUser | None:
    raw = request.cookies.get(_cookie_name())
    if not raw:
        return None
    data = load_session(raw)
    if not data or "uid" not in data:
        return None
    user = db.get(PortalUser, data["uid"])
    if user is None or not user.is_active:
        return None
    if data.get("sv") != user.session_version:
        return None
    return user


def _cookie_name() -> str:
    from ..config import get_settings

    return get_settings().session_cookie


class LoginRequired(Exception):
    """Przechwytywane przez handler, ktory przekierowuje na /login."""


def require_user(user: PortalUser | None = Depends(current_user)) -> PortalUser:
    if user is None:
        raise LoginRequired()
    return user


class SuperadminRequired(Exception):
    """Przechwytywane przez handler, ktory zwraca 403."""


def require_superadmin(user: PortalUser = Depends(require_user)) -> PortalUser:
    """Dostep do zarzadzania firmami, kontami i wersjami agenta.

    Celowo osobna zaleznosc, a nie sprawdzenie roli w kazdym widoku:
    zapomniana kontrola w jednym miejscu otwieralaby caly panel globalny.
    """
    if not user.is_superadmin:
        raise SuperadminRequired()
    return user


def widzi_wszystkie_firmy(user: PortalUser) -> bool:
    """Czy konto moze ogladac dane dowolnej firmy, nie tylko wlasnej.

    Superadmin zarzadza calym systemem; audytor globalny ma te sama szerokosc
    widoku, ale wylacznie do odczytu (patrz tenant_context_for).
    """
    return bool(user.is_superadmin or user.is_global_viewer)


def firmy_konta(db: Session, user: PortalUser) -> list[Tenant]:
    """Firmy, ktore konto moze ogladac - w kolejnosci alfabetycznej.

    Do niedawna odpowiedz byla jednoznaczna: albo konto widzi wszystkie firmy,
    albo dokladnie te jedna, do ktorej nalezy. Helpdesk dolozyl trzeci
    przypadek - technika, ktory nie nalezy do zadnej firmy, a obsluguje
    kilka. Jego uprawnienie to lista wpisow w helpdesk_dostepy i nadaje ja
    wylacznie superadmin.

    Lista powstaje w jednym miejscu, bo sluzy do dwoch rzeczy naraz: wyboru
    firmy w pasku i kontroli, czy wolno ja wybrac. Dwie osobne odpowiedzi na
    to samo pytanie predzej czy pozniej rozjechalyby sie o jedna firme.
    """
    if widzi_wszystkie_firmy(user):
        return list(db.execute(select(Tenant).order_by(Tenant.name)).scalars())

    identyfikatory: set[str] = set()
    if user.tenant_id:
        identyfikatory.add(user.tenant_id)
    identyfikatory.update(db.execute(
        select(HelpdeskDostep.tenant_id).where(HelpdeskDostep.user_id == user.id)
    ).scalars())
    if not identyfikatory:
        return []

    return list(db.execute(
        select(Tenant)
        .where(Tenant.id.in_(identyfikatory), Tenant.is_active.is_(True))
        .order_by(Tenant.name)
    ).scalars())


def tenant_context_for(user: PortalUser, tenant: Tenant) -> TenantContext:
    return TenantContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        actor=user.email,
        is_superadmin=user.is_superadmin,
        # Audytor globalny nie zapisuje niczego w zadnej firmie - warunek jest
        # tutaj, bo to jedyne miejsce, w ktorym powstaje prawo do zapisu.
        can_write=(user.is_superadmin or user.role == "admin") and not user.is_global_viewer,
    )


def verify_csrf(request: Request, user: PortalUser, form_token: str | None) -> None:
    if not form_token or not check_csrf_token(form_token, user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="nieprawidlowy token CSRF")


def naive_utc(value):
    """Alias na models.as_utc - uzywany przez filtry szablonow."""
    return as_utc(value)
