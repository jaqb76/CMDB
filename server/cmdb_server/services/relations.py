"""Relacje kierunkowe zasobow, zawsze w granicach jednej firmy."""
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, joinedload

from ..models import AssetRelation, RELATION_KINDS
from .scoping import TenantContext, get_asset


def list_relations(db: Session, ctx: TenantContext, asset_id: str = ""):
    query = select(AssetRelation).where(AssetRelation.tenant_id == ctx.tenant_id)
    if asset_id:
        query = query.where((AssetRelation.source_id == asset_id) | (AssetRelation.target_id == asset_id))
    return db.execute(query.options(joinedload(AssetRelation.source), joinedload(AssetRelation.target))
                      .order_by(AssetRelation.kind, AssetRelation.created_at)).scalars().all()


def add_relation(db: Session, ctx: TenantContext, source_id: str, target_id: str, kind: str):
    if not ctx.can_write:
        raise HTTPException(403, "konto ma uprawnienia tylko do odczytu")
    if kind not in RELATION_KINDS or source_id == target_id:
        raise HTTPException(422, "nieprawidlowy rodzaj relacji lub relacja do samego siebie")
    # Chroni kontrole cykli i kardynalnosci przed rownoleglymi zapisami.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "relations:" + ctx.tenant_id})
    source, target = get_asset(db, ctx, source_id), get_asset(db, ctx, target_id)
    if source is None or target is None:
        raise HTTPException(404, "nie znaleziono zasobu")
    allowed = {
        "vm_host": ({"vm", "komputer"}, {"host", "komputer"}),
        "host_cluster": ({"host", "komputer"}, {"klaster"}),
        "application_server": ({"aplikacja"}, {"host", "komputer", "vm"}),
    }
    source_types, target_types = allowed[kind]
    if source.typ not in source_types or target.typ not in target_types:
        raise HTTPException(422, "typy zasobow nie pasuja do kierunku relacji")
    rows = list_relations(db, ctx)
    adjacency = {}
    for row in rows:
        if (row.source_id, row.target_id, row.kind) == (source_id, target_id, kind):
            return row, False
        if kind in {"vm_host", "host_cluster"} and row.source_id == source_id and row.kind == kind:
            raise HTTPException(409, "zasob ma juz taka relacje; usun ja przed zmiana")
        adjacency.setdefault(row.source_id, set()).add(row.target_id)
    pending, seen = [target_id], set()
    while pending:
        node = pending.pop()
        if node == source_id:
            raise HTTPException(409, "relacja utworzylaby cykl zaleznosci")
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    relation = AssetRelation(tenant_id=ctx.tenant_id, source_id=source_id,
                             target_id=target_id, kind=kind, created_by=ctx.actor)
    db.add(relation)
    db.flush()
    return relation, True
