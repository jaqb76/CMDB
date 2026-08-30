from unittest.mock import Mock

import pytest

from cmdb_agent import main
from cmdb_agent.config import AgentConfig
from cmdb_agent.state import AgentState
from cmdb_agent.transport import ApiError


@pytest.mark.parametrize("code", [401, 403, 409])
def test_rejected_credential_never_automatically_reenrolls(tmp_path, monkeypatch, code):
    config = AgentConfig(server_url="https://cmdb.example", enrollment_token="bootstrap", data_dir=tmp_path)
    state = AgentState(agent_token="revoked", asset_id="asset", machine_id="machine")
    monkeypatch.setattr(main, "get_collector", lambda c: object())
    monkeypatch.setattr(main, "build_report", lambda c: {"machine_id": "machine"})
    monkeypatch.setattr(main, "flush_spool", lambda *a: None)
    enroll = Mock()
    monkeypatch.setattr(main, "do_enroll", enroll)
    monkeypatch.setattr(main, "record_attempt", Mock())
    client = Mock()
    client.post.side_effect = ApiError(code, "rejected")
    assert main.do_run(config, state, client) == 2
    enroll.assert_not_called()
    main.record_attempt.assert_called_once()


def test_bootstrap_removed_but_other_configuration_preserved(tmp_path):
    import json
    from cmdb_agent.config import load_config, forget_enrollment_token
    path = tmp_path / "agent.conf"
    path.write_text(json.dumps({"server_url": "https://cmdb.example", "enrollment_token": "bootstrap", "collect_processes": False}))
    config = load_config(path)
    forget_enrollment_token(config)
    saved = json.loads(path.read_text())
    assert "enrollment_token" not in saved
    assert saved["server_url"] == "https://cmdb.example"
    assert saved["collect_processes"] is False
    assert config.enrollment_token == ""
