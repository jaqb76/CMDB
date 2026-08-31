from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from cmdb_agent import __version__, status
from cmdb_agent.gui import status_window
from cmdb_agent.config import AgentConfig


def test_gui_version_independent_of_old_status(monkeypatch):
    snapshot = status.AgentStatus(agent_version="0.5.5", last_status="ok",
        last_sync_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    monkeypatch.setattr(status, "read", lambda _: snapshot)
    window = status_window.StatusWindow(Mock(), AgentConfig(), Mock(), Mock(), Mock())
    window.window, window.state_var, window.state_label, window.problem_var = Mock(), Mock(), Mock(), Mock()
    window.values = {key: Mock() for key in ("last_sync", "last_attempt", "next_sync", "machine", "tenant",
                                            "server")}
    window.refresh()
    assert not {"version", "report_version", "discovery"} & set(window.values)
    window.state_var.set.assert_called_once_with("Brak świeżej synchronizacji")


def test_old_scanner_state_is_not_presented_as_live():
    snapshot = status.AgentStatus(published_at="2000-01-01T00:00:00Z", discovery={"state": "running"})
    assert status.discovery_label(snapshot).startswith("Nieaktualny status skanera")
