"""Wersja portalu w stopce.

Pytanie "na czym pracujemy" pada przy kazdym zgloszeniu bledu, a strona przed
wdrozeniem i po nim wyglada tak samo. Wazniejsze od samego pokazania numeru
jest to, ZEBY NIE ZMYSLAC: falszywy numer kazalby uznac poprawke za wdrozona.
"""
from __future__ import annotations

from cmdb_server import wersja

from .test_tenant_isolation import _login

HASLO = "bardzo-dlugie-haslo"


def _zalogowany(client, tenant, make_user, email="admin@firma-a.pl"):
    make_user(tenant["id"], email, HASLO)
    _login(client, email, HASLO)


def test_stopka_pokazuje_wersje(client, tenant_a, make_user):
    _zalogowany(client, tenant_a, make_user)
    strona = client.get("/").text
    assert "wersja " + wersja.opis() in strona


def test_wersja_ze_srodowiska_ma_pierwszenstwo(monkeypatch):
    """Obraz produkcyjny nie ma katalogu .git - numer wpisuje sie przy
    budowaniu i to on opisuje dzialajacy obraz."""
    wersja.opis.cache_clear()
    monkeypatch.setenv("CMDB_WERSJA", "2026.09.01-abc1234")
    try:
        assert wersja.opis() == "2026.09.01-abc1234"
    finally:
        wersja.opis.cache_clear()


def test_pusta_zmienna_nie_daje_pustej_stopki(monkeypatch):
    """Zmienna ustawiona na pusto to brak informacji, a nie wersja o pustej
    nazwie - stopka ma wtedy siegnac po nastepne zrodlo."""
    wersja.opis.cache_clear()
    monkeypatch.setenv("CMDB_WERSJA", "   ")
    try:
        assert wersja.opis().strip()
    finally:
        wersja.opis.cache_clear()


def test_nieznana_zamiast_zgadywania(monkeypatch):
    """Gdy nie ma ani zmiennej, ani repozytorium, mowimy o tym wprost.

    Wersja pakietu nie jest tu ratunkiem: "0.1.0" nie zmienilo sie od poczatku
    projektu, wiec wygladaloby jak numer wydania, nie mowiac o niczym."""
    wersja.opis.cache_clear()
    monkeypatch.delenv("CMDB_WERSJA", raising=False)
    monkeypatch.setattr(wersja, "_z_repozytorium", lambda: None)
    try:
        assert wersja.opis() == wersja.NIEZNANA
    finally:
        wersja.opis.cache_clear()


def test_health_podaje_wersje(client):
    """Czy wdrozenie doszlo, musi dac sie sprawdzic bez logowania do panelu
    i bez dostepu do serwera - inaczej jedynym dowodem jest 'chyba widac'."""
    dane = client.get("/api/v1/health").json()
    assert dane["wersja"] == wersja.opis()
