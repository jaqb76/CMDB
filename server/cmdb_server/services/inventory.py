"""Przyjmowanie raportow od agentow.

Kroki: normalizacja -> upsert maszyny -> deduplikacja snapshotu -> retencja.

Deduplikacja: agent raportuje cyklicznie, wiec wiekszosc raportow jest
identyczna. Liczymy skrot po "stabilnej" czesci raportu (bez uptime, wolnego
miejsca, listy procesow itp.) i gdy nic sie nie zmienilo, aktualizujemy tylko
last_seen zamiast pisac kolejny wiersz JSON.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Asset, InventorySnapshot, utcnow
from ..schemas import InventoryReport
from .scoping import TenantContext

# Pola zmieniajace sie przy kazdym odczycie - nie moga wywolywac nowego snapshotu.
VOLATILE_PATHS: tuple[str, ...] = (
    "agent.collected_at",
    "agent.duration_ms",
    "os.last_boot",
    "os.uptime_seconds",
    "os.free_physical_memory_bytes",
    "hardware.memory.available_bytes",
    "hardware.storage.logical_disks[].free_bytes",
    "hardware.storage.logical_disks[].free_percent",
    "software.processes",
    "users.sessions",
    "errors",
)


def _prune(node: Any, segments: list[str]) -> None:
    """Usuwa wartosc wskazana sciezka. Segment '[]' = 'dla kazdego elementu listy'."""
    if not segments or node is None:
        return
    head, rest = segments[0], segments[1:]

    if head == "[]":
        if isinstance(node, list):
            for item in node:
                _prune(item, rest)
        return

    if not isinstance(node, dict):
        return
    if not rest:
        node.pop(head, None)
        return
    _prune(node.get(head), rest)


def _split_path(path: str) -> list[str]:
    segments: list[str] = []
    for part in path.split("."):
        if part.endswith("[]"):
            segments.append(part[:-2])
            segments.append("[]")
        else:
            segments.append(part)
    return segments


def stable_fingerprint(payload: dict) -> str:
    """SHA-256 raportu po odcieciu pol ulotnych."""
    stable = json.loads(json.dumps(payload, default=str))
    for path in VOLATILE_PATHS:
        _prune(stable, _split_path(path))
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _bytes_to_gb(value: Any) -> float | None:
    try:
        return round(int(value) / (1024**3), 1)
    except (TypeError, ValueError):
        return None


def summarize(payload: dict) -> dict:
    """Skrot do listy maszyn - zeby nie ladowac calego JSON-a przy renderowaniu tabeli."""
    hardware = payload.get("hardware") or {}
    software = payload.get("software") or {}
    users = payload.get("users") or {}
    network = payload.get("network") or {}
    os_info = payload.get("os") or {}

    cpu = hardware.get("cpu") or {}
    memory = hardware.get("memory") or {}
    storage = hardware.get("storage") or {}
    logical = storage.get("logical_disks") or []

    total_disk = 0
    for disk in logical:
        try:
            total_disk += int(disk.get("size_bytes") or 0)
        except (TypeError, ValueError):
            continue

    services = software.get("services") or []
    running = sum(1 for s in services if str(s.get("state", "")).lower() in {"running", "active"})

    addresses: list[str] = []
    for iface in network.get("interfaces") or []:
        for addr in iface.get("ip_addresses") or []:
            if addr and addr not in addresses:
                addresses.append(addr)

    return {
        "cpu_model": cpu.get("model"),
        "cpu_cores": cpu.get("physical_cores"),
        "cpu_threads": cpu.get("logical_cores"),
        "memory_gb": _bytes_to_gb(memory.get("total_bytes")),
        "storage_gb": _bytes_to_gb(total_disk) if total_disk else None,
        "disks": len(logical),
        "packages": len(software.get("packages") or []),
        "updates": len(software.get("updates") or []),
        "services_total": len(services),
        "services_running": running,
        "processes": len(software.get("processes") or []),
        "local_users": len(users.get("local_accounts") or []),
        "administrators": [
            a.get("name") for a in (users.get("administrators") or []) if a.get("name")
        ],
        "sessions": len(users.get("sessions") or []),
        "ip_addresses": addresses[:8],
        "last_boot": os_info.get("last_boot"),
        "collector_errors": len(payload.get("errors") or []),
    }


def _primary_ip(payload: dict) -> str | None:
    for iface in (payload.get("network") or {}).get("interfaces") or []:
        if not iface.get("is_up", True):
            continue
        for addr in iface.get("ip_addresses") or []:
            if addr and not str(addr).startswith(("127.", "169.254.", "::1")):
                return str(addr)
    return None


def apply_identity(asset: Asset, report: InventoryReport, payload: dict) -> None:
    """Przepisuje do kolumn relacyjnych to, po czym filtrujemy w UI."""
    identity = report.identity
    hardware = payload.get("hardware") or {}
    system = hardware.get("system") or {}
    os_info = payload.get("os") or {}

    asset.hostname = identity.hostname
    asset.fqdn = _first(identity.fqdn, asset.fqdn)
    asset.domain = _first(identity.domain, asset.domain)
    asset.os_family = identity.os_family
    asset.os_name = _first(os_info.get("name"), os_info.get("caption"), asset.os_name)
    asset.os_version = _first(os_info.get("version"), asset.os_version)
    asset.manufacturer = _first(system.get("manufacturer"), asset.manufacturer)
    asset.model = _first(system.get("model"), asset.model)
    asset.serial_number = _first(system.get("serial_number"), asset.serial_number)
    asset.primary_ip = _first(_primary_ip(payload), asset.primary_ip)
    asset.agent_version = report.agent.version


def _normalize_collected_at(value: datetime) -> datetime:
    """Nie ufamy zegarowi klienta bardziej niz to konieczne."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    now = utcnow()
    if value > now + timedelta(minutes=10):
        return now
    if value < now - timedelta(days=365):
        return now
    return value


def store_report(
    db: Session, ctx: TenantContext, asset: Asset, report: InventoryReport
) -> tuple[InventorySnapshot | None, bool]:
    """Zapisuje raport. Zwraca (snapshot, czy_stan_sie_zmienil)."""
    settings = get_settings()
    payload = report.model_dump(mode="json")
    fingerprint = stable_fingerprint(payload)
    collected_at = _normalize_collected_at(report.agent.collected_at)

    apply_identity(asset, report, payload)
    asset.facts = summarize(payload)
    asset.last_seen = utcnow()
    asset.is_active = True

    previous = db.execute(
        select(InventorySnapshot)
        .where(InventorySnapshot.asset_id == asset.id)
        .order_by(InventorySnapshot.collected_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if previous is not None and previous.payload_hash == fingerprint:
        # Stan bez zmian - odswiezamy tylko czas ostatniego kontaktu.
        return previous, False

    encoded = json.dumps(payload, ensure_ascii=False)
    snapshot = InventorySnapshot(
        tenant_id=ctx.tenant_id,
        asset_id=asset.id,
        schema_version=report.schema_version,
        collected_at=collected_at,
        payload_hash=fingerprint,
        size_bytes=len(encoded.encode("utf-8")),
        payload=payload,
    )
    db.add(snapshot)
    asset.last_change_at = utcnow()

    if settings.snapshot_retention > 0:
        db.flush()
        _apply_retention(db, asset.id, settings.snapshot_retention)

    return snapshot, True


def _apply_retention(db: Session, asset_id: str, keep: int) -> None:
    """Zostawia N najnowszych snapshotow danej maszyny."""
    total = db.execute(
        select(func.count(InventorySnapshot.id)).where(InventorySnapshot.asset_id == asset_id)
    ).scalar_one()
    if total <= keep:
        return
    stale_ids = (
        db.execute(
            select(InventorySnapshot.id)
            .where(InventorySnapshot.asset_id == asset_id)
            .order_by(InventorySnapshot.collected_at.desc())
            .offset(keep)
        )
        .scalars()
        .all()
    )
    if stale_ids:
        db.execute(delete(InventorySnapshot).where(InventorySnapshot.id.in_(stale_ids)))
