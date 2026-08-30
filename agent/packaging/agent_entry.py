"""Punkt wejscia dla cmdb-agent.exe (PyInstaller).

Wskazanie PyInstallerowi wprost cmdb_agent/main.py nie dziala: modul uzywa
importow wzglednych ("from . import status"), a uruchomiony jako __main__
traci kontekst pakietu i konczy sie bledem "attempted relative import with
no known parent package". Dlatego punktem wejscia jest osobny plik spoza
pakietu, ktory importuje go normalnie.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdb_agent.windows_entry import main

if __name__ == "__main__":
    sys.exit(main())
