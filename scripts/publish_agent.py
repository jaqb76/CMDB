"""Sign tested bytes, publish a draft, upload assets, then make it visible.

Only the publication job has the signing key/contents:write. Never executes
downloaded binaries; those were tested in separate jobs before this job starts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from cmdb_server.release_manifest import sign, verify, decode, key_id
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def prepare(directory, version, repo, ref, commit, run_id, key):
    changelog = json.loads((directory / "agent-release-changelog.json").read_text(encoding="utf-8"))
    # Wydanie obejmuje tylko te systemy, ktorych pliki faktycznie zbudowano.
    # Zmiana w skryptach jednego systemu nie ma prawa wypchnac drugiego: plik
    # bylby bajt w bajt taki sam jak poprzedni, a opis zmian mowilby nie o nim.
    wszystkie = [("worker", "windows", "x86_64", "cmdb-agent.exe"),
                 ("setup", "windows", "x86_64", f"CMDB-Agent-Setup-{version}.exe"),
                 ("source", "linux", "zrodla", "cmdb-agent-zrodla.tar.gz")]
    files = [pozycja for pozycja in wszystkie if (directory / pozycja[3]).is_file()]
    if not files:
        raise ValueError("Release without artifacts")
    systemy = {pozycja[1] for pozycja in files}
    if ("worker" in {p[0] for p in files}) != ("setup" in {p[0] for p in files}):
        raise ValueError("Windows release needs both the worker and its installer")
    changelog = [{k: v for k, v in wpis.items() if k != "systemy" or len(systemy) > 1}
                 for wpis in changelog
                 if systemy & set(wpis.get("systemy", ("windows", "linux")))]
    if not changelog:
        raise ValueError("Release without a changelog entry for its systems")
    catalog = json.loads((directory / "verified-worker.json").read_text())         if (directory / "verified-worker.json").is_file() else None
    artifacts = []
    for kind, system, arch, name in files:
        content = (directory / name).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if kind == "worker" and catalog != {digest: {"version": version, "arch": arch}}:
            raise ValueError("Worker differs from the tested binary")
        if kind == "source":
            metadata = json.loads((directory / "cmdb-agent-zrodla.json").read_text())
            if metadata["version"] != version or metadata["sha256"] != digest:
                raise ValueError("Source package differs from its tested metadata")
        artifacts.append({"kind": kind, "os": system, "arch": arch, "name": name,
                          "sha256": digest, "size": len(content), "protocol": "cmdb-policy-v1"})
    payload = {"schema": 2, "repository": repo, "ref": ref,
        "workflow": ".github/workflows/cmdb-tests.yml", "commit": commit, "run_id": run_id,
        "version": version, "tag": "agent-v" + version, "changelog": changelog,
        "artifacts": artifacts}
    envelope = sign(payload, key)
    public = Ed25519PrivateKey.from_private_bytes(decode(key, 32)).public_key().public_bytes_raw()
    import base64
    verify(envelope, {key_id(public): base64.b64encode(public).decode()}, repo, ref)
    target = directory / "cmdb-release.json"
    target.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
    return payload, [directory / a[3] for a in files] + [target]


def publish(payload, files, token):
    repo = payload["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository")
    def api(url, data, method="POST", binary=False):
        encoded = data if binary else json.dumps(data).encode()
        request = Request(url, data=encoded, method=method, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/octet-stream" if binary else "application/json",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    labels = {"added": "Dodano", "fixed": "Poprawiono", "security": "Bezpieczeństwo"}
    nazwy = {"windows": "Windows", "linux": "Linux"}
    systemy = sorted({a["os"] for a in payload["artifacts"]})
    notes = ["Automatycznie przetestowane wydanie. Import do CMDB nie zmienia przypisań maszyn.",
             "Dotyczy: " + ", ".join(nazwy[s] for s in systemy) + ".", ""]
    for category in ("added", "fixed", "security"):
        rows = []
        for item in payload["changelog"]:
            if item["category"] != category:
                continue
            # Gdy wydanie obejmuje oba systemy, a wpis dotyczy jednego,
            # mowimy o tym wprost - inaczej czytelnik przypisze zmiane
            # takze temu plikowi, w ktorym jej nie ma.
            wlasne = item.get("systemy")
            dopisek = (" (tylko " + ", ".join(nazwy[s] for s in wlasne) + ")"
                       if wlasne and len(systemy) > 1 else "")
            rows.append(item["text"] + dopisek)
        if rows:
            notes.extend(["## " + labels[category], *["- " + row for row in rows], ""])
    notes.extend([f"Commit: {payload['commit']}",
                  f"CI: https://github.com/{repo}/actions/runs/{payload['run_id']}"])
    # Never replace assets of an existing published version.
    release = api(f"https://api.github.com/repos/{repo}/releases", {
        "tag_name": payload["tag"], "target_commitish": payload["commit"],
        "name": ("CMDB Agent " + payload["version"] + " - "
                 + ", ".join(nazwy[s] for s in systemy)), "draft": True,
        "body": "\n".join(notes)})
    for path in files:
        api(f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets?name={quote(path.name)}",
            path.read_bytes(), binary=True)
    api(f"https://api.github.com/repos/{repo}/releases/{release['id']}", {"draft": False, "make_latest": "false"}, method="PATCH")
    print("Published " + payload["tag"] + "; no CMDB targets changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/claude/os-data-collection-agent-gfz2o8":
        raise SystemExit("Publication allowed only for a push to the approved branch")
    key = os.environ.get("CMDB_RELEASE_SIGNING_KEY", "")
    if not key:
        raise SystemExit("Configure the CMDB_RELEASE_SIGNING_KEY secret in the agent-release environment first")
    payload, files = prepare(args.directory, args.version, os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"], int(os.environ["GITHUB_RUN_ID"]), key)
    publish(payload, files, os.environ["GITHUB_TOKEN"])
