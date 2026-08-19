"""Szkielet kolektora.

Kolektor sklada sie z niezaleznych krokow. Kazdy krok wypelnia jedna sciezke
w raporcie (np. "hardware.cpu"). Wyjatek w jednym kroku nie przerywa calosci -
laduje na liscie errors, a raport idzie do serwera z tym, co udalo sie zebrac.
To wazne w praktyce: na czesci maszyn pojedyncze zapytania WMI potrafia
zwrocic blad uprawnien albo timeout, a niekompletny raport jest duzo lepszy
niz jego brak.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

Step = tuple[str, Callable[[], Any]]


def set_path(target: dict, path: str, value: Any) -> None:
    """set_path(d, 'hardware.cpu', {...}) -> d['hardware']['cpu'] = {...}"""
    keys = path.split(".")
    node = target
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


class BaseCollector:
    name = "base"
    os_family = "unknown"

    def __init__(self, config):
        self.config = config
        self.errors: list[dict] = []

    # --- do nadpisania w klasach pochodnych -------------------------------

    def machine_id(self) -> str:
        """Stabilny identyfikator maszyny - nie moze zmieniac sie miedzy startami."""
        raise NotImplementedError

    def identity(self) -> dict:
        raise NotImplementedError

    def steps(self) -> list[Step]:
        return []

    # --- wspolna logika ---------------------------------------------------

    def record_error(self, where: str, message: str) -> None:
        log.warning("kolektor %s: %s", where, message)
        self.errors.append({"collector": where, "message": str(message)[:500]})

    def limit(self, items: list) -> list:
        """Przycina zbyt dlugie listy - raport nie moze urosnac bez ograniczen."""
        cap = getattr(self.config, "max_items_per_section", 5000)
        if len(items) > cap:
            self.record_error(
                "limit", f"lista przycieta z {len(items)} do {cap} pozycji"
            )
            return items[:cap]
        return items

    def collect(self) -> tuple[dict, list[dict]]:
        """Uruchamia wszystkie kroki i zwraca (sekcje, bledy)."""
        sections: dict[str, Any] = {}
        for path, func in self.steps():
            started = time.monotonic()
            try:
                value = func()
            except Exception as exc:  # celowo szeroko - zaden krok nie moze wywrocic agenta
                self.record_error(path, f"{type(exc).__name__}: {exc}")
                continue
            if value is not None:
                set_path(sections, path, value)
            log.debug("krok %s zajal %.0f ms", path, (time.monotonic() - started) * 1000)
        return sections, self.errors
