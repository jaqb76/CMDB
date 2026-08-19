"""Skladanie raportu inwentaryzacyjnego w formacie oczekiwanym przez serwer."""
from __future__ import annotations

import hashlib
import json
import logging
import time

from . import SCHEMA_VERSION, __version__
from .collectors.base import BaseCollector
from .collectors.common import utc_now_iso

log = logging.getLogger(__name__)


def build_report(collector: BaseCollector) -> dict:
    started = time.monotonic()
    machine_id = collector.machine_id()
    identity = collector.identity()
    sections, errors = collector.collect()
    duration_ms = int((time.monotonic() - started) * 1000)

    report = {
        "schema_version": SCHEMA_VERSION,
        "machine_id": machine_id,
        "agent": {
            "version": __version__,
            "collector": collector.name,
            "collected_at": utc_now_iso(),
            "duration_ms": duration_ms,
        },
        "identity": identity,
        "hardware": sections.get("hardware", {}),
        "os": sections.get("os", {}),
        "network": sections.get("network", {}),
        "software": sections.get("software", {}),
        "users": sections.get("users", {}),
        "errors": errors,
    }

    log.info(
        "raport zebrany w %d ms: %d pakietow, %d uslug, %d kont, %d bledow",
        duration_ms,
        len(report["software"].get("packages") or []),
        len(report["software"].get("services") or []),
        len(report["users"].get("local_accounts") or []),
        len(errors),
    )
    return report


def report_size(report: dict) -> int:
    return len(json.dumps(report, ensure_ascii=False).encode("utf-8"))


def report_hash(report: dict) -> str:
    """Skrot uzywany lokalnie do logowania, czy cokolwiek sie zmienilo."""
    stripped = {k: v for k, v in report.items() if k != "agent"}
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
