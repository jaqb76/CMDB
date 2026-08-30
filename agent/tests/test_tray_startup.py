"""Headless GUI regressions: no network, display, or administrator required."""
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from cmdb_agent.config import AgentConfig
from cmdb_agent.state import AgentState
from cmdb_agent.gui import tray_app, common, settings_window


@pytest.mark.parametrize("enrolled", [False, True])
def test_startup_only_schedules_tray_maintenance(monkeypatch, enrolled):
    root = Mock()
    monkeypatch.setattr(tray_app.tk, "Tk", lambda: root)
    monkeypatch.setattr(tray_app.threading, "Thread", Mock())
    monkeypatch.setattr(tray_app.TrayApp, "_build_icon", Mock())
    monkeypatch.setattr(tray_app.status_module, "read", Mock(return_value=Mock(configured=enrolled, enrolled=enrolled)))
    app = tray_app.TrayApp(AgentConfig())
    app.show_status, app.open_settings = Mock(), Mock()
    assert app.run() == 0
    root.withdraw.assert_called_once()
    assert {call.args[1] for call in root.after.call_args_list} == {app._poll_commands, app._refresh_icon}
    app.show_status.assert_not_called()
    app.open_settings.assert_not_called()
    assert app.status_window.window is None
    # Menu command still opens status on explicit request.
    app._enqueue("status")()
    app._poll_commands()
    app.show_status.assert_called_once()


@pytest.mark.parametrize("gui,visibility", [(False, 0), (True, 1)])
def test_elevated_worker_hidden_but_settings_visible(monkeypatch, gui, visibility):
    shell = Mock()
    shell.shell32.ShellExecuteW.return_value = 42
    monkeypatch.setattr(common, "is_windows", lambda: True)
    monkeypatch.setattr(common, "agent_executable", lambda **_: ("agent.exe", []))
    monkeypatch.setattr(common.ctypes, "windll", shell, raising=False)
    assert common.run_agent_elevated(["configure" if gui else "run"], gui=gui)
    assert shell.shell32.ShellExecuteW.call_args.args[-1] == visibility


def test_settings_save_keeps_advanced_fields_and_does_not_require_bootstrap(tmp_path, monkeypatch):
    window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
    window.config = AgentConfig(server_url="https://cmdb.example", data_dir=tmp_path,
                                pin_sha256="ab" * 32, collect_updates=False, discovery_rate=7)
    window.config_path = tmp_path / "agent.conf"
    window.config_path.write_text('{"future_setting": true}')
    monkeypatch.setattr(settings_window, "_harden_file", Mock())
    monkeypatch.setattr(settings_window, "load_state", lambda _: AgentState(
        server_url="https://cmdb.example", agent_token="key", asset_id="asset"))
    assert window._already_enrolled("https://cmdb.example/")
    assert not window._already_enrolled("https://another.example")
    candidate = replace(window.config, discovery_enabled=True)
    assert window._write_config(candidate)
    payload = json.loads(window.config_path.read_text())
    assert payload["future_setting"] and payload["discovery_enabled"]
    assert payload["discovery_rate"] == 7 and not payload["collect_updates"]
    assert payload["pin_sha256"] == "ab" * 32
    assert "enrollment_token" not in payload and "_source_path" not in payload


@pytest.mark.parametrize("old_bootstrap", ["", "cmdb_ent_previous_bootstrap"])
def test_registered_settings_accept_empty_token_and_retain_limits(tmp_path, monkeypatch, old_bootstrap):
    window = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
    window.config = AgentConfig(server_url="https://cmdb.example", data_dir=tmp_path,
                                collect_updates=False, discovery_budget_seconds=123, enrollment_token=old_bootstrap)
    values = {"server_var": "https://cmdb.example", "token_var": old_bootstrap, "ca_var": "",
              "interval_var": "4", "processes_var": False, "discovery_var": True,
              "discovery_auto_var": False, "discovery_cidrs_var": "10.1.1.0/24"}
    for key, value in values.items():
        setattr(window, key, Mock(get=Mock(return_value=value)))
    window._set_message = Mock()
    monkeypatch.setattr(settings_window, "load_state", lambda _: AgentState(
        server_url="https://cmdb.example", agent_token="key", asset_id="asset"))
    candidate = window._collect()
    assert candidate is not None and candidate.discovery_cidrs == ["10.1.1.0/24"]
    assert not candidate.enrollment_token and not candidate.collect_updates
    assert candidate.discovery_budget_seconds == 123
    window._set_message.assert_not_called()
