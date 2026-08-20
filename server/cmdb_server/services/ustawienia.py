"""Ustawienia obowiazujace dla danej firmy.

Firmy roznia sie charakterem: flota laptopow handlowcow bywa offline
tygodniami, a serwerownia odzywa sie co godzine i tydzien ciszy oznacza
awarie. Jedna wartosc dla wszystkich albo zalewa falszywymi alarmami,
albo przemilcza prawdziwe.

Puste ustawienie firmy znaczy "jak w konfiguracji serwera" - nie kopiujemy
wartosci domyslnej do kazdej firmy, bo zmiana globalna przestalaby wtedy
cokolwiek zmieniac.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import get_settings
from ..models import Tenant, utcnow


def interwal_raportowania(firma: Tenant | None) -> int:
    domyslny = get_settings().report_interval_seconds
    if firma is None or not firma.report_interval_seconds:
        return domyslny
    return firma.report_interval_seconds


def prog_bez_kontaktu(firma: Tenant | None) -> int:
    domyslny = get_settings().stale_after_hours
    if firma is None or not firma.stale_after_hours:
        return domyslny
    return firma.stale_after_hours


def retencja_raportow(firma: Tenant | None) -> int:
    domyslna = get_settings().snapshot_retention
    if firma is None or firma.snapshot_retention is None:
        return domyslna
    return firma.snapshot_retention


def granica_aktywnosci(firma: Tenant | None) -> datetime:
    """Moment, przed ktorym brak kontaktu oznacza agenta nieaktywnego."""
    return utcnow() - timedelta(hours=prog_bez_kontaktu(firma))
