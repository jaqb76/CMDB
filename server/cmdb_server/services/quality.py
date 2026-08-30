"""Jakosc biezacego inwentarza, bez zaladowania pelnych raportow JSON."""
from datetime import timedelta

from sqlalchemy import func, select

from ..models import Asset, AssetCurrentReport, InventorySnapshot, Tenant, as_utc, utcnow
from .duplicates import znajdz_duplikaty
from .ustawienia import prog_bez_kontaktu

QUALITY_LABELS = {
    "errors": "Bledy kolektorow",
    "stale": "Nieaktualne odczyty",
    "owner": "Brak opiekuna",
    "duplicates": "Podejrzenie duplikatu",
}


def quality_rows(db, ctx):
    cutoff = utcnow() - timedelta(hours=prog_bez_kontaktu(db.get(Tenant, ctx.tenant_id)))
    history = (select(InventorySnapshot.asset_id,
                      func.max(InventorySnapshot.collected_at).label("collected_at"))
               .where(InventorySnapshot.tenant_id == ctx.tenant_id)
               .group_by(InventorySnapshot.asset_id).subquery())
    rows = db.execute(select(Asset.id, Asset.hostname, Asset.zrodlo, Asset.owner_id,
                             Asset.last_seen, Asset.facts,
                             func.coalesce(AssetCurrentReport.collected_at, history.c.collected_at).label("measured_at"))
        .outerjoin(AssetCurrentReport, (AssetCurrentReport.asset_id == Asset.id) &
                   (AssetCurrentReport.tenant_id == ctx.tenant_id))
        .outerjoin(history, history.c.asset_id == Asset.id)
        .where(Asset.tenant_id == ctx.tenant_id, Asset.lifecycle == "aktywny")
        .order_by(Asset.hostname, Asset.id)).all()
    duplicate_ids = {asset.id for group in znajdz_duplikaty(db, ctx.tenant_id) for asset in group["maszyny"]}
    result = []
    counts = dict.fromkeys(QUALITY_LABELS, 0)
    for row in rows:
        issues = []
        errors = (row.facts or {}).get("collector_errors", 0)
        if row.zrodlo == "agent":
            if errors:
                issues.append("errors")
            # Kontakt z agentem i data pomiaru to rozne rzeczy: replay starego
            # raportu nie moze sprawic, ze stary inwentarz stanie sie swiezy.
            if (row.measured_at is None or as_utc(row.measured_at) < cutoff or
                    row.last_seen is None or as_utc(row.last_seen) < cutoff):
                issues.append("stale")
        if not row.owner_id:
            issues.append("owner")
        if row.id in duplicate_ids:
            issues.append("duplicates")
        for issue in issues:
            counts[issue] += 1
        if issues:
            result.append({"asset": row, "issues": issues, "errors": errors})
    return result, counts, len(rows)
