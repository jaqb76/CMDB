"""Rodzaj sprzetu jako slownik i pola wlasciwe dla rodzaju.

Najwazniejszy jest tu przypadek zmiany rodzaju: wartosci maja ZOSTAC w bazie
i wrocic po powrocie do poprzedniego rodzaju. Skasowanie ich byloby utrata
danych ukryta pod zwykla zmiana pola w formularzu.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, SchematSlownika, WpisSlownika
from cmdb_server.services import rodzaje, wzorzec

from .test_sprzet_slowniki_konta import _admin_firmy, _dodaj_sprzet
from .test_tenant_isolation import _extract_csrf

SERIAL = "MON-77-2026"


def _sprzet(client, tenant, nazwa="MON-1", typ="monitor"):
    _dodaj_sprzet(client, nazwa=nazwa, typ=typ)
    with SessionLocal() as db:
        return db.execute(select(Asset).where(Asset.hostname == nazwa)).scalar_one().id


def _zapisz_dane(client, asset_id, **pola):
    strona = client.get(f"/assets/{asset_id}").text
    dane = {"nazwa": "MON-1", "typ": "monitor", "producent": "", "model": "",
            "numer_seryjny": "", "ip": "", "rola": "", "uwagi": "",
            "csrf_token": _extract_csrf(strona)}
    dane.update(pola)
    return client.post(f"/assets/{asset_id}/dane", data=dane, follow_redirects=False)


# --- rodzaje jako slownik ---------------------------------------------------

def test_rodzaje_startowe_powstaja_z_wzorca(client, tenant_a, make_user):
    """Klucze musza zgadzac sie z tymi, ktore leza juz przy sprzecie -
    inaczej istniejace maszyny zostalyby bez rozpoznawalnego rodzaju."""
    _admin_firmy(client, tenant_a, make_user)
    strona = client.get("/slowniki?kategoria=rodzaj")
    assert strona.status_code == 200

    with SessionLocal() as db:
        wpisy = db.execute(select(WpisSlownika).where(
            WpisSlownika.kategoria == "rodzaj")).scalars().all()
        klucze = {(w.atrybuty or {}).get("klucz_rodzaju") for w in wpisy}
    assert {k for k, _ in wzorzec.RODZAJE_STARTOWE} <= klucze
    for chroniony in wzorzec.KLUCZE_CHRONIONE:
        assert chroniony in klucze, "relacje rozpoznaja sprzet po tych kluczach"


def test_firma_moze_dolozyc_wlasny_rodzaj(client, tenant_a, make_user):
    from .test_sprzet_slowniki_konta import _dodaj_wpis

    _admin_firmy(client, tenant_a, make_user)
    client.get("/slowniki?kategoria=rodzaj")
    wpis_id = _dodaj_wpis(client, "rodzaj", "Projektor",
                          {"pole_klucz_rodzaju": "projektor"})
    with SessionLocal() as db:
        assert db.get(WpisSlownika, wpis_id).atrybuty["klucz_rodzaju"] == "projektor"


# --- pola wlasciwe dla rodzaju ----------------------------------------------

def test_kazdy_rodzaj_ma_wlasny_zestaw_pol(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    monitor = client.get("/rodzaje/monitor/pola").text
    siec = client.get("/rodzaje/siec/pola").text
    assert "Przekątna" in monitor and "Liczba portów" not in monitor
    assert "Liczba portów" in siec and "Przekątna" not in siec


def test_karta_sprzetu_pokazuje_pola_swojego_rodzaju(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    strona = client.get(f"/assets/{asset_id}").text
    assert "pole_przekatna_cale" in strona
    assert "pole_liczba_portow" not in strona, "pola przelacznika nie dotycza monitora"


def test_wartosci_pol_zapisuja_sie_i_sa_sprawdzane(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)

    odpowiedz = _zapisz_dane(client, asset_id, pole_przekatna_cale="27",
                             pole_rozdzielczosc="2560x1440", pole_matryca="IPS")
    assert odpowiedz.status_code == 303 and "blad=" not in odpowiedz.headers["location"]
    with SessionLocal() as db:
        assert db.get(Asset, asset_id).atrybuty["przekatna_cale"] == 27

    # Zakres z wzorca obowiazuje.
    odpowiedz = _zapisz_dane(client, asset_id, pole_przekatna_cale="500")
    assert "blad=" in odpowiedz.headers["location"]


def test_pole_spoza_listy_wyboru_odrzucone(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    odpowiedz = _zapisz_dane(client, asset_id, pole_matryca="plazmowa")
    assert "blad=" in odpowiedz.headers["location"]


# --- zmiana rodzaju ---------------------------------------------------------

def test_zmiana_rodzaju_ukrywa_ale_nie_kasuje(client, tenant_a, make_user):
    """Rozstrzygniecie z projektu: wartosci zostaja w bazie, znikaja z widoku,
    a przy zapisie pada ostrzezenie ile pol przestalo byc widocznych."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    _zapisz_dane(client, asset_id, pole_przekatna_cale="27", pole_rozdzielczosc="2560x1440")

    odpowiedz = _zapisz_dane(client, asset_id, typ="drukarka")
    assert odpowiedz.status_code == 303
    assert "ukryte=" in odpowiedz.headers["location"], "ostrzezenie o ukrytych polach"
    import urllib.parse

    adres = urllib.parse.unquote(odpowiedz.headers["location"])
    assert "Przekątna" in adres, "ostrzezenie wymienia pola"

    with SessionLocal() as db:
        sprzet = db.get(Asset, asset_id)
        assert sprzet.typ == "drukarka"
        assert sprzet.atrybuty["przekatna_cale"] == 27, "wartosc zostaje w bazie"

    strona = client.get(f"/assets/{asset_id}").text
    assert "pole_przekatna_cale" not in strona, "znika z widoku"
    assert "pole_licznik_wydrukow" in strona, "pojawiaja sie pola drukarki"


def test_zmiana_rodzaju_nie_zapisuje_pol_z_formularza(client, tenant_a, make_user):
    """Formularz opisuje rodzaj, ktory wlasnie przestaje obowiazywac. Zapisanie
    jego pol pod nowym rodzajem byloby przypisaniem wartosci nie tam, gdzie
    czlowiek je wpisal."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    _zapisz_dane(client, asset_id, pole_przekatna_cale="27")
    _zapisz_dane(client, asset_id, typ="drukarka", pole_przekatna_cale="99")

    with SessionLocal() as db:
        assert db.get(Asset, asset_id).atrybuty["przekatna_cale"] == 27


def test_powrot_do_rodzaju_odzyskuje_wartosci(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    _zapisz_dane(client, asset_id, pole_przekatna_cale="27")
    _zapisz_dane(client, asset_id, typ="drukarka")
    _zapisz_dane(client, asset_id, typ="monitor")

    strona = client.get(f"/assets/{asset_id}").text
    assert 'value="27"' in strona, "wartosc wrocila na karte"


# --- edycja zestawu pol -----------------------------------------------------

def test_pole_z_wartosciami_nie_daje_sie_usunac(client, tenant_a, make_user):
    """Ta sama zasada co przy slownikach - kasowanie danych i kasowanie pola
    to dwie osobne decyzje."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    _zapisz_dane(client, asset_id, pole_przekatna_cale="27")

    csrf = _extract_csrf(client.get("/rodzaje/monitor/pola").text)
    bez_przekatnej = {"kategoria": "sprzet", "wersja": 1, "pola": [
        p for p in wzorzec.wzorcowe_pola_rodzaju("monitor").model_dump(exclude_none=True)["pola"]
        if p["klucz"] != "przekatna_cale"]}
    odpowiedz = client.post("/rodzaje/monitor/pola",
                            data={"definicja": json.dumps(bez_przekatnej), "csrf_token": csrf},
                            follow_redirects=False)
    assert "blad=" in odpowiedz.headers["location"]

    with SessionLocal() as db:
        zapisany = db.execute(select(SchematSlownika).where(
            SchematSlownika.kategoria == "sprzet",
            SchematSlownika.rodzaj == "monitor")).scalar_one()
        assert any(p["klucz"] == "przekatna_cale" for p in zapisany.definicja["pola"])


def test_pola_rodzaju_zmienia_tylko_administrator(client, tenant_a, make_user):
    make_user(tenant_a["id"], "widz@firma-a.pl", "bardzo-dlugie-haslo", role="viewer")
    from .test_tenant_isolation import _login

    _login(client, "widz@firma-a.pl", "bardzo-dlugie-haslo")
    assert client.get("/rodzaje/monitor/pola").status_code == 403


def test_pola_rodzaju_nie_dotykaja_maszyny_z_agentem(client, tenant_a, make_user):
    """Rozstrzygniecie z projektu: maszyna z agentem opisuje sie raportem."""
    from .factories import build_report
    from .test_agent_api import enroll

    dane = enroll(client, tenant_a["token"], machine_id="maszyna-rodzaj-01").json()
    client.post("/api/v1/inventory",
                headers={"Authorization": f"Bearer {dane['agent_token']}"},
                json=build_report(machine_id="maszyna-rodzaj-01", hostname="SRV-RODZ"))
    _admin_firmy(client, tenant_a, make_user)

    strona = client.get(f"/assets/{dane['asset_id']}").text
    assert "Wlasciwe dla rodzaju" not in strona
    assert "pole_przekatna_cale" not in strona


def test_schematy_rodzajow_nie_wychodza_poza_firme(client, tenant_a, tenant_b, make_user):
    _admin_firmy(client, tenant_a, make_user)
    client.get("/rodzaje/monitor/pola")
    with SessionLocal() as db:
        wiersze = db.execute(select(SchematSlownika).where(
            SchematSlownika.kategoria == "sprzet")).scalars().all()
        assert {w.tenant_id for w in wiersze} == {tenant_a["id"]}


# --- formularz dodawania ----------------------------------------------------

def test_formularz_dodawania_zna_pola_wszystkich_rodzajow(client, tenant_a, make_user):
    """Zestawy ida do strony naraz, zeby zmiana rodzaju przestawiala formularz
    bez przeladowania - to skasowaloby wpisane juz wartosci."""
    _admin_firmy(client, tenant_a, make_user)
    strona = client.get("/assets/nowy").text
    assert "data-rodzaj-sprzetu" in strona
    assert "przekatna_cale" in strona, "pola monitora"
    assert "liczba_portow" in strona, "pola przelacznika"
    assert "data-pola-rodzaju-lista" in strona


def test_dodany_sprzet_zapisuje_pola_swojego_rodzaju(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/assets/nowy").text)
    odpowiedz = client.post("/assets/nowy", data={
        "nazwa": "MON-NOWY", "typ": "monitor", "producent": "Dell", "model": "U2723",
        "numer_seryjny": "", "ip": "", "lokalizacja_id": "", "rola": "",
        "owner_id": "", "uzytkownik_id": "", "dostawca_id": "", "uwagi": "",
        "pole_przekatna_cale": "27", "pole_matryca": "IPS",
        "csrf_token": csrf}, follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text

    with SessionLocal() as db:
        sprzet = db.execute(select(Asset).where(Asset.hostname == "MON-NOWY")).scalar_one()
        assert sprzet.atrybuty["przekatna_cale"] == 27
        assert sprzet.atrybuty["matryca"] == "IPS"


def test_skrypt_przestawia_pola_bez_przeladowania():
    """Przeladowanie skasowaloby to, co czlowiek zdazyl juz wpisac wyzej."""
    from pathlib import Path

    skrypt = (Path(__file__).resolve().parent.parent
              / "cmdb_server" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-rodzaj-sprzetu" in skrypt
    assert "data-pola-rodzaju-lista" in skrypt


def test_formularz_dodawania_jest_zwiezly(client, tenant_a, make_user):
    """Nazwa, rola i uwagi opisywaly to samo trzy razy, a uzytkownik przy
    monitorze nie znaczy nic. Zostaje niezbedne minimum."""
    _admin_firmy(client, tenant_a, make_user)
    strona = client.get("/assets/nowy").text

    assert 'name="rola"' not in strona, "role ustawia sie na karcie"
    assert 'name="uzytkownik_id"' not in strona, "uzytkownika ustawia sie na karcie"
    assert strona.count('name="uwagi"') == 1, "jedno pole opisowe wystarczy"

    # Ten sam uklad co na karcie: pola tozsamosci w jednym zawijanym wierszu.
    assert strona.count('class="row-form"') >= 2
    for pole in ('name="nazwa"', 'name="numer_seryjny"', 'name="ip"',
                 'name="lokalizacja_id"', 'name="dostawca_id"', 'name="owner_id"'):
        assert pole in strona, f"{pole} jest potrzebne"


def test_adres_ip_i_numer_seryjny_maja_wyjasnienie(client, tenant_a, make_user):
    """Oba wygladaja na zbedne przy monitorze, a maja funkcje: po numerze wpis
    laczy sie z agentem, po adresie rozpoznaje go wykrywanie sieci."""
    _admin_firmy(client, tenant_a, make_user)
    strona = client.get("/assets/nowy").text
    assert "połączy ten wpis z agentem" in strona
    assert "rozpozna go wykrywanie sieci" in strona


def test_rola_jest_tylko_w_jednym_formularzu(client, tenant_a, make_user):
    """Rola byla i w przypisaniach, i w danych sprzetu - dwa pola opisujace
    te sama wartosc rozjezdzaja sie przy pierwszym zapisie jednego z nich."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    strona = client.get(f"/assets/{asset_id}").text
    assert strona.count('name="role_label"') == 1
    assert 'name="rola"' not in strona


def test_zapis_danych_nie_kasuje_roli(client, tenant_a, make_user):
    """Panel danych sprzetu nie wysyla juz roli, wiec nie moze jej tez zerowac."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    client.post(f"/assets/{asset_id}/owner", data={
        "owner_id": "", "uzytkownik_id": "", "lokalizacja_id": "",
        "role_label": "glowny monitor", "csrf_token": csrf}, follow_redirects=False)

    _zapisz_dane(client, asset_id, producent="AOC")
    with SessionLocal() as db:
        sprzet = db.get(Asset, asset_id)
        assert sprzet.role_label == "glowny monitor", "rola przetrwala zapis danych"
        assert sprzet.manufacturer == "AOC"


def test_sprzet_sieciowy_nie_ma_drugiego_adresu(client, tenant_a, make_user):
    """Pole "adres zarzadzania" powtarzalo Adres IP, ktory ma juz kolumne
    i po ktorym dopasowuje sie wykrywanie sieci."""
    _admin_firmy(client, tenant_a, make_user)
    strona = client.get("/rodzaje/siec/pola").text
    assert "adres_zarzadzania" not in strona
    assert "liczba_portow" in strona


def test_uwagi_maja_wlasna_zakladke_i_jedno_pole(client, tenant_a, make_user):
    """To samo pole stalo w danych sprzetu i w zakupie - dwa miejsca na jedna
    wartosc rozjezdzaja sie przy zapisie tego, ktore akurat bylo puste."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    strona = client.get(f"/assets/{asset_id}").text

    assert 'data-tab="uwagi"' in strona
    assert strona.count('name="uwagi"') == 1
    assert 'name="purchase_notes"' not in strona


def test_wycofanie_ma_wlasna_zakladke_i_skrot_w_naglowku(client, tenant_a, make_user):
    """Wycofanie stalo w zakladce "Zmiany" - w miejscu, ktorego nikt nie szuka,
    gdy chce zdjac sprzet ze stanu. Osoba testujaca nie znalazla tego wcale."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    strona = client.get(f"/assets/{asset_id}").text

    assert 'data-tab="cykl"' in strona
    assert 'data-panel="cykl"' in strona
    assert 'href="#cykl"' in strona, "skrot z naglowka karty"
    assert strona.count("Cykl życia") >= 2, "przycisk zakladki i naglowek panelu"
    # Panel zmian nie ma juz przy sobie decyzji o wycofaniu.
    zmiany = strona.split('data-panel="zmiany"')[1].split('data-panel=')[0]
    assert "Wycofaj z użytku" not in zmiany


def test_uwagi_widac_w_podsumowaniu(client, tenant_a, make_user):
    """Podsumowanie siegalo po asset.notes - pole, ktorego w modelu nie ma,
    wiec wartosc byla ZAWSZE nieznana, niezaleznie od tego, co wpisano."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    assert client.post(f"/assets/{asset_id}/uwagi",
                       data={"uwagi": "stoi w szafie R3", "csrf_token": csrf},
                       follow_redirects=False).status_code == 303

    strona = client.get(f"/assets/{asset_id}").text
    assert strona.count("stoi w szafie R3") >= 2, "w podsumowaniu i w zakladce"


def test_zapis_danych_nie_kasuje_uwag(client, tenant_a, make_user):
    """Mapper przepisuje wylacznie to, co formularz przyslal - inaczej
    usuniecie pola z jednego formularza zerowaloby wartosc z innego."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    client.post(f"/assets/{asset_id}/uwagi",
                data={"uwagi": "klucz u kierownika", "csrf_token": csrf},
                follow_redirects=False)

    _zapisz_dane(client, asset_id, producent="AOC")
    with SessionLocal() as db:
        assert db.get(Asset, asset_id).purchase_notes == "klucz u kierownika"


def test_pasek_akcji_rozdziela_zapis_od_usuwania(client, tenant_a, make_user):
    """Dwie akcje o roznych skutkach nie stoja obok siebie: zapis po lewej,
    usuwanie przy prawej krawedzi."""
    _admin_firmy(client, tenant_a, make_user)
    asset_id = _sprzet(client, tenant_a)
    strona = client.get(f"/assets/{asset_id}").text

    assert 'class="pasek-akcji"' in strona
    # Przycisk zapisu stoi poza formularzem i wskazuje go atrybutem "form" -
    # formularzy nie wolno zagniezdzac, a usuwanie jest osobnym.
    assert 'form="dane-sprzetu"' in strona
    assert 'id="dane-sprzetu"' in strona
