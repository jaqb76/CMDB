import json
import importlib.util
from pathlib import Path
import subprocess

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_plan.py"
_SPEC = importlib.util.spec_from_file_location("cmdb_release_plan", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
is_agent_input = _MODULE.is_agent_input
read_changelog = _MODULE.read_changelog


def test_only_runtime_and_packaging_inputs_request_release():
    assert is_agent_input("agent/cmdb_agent/main.py")
    assert is_agent_input("agent/packaging/cmdb-agent.iss")
    assert not is_agent_input("server/cmdb_server/templates/assets.html")
    assert not is_agent_input("agent/tests/test_discovery.py")
    assert not is_agent_input("docs/agent-windows.md")


def test_changelog_is_required_and_strict(tmp_path):
    path = tmp_path / "agent/changelog/change.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"category": "fixed", "text": "Poprawiono działanie zasobnika Windows."}))
    assert read_changelog(tmp_path, ["agent/changelog/change.json"])[0]["category"] == "fixed"
    path.write_text(json.dumps({"category": "unknown", "text": "Poprawiono działanie zasobnika Windows."}))
    with pytest.raises(ValueError):
        read_changelog(tmp_path, ["agent/changelog/change.json"])


def test_first_push_uses_parent_instead_of_zero_object(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README").write_text("one")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True)
    (tmp_path / "README").write_text("two")
    subprocess.run(["git", "commit", "-qam", "two"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    old = _MODULE.subprocess
    try:
        # changed_files korzysta z cwd procesu, dlatego na chwile przekazujemy
        # wywolania do malego repozytorium testowego.
        class Local:
            @staticmethod
            def run(args, **kwargs):
                return subprocess.run(args, cwd=tmp_path, **kwargs)
        _MODULE.subprocess = Local
        assert _MODULE.changed_files("0" * 40, head) == ["README"]
    finally:
        _MODULE.subprocess = old
