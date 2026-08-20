"""Punkt wejscia dla cmdb-agent-tray.exe (PyInstaller).

Bez argumentow uruchamia ikone w zasobniku. Z argumentami zachowuje sie jak
zwykly agent - dzieki temu okno ustawien ("configure") mozna otworzyc tym
samym plikiem. Jest to konieczne, bo cmdb-agent.exe budowany jest bez
warstwy graficznej: to on chodzi jako SYSTEM na kazdej maszynie i nie ma
powodu, zeby wozil ze soba tkinter, pystray i Pillow.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdb_agent.main import main, main_tray

if __name__ == "__main__":
    argumenty = sys.argv[1:]
    sys.exit(main(argumenty) if argumenty else main_tray())
