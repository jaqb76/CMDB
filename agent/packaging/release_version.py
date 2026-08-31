"""CI is the version allocator; no git commits/tags or shared mutable counter.

0.6.<workflow run number>+<run attempt> is unique for retries as well as runs.
The source checkout is ephemeral. Local builds keep the checked-in version.
"""
import argparse
import os
from pathlib import Path
import re


def ci_version(run, attempt):
    if not re.fullmatch(r"[1-9][0-9]{0,4}", str(run)) or not re.fullmatch(r"[1-9][0-9]{0,4}", str(attempt)):
        raise ValueError("Invalid CI run/attempt")
    if int(run) > 65535 or int(attempt) > 65535:
        raise ValueError("CI counter exceeds Windows version range; start a new release series")
    return f"0.6.{int(run)}+{int(attempt)}"


def write_version(root, version):
    source = Path(root) / "cmdb_agent" / "__init__.py"
    text = source.read_text(encoding="utf-8")
    text, count = re.subn(r'^__version__ = "[^"]+"$', f'__version__ = "{version}"', text, flags=re.M)
    if count != 1:
        raise ValueError("Missing single version source")
    source.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    version = ci_version(os.environ["GITHUB_RUN_NUMBER"], os.environ["GITHUB_RUN_ATTEMPT"])
    if args.write:
        write_version(Path(__file__).resolve().parents[1], version)
    print(version)
