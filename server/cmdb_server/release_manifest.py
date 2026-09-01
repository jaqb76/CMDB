"""Signed release protocol, shared with the isolated publication job.

The public key/repository/ref are deployment configuration, never taken from
an upload. Exact payload bytes are signed (no ambiguous JSON canonicalization).
"""
import base64
import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[0-9]+)?"
MAX_MANIFEST = 16384
CONTEXT = b"CMDB-RELEASE-V1\x00"


class ManifestError(ValueError):
    pass


def key_id(raw):
    return hashlib.sha256(raw).hexdigest()[:16]


def decode(value, size=None):
    try:
        raw = base64.b64decode(value, validate=True)
        if size is not None and len(raw) != size:
            raise ValueError()
        return raw
    except (ValueError, TypeError) as exc:
        raise ManifestError("Niepoprawny format podpisu/klucza") from exc


def require(condition):
    if not condition:
        raise ManifestError("Niepoprawny manifest")


def validate(payload, repository, ref):
    try:
        require(set(payload) == {"schema", "repository", "ref", "workflow", "commit", "run_id", "version", "tag", "changelog", "artifacts"})
        require(type(payload["schema"]) is int and payload["schema"] == 2)
        require(payload["repository"] == repository and payload["ref"] == ref)
        require(payload["workflow"] == ".github/workflows/cmdb-tests.yml")
        require(re.fullmatch(r"[0-9a-f]{40}", payload["commit"]))
        require(type(payload["run_id"]) is int and payload["run_id"] > 0)
        require(re.fullmatch(VERSION, payload["version"]) and len(payload["version"]) <= 32)
        require(payload["tag"] == "agent-v" + payload["version"])
        changelog = payload["changelog"]
        require(isinstance(changelog, list) and 0 < len(changelog) <= 30)
        for item in changelog:
            require(set(item) - {"systemy"} == {"category", "text"})
            require(item["category"] in {"added", "fixed", "security"})
            require(isinstance(item["text"], str) and 10 <= len(item["text"]) <= 300)
            if "systemy" in item:
                require(isinstance(item["systemy"], list) and item["systemy"])
                require(not set(item["systemy"]) - {"windows", "linux"})
        artifacts = payload["artifacts"]
        # Wydanie moze dotyczyc jednego systemu: zmiana w skryptach Linuksa nie
        # tworzy nowego pliku dla Windows. Windows wystepuje wylacznie w parze -
        # instalator bez workera nie ma czego zainstalowac, a worker bez
        # instalatora nie ma jak trafic na maszyne.
        require(isinstance(artifacts, list) and 0 < len(artifacts) <= 3)
        expected = {"worker": ("windows", "x86_64", "cmdb-agent.exe"),
                    "setup": ("windows", "x86_64", f"CMDB-Agent-Setup-{payload['version']}.exe"),
                    "source": ("linux", "zrodla", "cmdb-agent-zrodla.tar.gz")}
        rodzaje = {a["kind"] for a in artifacts}
        require(len(rodzaje) == len(artifacts) and not rodzaje - set(expected))
        require(("worker" in rodzaje) == ("setup" in rodzaje))
        for artifact in artifacts:
            require(set(artifact) == {"kind", "os", "arch", "name", "sha256", "size", "protocol"})
            require((artifact["os"], artifact["arch"], artifact["name"]) == expected[artifact["kind"]])
            require(artifact["protocol"] == "cmdb-policy-v1")
            require(type(artifact["size"]) is int and 0 < artifact["size"] <= 128 * 1024 * 1024)
            require(re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]))
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise ManifestError("Manifest nie pasuje do zatwierdzonego repozytorium/protokolu") from exc
    return payload


def sign(payload, private_b64):
    validate(payload, payload["repository"], payload["ref"])
    key = Ed25519PrivateKey.from_private_bytes(decode(private_b64, 32))
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {"key_id": key_id(key.public_key().public_bytes_raw()),
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(key.sign(CONTEXT + raw)).decode()}


def verify(envelope, public_keys, repository, ref):
    try:
        if set(envelope) != {"key_id", "payload", "signature"}:
            raise ManifestError("Niepoprawna koperta manifestu")
        if len(json.dumps(envelope)) > MAX_MANIFEST:
            raise ManifestError("Manifest przekracza limit")
        raw_key = decode(public_keys[envelope["key_id"]], 32)
        if key_id(raw_key) != envelope["key_id"]:
            raise ManifestError("Niezgodny identyfikator klucza")
        raw = decode(envelope["payload"])
        Ed25519PublicKey.from_public_bytes(raw_key).verify(decode(envelope["signature"], 64), CONTEXT + raw)
        return validate(json.loads(raw), repository, ref)
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ManifestError("Brak prawidlowego podpisu zatwierdzonego wydawcy") from exc
