"""Blokada logowania do panelu po nieudanych probach.

Tokeny agentow mialy limit od poczatku, panel nie mial zadnego - a to on jest
jedynym miejscem, gdzie zgadywanie hasla ma sens. Serwer wystawiony do
internetu dostaje staly ruch skanerow.

Stan trzyma baza, nie pamiec procesu: serwer produkcyjny dziala w kilku
procesach roboczych, wiec licznik w pamieci dawalby tylokrotnie wiecej prob,
ile jest procesow, a restart zerowalby go doszczetnie.
"""
from __future__ import annotations

import pytest
from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import BlokadaLogowania
from cmdb_server.services import logowanie

HASLO = "haslo-do-testow-123"


@pytest.fixture()
def konto(make_user, tenant_a):
    make_user(tenant_a["id"], "admin@blokada.pl", HASLO)
    return "admin@blokada.pl"


def _zaloguj(client, email, haslo):
    return client.post(
        "/login", data={"email": email, "password": haslo}, follow_redirects=False
    )


def test_poprawne_haslo_wpuszcza(client, konto):
    assert _zaloguj(client, konto, HASLO).status_code == 303


def test_trzecia_pomylka_zaklada_blokade(client, konto):
    for _ in range(2):
        assert _zaloguj(client, konto, "zle").status_code == 401

    trzecia = _zaloguj(client, konto, "zle")
    assert trzecia.status_code == 429
    assert "zablokowane" in trzecia.text.lower()


def test_po_blokadzie_poprawne_haslo_tez_nie_wpuszcza(client, konto):
    """Inaczej blokada byla by tylko utrudnieniem, a nie zabezpieczeniem."""
    for _ in range(3):
        _zaloguj(client, konto, "zle")

    odpowiedz = _zaloguj(client, konto, HASLO)
    assert odpowiedz.status_code == 429
    assert "cmdb_session" not in odpowiedz.cookies


def test_udane_logowanie_kasuje_licznik(client, konto):
    """Dwie pomylki i poprawne haslo nie moga zblizac do blokady."""
    _zaloguj(client, konto, "zle")
    _zaloguj(client, konto, "zle")
    assert _zaloguj(client, konto, HASLO).status_code == 303

    # Licznik od zera: kolejne dwie pomylki nadal nie blokuja.
    assert _zaloguj(client, konto, "zle").status_code == 401
    assert _zaloguj(client, konto, "zle").status_code == 401


def test_blokada_przezywa_restart(client, konto):
    """Licznik w pamieci procesu zerowalby sie przy kazdym wdrozeniu,
    a atakujacy nie musi czekac na restart - wystarczy, ze go doczeka."""
    for _ in range(3):
        _zaloguj(client, konto, "zle")

    with SessionLocal() as db:
        wpisy = db.query(BlokadaLogowania).all()
    assert wpisy, "blokada musi byc zapisana w bazie, nie w pamieci"
    assert any(w.blokada_do is not None for w in wpisy)


def test_blokowany_jest_takze_adres(client, tenant_a, make_user):
    """Proba rozproszona po wielu kontach z jednego adresu tez ma byc odciete."""
    for numer in range(3):
        make_user(tenant_a["id"], f"konto{numer}@blokada.pl", HASLO)

    for numer in range(3):
        _zaloguj(client, f"konto{numer}@blokada.pl", "zle")

    # Czwarte konto, nietkniete - blokuje adres, nie konto.
    make_user(tenant_a["id"], "czwarte@blokada.pl", HASLO)
    assert _zaloguj(client, "czwarte@blokada.pl", HASLO).status_code == 429


def test_komunikat_nie_zdradza_czy_konto_istnieje(client, konto):
    """Inaczej formularz dalby sie uzyc do sprawdzania, ktore konta istnieja."""
    istniejace = _zaloguj(client, konto, "zle")
    nieistniejace = _zaloguj(client, "nie-ma-takiego@blokada.pl", "zle")

    assert istniejace.status_code == nieistniejace.status_code == 401
    assert "Nieprawidłowy login lub hasło." in istniejace.text
    assert "Nieprawidłowy login lub hasło." in nieistniejace.text


# --- zdejmowanie blokady ----------------------------------------------------

def test_administrator_moze_zdjac_blokade(client, konto):
    """Kto zna adres administratora, moze go zablokowac trzema bledami -
    musi wiec istniec droga wyjscia, ktora nie wymaga czekania."""
    for _ in range(3):
        _zaloguj(client, konto, "zle")
    assert _zaloguj(client, konto, HASLO).status_code == 429

    # Blokada obejmuje konto ORAZ adres, wiec zdjecie samego konta nie
    # wystarczy, gdy administrator pomylil haslo u siebie.
    with SessionLocal() as db:
        logowanie.odblokuj(db, email=konto)
    assert _zaloguj(client, konto, HASLO).status_code == 429, "adres nadal zablokowany"

    with SessionLocal() as db:
        assert logowanie.odblokuj(db, wszystko=True) >= 1

    assert _zaloguj(client, konto, HASLO).status_code == 303


def test_zdjecie_blokady_nieistniejacej_nie_jest_bledem(client):
    with SessionLocal() as db:
        assert logowanie.odblokuj(db, email="nikt@blokada.pl") == 0


# --- okno czasowe -----------------------------------------------------------

def test_dawne_pomylki_nie_sumuja_sie_z_nowymi(client, konto, monkeypatch):
    """Trzy pomylki rozlozone na pol roku to nie jest atak."""
    from datetime import timedelta

    from cmdb_server.models import utcnow

    _zaloguj(client, konto, "zle")
    _zaloguj(client, konto, "zle")

    # Cofamy ostatnia probe poza okno.
    with SessionLocal() as db:
        for wpis in db.query(BlokadaLogowania).all():
            wpis.ostatnia_proba = utcnow() - timedelta(hours=logowanie.OKNO_GODZIN + 1)
        db.commit()

    assert _zaloguj(client, konto, "zle").status_code == 401, "licznik mial ruszyc od nowa"


def test_prog_i_czas_sa_konfigurowalne(client, konto, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_failures", 2)
    for _ in range(1):
        assert _zaloguj(client, konto, "zle").status_code == 401
    assert _zaloguj(client, konto, "zle").status_code == 429
