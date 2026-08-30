"""Dispatch and lifecycle tests; no real network traffic."""
from unittest.mock import Mock

import pytest

from cmdb_agent import main as commands, windows_entry
from cmdb_agent.config import AgentConfig
from cmdb_agent.gui.tray_app import TrayApp


@pytest.mark.parametrize("args,expected", [([], ["gui"]), (["run"], ["run"]),
    (["--version"], ["--version"]), (["configure"], ["configure"]),
    (["--config", "C:/agent.conf", "status", "--json"], ["--config", "C:/agent.conf", "status", "--json"])])
def test_one_exe_dispatch(monkeypatch, args, expected):
    monkeypatch.setattr(windows_entry.sys, "platform", "win32")
    monkeypatch.setattr(windows_entry, "restore_output", Mock())
    main = Mock(return_value=7)
    monkeypatch.setattr(commands, "main", main)
    assert windows_entry.main(args) == 7
    main.assert_called_once_with(expected)


def test_linux_entry_keeps_cli_semantics(monkeypatch):
    monkeypatch.setattr(windows_entry.sys, "platform", "linux")
    main = Mock(return_value=2)
    monkeypatch.setattr(commands, "main", main)
    assert windows_entry.main([]) == 2
    main.assert_called_once_with([])


def test_output_restored_without_opening_console(monkeypatch):
    output = Mock()
    monkeypatch.setattr(windows_entry.sys, "platform", "win32")
    monkeypatch.setattr(windows_entry.sys, "stdout", None)
    monkeypatch.setattr(windows_entry.sys, "stderr", None)
    inherited = Mock(return_value=output)
    monkeypatch.setattr(windows_entry, "_inherited_output", inherited)
    windows_entry.restore_output()
    assert windows_entry.sys.stdout is output and windows_entry.sys.stderr is output
    assert [c.args[0] for c in inherited.call_args_list] == [-11, -12]


def test_tray_restarts_after_binary_replacement(monkeypatch):
    app = TrayApp.__new__(TrayApp)
    app._executable_signature = (10, 100)
    app.restart_requested = False
    app.root = Mock()
    app.quit = Mock()
    monkeypatch.setattr(app, "_file_signature", lambda: (20, 200))
    app._refresh_icon()
    assert app.restart_requested
    app.quit.assert_called_once()
    app.root.after.assert_not_called()


def test_temporary_missing_binary_during_rename_does_not_restart(monkeypatch):
    from cmdb_agent.gui import tray_app
    app = TrayApp.__new__(TrayApp)
    app._executable_signature = (10, 100)
    app.restart_requested = False
    app.root, app.config, app.icon = Mock(), AgentConfig(), None
    monkeypatch.setattr(app, "_file_signature", lambda: None)
    monkeypatch.setattr(tray_app.status_module, "read", Mock())
    app._refresh_icon()
    assert not app.restart_requested
    app.root.after.assert_called_once()
