"""Raporty cykliczne wysylane poczta.

Trzy rzeczy sa tu wazniejsze od reszty: raport jednej firmy nie moze zawierac
danych innej, blad wysylki nie moze zatrzymac pozostalych raportow, a haslo
SMTP musi dac sie odzyskac, bo serwer podaje je przy kazdym polaczeniu.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    Asset,
    DefinicjaRaportu,
    Tenant,
    UstawieniaPoczty,
    utcnow,
)
from cmdb_server.services import poczta, raporty, sekrety
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _login

HASLO = "haslo-do-testow-123"


def _maszyna(client, tenant, hostname, machine_id, **pola):
    """Rejestruje maszyne i ustawia jej pola zakupu."""
    token = enroll(client, tenant["token"], machine_id=machine_id, hostname=hostname).json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id=machine_id, hostname=hostname),
    )
    if pola:
        with SessionLocal() as db:
            maszyna = db.execute(
                select(Asset).where(Asset.hostname == hostname)
            ).scalar_one()
            for nazwa, wartosc in pola.items():
                setattr(maszyna, nazwa, wartosc)
            db.commit()


def _definicja(tenant_id, rodzaj="sprzet", adresaci="it@firma.pl", **pola):
    with SessionLocal() as db:
        definicja = DefinicjaRaportu(
            tenant_id=tenant_id, nazwa=f"raport {rodzaj}", rodzaj=rodzaj,
            adresaci=adresaci, **pola,
        )
        db.add(definicja)
        db.commit()
        return definicja.id


# --- dane raportow ----------------------------------------------------------

def test_raport_sprzetu_liczy_maszyny(client, tenant_a):
    _maszyna(client, tenant_a, "SRV-01", "maszyna-rap-0001")
    _maszyna(client, tenant_a, "SRV-02", "maszyna-rap-0002")

    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_a["id"])
        dane = raporty.zbuduj(db, tenant, "sprzet")["dane"]

    assert dane["liczba"] == 2
    assert dane["wykres_systemy"], "wykres systemow nie moze byc pusty"


def test_raport_widzi_tylko_maszyny_swojej_firmy(client, tenant_a, tenant_b):
    """Sedno wielofirmowosci: raport wysylany do jednej organizacji nie moze
    zawierac maszyn innej."""
    _maszyna(client, tenant_a, "FIRMA-A-01", "maszyna-a-rap-001")
    _maszyna(client, tenant_b, "FIRMA-B-01", "maszyna-b-rap-001")

    with SessionLocal() as db:
        dane = raporty.zbuduj(db, db.get(Tenant, tenant_a["id"]), "sprzet")["dane"]

    nazwy = [m.hostname for m in dane["maszyny"]]
    assert nazwy == ["FIRMA-A-01"]


def test_raport_gwarancji_dzieli_na_kategorie(client, tenant_a):
    dzisiaj = date.today()
    _maszyna(client, tenant_a, "PO-TERMINIE", "maszyna-gw-0001",
             warranty_until=dzisiaj - timedelta(days=10))
    _maszyna(client, tenant_a, "KONCZY-SIE", "maszyna-gw-0002",
             warranty_until=dzisiaj + timedelta(days=30))
    _maszyna(client, tenant_a, "NA-GWARANCJI", "maszyna-gw-0003",
             warranty_until=dzisiaj + timedelta(days=400))
    _maszyna(client, tenant_a, "BEZ-DATY", "maszyna-gw-0004")

    with SessionLocal() as db:
        dane = raporty.zbuduj(db, db.get(Tenant, tenant_a["id"]), "gwarancje")["dane"]

    assert [m.hostname for m in dane["po_terminie"]] == ["PO-TERMINIE"]
    assert [m.hostname for m in dane["koncza_sie"]] == ["KONCZY-SIE"]
    assert dane["na_gwarancji"] == 1
    # Brak daty to nie to samo co brak gwarancji - to tylko nieuzupelnione pole.
    assert [m.hostname for m in dane["bez_danych"]] == ["BEZ-DATY"]


def test_wykres_liczy_szerokosc_wzgledem_najwiekszej():
    """Wzgledem sumy przy kilkunastu kategoriach wszystkie paski bylyby
    nieczytelnie krotkie."""
    paski = raporty.wykres([("a", 10), ("b", 5), ("c", 1)])
    assert paski[0]["szerokosc"] == 100
    assert paski[1]["szerokosc"] == 50
    assert paski[2]["szerokosc"] >= 2, "najmniejszy pasek musi byc jeszcze widoczny"


def test_wykres_pustych_danych_nie_wybucha():
    assert raporty.wykres([]) == []


# --- tresc wiadomosci -------------------------------------------------------

@pytest.mark.parametrize("rodzaj", ["podatnosci", "sprzet", "gwarancje"])
def test_kazdy_rodzaj_ma_obie_wersje_tresci(client, tenant_a, rodzaj):
    """Wersja tekstowa nie jest ozdoba - czesc filtrow ocenia wiadomosc bez
    alternatywy tekstowej jako podejrzana."""
    _maszyna(client, tenant_a, "SRV-TRESC", "maszyna-tresc-001")

    with SessionLocal() as db:
        html, tekst = raporty.renderuj(
            raporty.zbuduj(db, db.get(Tenant, tenant_a["id"]), rodzaj)
        )

    assert "<table" in html
    assert tekst.strip()
    assert "<" not in tekst, "wersja tekstowa nie moze zawierac znacznikow"


def test_raport_nie_uzywa_skryptow_ani_svg(client, tenant_a):
    """Klienty poczty nie uruchamiaja skryptow, a wiekszosc wycina SVG -
    wykresy musza byc zbudowane z tabel i tla."""
    _maszyna(client, tenant_a, "SRV-CSP", "maszyna-csp-0001")

    with SessionLocal() as db:
        html, _ = raporty.renderuj(
            raporty.zbuduj(db, db.get(Tenant, tenant_a["id"]), "sprzet")
        )

    assert "<script" not in html.lower()
    assert "<svg" not in html.lower()


# --- harmonogram ------------------------------------------------------------

def test_nowy_raport_idzie_od_razu(tenant_a):
    """Inaczej po dodaniu definicji trzeba by czekac caly okres, nie wiedzac,
    czy cokolwiek dziala."""
    with SessionLocal() as db:
        definicja = db.get(DefinicjaRaportu, _definicja(tenant_a["id"]))
        assert raporty.nalezy_wyslac(definicja) is True


def test_swiezo_wyslany_raport_czeka(tenant_a):
    identyfikator = _definicja(tenant_a["id"], czestotliwosc="tygodniowo")
    with SessionLocal() as db:
        definicja = db.get(DefinicjaRaportu, identyfikator)
        definicja.ostatnia_wysylka = utcnow()
        db.commit()
        assert raporty.nalezy_wyslac(definicja) is False


def test_po_uplywie_okresu_raport_idzie_ponownie(tenant_a):
    identyfikator = _definicja(tenant_a["id"], czestotliwosc="dziennie")
    with SessionLocal() as db:
        definicja = db.get(DefinicjaRaportu, identyfikator)
        definicja.ostatnia_wysylka = utcnow() - timedelta(days=2)
        db.commit()
        assert raporty.nalezy_wyslac(definicja) is True


def test_wylaczony_raport_nie_idzie(tenant_a):
    identyfikator = _definicja(tenant_a["id"], aktywny=False)
    with SessionLocal() as db:
        assert raporty.nalezy_wyslac(db.get(DefinicjaRaportu, identyfikator)) is False


@pytest.mark.parametrize(
    "zapis, oczekiwane",
    [
        ("a@firma.pl", ["a@firma.pl"]),
        ("a@firma.pl, b@firma.pl", ["a@firma.pl", "b@firma.pl"]),
        ("a@firma.pl; b@firma.pl", ["a@firma.pl", "b@firma.pl"]),
        ("a@firma.pl\nb@firma.pl", ["a@firma.pl", "b@firma.pl"]),
        ("bez-malpy, b@firma.pl", ["b@firma.pl"]),
        ("", []),
    ],
)
def test_rozbior_adresatow(zapis, oczekiwane, tenant_a):
    definicja = DefinicjaRaportu(tenant_id=tenant_a["id"], nazwa="x",
                                 rodzaj="sprzet", adresaci=zapis)
    assert raporty.adresaci(definicja) == oczekiwane


# --- wysylka ----------------------------------------------------------------

def _ustaw_poczte(tenant_id, haslo="tajne"):
    with SessionLocal() as db:
        db.add(UstawieniaPoczty(
            tenant_id=tenant_id, host="smtp.firma.pl", port=587,
            uzytkownik="cmdb", haslo_szyfr=sekrety.zaszyfruj(haslo),
            nadawca="cmdb@firma.pl",
        ))
        db.commit()


def test_blad_wysylki_nie_zatrzymuje_pozostalych(client, tenant_a, monkeypatch):
    """Wyjatek przerwalby caly przebieg i kolejne raporty tez by nie poszly."""
    _maszyna(client, tenant_a, "SRV-BLAD", "maszyna-blad-0001")
    _ustaw_poczte(tenant_a["id"])
    pierwszy = _definicja(tenant_a["id"], rodzaj="sprzet")
    drugi = _definicja(tenant_a["id"], rodzaj="gwarancje")

    wyslane = []

    def czasem_padnij(db, tenant_id, adresaci, temat, html, tekst):
        if "Inwentaryzacja" in temat:
            raise poczta.BladPoczty("serwer odrzucil wiadomosc")
        wyslane.append(temat)

    monkeypatch.setattr(poczta, "wyslij", czasem_padnij)

    with SessionLocal() as db:
        podsumowanie = raporty.wyslij_zalegle(db)
        stan = {d.id: (d.ostatni_status, d.ostatni_blad)
                for d in db.execute(select(DefinicjaRaportu)).scalars()}

    assert podsumowanie["wyslane"] == 2, "oba raporty maja byc sprobowane"
    assert len(wyslane) == 1
    assert stan[pierwszy][0] == "blad"
    assert "odrzucil" in stan[pierwszy][1]
    assert stan[drugi][0] == "ok"


def test_wysylka_bez_ustawien_poczty_konczy_sie_zapisanym_bledem(client, tenant_a):
    """Administrator ma zobaczyc powod w panelu, a nie zastanawiac sie,
    czemu nic nie przychodzi."""
    _maszyna(client, tenant_a, "SRV-BEZ", "maszyna-bez-0001")
    identyfikator = _definicja(tenant_a["id"])

    with SessionLocal() as db:
        raporty.wyslij_raport(db, db.get(DefinicjaRaportu, identyfikator))
        definicja = db.get(DefinicjaRaportu, identyfikator)

    assert definicja.ostatni_status == "blad"
    assert "poczty" in definicja.ostatni_blad


def test_raport_bez_adresatow_nie_jest_wysylany(client, tenant_a):
    _ustaw_poczte(tenant_a["id"])
    identyfikator = _definicja(tenant_a["id"], adresaci="to-nie-jest-adres")

    with SessionLocal() as db:
        raporty.wyslij_raport(db, db.get(DefinicjaRaportu, identyfikator))
        definicja = db.get(DefinicjaRaportu, identyfikator)

    assert definicja.ostatni_status == "blad"
    assert "adres" in definicja.ostatni_blad


# --- haslo SMTP -------------------------------------------------------------

def test_haslo_smtp_jest_odwracalne():
    """Skrot tu nie wystarczy: serwer podaje haslo przy kazdym polaczeniu."""
    zaszyfrowane = sekrety.zaszyfruj("moje-haslo")
    assert zaszyfrowane != "moje-haslo"
    assert sekrety.odszyfruj(zaszyfrowane) == "moje-haslo"


def test_nieczytelne_haslo_daje_none_zamiast_wyjatku():
    """Zwykle znaczy, ze zmienil sie CMDB_SECRET_KEY - to nie jest awaria
    serwera, tylko sytuacja, w ktorej trzeba wpisac haslo ponownie."""
    assert sekrety.odszyfruj("to-nie-jest-szyfrogram") is None


def test_wysylka_mowi_wprost_gdy_hasla_nie_da_sie_odczytac(client, tenant_a):
    with SessionLocal() as db:
        db.add(UstawieniaPoczty(
            tenant_id=tenant_a["id"], host="smtp.firma.pl", uzytkownik="cmdb",
            haslo_szyfr="uszkodzony-szyfrogram", nadawca="cmdb@firma.pl",
        ))
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(poczta.BladPoczty, match="wpisz je ponownie"):
            poczta.wyslij(db, tenant_a["id"], ["a@firma.pl"], "temat", "<p>x</p>", "x")


# --- panel ------------------------------------------------------------------

def test_strona_raportow_dziala(client, tenant_a, make_user):
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get("/raporty").text
    assert "Poczta wychodzaca" in strona
    assert "Raporty cykliczne" in strona


def test_haslo_smtp_nie_trafia_do_html(client, tenant_a, make_user):
    """Pole hasla ma byc puste przy edycji - inaczej wystarczyloby podejrzec
    zrodlo strony."""
    _ustaw_poczte(tenant_a["id"], haslo="bardzo-tajne")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get("/raporty").text
    assert "bardzo-tajne" not in strona


def test_nie_da_sie_wyslac_raportu_innej_firmy(client, tenant_a, tenant_b, make_user):
    """Sam identyfikator z cudzego panelu nie moze wystarczyc."""
    obcy = _definicja(tenant_b["id"])
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    csrf = client.get("/raporty").text.split('name="csrf_token" value="')[1].split('"')[0]
    odpowiedz = client.post(
        f"/raporty/definicje/{obcy}/wyslij",
        data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert odpowiedz.status_code == 404


def test_podglad_raportu_bez_wysylania(client, tenant_a, make_user):
    _maszyna(client, tenant_a, "SRV-PODGLAD", "maszyna-pod-0001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    odpowiedz = client.get("/raporty/podglad/sprzet")
    assert odpowiedz.status_code == 200
    assert "Inwentaryzacja sprzetu" in odpowiedz.text


def test_nieznany_rodzaj_raportu_daje_404(client, tenant_a, make_user):
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)
    assert client.get("/raporty/podglad/wymyslony").status_code == 404
