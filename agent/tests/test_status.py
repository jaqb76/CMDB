"""Status agenta: data ostatniej UDANEJ synchronizacji i plik dla GUI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cmdb_agent import status
from cmdb_agent.config import AgentConfig
from cmdb_agent.state import AgentState, StateWriteError, load_state, save_state


def _config(tmp_path) -> AgentConfig:
    return AgentConfig(server_url="https://cmdb.firma.pl", data_dir=tmp_path)


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def test_status_of_fresh_agent(tmp_path):
    snapshot = status.build_status(AgentConfig(data_dir=tmp_path), AgentState())
    assert snapshot.configured is False
    assert snapshot.enrolled is False
    assert snapshot.last_status == "not_configured"
    assert snapshot.last_sync_at == ""


def test_status_after_successful_sync(tmp_path):
    state = AgentState(
        agent_token="cmdb_agt_x", asset_id="a1", tenant_slug="firma",
        last_sync_at=_iso(minutes=5), last_attempt_at=_iso(minutes=5), last_status="ok",
    )
    snapshot = status.build_status(_config(tmp_path), state)
    assert snapshot.last_status == "ok"
    assert snapshot.status_label == "synchronizacja poprawna"
    assert snapshot.enrolled is True
    assert snapshot.next_sync_estimate            # znamy przyblizony czas nastepnej
    assert snapshot.warnings == []


def test_failed_sync_keeps_last_successful_date(tmp_path):
    """Sedno wymagania: nieudana proba NIE moze przesunac daty ostatniej
    poprawnej synchronizacji - inaczej okno statusu klamaloby operatorowi."""
    good = _iso(hours=3)
    state = AgentState(
        agent_token="cmdb_agt_x", asset_id="a1",
        last_sync_at=good, last_attempt_at=_iso(minutes=1),
        last_status="offline", last_error="Connection refused",
    )
    snapshot = status.build_status(_config(tmp_path), state)

    assert snapshot.last_sync_at == good
    assert snapshot.last_attempt_at != snapshot.last_sync_at
    assert snapshot.last_status == "offline"
    assert snapshot.status_label == "brak lacznosci z serwerem"
    assert snapshot.last_error == "Connection refused"


def test_long_silence_raises_warning(tmp_path):
    config = _config(tmp_path)
    config.report_interval_seconds = 3600
    state = AgentState(
        agent_token="t", asset_id="a", last_sync_at=_iso(days=2), last_status="ok"
    )
    snapshot = status.build_status(config, state)
    assert any("przeterminowana" in w for w in snapshot.warnings)


def test_spooled_reports_are_reported(tmp_path):
    state = AgentState(agent_token="t", asset_id="a", last_status="ok", last_sync_at=_iso(minutes=2))
    snapshot = status.build_status(_config(tmp_path), state, spooled=3)
    assert snapshot.spooled_reports == 3
    assert any("oczekujace" in w for w in snapshot.warnings)


def test_published_file_contains_no_secrets(tmp_path):
    """Plik statusu czyta zwykly uzytkownik - nie moze byc w nim tokenu."""
    config = _config(tmp_path)
    state = AgentState(
        agent_token="cmdb_agt_tajnetajne_abcdefgh",
        asset_id="a1",
        tenant_slug="firma",
        last_sync_at=_iso(minutes=1),
        last_status="ok",
    )
    status.publish(config, state)

    raw = status.status_path(config).read_text(encoding="utf-8")
    assert "cmdb_agt_" not in raw
    assert "cmdb_ent_" not in raw
    assert "tajnetajne" not in raw
    # ...a jednoczesnie zawiera to, co GUI ma pokazac
    parsed = json.loads(raw)
    assert parsed["tenant_slug"] == "firma"
    assert parsed["last_status"] == "ok"


def test_publish_read_round_trip(tmp_path):
    config = _config(tmp_path)
    state = AgentState(
        agent_token="t", asset_id="a1", tenant_slug="firma",
        last_sync_at=_iso(minutes=7), last_status="ok",
    )
    published = status.publish(config, state)
    loaded = status.read(config)

    assert loaded.last_sync_at == published.last_sync_at
    assert loaded.tenant_slug == "firma"
    assert loaded.last_status == "ok"


def test_read_without_file_is_safe(tmp_path):
    assert status.read(_config(tmp_path)).last_status == "not_configured"


def test_read_of_corrupted_file_is_safe(tmp_path):
    config = _config(tmp_path)
    status.public_dir(config).mkdir(parents=True, exist_ok=True)
    status.status_path(config).write_text("{uszkodzony", encoding="utf-8")
    assert status.read(config).last_status == "not_configured"


def test_status_file_is_readable_for_other_users(tmp_path):
    """Ikona w zasobniku dziala jako zalogowany uzytkownik, a agent jako SYSTEM."""
    import os
    import stat

    config = _config(tmp_path)
    status.publish(config, AgentState(agent_token="t", asset_id="a"))
    if os.name == "posix":
        mode = stat.S_IMODE(status.status_path(config).stat().st_mode)
        assert mode & stat.S_IROTH, oct(mode)


def test_state_round_trip_keeps_sync_fields(tmp_path):
    """Pola synchronizacji musza przetrwac zapis i odczyt - w szczegolnosci
    bool, ktory wczesniej zamienial sie w pusty napis.

    Na Windows katalog stanu jest zastrzegany dla SYSTEM i administratorow,
    wiec zwykly uzytkownik nie odczyta go z powrotem. To zamierzone: w pliku
    lezy poswiadczenie maszyny.
    """
    import os

    import pytest as _pytest

    path = tmp_path / "agent-state.json"
    original = AgentState(
        agent_token="t", asset_id="a", last_sync_at=_iso(minutes=2),
        last_status="ok", last_sync_changed=True,
    )
    try:
        save_state(path, original)
    except StateWriteError:
        _pytest.skip("Windows: katalog stanu zastrzezony dla SYSTEM i administratorow")

    loaded = load_state(path)
    if not loaded.agent_token and os.name == "nt":
        _pytest.skip("Windows: plik stanu nieczytelny dla zwyklego uzytkownika")

    assert loaded.last_sync_at == original.last_sync_at
    assert loaded.last_status == "ok"
    assert loaded.last_sync_changed is True     # bool nie moze zamienic sie w ""


def test_formatting_helpers():
    assert status.format_local("") == "brak"
    assert status.format_relative("") == "nigdy"
    assert status.format_relative(_iso(seconds=10)) == "przed chwila"
    assert status.format_relative(_iso(minutes=30)) == "30 min temu"
    assert status.format_relative(_iso(hours=5)) == "5 godz. temu"
    assert status.format_relative(_iso(days=3)) == "3 dni temu"
    assert status.format_local("to nie jest data") == "brak"
