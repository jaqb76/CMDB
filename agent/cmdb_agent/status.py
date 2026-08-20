"""Publikowanie stanu agenta dla interfejsu uzytkownika.

Rozdzial odpowiedzialnosci:

  agent-state.json  - zawiera poswiadczenie maszyny, dostep tylko SYSTEM
                      i administratorzy,
  public/status.json - nie zawiera zadnych sekretow, czytelny dla kazdego
                      zalogowanego uzytkownika.

Agent chodzi jako SYSTEM (inaczej nie zbierze czesci danych), a ikona
w zasobniku jako zalogowany uzytkownik. Zamiast rozluzniac dostep do pliku
z tokenem, publikujemy osobny plik statusu.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .state import AgentState, harden_public_directory

log = logging.getLogger(__name__)

STATUS_FILENAME = "status.json"

# Opisy stanow pokazywane w oknie - jedno zrodlo prawdy dla GUI i wiersza polecen.
STATUS_LABELS = {
    "never": "jeszcze nie synchronizowano",
    "ok": "synchronizacja poprawna",
    "error": "serwer odrzucil raport",
    "offline": "brak lacznosci z serwerem",
    "not_configured": "agent nieskonfigurowany",
}


@dataclass
class AgentStatus:
    """Migawka stanu agenta - to czyta ikona w zasobniku."""

    agent_version: str = __version__
    hostname: str = ""
    configured: bool = False
    enrolled: bool = False
    server_url: str = ""
    tenant_slug: str = ""
    asset_id: str = ""

    last_sync_at: str = ""
    last_attempt_at: str = ""
    last_status: str = "never"
    last_error: str = ""
    last_sync_changed: bool = False

    report_interval_seconds: int = 3600
    next_sync_estimate: str = ""
    spooled_reports: int = 0
    published_at: str = ""
    warnings: list = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.last_status, self.last_status)


def _parse_iso(value: str):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_status(config, state: AgentState, spooled: int = 0) -> AgentStatus:
    configured = bool(config.server_url)
    last_status = state.last_status or "never"
    if not configured or not state.is_enrolled:
        last_status = "not_configured"

    next_estimate = ""
    last_attempt = _parse_iso(state.last_attempt_at)
    if last_attempt and configured:
        next_estimate = (
            last_attempt + timedelta(seconds=config.report_interval_seconds)
        ).isoformat()

    warnings = []
    if spooled:
        warnings.append(f"raporty oczekujace na wyslanie: {spooled}")
    stale_after = timedelta(seconds=config.report_interval_seconds * 3)
    last_sync = _parse_iso(state.last_sync_at)
    if last_sync and datetime.now(timezone.utc) - last_sync > stale_after:
        warnings.append("ostatnia udana synchronizacja jest znacznie przeterminowana")

    return AgentStatus(
        hostname=platform.node(),
        configured=configured,
        enrolled=state.is_enrolled,
        server_url=config.server_url,
        tenant_slug=state.tenant_slug,
        asset_id=state.asset_id,
        last_sync_at=state.last_sync_at,
        last_attempt_at=state.last_attempt_at,
        last_status=last_status,
        last_error=state.last_error,
        last_sync_changed=state.last_sync_changed,
        report_interval_seconds=config.report_interval_seconds,
        next_sync_estimate=next_estimate,
        spooled_reports=spooled,
        published_at=datetime.now(timezone.utc).isoformat(),
        warnings=warnings,
    )


def public_dir(config) -> Path:
    return config.data_dir / "public"


def status_path(config) -> Path:
    return public_dir(config) / STATUS_FILENAME


def publish(config, state: AgentState, spooled: int = 0) -> AgentStatus:
    """Zapisuje status atomowo. Blad zapisu nie moze przerwac pracy agenta."""
    status = build_status(config, state, spooled)
    target = status_path(config)
    try:
        harden_public_directory(public_dir(config))
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".status-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(status), handle, indent=2, ensure_ascii=False)
            if os.name == "posix":
                os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, target)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.warning("nie udalo sie opublikowac statusu w %s: %s", target, exc)
    return status


def read(config) -> AgentStatus:
    """Odczyt po stronie GUI. Brak pliku = agent jeszcze nie wystartowal."""
    path = status_path(config)
    if not path.is_file():
        return AgentStatus(hostname=platform.node(), last_status="not_configured")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("nie moge odczytac statusu (%s)", exc)
        return AgentStatus(hostname=platform.node(), last_status="not_configured")
    known = {name: raw[name] for name in AgentStatus.__dataclass_fields__ if name in raw}
    try:
        return AgentStatus(**known)
    except TypeError:
        return AgentStatus(hostname=platform.node(), last_status="not_configured")


def format_local(value: str) -> str:
    """Czas w strefie lokalnej - operator nie chce przeliczac UTC w pamieci."""
    parsed = _parse_iso(value)
    if parsed is None:
        return "brak"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_relative(value: str) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "nigdy"
    delta = datetime.now(timezone.utc) - parsed
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "za chwile"
    if seconds < 60:
        return "przed chwila"
    if seconds < 3600:
        return f"{seconds // 60} min temu"
    if seconds < 86400:
        return f"{seconds // 3600} godz. temu"
    return f"{seconds // 86400} dni temu"
