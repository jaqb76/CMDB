"""Decide whether a commit range needs an agent release and collect its changelog."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


SYSTEMS = ("windows", "linux")

# Co nalezy wylacznie do jednego systemu. Reszta wejsc agenta jest wspolna:
# kod pakietu dziala na obu, wiec jego zmiana dotyczy obu wydan.
SYSTEM_ONLY = {
    "linux": (
        "agent/packaging/install-agent.sh",
        "agent/packaging/uninstall-agent.sh",
    ),
    "windows": (
        "agent/packaging/install.ps1",
        "agent/packaging/install-agent.ps1",
        "agent/packaging/uninstall-agent.ps1",
        "agent/packaging/build-agent.ps1",
        "agent/packaging/cmdb-agent.iss",
        "agent/packaging/agent_entry.py",
        "agent/packaging/tray_entry.py",
        "agent/packaging/smoke-gui.py",
        "agent/packaging/smoke-unified.py",
        "agent/packaging/CMDB-Agent-Setup.exe",
    ),
}

AGENT_INPUTS = (
    "agent/cmdb_agent/",
    "agent/packaging/",
    "agent/pyproject.toml",
    "scripts/publish_agent.py",
    "scripts/check_release.py",
    "server/cmdb_server/release_manifest.py",
)
CHANGELOG_PREFIX = "agent/changelog/"
CATEGORIES = {"added", "fixed", "security"}


def changed_files(base: str, head: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", base) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("Release comparison requires full commit identifiers")
    # Pierwszy push nowej galezi ma event.before zlozony z samych zer.
    # Porownujemy wtedy z rodzicem, zamiast wywracac caly workflow.
    if base == "0" * 40:
        parent = subprocess.run(["git", "rev-parse", head + "^"], check=False,
                                capture_output=True, text=True)
        if parent.returncode == 0:
            base = parent.stdout.strip()
        else:
            result = subprocess.run(["git", "show", "--pretty=", "--name-only", head],
                                    check=True, capture_output=True, text=True)
            return [line for line in result.stdout.splitlines() if line]
    result = subprocess.run(["git", "diff", "--name-only", base, head, "--"],
                            check=True, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def is_agent_input(path: str) -> bool:
    return any(path == item or path.startswith(item) for item in AGENT_INPUTS)


def system_of(path: str) -> str | None:
    """System, do ktorego nalezy plik. None znaczy: wspolny dla obu."""
    for system, paths in SYSTEM_ONLY.items():
        if path in paths:
            return system
    return None


def touched_systems(files: list[str]) -> set[str]:
    """Ktorych wydan dotyka ta zmiana.

    Zmiana wylacznie w skryptach jednego systemu nie ma prawa wypchnac wydania
    dla drugiego: plik dla Windows bylby bajt w bajt taki sam jak poprzedni,
    a jego opis zmian mowilby o Linuksie.
    """
    dotkniete: set[str] = set()
    for path in files:
        if not is_agent_input(path):
            continue
        system = system_of(path)
        if system is None:
            return set(SYSTEMS)          # wejscie wspolne - oba wydania
        dotkniete.add(system)
    return dotkniete


def notes_for(notes: list[dict], system: str) -> list[dict]:
    """Opis zmian jednego wydania: wpisy tego systemu oraz wspolne."""
    return [{k: v for k, v in wpis.items() if k != "systemy"}
            for wpis in notes if system in wpis.get("systemy", SYSTEMS)]


def read_changelog(root: Path, files: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name in sorted(path for path in files if path.startswith(CHANGELOG_PREFIX) and path.endswith(".json")):
        row = json.loads((root / name).read_text(encoding="utf-8"))
        if {"category", "text"} - set(row) or set(row) - {"category", "text", "systemy"}:
            raise ValueError(f"Invalid agent changelog fragment: {name}")
        if row["category"] not in CATEGORIES:
            raise ValueError(f"Invalid agent changelog fragment: {name}")
        text = " ".join(str(row["text"]).split())
        if not 10 <= len(text) <= 300:
            raise ValueError(f"Invalid agent changelog text: {name}")
        wpis = {"category": row["category"], "text": text}
        # Wpis moze dotyczyc jednego systemu. Bez tego pola dotyczy obu -
        # tak jak wiekszosc zmian, bo kod agenta jest wspolny.
        systemy = row.get("systemy")
        if systemy is not None:
            if not isinstance(systemy, list) or not systemy or set(systemy) - set(SYSTEMS):
                raise ValueError(f"Invalid agent changelog systems: {name}")
            wpis["systemy"] = sorted(set(systemy))
        result.append(wpis)
    return result


def plan(root: Path, base: str, head: str) -> tuple[set[str], list[dict[str, str]]]:
    files = changed_files(base, head)
    systemy = touched_systems(files)
    notes = read_changelog(root, files)
    if systemy and not notes:
        raise ValueError("Agent changed without a new agent/changelog/*.json fragment")
    for system in sorted(systemy):
        if not notes_for(notes, system):
            raise ValueError(
                f"Agent changed for {system} without a changelog entry for that system")
    return systemy, notes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    systemy, notes = plan(root, args.base, args.head)
    args.output.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write("agent_changed=" + ("true" if systemy else "false") + "\n")
        for system in SYSTEMS:
            stream.write(
                f"{system}_changed=" + ("true" if system in systemy else "false") + "\n")
    print("Agent release required for: " + (", ".join(sorted(systemy)) or "(nothing)"))
