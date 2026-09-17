"""Oznaczenie instancji: pasek, etykieta i prefiks w tytule karty.

Rzecz wyglada na kosmetyke, a nie jest: instancja testowa trzymajaca kopie
produkcji wyglada dokladnie tak samo jak produkcja. Pomylka kosztuje wtedy
tyle, ile kosztuje zmiana zrobiona nie tam, gdzie sie mysli.
"""
from __future__ import annotations

import pytest

from cmdb_server.api import ui
from cmdb_server.config import get_settings

from .test_tenant_isolation import _login

HASLO = "haslo-instancji-2026"


@pytest.fixture
def oznacz(monkeypatch):
    """Ustawia oznaczenie tak, jak zrobilby to plik .env.

    Wartosc trafia do szablonow przez globalne pole srodowiska Jinja, liczone
    raz przy imporcie - w tescie podmieniamy je wprost.
    """
    def ustaw(etykieta: str, barwa: str = "bursztyn"):
        monkeypatch.setitem(ui.templates.env.globals, "instancja", etykieta)
        monkeypatch.setitem(ui.templates.env.globals, "instancja_barwa", barwa)

    return ustaw


def test_produkcja_nie_ma_zadnego_paska(client, tenant_a, make_user, oznacz):
    """Pusta etykieta znaczy produkcje - i wtedy nie ma tam nic."""
    oznacz("")
    make_user(tenant_a["id"], "operator@bongo.pl", HASLO)
    _login(client, "operator@bongo.pl", HASLO)

    strona = client.get("/").text
    assert "pasek-instancji" not in strona
    assert "[" not in strona.split("<title>")[1].split("</title>")[0]


def test_dev_ma_pasek_i_prefiks_w_tytule(client, tenant_a, make_user, oznacz):
    oznacz("DEVELOPMENT", "czerwony")
    make_user(tenant_a["id"], "operator@bongo.pl", HASLO)
    _login(client, "operator@bongo.pl", HASLO)

    strona = client.get("/").text
    assert 'class="pasek-instancji pasek-czerwony"' in strona
    assert "DEVELOPMENT" in strona
    # Wlasciwa karte w przegladarce znajduje sie po tytule, nie po kolorze.
    assert strona.split("<title>")[1].startswith("[DEVELOPMENT] ")


def test_oznaczenie_jest_takze_na_logowaniu(client, oznacz):
    """Najwazniejsze miejsce: czlowiek loguje sie "do CMDB", nie patrzac, do ktorego."""
    oznacz("DEVELOPMENT")
    strona = client.get("/login").text

    assert "pasek-instancji" in strona
    assert strona.split("<title>")[1].startswith("[DEVELOPMENT] ")


def test_oznaczenie_jest_takze_w_administracji(client, make_user, oznacz):
    """Ekrany administracji maja wlasny szablon bazowy - latwo je pominac."""
    oznacz("DEVELOPMENT")
    make_user(None, "szef@mojadomena.pl", HASLO)
    _login(client, "szef@mojadomena.pl", HASLO)

    strona = client.get("/admin").text
    assert "pasek-instancji" in strona


def test_dev_wlacza_oznaczenie_bez_wpisywania_etykiety():
    """Zapomniec mozna w obie strony, ale tylko jedna boli.

    Instancja testowa wygladajaca jak produkcja jest grozna; produkcja
    z paskiem "DEVELOPMENT" jest tylko brzydka. Dlatego CMDB_ENV=dev wystarcza.
    """
    ustawienia = get_settings()
    poprzednie_env, poprzednia = ustawienia.env, ustawienia.instancja
    try:
        ustawienia.instancja = ""
        ustawienia.env = "dev"
        assert ustawienia.etykieta_instancji == "DEVELOPMENT"

        ustawienia.env = "prod"
        assert ustawienia.etykieta_instancji == ""

        ustawienia.instancja = "KOPIA Z 17.09"
        assert ustawienia.etykieta_instancji == "KOPIA Z 17.09"
    finally:
        ustawienia.env, ustawienia.instancja = poprzednie_env, poprzednia


def test_barwa_spoza_listy_nie_przechodzi():
    """Literowka w .env ma wywalic serwer przy starcie, a nie dac instancje
    bez oznaczenia - bo tej drugiej nikt nie zauwazy."""
    from pydantic import ValidationError

    from cmdb_server.config import Settings

    with pytest.raises(ValidationError):
        Settings(instancja_barwa="zloty", secret_key="x" * 32)
