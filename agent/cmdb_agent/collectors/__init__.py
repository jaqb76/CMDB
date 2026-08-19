"""Kolektory danych dla poszczegolnych systemow operacyjnych."""
from __future__ import annotations

import sys

from .base import BaseCollector


def get_collector(config) -> BaseCollector:
    """Wybiera kolektor pasujacy do biezacego systemu."""
    if sys.platform == "win32":
        from .windows import WindowsCollector

        return WindowsCollector(config)
    if sys.platform.startswith("linux"):
        from .linux import LinuxCollector

        return LinuxCollector(config)
    raise RuntimeError(
        f"system '{sys.platform}' nie jest jeszcze obslugiwany - "
        "dodaj kolektor w cmdb_agent/collectors/"
    )
