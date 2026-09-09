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
# Monitorowanie uslug pisze wlasny plik, bo chodzi we WLASNYM procesie
# (cmdb-agent monitor) rownolegle do inwentaryzacji. Oba procesy zapisuja caly
# dokument, wiec wspolny plik znaczylby, ze kazdy zapis kasuje wpisy drugiego.
MONITORING_FILENAME = "monitoring.json"

# Po ilu odstepach petli monitorowania uznajemy plik za nieaktualny. Petla
# publikuje status co obrot (kilka sekund), wiec minuta ciszy to juz nie
# "chwila zwloki", tylko zatrzymany proces.
MONITORING_STALE_SECONDS = 120

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
    discovery: dict = field(default_factory=dict)
    monitoring: dict = field(default_factory=dict)

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

    monitoring = read_monitoring(config)

    warnings = []
    if spooled:
        warnings.append(f"raporty oczekujace na wyslanie: {spooled}")
    # Cisza monitorowania wyglada dokladnie tak samo jak sprawna usluga, wiec
    # sama musi byc widoczna - inaczej nikt sie nie dowie, ze nic nie jest
    # sprawdzane. Pytamy o to tylko agenta zarejestrowanego: na maszynie, ktora
    # nie zglosila sie jeszcze do CMDB, brak monitorowania jest oczywisty i
    # doniesienie o nim tylko zaglusza to, czego naprawde brakuje.
    if state.is_enrolled and monitoring.get("stan") == "brak":
        warnings.append(
            "monitorowanie uslug nie bylo na tej maszynie uruchomione "
            "(usluga cmdb-agent-monitor)")
    elif state.is_enrolled and monitoring.get("stan") == "zatrzymane":
        warnings.append("proces monitorowania uslug nie odpowiada")
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
        discovery=state.discovery_status,
        monitoring=monitoring,
    )


def public_dir(config) -> Path:
    return config.data_dir / "public"


def status_path(config) -> Path:
    return public_dir(config) / STATUS_FILENAME


def monitoring_path(config) -> Path:
    return public_dir(config) / MONITORING_FILENAME


def read_monitoring(config) -> dict:
    """Status monitorowania uslug zapisany przez proces "cmdb-agent monitor".

    Brak pliku nie jest bledem odczytu tylko odpowiedzia: ten proces nigdy na
    tej maszynie nie wystartowal. Rozroznienie jest wazne, bo "nie ma celow"
    i "nie ma monitorowania" naprawia sie w dwoch zupelnie roznych miejscach.
    """
    path = monitoring_path(config)
    if not path.is_file():
        return {"stan": "brak"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("nie moge odczytac statusu monitorowania (%s)", exc)
        return {"stan": "brak"}
    if not isinstance(raw, dict):
        return {"stan": "brak"}
    opublikowano = _parse_iso(raw.get("opublikowano", ""))
    swiezy = bool(opublikowano) and (
        datetime.now(timezone.utc) - opublikowano
    ) < timedelta(seconds=MONITORING_STALE_SECONDS)
    if not swiezy:
        raw["stan"] = "zatrzymane"
    elif not raw.get("cele"):
        raw["stan"] = "bez_celow"
    elif raw.get("ostatni_blad"):
        raw["stan"] = "blad"
    else:
        raw["stan"] = "dziala"
    return raw


MONITORING_LABELS = {
    "brak": "Nie uruchomiono na tej maszynie",
    "zatrzymane": "Proces monitorowania nie odpowiada",
    "bez_celow": "Działa — panel CMDB nie przypisał tej maszynie żadnej usługi",
    "blad": "Działa, ale ostatnia wymiana z serwerem się nie udała",
    "dziala": "Działa",
}


def sprawdzone_cele(monitoring: dict) -> int:
    """Ile celow ma za soba CHOC JEDNA sonde - a nie ile ich przydzielono."""
    lista = monitoring.get("lista") if isinstance(monitoring, dict) else None
    if not isinstance(lista, list):
        return 0
    return sum(1 for wpis in lista
               if isinstance(wpis, dict) and wpis.get("ostatnia_sonda"))


def monitoring_label(monitoring: dict) -> str:
    """Napis mowi o wykonanych sondach, a nie o dlugosci listy z polityki.

    "Sprawdza 2 uslugi" bylo twierdzeniem o pomiarze, a liczylo cele przydzielone
    przez panel - wiec agent, ktory pobral polityke i nie zdazyl (albo nie umial)
    wykonac ani jednej sondy, mowil dokladnie to samo, co agent pracujacy
    poprawnie. Taki status przecenia to, co wiadomo, i wlasnie na nim mozna sie
    przejechac przy szukaniu przyczyny ciszy w panelu.
    """
    data = monitoring if isinstance(monitoring, dict) else {}
    etykieta = MONITORING_LABELS.get(data.get("stan"), MONITORING_LABELS["brak"])
    if data.get("stan") not in ("dziala", "blad"):
        return etykieta

    cele = data.get("cele") or 0
    sprawdzone = sprawdzone_cele(data)
    if not cele:
        return etykieta
    if not sprawdzone:
        # Petla zyje i zna cele, ale zadnego jeszcze nie dotknela. Zaraz po
        # starcie to normalne i mija w sekundy; utrzymujace sie znaczy klopot.
        return f"{etykieta} · przydzielono {cele} usł., jeszcze bez sondy"
    if sprawdzone < cele:
        return f"{etykieta} · sprawdza {sprawdzone} z {cele} usł."
    return f"{etykieta} · sprawdza {cele} usł."


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
        snapshot = AgentStatus(**known)
        # Monitorowanie chodzi w innym procesie i publikuje sie co kilka sekund,
        # a ten plik zapisuje inwentaryzacja raz na godzine. Zapisana tu migawka
        # jest wiec z zalozenia przeterminowana - czytamy zywy plik monitora,
        # inaczej ikona pokazywalaby "dziala" godzine po jego smierci.
        snapshot.monitoring = read_monitoring(config)
        # Stored success is historical. Re-evaluate freshness in every GUI read.
        if is_stale(snapshot) and "Status jest nieaktualny — brak świeżej synchronizacji." not in snapshot.warnings:
            snapshot.warnings = [*snapshot.warnings, "Status jest nieaktualny — brak świeżej synchronizacji."]
        return snapshot
    except TypeError:
        return AgentStatus(hostname=platform.node(), last_status="not_configured")


def is_stale(snapshot) -> bool:
    last = _parse_iso(snapshot.last_sync_at)
    return bool(last and datetime.now(timezone.utc) - last > timedelta(
        seconds=max(3600, snapshot.report_interval_seconds * 3)))


def discovery_label(snapshot) -> str:
    data = snapshot.discovery if isinstance(snapshot.discovery, dict) else {}
    labels = {"disabled": "Wyłączone przez politykę CMDB", "unavailable": "Zablokowane — brak ważnej polityki CMDB",
              "waiting": "Dozwolone — oczekiwanie na termin skanu", "running": "Skanowanie w toku",
              "completed": "Ostatni skan zakończony", "partial": "Ostatni skan niepełny lub z błędem"}
    label = labels.get(data.get("state"), "Brak aktualnej informacji o polityce CMDB")
    measured = _parse_iso(snapshot.published_at)
    if measured is None or datetime.now(timezone.utc) - measured > timedelta(
            seconds=max(3600, snapshot.report_interval_seconds * 3)):
        label = "Nieaktualny status skanera — " + label.lower()
    if data.get("last_scan_at"):
        label += " · " + format_local(data["last_scan_at"])
    return label


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
