"""Czyja jest piłka: kiedy zgłoszenie czeka na naszą odpowiedź.

Regula jest jedna: zgloszenie czeka, dopoki ostatnia wiadomosc w watku
przyszla od klienta. Kasuje ja WYLACZNIE odpowiedz, ktora do niego poszla -
nie obejrzenie sprawy, nie zmiana statusu, nie komentarz wewnetrzny.

Dzieki temu nie trzeba nigdzie trzymac stanu "przeczytane". Cena jest taka,
ze regula musi byc szczelna w kilku miejscach naraz - i wlasnie o tym sa te
testy.
"""
from __future__ import annotations

import pytest

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    STATUS_ZAMKNIETE,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    HelpdeskUstawienia,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    Zgloszenie,
)
from cmdb_server.security import hash_password
from cmdb_server.services import helpdesk, helpdesk_wysylka, sekrety

from .test_helpdesk_skrzynka import AtrapaSMTP
from .test_tenant_isolation import _extract_csrf, _login

HASLO = "haslo-czekania-2026"


@pytest.fixture
def smtp(monkeypatch):
    AtrapaSMTP.wyslane = []
    monkeypatch.setattr(helpdesk_wysylka, "_polaczenie", lambda konfiguracja: AtrapaSMTP())
    return AtrapaSMTP


def _firma(tenant_id: str) -> None:
    with SessionLocal() as db:
        helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_id), "BON")
        helpdesk.dodaj_domene(db, tenant_id, "bongo.pl")
        db.commit()


def _skrzynka() -> None:
    with SessionLocal() as db:
        db.add(HelpdeskUstawienia(
            klucz="helpdesk", imap_host="imap.mojadomena.pl",
            smtp_host="smtp.mojadomena.pl", smtp_uzytkownik="helpdesk@mojadomena.pl",
            smtp_haslo_szyfr=sekrety.zaszyfruj("tajne"),
            nadawca="helpdesk@mojadomena.pl", nazwa_nadawcy="Helpdesk", aktywne=True,
        ))
        db.commit()


def _technik(email: str, firmy: list[str]) -> str:
    with SessionLocal() as db:
        user = PortalUser(tenant_id=None, email=email, full_name="Wladek Nowak",
                          password_hash=hash_password(HASLO), role="admin")
        db.add(user)
        db.flush()
        for tenant_id in firmy:
            helpdesk.nadaj_dostep(db, user.id, tenant_id)
        db.commit()
        return user.id


def _zgloszenie(tenant_id: str, temat: str = "Nie dziala drukarka") -> str:
    with SessionLocal() as db:
        z = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_id, temat=temat, tresc="Od rana nie dziala.",
            zglaszajacy_email="jan@bongo.pl",
        )
        db.commit()
        return z.id


def _czeka(tenant_id: str) -> set[str]:
    with SessionLocal() as db:
        return helpdesk.czekaja_na_odpowiedz(db, [tenant_id])


# --- sama regula ------------------------------------------------------------

def test_nowe_zgloszenie_czeka_od_razu(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])

    assert zgloszenie_id in _czeka(tenant_a["id"])


def test_odpowiedz_do_klienta_kasuje_licznik(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]])

    with SessionLocal() as db:
        wladek = db.query(PortalUser).filter_by(email="wladek@mojadomena.pl").one()
        helpdesk_wysylka.odpowiedz_klientowi(
            db, db.get(Zgloszenie, zgloszenie_id), "Prosze zrestartowac.", wladek
        )
        db.commit()

    assert zgloszenie_id not in _czeka(tenant_a["id"])


def test_odpowiedz_klienta_wraca_do_czekajacych(client, tenant_a, smtp):
    """Pilka odbija sie tam i z powrotem, a licznik ma za nia nadazac."""
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]])

    with SessionLocal() as db:
        wladek = db.query(PortalUser).filter_by(email="wladek@mojadomena.pl").one()
        helpdesk_wysylka.odpowiedz_klientowi(
            db, db.get(Zgloszenie, zgloszenie_id), "Prosze zrestartowac.", wladek
        )
        db.commit()
    assert zgloszenie_id not in _czeka(tenant_a["id"])

    with SessionLocal() as db:
        helpdesk.dopisz_wiadomosc(
            db, db.get(Zgloszenie, zgloszenie_id), rodzaj="od_klienta",
            tresc="Zrestartowalem, dalej nie dziala.", autor_email="jan@bongo.pl",
        )
        db.commit()

    assert zgloszenie_id in _czeka(tenant_a["id"])


def test_komentarz_wewnetrzny_nie_kasuje_licznika(client, tenant_a, smtp):
    """Notatka dla siebie nie jest odpowiedzia - klient nic nie dostal."""
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    technik_id = _technik("wladek@mojadomena.pl", [tenant_a["id"]])

    with SessionLocal() as db:
        helpdesk.dopisz_wiadomosc(
            db, db.get(Zgloszenie, zgloszenie_id), rodzaj=WPIS_WEWNETRZNY,
            tresc="Sprawdzic sterownik.", autor=db.get(PortalUser, technik_id),
        )
        db.commit()

    assert zgloszenie_id in _czeka(tenant_a["id"])


def test_potwierdzenie_automatyczne_nie_kasuje_licznika(client, tenant_a, smtp):
    """Automat pisze do klienta, ale nikt mu nie odpowiedzial.

    To jest pulapka tej reguly: potwierdzenie przyjecia idzie do klienta
    w chwili powstania zgloszenia. Gdyby liczylo sie jak odpowiedz, KAZDE nowe
    zgloszenie kasowaloby sie samo i licznik nigdy nic by nie pokazal.
    """
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])

    with SessionLocal() as db:
        helpdesk_wysylka.wyslij_potwierdzenie(db, db.get(Zgloszenie, zgloszenie_id))
        db.commit()
        # Potwierdzenie faktycznie wyszlo - to nie jest test na jego brak.
        assert db.query(WpisZgloszenia).filter_by(
            zgloszenie_id=zgloszenie_id, rodzaj=WPIS_DO_KLIENTA
        ).count() == 1

    assert zgloszenie_id in _czeka(tenant_a["id"])


def test_niewyslana_odpowiedz_nie_kasuje_licznika(client, tenant_a, monkeypatch):
    """Klient nic nie dostal, wiec pilka dalej jest po naszej stronie."""
    import smtplib

    monkeypatch.setattr(
        helpdesk_wysylka, "_polaczenie",
        lambda konfiguracja: AtrapaSMTP(blad=smtplib.SMTPException("serwer nie odpowiada")),
    )
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    technik_id = _technik("wladek@mojadomena.pl", [tenant_a["id"]])

    with SessionLocal() as db:
        with pytest.raises(helpdesk_wysylka.BladWysylki):
            helpdesk_wysylka.odpowiedz_klientowi(
                db, db.get(Zgloszenie, zgloszenie_id), "Prosze zrestartowac.",
                db.get(PortalUser, technik_id),
            )
        db.commit()

    assert zgloszenie_id in _czeka(tenant_a["id"])


def test_zamkniete_zgloszenie_nie_czeka(client, tenant_a, smtp):
    """Zamkniecie jest odpowiedzia samo w sobie.

    Wiadomosc o zakonczeniu bywa wylaczona w ustawieniach skrzynki, wiec bez
    tego wyjatku zamkniete zgloszenie zostawaloby w liczniku na zawsze.
    """
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])

    with SessionLocal() as db:
        helpdesk.zmien_status(db, db.get(Zgloszenie, zgloszenie_id),
                              STATUS_ZAMKNIETE, autor="technik")
        db.commit()

    assert zgloszenie_id not in _czeka(tenant_a["id"])


def test_zmiana_statusu_nie_kasuje_licznika(client, tenant_a, smtp):
    """Przestawienie na "W trakcie" to nie jest odpowiedz dla klienta."""
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])

    with SessionLocal() as db:
        helpdesk.zmien_status(db, db.get(Zgloszenie, zgloszenie_id),
                              "w_trakcie", autor="technik")
        db.commit()

    assert zgloszenie_id in _czeka(tenant_a["id"])


# --- granica firm -----------------------------------------------------------

def test_licznik_nie_wychodzi_poza_firmy_technika(client, tenant_a, tenant_b, smtp):
    _firma(tenant_a["id"])
    with SessionLocal() as db:
        helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_b["id"]), "KLE")
        helpdesk.dodaj_domene(db, tenant_b["id"], "klepsydra.pl")
        db.commit()
    _zgloszenie(tenant_a["id"])
    _zgloszenie(tenant_b["id"], "Dysk")
    _technik("wladek@mojadomena.pl", [tenant_a["id"]])

    with SessionLocal() as db:
        wladek = db.query(PortalUser).filter_by(email="wladek@mojadomena.pl").one()
        firmy = helpdesk.firmy_technika(db, wladek)
        assert helpdesk.ile_czeka(db, firmy) == 1


# --- panel ------------------------------------------------------------------

def test_belka_pokazuje_liczbe_i_znika_po_odpowiedzi(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]])
    _login(client, "wladek@mojadomena.pl", HASLO)

    strona = client.get("/helpdesk").text
    assert "czeka-sa" in strona, "belka miala pokazac, ze cos czeka"
    assert "Czeka na odpowiedź" in strona
    assert "znacznik-czeka" in strona, "zgloszenie mialo byc wyroznione na tablicy"

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _extract_csrf(client.get(sciezka).text),
        "rodzaj": WPIS_DO_KLIENTA, "tresc": "Prosze zrestartowac.",
    }, follow_redirects=False)

    strona = client.get("/helpdesk").text
    assert "czeka-sa" not in strona
    assert "Wszystko odpisane" in strona


def test_licznik_jako_json(client, tenant_a, smtp):
    """Trasa do odswiezania belki. Niczego nie oznacza jako zalatwione."""
    _firma(tenant_a["id"])
    _zgloszenie(tenant_a["id"])
    _zgloszenie(tenant_a["id"], "Druga sprawa")
    _technik("wladek@mojadomena.pl", [tenant_a["id"]])
    _login(client, "wladek@mojadomena.pl", HASLO)

    odpowiedz = client.get("/helpdesk/licznik")
    assert odpowiedz.status_code == 200
    assert odpowiedz.json() == {"czeka": 2}

    # Powtorne zapytanie daje to samo - odpytywanie nie kasuje licznika.
    assert client.get("/helpdesk/licznik").json() == {"czeka": 2}


def test_konto_bez_helpdesku_nie_widzi_znacznika(client, tenant_a, make_user, smtp):
    _firma(tenant_a["id"])
    _zgloszenie(tenant_a["id"])
    make_user(tenant_a["id"], "operator@bongo.pl", HASLO)
    _login(client, "operator@bongo.pl", HASLO)

    assert "czeka-znacznik" not in client.get("/").text
