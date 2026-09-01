"""Schemat slownika: typy, formaty, pola wymagane i granice edycji.

Sprawdzamy nie tylko to, co ma dzialac, ale i to, czego system ma odmowic -
schemat jest danymi, wiec kazda odmowa musi byc jawna, a nie przypadkowa.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, SchematSlownika, WpisSlownika
from cmdb_server.services import schemat as definicje
from cmdb_server.services import wzorzec

from .test_tenant_isolation import _extract_csrf, _login

HASLO = "bardzo-dlugie-haslo"


def _admin(client, tenant, make_user, email="admin@firma-a.pl"):
    make_user(tenant["id"], email, HASLO)
    _login(client, email, HASLO)


def _wpis(client, kategoria="dostawca"):
    csrf = _extract_csrf(client.get("/slowniki?kategoria=dostawca").text)
    odpowiedz = client.post("/slowniki", data={"kategoria": kategoria,
                                               "csrf_token": csrf}, follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text
    return odpowiedz.headers["location"].rsplit("/", 1)[1]


# --- formaty ----------------------------------------------------------------

@pytest.mark.parametrize("wejscie,oczekiwane", [
    ("00950", "00-950"),
    ("00-950", "00-950"),
    (" 02 566 ", "02-566"),
])
def test_kod_pocztowy_dochodzi_do_jednej_postaci(wejscie, oczekiwane):
    """Dwa zapisy tego samego kodu maja byc ta sama wartoscia."""
    pole = definicje.Pole(klucz="k", etykieta="Kod", typ="tekst", format="kod_pocztowy")
    assert definicje._sprawdz_pole(pole, wejscie) == oczekiwane


def test_kod_pocztowy_odrzuca_zla_dlugosc():
    pole = definicje.Pole(klucz="k", etykieta="Kod", typ="tekst", format="kod_pocztowy")
    with pytest.raises(ValueError):
        definicje._sprawdz_pole(pole, "0500")


def test_nip_sprawdza_sume_kontrolna_a_nie_dlugosc():
    """Sama dlugosc przepuszcza numer wpisany po to, zeby formularz przestal
    marudzic - a to wlasnie ten rodzaj danych psuje pozniej raporty."""
    pole = definicje.Pole(klucz="nip", etykieta="NIP", typ="tekst", format="nip")
    assert definicje._sprawdz_pole(pole, "123-456-32-18") == "1234563218"
    with pytest.raises(ValueError):
        definicje._sprawdz_pole(pole, "1234563219")


def test_adres_www_dostaje_schemat_polaczenia():
    pole = definicje.Pole(klucz="p", etykieta="Portal", typ="tekst", format="url")
    assert definicje._sprawdz_pole(pole, "support.dell.com") == "https://support.dell.com"


def test_liczba_pilnuje_zakresu():
    pole = definicje.Pole(klucz="h", etykieta="Godziny", typ="liczba", min=1, max=720)
    assert definicje._sprawdz_pole(pole, "24") == 24
    with pytest.raises(ValueError):
        definicje._sprawdz_pole(pole, "1000")


# --- ksztalt schematu -------------------------------------------------------

def test_format_tylko_dla_tekstu():
    """Format opisuje wyglad napisu; przy liczbie albo dacie nie ma sensu."""
    with pytest.raises(ValueError):
        definicje.Pole(klucz="x", etykieta="X", typ="liczba", format="email")


def test_jedna_rola_jeden_powod():
    """Dwa adresy zgloszen to pytanie, na ktore system nie umie odpowiedziec."""
    with pytest.raises(ValueError):
        definicje.Schemat(kategoria="dostawca", pola=[
            {"klucz": "a", "etykieta": "A", "typ": "tekst", "rola": "email_zgloszen"},
            {"klucz": "b", "etykieta": "B", "typ": "tekst", "rola": "email_zgloszen"},
        ])


def test_limit_pol_na_slownik():
    """Bez granicy pierwsze wklejenie kolumn z arkusza zamienia formularz
    w cos, czego nikt nie otworzy."""
    nadmiar = [{"klucz": f"p{n}", "etykieta": f"P{n}", "typ": "tekst"}
               for n in range(definicje.MAKS_POL + 1)]
    with pytest.raises(ValueError):
        definicje.Schemat(kategoria="dostawca", pola=nadmiar)


def test_wzorzec_jest_poprawny_dla_kazdej_kategorii():
    for kategoria in ("dzial", "lokalizacja", "dostawca"):
        assert wzorzec.wzorcowy(kategoria).pola


# --- wymagane zalezy od miejsca powstania wartosci --------------------------

def test_formularz_maszyny_wybiera_gotowy_rekord(client, tenant_a, make_user):
    from .test_sprzet_slowniki_konta import _dodaj_sprzet, _dodaj_wpis
    _admin(client, tenant_a, make_user)
    dostawca_id = _dodaj_wpis(client, "dostawca", "Nowy Dostawca")
    _dodaj_sprzet(client, nazwa="DRUKARKA-1", dostawca_id=dostawca_id, lokalizacja="")

    with SessionLocal() as db:
        wpis = db.get(WpisSlownika, dostawca_id)
        assert wpis.wartosc == "Nowy Dostawca"
        assert wpis.atrybuty["nazwa_firmy"] == "Nowy Dostawca"
        assert db.execute(select(Asset)).scalar_one().dostawca_id == wpis.id


def test_karta_slownika_nie_zapisze_sie_bez_wymaganych(client, tenant_a, make_user):
    """Swiadoma edycja to inne miejsce niz formularz maszyny."""
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)

    odpowiedz = client.post(f"/slowniki/wpis/{wpis_id}",
                            data={"pole_nip": "", "csrf_token": csrf},
                            follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert "blad=" in odpowiedz.headers["location"], "brak wymaganego pola ma zatrzymac zapis"

    with SessionLocal() as db:
        assert db.get(WpisSlownika, wpis_id).atrybuty == {}


def test_karta_zapisuje_i_normalizuje(client, tenant_a, make_user):
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)

    assert client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "portal",
        "pole_email_zgloszen": "Serwis@DELL.pl",
        "pole_telefon_wsparcia": "225790000",
        "csrf_token": csrf}, follow_redirects=False).status_code == 303

    with SessionLocal() as db:
        dane = db.get(WpisSlownika, wpis_id).atrybuty
        assert dane["email_zgloszen"] == "serwis@dell.pl"
        assert dane["telefon_wsparcia"] == "+48 22 579 00 00"


def test_lista_pokazuje_czego_brakuje(client, tenant_a, make_user):
    _admin(client, tenant_a, make_user)
    _wpis(client)
    strona = client.get("/slowniki?kategoria=dostawca").text
    assert "szkic" in strona
    assert "Sposob kontaktu" in strona, "ma byc widac, ktorego pola brakuje"


# --- edycja schematu --------------------------------------------------------

def test_schemat_zmienia_tylko_administrator(client, tenant_a, make_user):
    """Schemat rzadzi tym, co widza wszyscy w firmie."""
    make_user(tenant_a["id"], "widz@firma-a.pl", HASLO, role="viewer")
    _login(client, "widz@firma-a.pl", HASLO)
    assert client.get("/slowniki/dostawca/schemat").status_code == 403


def test_pole_z_wartosciami_nie_daje_sie_usunac(client, tenant_a, make_user):
    """Kasowanie danych i kasowanie pola to dwie osobne decyzje."""
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)
    client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "portal", "pole_nip": "1234563218",
        "csrf_token": csrf}, follow_redirects=False)

    strona = client.get("/slowniki/dostawca/schemat").text
    csrf = _extract_csrf(strona)
    bez_nipu = {"kategoria": "dostawca", "wersja": 1, "pola": [
        p for p in wzorzec.wzorcowy("dostawca").model_dump(exclude_none=True)["pola"]
        if p["klucz"] != "nip"]}
    odpowiedz = client.post("/slowniki/dostawca/schemat",
                            data={"definicja": json.dumps(bez_nipu), "csrf_token": csrf},
                            follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert "blad=" in odpowiedz.headers["location"]

    with SessionLocal() as db:
        zapisany = db.execute(select(SchematSlownika).where(
            SchematSlownika.kategoria == "dostawca")).scalar_one()
        assert any(p["klucz"] == "nip" for p in zapisany.definicja["pola"])


def test_po_wyczyszczeniu_wartosci_pole_znika(client, tenant_a, make_user):
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)
    client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "portal", "pole_nip": "1234563218",
        "csrf_token": csrf}, follow_redirects=False)

    csrf = _extract_csrf(client.get("/slowniki/dostawca/schemat").text)
    assert client.post("/slowniki/dostawca/schemat/wyczysc",
                       data={"klucz": "nip", "csrf_token": csrf},
                       follow_redirects=False).status_code == 303

    csrf = _extract_csrf(client.get("/slowniki/dostawca/schemat").text)
    bez_nipu = {"kategoria": "dostawca", "wersja": 1, "pola": [
        p for p in wzorzec.wzorcowy("dostawca").model_dump(exclude_none=True)["pola"]
        if p["klucz"] != "nip"]}
    odpowiedz = client.post("/slowniki/dostawca/schemat",
                            data={"definicja": json.dumps(bez_nipu), "csrf_token": csrf},
                            follow_redirects=False)
    assert "blad=" not in odpowiedz.headers["location"]

    with SessionLocal() as db:
        zapisany = db.execute(select(SchematSlownika).where(
            SchematSlownika.kategoria == "dostawca")).scalar_one()
        assert not any(p["klucz"] == "nip" for p in zapisany.definicja["pola"])


def test_bledny_schemat_nie_psuje_slownika(client, tenant_a, make_user):
    _admin(client, tenant_a, make_user)
    client.get("/slowniki")
    csrf = _extract_csrf(client.get("/slowniki/dostawca/schemat").text)
    odpowiedz = client.post("/slowniki/dostawca/schemat",
                            data={"definicja": "{to nie jest json", "csrf_token": csrf},
                            follow_redirects=False)
    assert "blad=" in odpowiedz.headers["location"]

    with SessionLocal() as db:
        zapisany = db.execute(select(SchematSlownika).where(
            SchematSlownika.kategoria == "dostawca")).scalar_one()
        assert zapisany.definicja["pola"], "poprzedni schemat zostaje nietkniety"


def test_schemat_firmy_nie_wychodzi_poza_nia(client, tenant_a, tenant_b, make_user):
    _admin(client, tenant_a, make_user)
    client.get("/slowniki")
    with SessionLocal() as db:
        wiersze = db.execute(select(SchematSlownika)).scalars().all()
        assert {w.tenant_id for w in wiersze} == {tenant_a["id"]}


# --- role -------------------------------------------------------------------

def test_raport_siega_po_role_a_nie_po_nazwe_pola(client, tenant_a, make_user):
    """Firma moze nazwac pole po swojemu; raport ma dzialac dalej."""
    from cmdb_server.services import slowniki
    from cmdb_server.services.scoping import TenantContext

    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)
    client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "e-mail",
        "pole_email_zgloszen": "serwis@dell.pl", "csrf_token": csrf},
        follow_redirects=False)

    ctx = TenantContext(tenant_id=tenant_a["id"], tenant_slug="firma-a", actor="test")
    with SessionLocal() as db:
        wpis = db.get(WpisSlownika, wpis_id)
        assert slowniki.wg_roli(db, ctx, "dostawca", wpis, "email_zgloszen") == "serwis@dell.pl"


def test_brak_roli_nie_wywraca_raportu(client, tenant_a, make_user):
    """Gdy roli nie pelni zadne pole, funkcja dostaje None i moze o tym
    powiedziec - zamiast dzialac po cichu blednie."""
    from cmdb_server.services import slowniki
    from cmdb_server.services.scoping import TenantContext

    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get("/slowniki/dostawca/schemat").text)
    bez_roli = {"kategoria": "dostawca", "wersja": 1, "pola": [
        {"klucz": "nazwa_wlasna", "etykieta": "Cokolwiek", "typ": "tekst", "w_etykiecie": True}]}
    client.post("/slowniki/dostawca/schemat",
                data={"definicja": json.dumps(bez_roli), "csrf_token": csrf},
                follow_redirects=False)

    ctx = TenantContext(tenant_id=tenant_a["id"], tenant_slug="firma-a", actor="test")
    with SessionLocal() as db:
        wpis = db.get(WpisSlownika, wpis_id)
        assert slowniki.wg_roli(db, ctx, "dostawca", wpis, "email_zgloszen") is None


# --- wizualny edytor --------------------------------------------------------

def test_strona_schematu_daje_edytor_i_surowy_json(client, tenant_a, make_user):
    """Przyciski dla wiekszosci, JSON dla tych, ktorzy go wola."""
    _admin(client, tenant_a, make_user)
    strona = client.get("/slowniki/dostawca/schemat").text
    assert "data-edytor-schematu" in strona
    assert 'data-widok="formularz"' in strona and 'data-widok="json"' in strona
    assert 'name="definicja"' in strona, "wysylane jest nadal jedno pole"


def test_edytor_dostaje_te_same_pojecia_co_serwer(client, tenant_a, make_user):
    """Gdyby lista typow w przegladarce byla wlasna, rozjechalaby sie z ta,
    ktora sprawdza serwer - i formularz proponowalby wartosci do odrzucenia."""
    _admin(client, tenant_a, make_user)
    strona = client.get("/slowniki/dostawca/schemat").text
    for typ in definicje.TYPY:
        assert typ in strona
    for format_ in definicje.FORMATY:
        assert format_ in strona
    for rola in definicje.ROLE:
        assert rola in strona


def test_edytor_wie_ktore_pola_maja_wartosci(client, tenant_a, make_user):
    """Bez tego przycisk usuwania dalby sie kliknac i odbil od serwera."""
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)
    client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "portal", "pole_nip": "1234563218",
        "csrf_token": csrf}, follow_redirects=False)

    strona = client.get("/slowniki/dostawca/schemat").text
    assert "data-uzycia" in strona
    assert '"nip": 1' in strona.replace("&#34;", '"')


def test_skrypt_edytora_nie_uzywa_procedur_w_znacznikach():
    """Polityka bezpieczenstwa dopuszcza wylacznie skrypty z tego serwera,
    wiec onclick w tresci strony po prostu by nie zadzialal."""
    from pathlib import Path

    katalog = Path(__file__).resolve().parent.parent / "cmdb_server"
    szablon = (katalog / "templates" / "slownik_schemat.html").read_text(encoding="utf-8")
    assert "onclick" not in szablon and "oninput" not in szablon
    assert (katalog / "static" / "schemat.js").is_file()


# --- osoby jako slownik i liczenie uzycia -----------------------------------

def test_osoba_jest_kategoria_slownika(client, tenant_a, make_user):
    """Osoby mialy wlasna tabele i wlasny formularz, wiec te same pojecia
    dzialaly w dwoch miejscach inaczej."""
    from cmdb_server.models import KATEGORIE_SLOWNIKA

    assert "osoba" in KATEGORIE_SLOWNIKA
    _admin(client, tenant_a, make_user)
    strona = client.get("/slowniki?kategoria=osoba")
    assert strona.status_code == 200
    assert "Imie i nazwisko" in strona.text or "Osoby" in strona.text


def test_stary_adres_osob_prowadzi_do_slownika(client, tenant_a, make_user):
    _admin(client, tenant_a, make_user)
    odpowiedz = client.get("/owners", follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert "kategoria=osoba" in odpowiedz.headers["location"]


def test_slowniki_maja_zakladki_i_pokazuja_jeden_naraz(client, tenant_a, make_user):
    """Cztery kategorie zlozone jedna pod druga zmuszaly do przewijania przez
    trzy nieinteresujace, zeby dojsc do czwartej."""
    _admin(client, tenant_a, make_user)
    strona = client.get("/slowniki?kategoria=dostawca").text
    for kategoria in ("osoba", "lokalizacja", "dzial", "dostawca"):
        assert f"/slowniki?kategoria={kategoria}" in strona


def test_kolumny_to_szesc_pierwszych_pol_schematu(client, tenant_a, make_user):
    """Kolejnosc w schemacie jest jedynym kryterium.

    Wczesniej odsiewalismy pola etykiety i notatki "madrzej" - skutek byl taki,
    ze przestawienie kolejnosci nie zmienialo tabeli i nie dalo sie dojsc,
    dlaczego. Teraz o kolumnach decyduje administrator edytorem schematu.
    """
    from cmdb_server.api.ui import KOLUMNY_SLOWNIKA
    from cmdb_server.services import wzorzec

    _admin(client, tenant_a, make_user)
    _wpis(client)                       # naglowki widac dopiero z wierszem
    oczekiwane = [p.etykieta for p in wzorzec.wzorcowy("dostawca").pola[:KOLUMNY_SLOWNIKA]]
    strona = client.get("/slowniki?kategoria=dostawca").text
    for etykieta in oczekiwane:
        assert etykieta in strona, f"brakuje kolumny {etykieta}"


def test_lista_pokazuje_wartosci_w_kolumnach(client, tenant_a, make_user):
    """Kolumny biora sie ze schematu, wiec widac to, co firma u siebie prowadzi."""
    _admin(client, tenant_a, make_user)
    wpis_id = _wpis(client)
    csrf = _extract_csrf(client.get(f"/slowniki/wpis/{wpis_id}").text)
    client.post(f"/slowniki/wpis/{wpis_id}", data={
        "pole_nazwa_firmy": "Dell", "pole_kanal_zgloszen": "portal",
        "pole_telefon_wsparcia": "225790000", "csrf_token": csrf},
        follow_redirects=False)

    strona = client.get("/slowniki?kategoria=dostawca").text
    assert "Telefon wsparcia" in strona, "kolumna ze schematu"
    assert "+48 22 579 00 00" in strona, "wartosc w kolumnie"


def test_uzycie_dzialu_liczy_odwolania_z_innych_slownikow(client, tenant_a, make_user):
    """Dzial nie ma wlasnej kolumny w tabeli maszyn - prowadza do niego tylko
    pola odwolania z innych slownikow. Liczenie samych maszyn pokazywalo zero
    przy dziale, ktory faktycznie byl uzywany - a od tej liczby zalezy,
    czy ktos go usunie."""
    from .test_sprzet_slowniki_konta import _dodaj_wpis

    _admin(client, tenant_a, make_user)
    dzial_id = _dodaj_wpis(client, "dzial", "Ksiegowosc")
    _dodaj_wpis(client, "osoba", "Marta Nowak",
                {"pole_email": "marta@firma.pl", "pole_dzial": dzial_id})

    strona = client.get("/slowniki?kategoria=dzial").text
    wiersz = [linia for linia in strona.splitlines() if "Ksiegowosc" in linia]
    assert wiersz, "dzial ma byc na liscie"
    from cmdb_server.services import slowniki
    from cmdb_server.services.scoping import TenantContext

    ctx = TenantContext(tenant_id=tenant_a["id"], tenant_slug="firma-a", actor="test")
    with SessionLocal() as db:
        dzial = db.get(WpisSlownika, dzial_id)
        assert slowniki.uzycie_wpisu(db, ctx, dzial) == 1, "osoba wskazuje ten dzial"


def test_okno_podgladu_ma_nieprzezroczyste_tlo():
    """Nieistniejaca zmienna CSS daje tlo przezroczyste - przez okno widac bylo
    strone pod spodem."""
    from pathlib import Path

    styl = (Path(__file__).resolve().parent.parent
            / "cmdb_server" / "static" / "app.css").read_text(encoding="utf-8")
    fragment = styl.split(".modal-card {")[1].split("}")[0]
    assert "var(--surface)" in fragment
    for zmienna in ("--panel", "--line", "--soft"):
        assert f"var({zmienna})" not in styl, f"{zmienna} nie jest zdefiniowana w app.css"
