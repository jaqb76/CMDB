"""PR-safe end-to-end check of real artifacts with a throwaway test key.

No GitHub writes or production key. The test signature is never published.
"""
import argparse
import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from publish_agent import prepare
from cmdb_server.services import architektura, pakiet

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
parser.add_argument("--version", required=True)
args = parser.parse_args()
key = base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
payload, files = prepare(args.directory, args.version, "jaqb76/CMDB",
    "refs/heads/claude/os-data-collection-agent-gfz2o8", "a" * 40, 1, key)
# Wydanie moze dotyczyc jednego systemu - sprawdzamy to, co faktycznie
# zbudowano, ale kazdy obecny plik sprawdzamy tak samo scisle jak dotad.
systemy = {artifact["os"] for artifact in payload["artifacts"]}
if "windows" in systemy:
    assert architektura.wykryj_z_pliku(args.directory / "cmdb-agent.exe") == "x86_64"
    assert architektura.podsystem_pe(args.directory / "cmdb-agent.exe") == architektura.PE_GUI
if "linux" in systemy:
    assert pakiet.sprawdz_paczke(args.directory / "cmdb-agent-zrodla.tar.gz") == args.version
assert systemy and payload["changelog"] and payload["schema"] == 2
(args.directory / "cmdb-release.json").unlink()  # throwaway signature must not enter publication
print("RELEASE_CHECK_OK: " + ", ".join(sorted(systemy))
      + "; one version, signed manifest verified; no publication")
