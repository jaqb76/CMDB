"""Punkt wejscia dla cmdb-agent-tray.exe (PyInstaller).

Osobny plik zamiast --name na cmdb_agent/main.py, zeby ikona startowala
od razu w trybie graficznym, bez podawania polecenia w argumentach.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdb_agent.main import main_tray

if __name__ == "__main__":
    sys.exit(main_tray(sys.argv[1:]))
