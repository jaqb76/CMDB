"""Izolacja tenantow.

Jedyne miejsce, w ktorym powstaje zapytanie o dane firmy. Kazda funkcja
wymaga TenantContext - nie da sie przypadkiem pobrac danych bez filtra,
bo nie ma wersji zapytania bez tenant_id.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..models import Asset, AuditLog, EnrollmentToken, InventorySnapshot, Owner


@dataclass(frozen=True)
class TenantContext:
    """Kontekst wykonania zapytania - z tokenu agenta albo z sesji panelu."""

    tenant_id: str
    tenant_slug: str
    actor: str
    is_superadmin: bool = False
    can_write: bool = False


def scoped(stmt: Select, model, ctx: TenantContext) -> Select:
    """Dokleja filtr tenant_id. Superadmin oglada dane wskazanej firmy,
    nie wszystkich naraz - podglad "wszystkiego" wymaga jawnej petli po firmach."""
    return stmt.where(model.tenant_id == ctx.tenant_id)


def assets_query(ctx: TenantContext) -> Select:
    return scoped(select(Asset), Asset, ctx)


def owners_query(ctx: TenantContext) -> Select:
    return scoped(select(Owner), Owner, ctx).order_by(Owner.full_name)


def tokens_query(ctx: TenantContext) -> Select:
    return scoped(select(EnrollmentToken), EnrollmentToken, ctx).order_by(
        EnrollmentToken.created_at.desc()
    )


def get_asset(db: Session, ctx: TenantContext, asset_id: str) -> Asset | None:
    return db.execute(assets_query(ctx).where(Asset.id == asset_id)).scalar_one_or_none()


def get_owner(db: Session, ctx: TenantContext, owner_id: str) -> Owner | None:
    return db.execute(owners_query(ctx).where(Owner.id == owner_id)).scalar_one_or_none()


def get_snapshot(db: Session, ctx: TenantContext, snapshot_id: str) -> InventorySnapshot | None:
    stmt = scoped(select(InventorySnapshot), InventorySnapshot, ctx).where(
        InventorySnapshot.id == snapshot_id
    )
    return db.execute(stmt).scalar_one_or_none()


def latest_snapshot(db: Session, ctx: TenantContext, asset_id: str) -> InventorySnapshot | None:
    stmt = (
        scoped(select(InventorySnapshot), InventorySnapshot, ctx)
        .where(InventorySnapshot.asset_id == asset_id)
        .order_by(InventorySnapshot.collected_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def audit(
    db: Session,
    ctx: TenantContext | None,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    actor: str | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=ctx.tenant_id if ctx else None,
            actor=actor or (ctx.actor if ctx else "system"),
            action=action,
            target=target,
            detail=detail,
            ip=ip,
        )
    )
