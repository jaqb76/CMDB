"""Uwierzytelnianie agentow i uzytkownikow panelu."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AgentCredential, EnrollmentToken, PortalUser, Tenant, as_utc, utcnow
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
    # Za reverse proxy (nginx) ufamy pierwszemu wpisowi X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
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


def tenant_context_for(user: PortalUser, tenant: Tenant) -> TenantContext:
    return TenantContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        actor=user.email,
        is_superadmin=user.is_superadmin,
        can_write=user.is_superadmin or user.role == "admin",
    )


def verify_csrf(request: Request, user: PortalUser, form_token: str | None) -> None:
    if not form_token or not check_csrf_token(form_token, user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="nieprawidlowy token CSRF")


def naive_utc(value):
    """Alias na models.as_utc - uzywany przez filtry szablonow."""
    return as_utc(value)
