"""Authorization tests: all requests/probes mocked; no network scans."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import json

import pytest

from cmdb_agent import __version__, discovery, main, upgrade
from cmdb_agent.config import AgentConfig
from cmdb_agent.state import AgentState


def response(state, path):
    return {"protocol": 1, "nonce": path.split("nonce=")[1], "asset_id": state.asset_id,
            "machine_id": state.machine_id, "revision": "revision-1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
            "policy": {"enabled": True, "auto_subnets": False, "cidrs": ["10.0.0.0/30"],
                       "interval_seconds": 86400, "max_hosts": 1024, "rate": 32, "budget_seconds": 300}}


@pytest.fixture
def setup_policy(tmp_path, monkeypatch):
    config = AgentConfig(data_dir=tmp_path)
    state = AgentState(asset_id="asset", machine_id="machine", agent_token="token")
    client = Mock()
    client.get.side_effect = lambda path, token: response(state, path)
    scan = Mock(return_value={"scanned_at": "", "ranges": [], "devices": [], "errors": [], "complete": True})
    monkeypatch.setattr(discovery, "scan", scan)
    from cmdb_agent import status
    monkeypatch.setattr(status, "publish", Mock())
    return config, state, client, scan


def test_fresh_policy_every_cycle_and_revocation(setup_policy):
    config, state, client, scan = setup_policy
    discovery.attach_discovery(config, state, {}, client)
    discovery.attach_discovery(config, state, {}, client)
    assert scan.call_count == 1 and client.get.call_count == 2
    assert client.get.call_args_list[0].args[0] != client.get.call_args_list[1].args[0]
    def disabled(path, token):
        payload = response(state, path)
        payload["policy"]["enabled"] = False
        payload["revision"] = "revision-2"
        return payload
    client.get.side_effect = disabled
    discovery.attach_discovery(config, state, {}, client)
    assert scan.call_count == 1 and state.discovery_status["state"] == "disabled"


@pytest.mark.parametrize("field,value", [
    ("nonce", "replayed"), ("asset_id", "another"), ("machine_id", "another"),
    ("expires_at", "2000-01-01T00:00:00+00:00"), ("expires_at", "2999-01-01T00:00:00+00:00"),
    ("expires_at", "2026-08-31T00:00:00"), ("protocol", True),
    ("policy", {"enabled": True}), ("revision", "")])
def test_untrusted_response_never_scans(setup_policy, field, value):
    config, state, client, scan = setup_policy
    def invalid(path, token):
        payload = response(state, path)
        payload[field] = value
        return payload
    client.get.side_effect = invalid
    discovery.attach_discovery(config, state, {}, client)
    scan.assert_not_called()
    assert state.discovery_status["state"] == "unavailable"


def test_outage_does_not_reuse_previous_grant_or_local_config(setup_policy):
    config, state, client, scan = setup_policy
    discovery.attach_discovery(config, state, {}, client)
    scan.reset_mock()
    config.discovery_enabled = True
    state.last_discovery_at = ""
    client.get.side_effect = TimeoutError("offline")
    discovery.attach_discovery(config, state, {}, client)
    discovery.attach_discovery(config, state, {})
    scan.assert_not_called()
    assert state.discovery_status["enabled"] is False


def test_worker_probe_does_not_load_configuration(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", Mock(side_effect=AssertionError("config touched")))
    assert main.main(["worker-probe", "--nonce", "a" * 32]) == 0
    assert json.loads(capsys.readouterr().out) == {"protocol": 1, "version": __version__,
        "nonce": "a" * 32, "commands": ["run", "enroll", "status"], "discovery_control": "cmdb-policy-v1"}
    assert main.main(["worker-probe", "--nonce", "bad"]) == 2


def test_health_check_requires_exact_version(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade.subprocess, "run", Mock(return_value=Mock(
        returncode=0, stdout=b"cmdb-agent 0.5.100", stderr=b"")))
    assert upgrade._czy_dziala(tmp_path / "agent.exe", "0.5.10")[0] is False


def test_failed_candidate_never_replaces_installed_binary(tmp_path, monkeypatch):
    installed = tmp_path / "agent.exe"
    installed.write_bytes(b"existing worker")
    monkeypatch.setattr(upgrade, "wlasny_plik", lambda: installed)
    monkeypatch.setattr(upgrade, "sprawdz_oferte", lambda *_: {"version": "0.5.10", "kind": "binary"})
    monkeypatch.setattr(upgrade, "_pobierz_i_sprawdz", lambda *args: args[-1].write_bytes(b"old tray"))
    monkeypatch.setattr(upgrade, "_czy_dziala", lambda *_: (False, "unsupported worker"))
    replace = Mock(side_effect=AssertionError("must not rename"))
    monkeypatch.setattr(upgrade, "_podmien", replace)
    assert upgrade.zastosuj(AgentConfig(), AgentState(agent_token="token"), Mock()) is None
    assert installed.read_bytes() == b"existing worker"
    replace.assert_not_called()
