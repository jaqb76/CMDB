"""Historyczny punkt wejscia; nowe buildy uzywaja jednego agent_entry.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdb_agent.windows_entry import main

if __name__ == "__main__":
    sys.exit(main())
