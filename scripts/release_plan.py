"""Decide whether a commit range needs an agent release and collect its changelog."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


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


def read_changelog(root: Path, files: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name in sorted(path for path in files if path.startswith(CHANGELOG_PREFIX) and path.endswith(".json")):
        row = json.loads((root / name).read_text(encoding="utf-8"))
        if set(row) != {"category", "text"} or row["category"] not in CATEGORIES:
            raise ValueError(f"Invalid agent changelog fragment: {name}")
        text = " ".join(str(row["text"]).split())
        if not 10 <= len(text) <= 300:
            raise ValueError(f"Invalid agent changelog text: {name}")
        result.append({"category": row["category"], "text": text})
    return result


def plan(root: Path, base: str, head: str) -> tuple[bool, list[dict[str, str]]]:
    files = changed_files(base, head)
    changed = any(is_agent_input(path) for path in files)
    notes = read_changelog(root, files)
    if changed and not notes:
        raise ValueError("Agent changed without a new agent/changelog/*.json fragment")
    return changed, notes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    changed, notes = plan(root, args.base, args.head)
    args.output.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.github_output.open("a", encoding="utf-8") as stream:
        stream.write("agent_changed=" + ("true" if changed else "false") + "\n")
    print("Agent release required" if changed else "No agent inputs changed; release skipped")
