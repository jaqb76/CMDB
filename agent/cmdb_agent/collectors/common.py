"""Pomocnicze funkcje kolektorow - uruchamianie polecen i konwersje."""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


def run_command(args: list[str], timeout: int = 120, check: bool = True) -> str:
    """Uruchamia proces bez powloki i zwraca stdout jako tekst."""
    stdout, kod, stderr = _uruchom(args, timeout)
    if check and kod != 0:
        raise CommandError(f"{args[0]} zakonczyl sie kodem {kod}: {stderr.strip()[:300]}")
    return stdout


def run_command_with_code(args: list[str], timeout: int = 120) -> tuple[str, int]:
    """Stdout wraz z kodem wyjscia.

    Nie kazdy kod rozny od zera oznacza blad: "dnf check-update" zwraca 100,
    gdy sa aktualizacje do zainstalowania, a "apt-get -s" rozroznia kodami
    brak blokady od faktycznej awarii. Bez kodu nie da sie tego odczytac.
    """
    stdout, kod, _ = _uruchom(args, timeout)
    return stdout, kod


def _uruchom(args: list[str], timeout: int) -> tuple[str, int, str]:
    creationflags = 0
    if sys.platform == "win32":
        # Bez migajacego okna konsoli, gdy agent chodzi jako zadanie interaktywne.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"nie znaleziono programu {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"{args[0]} przekroczyl limit {timeout} s") from exc

    return (
        completed.stdout.decode("utf-8", errors="replace"),
        completed.returncode,
        completed.stderr.decode("utf-8", errors="replace"),
    )


def to_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_bool(value, default=None):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def clean(value):
    """Puste stringi i typowe smieci z WMI zamieniamy na None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "n/a", "not specified", "to be filled by o.e.m.",
                                    "default string", "system serial number", "unknown"}:
        return None
    return text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def percent(used: int | None, total: int | None) -> float | None:
    if not total or used is None:
        return None
    return round(used / total * 100, 1)
