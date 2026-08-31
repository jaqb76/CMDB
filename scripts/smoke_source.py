"""Test the packaged Linux source, not just the source checkout. No network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from cmdb_server.services.pakiet import sprawdz_paczke

archive = Path(sys.argv[1])
version = sprawdz_paczke(archive)
with tempfile.TemporaryDirectory(prefix="cmdb-source-smoke-") as work:
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(work, filter="data")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(work) / "cmdb-agent")
    result = subprocess.run([sys.executable, "-m", "cmdb_agent.main", "--version"],
        env=env, cwd=work, capture_output=True, timeout=30, check=True)
    assert result.stdout.decode().strip() == "cmdb-agent " + version
    probe = subprocess.run([sys.executable, "-m", "cmdb_agent.main", "worker-probe", "--nonce", "a" * 32],
        env=env, cwd=work, capture_output=True, timeout=30, check=True)
    payload = json.loads(probe.stdout)
    assert payload["version"] == version and payload["discovery_control"] == "cmdb-policy-v1"
print("Packaged Linux source: version and worker protocol OK")
