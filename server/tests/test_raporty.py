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


# --- wybor kolumn -----------------------------------------------------------

def test_domyslne_kolumny_gdy_nic_nie_wybrano():
    """Raport dodany bez zastanowienia ma byc czytelny od razu."""
    from cmdb_server.services import kolumny

    assert [k["klucz"] for k in kolumny.wybrane(None)] == kolumny.DOMYSLNE
    assert [k["klucz"] for k in kolumny.wybrane([])] == kolumny.DOMYSLNE


def test_nieznana_kolumna_jest_pomijana():
    """Definicja mogla powstac przy wersji, ktora znala kolumne juz usunieta -
    to nie powod, zeby wywracac caly raport."""
    from cmdb_server.services import kolumny

    wybrane = kolumny.wybrane(["hostname", "kolumna-ktorej-nie-ma", "memory"])
    assert [k["klucz"] for k in wybrane] == ["hostname", "memory"]


def test_tabela_zawiera_wybrane_atrybuty(client, tenant_a):
    _maszyna(client, tenant_a, "SRV-KOL", "maszyna-kol-0001")

    with SessionLocal() as db:
        raport = raporty.zbuduj(
            db, db.get(Tenant, tenant_a["id"]), "sprzet",
            ["hostname", "primary_ip", "memory", "cpu"],
        )

    # Kolejnosc jest taka, jak podana - normalizacja do kolejnosci
    # katalogu nastepuje przy ZAPISIE definicji, nie przy budowaniu.
    assert [k["etykieta"] for k in raport["kolumny"]] == [
        "Nazwa", "Adres IP", "Pamiec", "Procesor",
    ]
    assert len(raport["wiersze"]) == 1
    assert raport["wiersze"][0]["komorki"][0]["wartosc"] == "SRV-KOL"


def test_kolumny_zapisuja_sie_w_kolejnosci_katalogu(client, tenant_a, make_user):
    """Tabela ma wygladac tak samo niezaleznie od kolejnosci klikania."""
    from cmdb_server.services import kolumny

    identyfikator = _definicja(tenant_a["id"])
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)
    csrf = client.get("/raporty").text.split('name="csrf_token" value="')[1].split('"')[0]

    client.post(
        f"/raporty/definicje/{identyfikator}/kolumny",
        data={"csrf_token": csrf, "kolumny": ["memory", "hostname", "cpu"]},
        follow_redirects=False,
    )

    with SessionLocal() as db:
        zapisane = db.get(DefinicjaRaportu, identyfikator).kolumny
    kolejnosc = [k["klucz"] for k in kolumny.KOLUMNY if k["klucz"] in {"memory", "hostname", "cpu"}]
    assert zapisane == kolejnosc


def test_kosztowne_kolumny_liczone_tylko_gdy_zaznaczone(client, tenant_a, monkeypatch):
    """Kolumna z liczba podatnosci przy stu maszynach to sto dopasowan -
    nie ma powodu placic za nia, gdy nikt jej nie chce."""
    from cmdb_server.services import cve

    _maszyna(client, tenant_a, "SRV-CVE", "maszyna-cve-0001")
    wywolania = []
    monkeypatch.setattr(
        cve, "dopasuj",
        lambda db, payload: wywolania.append(1) or {"status": "nieznany", "detail": "x"},
    )

    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_a["id"])
        raporty.zbuduj(db, tenant, "sprzet", ["hostname", "memory"])
    bez_cve = len(wywolania)

    with SessionLocal() as db:
        raporty.zbuduj(db, tenant, "sprzet", ["hostname", "cve_powazne"])

    assert bez_cve == 0, "podatnosci liczone mimo braku takiej kolumny"
    assert len(wywolania) > 0, "podatnosci nie policzone mimo zaznaczonej kolumny"


def test_wysylka_uzywa_kolumn_z_definicji(client, tenant_a, monkeypatch):
    """To, co widac w panelu, ma byc tym, co dostana adresaci."""
    _maszyna(client, tenant_a, "SRV-WYS", "maszyna-wys-0001")
    _ustaw_poczte(tenant_a["id"])
    identyfikator = _definicja(tenant_a["id"])
    with SessionLocal() as db:
        db.get(DefinicjaRaportu, identyfikator).kolumny = ["hostname", "serial_number"]
        db.commit()

    tresci = []
    monkeypatch.setattr(
        poczta, "wyslij",
        lambda db, t, a, temat, html, tekst: tresci.append(html),
    )

    with SessionLocal() as db:
        raporty.wyslij_raport(db, db.get(DefinicjaRaportu, identyfikator))

    assert tresci, "raport nie zostal wyslany"
    assert "Numer seryjny" in tresci[0]
    assert "Adres IP" not in tresci[0], "kolumna spoza wyboru trafila do wiadomosci"


# --- widok na stronie -------------------------------------------------------

def test_widok_pokazuje_caly_raport(client, tenant_a, make_user):
    _maszyna(client, tenant_a, "SRV-WIDOK", "maszyna-widok-001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get("/raporty/widok/sprzet").text
    assert "Inwentaryzacja sprzetu" in strona
    assert "SRV-WIDOK" in strona, "tabela maszyn musi byc na stronie"
    assert "Kolumny tabeli" in strona


def test_widok_odrzuca_nieznany_rodzaj(client, tenant_a, make_user):
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)
    assert client.get("/raporty/widok/wymyslony").status_code == 404


def test_widok_nie_pokazuje_raportu_innej_firmy(client, tenant_a, tenant_b, make_user):
    obcy = _definicja(tenant_b["id"])
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    assert client.get(f"/raporty/widok/sprzet?raport_id={obcy}").status_code == 404


def test_kolumna_opiekuna_pokazuje_imie_i_nazwisko(client, tenant_a):
    """Wczesniejsze testy tego nie objely, bo nie mialy ani jednego opiekuna -
    petla budujaca mape nigdy sie nie wykonywala i bledne pole nie wychodzilo."""
    from cmdb_server.models import WpisSlownika

    _maszyna(client, tenant_a, "SRV-OPIEKUN", "maszyna-opiekun-01")
    with SessionLocal() as db:
        opiekun = WpisSlownika(
            tenant_id=tenant_a["id"], kategoria="osoba", wartosc="Jan Kowalski",
            klucz="jan kowalski",
            atrybuty={"imie_nazwisko": "Jan Kowalski", "email": "jan@firma.pl"})
        db.add(opiekun)
        db.flush()
        maszyna = db.execute(
            select(Asset).where(Asset.hostname == "SRV-OPIEKUN")
        ).scalar_one()
        maszyna.owner_id = opiekun.id
        db.commit()

    with SessionLocal() as db:
        raport = raporty.zbuduj(
            db, db.get(Tenant, tenant_a["id"]), "sprzet", ["hostname", "owner"]
        )

    assert raport["wiersze"][0]["komorki"][1]["wartosc"] == "Jan Kowalski"


# --- zestawienia "pierwsza dziesiatka" --------------------------------------

def _raport_z_zasobami(hostname, *, pamiec=None, dysk=None, obciazenie=None,
                       admini=None, uptime=None, aktualizacje=None):
    """Raport agenta z wybranymi sekcjami zasobow."""
    raport = build_report(machine_id=f"maszyna-{hostname.lower()}", hostname=hostname)
    sprzet = raport.setdefault("hardware", {})
    if pamiec:
        calosc, dostepna = pamiec
        sprzet["memory"] = {"total_bytes": calosc, "available_bytes": dostepna}
    if dysk is not None:
        sprzet["storage"] = {"logical_disks": dysk}
    if obciazenie is not None:
        sprzet["load"] = obciazenie
    if admini is not None:
        raport.setdefault("users", {})["administrators"] = admini
    if uptime is not None:
        raport.setdefault("os", {})["uptime_seconds"] = uptime
    if aktualizacje is not None:
        raport.setdefault("software", {})["updates_pending"] = aktualizacje
    return raport


def _wyslij_raport_agenta(client, tenant, raport):
    token = enroll(client, tenant["token"], machine_id=raport["machine_id"],
                   hostname=raport["identity"]["hostname"]).json()["agent_token"]
    odpowiedz = client.post(
        "/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def _wykorzystanie(tenant_id):
    with SessionLocal() as db:
        return raporty.zbuduj(db, db.get(Tenant, tenant_id), "wykorzystanie")["dane"]


def test_zajecie_pamieci_liczone_z_dostepnej(client, tenant_a):
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "PAMIEC", pamiec=(8_000_000_000, 2_000_000_000)))

    pozycja = _wykorzystanie(tenant_a["id"])["pamiec"][0]
    assert pozycja["wartosc"] == 75.0
    assert "wolne" in pozycja["opis"]


def test_brany_jest_najpelniejszy_wolumen_a_nie_srednia(client, tenant_a):
    """Maszyna z zapelnionym dyskiem systemowym i pustym dyskiem danych ma
    problem, ktorego srednia nie widzi."""
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami("DYSKI", dysk=[
        {"mount": "C:", "used_percent": 95.0, "free_bytes": 5_000_000_000,
         "size_bytes": 100_000_000_000},
        {"mount": "D:", "used_percent": 5.0, "free_bytes": 950_000_000_000,
         "size_bytes": 1_000_000_000_000},
    ]))

    pozycja = _wykorzystanie(tenant_a["id"])["dyski"][0]
    assert pozycja["wartosc"] == 95.0
    assert "C:" in pozycja["opis"]


def test_wartosci_powyzej_progu_sa_wyroznione(client, tenant_a):
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "ALARM", pamiec=(10_000_000_000, 500_000_000)))

    pozycja = _wykorzystanie(tenant_a["id"])["pamiec"][0]
    assert pozycja["wartosc"] == 95.0
    assert pozycja["alarm"] is True


def test_zestawienie_jest_uszeregowane_i_ograniczone(client, tenant_a):
    """Dziesiec pozycji to swiadomy limit - dluzsza lista przestaje byc lista
    rzeczy do zrobienia."""
    from cmdb_server.services import zestawienia

    for numer in range(13):
        _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
            f"SRV{numer:02d}",
            pamiec=(100_000_000_000, (100 - numer * 5) * 1_000_000_000),
        ))

    lista = _wykorzystanie(tenant_a["id"])["pamiec"]
    assert len(lista) == zestawienia.ILE
    wartosci = [p["wartosc"] for p in lista]
    assert wartosci == sorted(wartosci, reverse=True), "lista musi byc uszeregowana"


def test_obciazenie_opisuje_swoje_zrodlo(client, tenant_a):
    """Srednia z 15 minut i trzysekundowa probka to dwie rozne rzeczy,
    a w jednej kolumnie wygladaja tak samo."""
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "LINUX", obciazenie={"percent": 82.0, "load_15": 3.3, "cores": 4,
                             "source": "loadavg-15min"}))
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "WINDOWS", obciazenie={"percent": 91.0, "samples": 3, "source": "licznik-3s"}))

    opisy = {p["maszyna"].hostname: p["opis"] for p in _wykorzystanie(tenant_a["id"])["procesor"]}
    assert "15 min" in opisy["LINUX"]
    assert "probka" in opisy["WINDOWS"]


def test_maszyna_bez_danych_o_zasobach_nie_trafia_na_liste(client, tenant_a):
    """Brak pomiaru to nie to samo co zerowe wykorzystanie."""
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami("BEZDANYCH"))

    dane = _wykorzystanie(tenant_a["id"])
    assert dane["procesor"] == [], "brak pomiaru to nie to samo co zerowe obciazenie"
    assert dane["restarty"] == []
    assert dane["administratorzy"] == []


def test_dlugi_czas_pracy_jest_zglaszany(client, tenant_a):
    """Czesc poprawek jadra zaczyna dzialac dopiero po restarcie."""
    from cmdb_server.services import zestawienia

    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "DLUGO", uptime=(zestawienia.DNI_BEZ_RESTARTU + 40) * 86400))
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami("SWIEZO", uptime=3 * 86400))

    nazwy = [p["maszyna"].hostname for p in _wykorzystanie(tenant_a["id"])["restarty"]]
    assert nazwy == ["DLUGO"]


def test_liczba_administratorow_jest_zglaszana(client, tenant_a):
    """Im wiecej kont z uprawnieniami, tym szersza powierzchnia ataku."""
    def konto(nazwa):
        return {"name": nazwa, "type": "uzytkownik", "source": "lokalne"}

    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "WIELUADM",
        admini=[konto(n) for n in ("Administrator", "jan", "anna", "serwis", "backup")]))
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "MALOADM", admini=[konto("Administrator")]))

    nazwy = [p["maszyna"].hostname for p in _wykorzystanie(tenant_a["id"])["administratorzy"]]
    assert nazwy == ["WIELUADM"]


def test_zestawienia_widza_tylko_swoja_firme(client, tenant_a, tenant_b):
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "MOJA", pamiec=(8_000_000_000, 1_000_000_000)))
    _wyslij_raport_agenta(client, tenant_b, _raport_z_zasobami(
        "OBCA", pamiec=(8_000_000_000, 100_000_000)))

    nazwy = [p["maszyna"].hostname for p in _wykorzystanie(tenant_a["id"])["pamiec"]]
    assert nazwy == ["MOJA"]


def test_raport_wykorzystania_ma_obie_wersje_tresci(client, tenant_a):
    _wyslij_raport_agenta(client, tenant_a, _raport_z_zasobami(
        "TRESC", pamiec=(8_000_000_000, 1_000_000_000)))

    with SessionLocal() as db:
        html, tekst = raporty.renderuj(
            raporty.zbuduj(db, db.get(Tenant, tenant_a["id"]), "wykorzystanie")
        )
    assert "Najwieksze zajecie pamieci" in html
    assert "Najwieksze zajecie pamieci" in tekst
    assert "<" not in tekst


# --- wybor kolumn w widoku --------------------------------------------------

def test_kolumny_z_adresu_dzialaja_bez_zapisanego_raportu(client, tenant_a, make_user):
    """Pola wyboru byly wylaczone, dopoki nie wskazano raportu - a to wlasnie
    wtedy uzytkownik pierwszy raz probuje czegos zaznaczyc."""
    _maszyna(client, tenant_a, "SRV-ADRES", "maszyna-adres-001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get("/raporty/widok/sprzet?kolumny=hostname&kolumny=serial_number").text
    assert "Numer seryjny" in strona
    assert "disabled" not in strona, "pola wyboru maja byc aktywne zawsze"


def test_wybor_z_adresu_ma_pierwszenstwo_przed_zapisem(client, tenant_a, make_user):
    """Zapis przy definicji jest wartoscia wyjsciowa, a nie ograniczeniem -
    inaczej nie dalo by sie niczego podejrzec bez zmieniania raportu."""
    identyfikator = _definicja(tenant_a["id"])
    with SessionLocal() as db:
        db.get(DefinicjaRaportu, identyfikator).kolumny = ["hostname"]
        db.commit()

    _maszyna(client, tenant_a, "SRV-PIERW", "maszyna-pierw-001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get(
        f"/raporty/widok/sprzet?raport_id={identyfikator}&kolumny=hostname&kolumny=vendor"
    ).text
    assert "Dostawca" in strona

    # Zapis pozostal nietkniety - podglad niczego nie zmienil.
    with SessionLocal() as db:
        assert db.get(DefinicjaRaportu, identyfikator).kolumny == ["hostname"]


def test_pola_wyboru_nie_sa_rozciagane_na_cala_szerokosc():
    """Globalna regula ustawiala szerokosc kazdego pola na 100%, przez co
    checkbox odpychal etykiete na drugi koniec wiersza."""
    from pathlib import Path

    styl = (Path(__file__).resolve().parent.parent
            / "cmdb_server" / "static" / "app.css").read_text(encoding="utf-8")
    assert 'input[type="checkbox"]' in styl
    fragment = styl.split('input[type="checkbox"]')[1].split("}")[0]
    assert "width: auto" in fragment


def test_kolumny_ukladaja_sie_w_trzy_kolumny():
    from pathlib import Path

    styl = (Path(__file__).resolve().parent.parent
            / "cmdb_server" / "static" / "app.css").read_text(encoding="utf-8")
    fragment = styl.split(".kolumny-siatka {")[1].split("}")[0]
    assert "repeat(3," in fragment


# --- wykresy kolowe w panelu -------------------------------------------------

def test_widok_rysuje_wykres_kolowy(client, tenant_a, make_user):
    """Strona nie renderuje juz wersji pocztowej - ta miala jasne tlo wpisane
    w atrybuty i slupki zamiast wykresu kolowego."""
    _maszyna(client, tenant_a, "SRV-KOLO", "maszyna-kolo-001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    strona = client.get("/raporty/widok/sprzet").text
    assert "wykres-kolowy" in strona
    assert "stroke-dasharray" in strona
    # Legenda z liczba i udzialem, jak w tabeli obok wykresu.
    assert "wykres-legenda" in strona
    assert "Udzial" in strona
    # Zadnych barw wpisanych w atrybuty - te nie znaja motywu ciemnego.
    assert "color:#1c2024" not in strona
    assert "background:#f5f6f8" not in strona


def test_podglad_pokazuje_wersje_pocztowa(client, tenant_a, make_user):
    """Podglad ma celowo pokazywac to, co dostanie adresat, razem
    z ograniczeniami klientow poczty."""
    _maszyna(client, tenant_a, "SRV-MAIL", "maszyna-mail-001")
    make_user(tenant_a["id"], "raporty@firma.pl", HASLO)
    _login(client, "raporty@firma.pl", HASLO)

    podglad = client.get("/raporty/podglad/sprzet").text
    assert "color:#1c2024" in podglad
    assert "<svg" not in podglad, "klienty poczty nie renderuja SVG"


def test_pierscien_zamyka_pelny_obwod():
    """Suma dlugosci segmentow ma dawac caly obwod - inaczej w pierscieniu
    zostaje szczelina albo segmenty na siebie nachodza."""
    from cmdb_server.services import wykresy

    wynik = wykresy.pierscien([("a", 3), ("b", 5), ("c", 11)])
    dlugosci = sum(float(s["dasharray"].split()[0]) for s in wynik["segmenty"])
    assert abs(dlugosci - wynik["obwod"]) < 0.01
    assert sum(s["udzial"] for s in wynik["segmenty"]) == 100.0


def test_pierscien_bez_danych_nie_rysuje_pustego_kola():
    """Pusty pierscien sugerowalby, ze wszystkie wartosci sa zerowe."""
    from cmdb_server.services import wykresy

    assert wykresy.pierscien([])["segmenty"] == []
    assert wykresy.pierscien([("a", 0), ("b", 0)])["segmenty"] == []


def test_pierscien_laczy_nadmiar_kategorii():
    """Kilkanascie kawalkow przestaje byc czytelne, a barwy zaczynaja sie
    powtarzac - wtedy dwa rozne segmenty wygladaja jak jeden."""
    from cmdb_server.services import wykresy

    wynik = wykresy.pierscien([(f"poz-{n}", 20 - n) for n in range(15)])
    assert len(wynik["segmenty"]) <= len(wykresy.PALETA)
    assert wynik["segmenty"][-1]["etykieta"] == wykresy.POZOSTALE
    # Laczenie nie moze gubic zadnej maszyny.
    assert wynik["suma"] == sum(20 - n for n in range(15))


def test_waga_podatnosci_ma_rozne_barwy():
    """W wersji pocztowej krytyczne i wysokie maja ten sam czerwony - na
    pierscieniu bylyby nieodroznialne."""
    from cmdb_server.services import wykresy

    wynik = wykresy.z_paskow(
        [{"etykieta": "krytyczne (9+)", "wartosc": 2},
         {"etykieta": "wysokie (7-9)", "wartosc": 3}],
        wykresy.PALETA_WAGI,
    )
    barwy = {s["barwa"] for s in wynik["segmenty"]}
    assert len(barwy) == 2
