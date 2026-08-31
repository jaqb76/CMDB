"""Publication ordering, without a GitHub token or network writes."""
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest


@pytest.fixture
def publisher():
    path = Path(__file__).resolve().parents[2] / "scripts" / "publish_agent.py"
    spec = importlib.util.spec_from_file_location("cmdb_test_publisher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failed_upload", [None, 0, 1, 2, 3])
def test_release_visible_only_after_all_uploads(publisher, tmp_path, monkeypatch, failed_upload):
    files = []
    for name in ("cmdb-agent.exe", "CMDB-Agent-Setup-0.6.42+1.exe",
                 "cmdb-agent-zrodla.tar.gz", "cmdb-release.json"):
        path = tmp_path / name
        path.write_bytes(b"test artifact")
        files.append(path)
    calls = []
    uploaded = 0

    def fake_open(request, timeout):
        nonlocal uploaded
        calls.append(request)
        assert timeout == 120
        if "uploads.github.com" in request.full_url:
            index = uploaded
            uploaded += 1
            if index == failed_upload:
                raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(json.dumps({"id": 123}).encode())

    monkeypatch.setattr(publisher, "urlopen", fake_open)
    payload = {"repository": "jaqb76/CMDB", "tag": "agent-v0.6.42+1",
               "version": "0.6.42+1", "commit": "a" * 40, "run_id": 42}
    if failed_upload is None:
        publisher.publish(payload, files, "TEST-NOT-A-TOKEN")
        assert uploaded == 4
        assert calls[-1].method == "PATCH"
        assert json.loads(calls[-1].data) == {"draft": False, "make_latest": "false"}
    else:
        with pytest.raises(HTTPError):
            publisher.publish(payload, files, "TEST-NOT-A-TOKEN")
        assert uploaded == failed_upload + 1
        assert all(request.method != "PATCH" for request in calls)
    assert calls[0].method == "POST"
    assert json.loads(calls[0].data)["draft"] is True
    assert json.loads(calls[0].data)["target_commitish"] == "a" * 40


def test_existing_tag_is_not_overwritten(publisher, monkeypatch):
    calls = []

    def conflict(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 422, "already exists", {}, None)

    monkeypatch.setattr(publisher, "urlopen", conflict)
    payload = {"repository": "jaqb76/CMDB", "tag": "agent-v0.6.42+1",
               "version": "0.6.42+1", "commit": "a" * 40, "run_id": 42}
    with pytest.raises(HTTPError):
        publisher.publish(payload, [], "TEST-NOT-A-TOKEN")
    assert len(calls) == 1 and calls[0].method == "POST"
