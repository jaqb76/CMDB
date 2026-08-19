"""Bufor raportow na wypadek braku lacznosci z serwerem.

Laptop poza biurem albo chwilowa awaria sieci nie moga oznaczac dziury w
inwentarzu. Nieudany raport laduje na dysku i idzie przy nastepnej okazji.
Bufor jest ograniczony - trzymamy najnowsze wpisy, starsze kasujemy.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_SPOOLED_REPORTS = 12


def spool_report(spool_dir: Path, report: dict) -> None:
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / f"report-{int(time.time() * 1000)}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    log.info("raport zapisany do bufora: %s", path.name)
    _trim(spool_dir)


def pending_reports(spool_dir: Path) -> list[Path]:
    if not spool_dir.is_dir():
        return []
    return sorted(spool_dir.glob("report-*.json"))


def load_report(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("odrzucam uszkodzony raport z bufora %s: %s", path.name, exc)
        path.unlink(missing_ok=True)
        return None


def _trim(spool_dir: Path) -> None:
    reports = pending_reports(spool_dir)
    for stale in reports[:-MAX_SPOOLED_REPORTS]:
        stale.unlink(missing_ok=True)
        log.debug("usunieto stary raport z bufora: %s", stale.name)
