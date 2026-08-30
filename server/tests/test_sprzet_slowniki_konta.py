"""Sprzet wpisywany recznie, slowniki firmowe, audytor globalny i hasla.

Cztery obszary trzymane w jednym pliku, bo splata je jeden scenariusz: firma
wpisuje drukarke, przy okazji powstaje lokalizacja w slowniku, audytor z innej
organizacji te dane oglada i nie moze ich ruszyc, a administrator zmienia
komus haslo.
"""
from __future__ import annotations

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    TYP_KOMPUTER,
    ZRODLO_RECZNE,
    Asset,
    Owner,
    PortalUser,
    WpisSlownika,
)
from cmdb_server.security import hash_password

from .factories import build_report
from .test_agent_api import enroll
from .test_tenant_isolation import _extract_csrf, _login


def _admin_firmy(client, tenant, make_user, email="admin@firma-a.pl"):
    make_user(tenant["id"], email, "bardzo-dlugie-haslo")
    _login(client, email, "bardzo-dlugie-haslo")


def _dodaj_osobe(client, imie: str, email: str, dzial: str = "") -> str:
    csrf = _extract_csrf(client.get("/owners").text)
    odpowiedz = client.post(
        "/owners",
        data={"full_name": imie, "email": email, "phone": "", "department": dzial,
              "notes": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303, odpowiedz.text
    with SessionLocal() as db:
        return db.execute(select(Owner).where(Owner.email == email)).scalar_one().id


def _dodaj_sprzet(client, **nadpisania):
    csrf = _extract_csrf(client.get("/assets/nowy").text)
    dane = {
        "nazwa": "SW-MAGAZYN-01",
        "typ": "siec",
        "producent": "Cisco",
        "model": "CBS350",
        "numer_seryjny": "SN-SW-1",
        "ip": "10.10.5.30",
        "lokalizacja": "Serwerownia A",
        "rola": "switch dostepowy",
        "owner_id": "",
        "uzytkownik_id": "",
        "dostawca": "Komputronik",
        "uwagi": "24 porty",
        "csrf_token": csrf,
    }
    dane.update(nadpisania)
    return client.post("/assets/nowy", data=dane, follow_redirects=False)


# --- sprzet wpisywany recznie ----------------------------------------------

def test_reczny_sprzet_trafia_na_liste_i_da_sie_go_edytowac(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)

    odpowiedz = _dodaj_sprzet(client)
    assert odpowiedz.status_code == 303

    with SessionLocal() as db:
        sprzet = db.execute(select(Asset)).scalar_one()
        assert sprzet.typ == "siec"
        assert sprzet.zrodlo == ZRODLO_RECZNE
        assert sprzet.machine_id.startswith("reczne:")
        assert sprzet.lokalizacja == "Serwerownia A"
        assert sprzet.vendor == "Komputronik"
        sprzet_id = sprzet.id

    lista = client.get("/assets")
    assert "SW-MAGAZYN-01" in lista.text
    assert "Sprzet sieciowy" in lista.text
    # Bez agenta nie ma sensu pisac "nigdy" w kolumnie ostatniego kontaktu.
    assert "bez agenta" in lista.text

    csrf = _extract_csrf(client.get(f"/assets/{sprzet_id}").text)
    zmiana = client.post(
        f"/assets/{sprzet_id}/dane",
        data={"nazwa": "SW-MAGAZYN-02", "typ": "drukarka", "producent": "HP",
              "model": "M404", "numer_seryjny": "SN-2", "ip": "10.10.5.31",
              "rola": "drukarka kadr", "uwagi": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert zmiana.status_code == 303
    with SessionLocal() as db:
        sprzet = db.get(Asset, sprzet_id)
        assert (sprzet.hostname, sprzet.typ, sprzet.model) == ("SW-MAGAZYN-02", "drukarka", "M404")


def test_reczny_sprzet_da_sie_usunac_a_maszyny_z_agentem_nie(client, tenant_a, make_user):
    """Wpis reczny to sam tekst; maszyna z agentem niesie historie raportow."""
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client)

    token = enroll(client, tenant_a["token"], machine_id="maszyna-0001", hostname="SRV").json()["agent_token"]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id="maszyna-0001", hostname="SRV"),
    )

    with SessionLocal() as db:
        reczny = db.execute(select(Asset).where(Asset.zrodlo == ZRODLO_RECZNE)).scalar_one().id
        z_agentem = db.execute(select(Asset).where(Asset.zrodlo == "agent")).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{reczny}").text)
    assert client.post(f"/assets/{z_agentem}/usun",
                       data={"csrf_token": csrf}, follow_redirects=False).status_code == 400
    assert client.post(f"/assets/{reczny}/usun",
                       data={"csrf_token": csrf}, follow_redirects=False).status_code == 303

    with SessionLocal() as db:
        assert db.get(Asset, reczny) is None
        assert db.get(Asset, z_agentem) is not None


def test_danych_maszyny_z_agentem_nie_edytuje_sie_recznie(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    token = enroll(client, tenant_a["token"], machine_id="maszyna-0001", hostname="SRV").json()["agent_token"]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token}"},
        json=build_report(machine_id="maszyna-0001", hostname="SRV"),
    )
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset)).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    odpowiedz = client.post(
        f"/assets/{asset_id}/dane",
        data={"nazwa": "PODMIENIONA", "typ": TYP_KOMPUTER, "producent": "", "model": "",
              "numer_seryjny": "", "ip": "", "rola": "", "uwagi": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400
    with SessionLocal() as db:
        assert db.get(Asset, asset_id).hostname == "SRV"


def test_reczny_sprzet_nie_liczy_sie_jako_bez_kontaktu(client, tenant_a, make_user):
    """Urzadzenie bez agenta nigdy sie nie odezwie - alarm bylby staly i falszywy."""
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client)

    assert "1" in client.get("/").text  # strona sie renderuje
    lista = client.get("/assets?state=stale")
    assert "SW-MAGAZYN-01" not in lista.text
    lista = client.get("/assets?state=online")
    assert "SW-MAGAZYN-01" not in lista.text
    # Bez filtra stanu sprzet jest widoczny.
    assert "SW-MAGAZYN-01" in client.get("/assets").text


# --- opiekun i uzytkownik ---------------------------------------------------

def test_opiekun_i_uzytkownik_to_dwie_rozne_osoby(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    opiekun = _dodaj_osobe(client, "Anna Nowak", "anna@firma-a.pl", "IT")
    uzytkownik = _dodaj_osobe(client, "Jan Kowalski", "jan@firma-a.pl", "Ksiegowosc")
    _dodaj_sprzet(client, typ="komputer", nazwa="LAPTOP-01")

    with SessionLocal() as db:
        asset_id = db.execute(select(Asset)).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    odpowiedz = client.post(
        f"/assets/{asset_id}/owner",
        data={"owner_id": opiekun, "uzytkownik_id": uzytkownik,
              "role_label": "laptop ksiegowosci", "lokalizacja": "Pokoj 214",
              "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303

    with SessionLocal() as db:
        sprzet = db.get(Asset, asset_id)
        assert sprzet.owner_id == opiekun
        assert sprzet.uzytkownik_id == uzytkownik
        assert sprzet.lokalizacja == "Pokoj 214"

    lista = client.get("/assets").text
    assert "Anna Nowak" in lista
    assert "uzywa: Jan Kowalski" in lista


def test_uzytkownik_spoza_firmy_odrzucony(client, tenant_a, tenant_b, make_user):
    _admin_firmy(client, tenant_b, make_user, email="admin@firma-b.pl")
    obcy = _dodaj_osobe(client, "Obca Osoba", "obca@firma-b.pl")
    client.post("/logout")

    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client)
    with SessionLocal() as db:
        asset_id = db.execute(
            select(Asset).where(Asset.tenant_id == tenant_a["id"])
        ).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    odpowiedz = client.post(
        f"/assets/{asset_id}/owner",
        data={"owner_id": "", "uzytkownik_id": obcy, "role_label": "",
              "lokalizacja": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


# --- slowniki ---------------------------------------------------------------

def test_nowa_wartosc_z_formularza_trafia_do_slownika(client, tenant_a, make_user):
    """Slownik zapelnia sie sam - to caly sens tego rozwiazania."""
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_osobe(client, "Anna Nowak", "anna@firma-a.pl", "Ksiegowosc")
    _dodaj_sprzet(client)

    with SessionLocal() as db:
        wpisy = {
            (w.kategoria, w.wartosc)
            for w in db.execute(select(WpisSlownika)).scalars()
        }
    assert ("dzial", "Ksiegowosc") in wpisy
    assert ("lokalizacja", "Serwerownia A") in wpisy
    assert ("dostawca", "Komputronik") in wpisy

    strona = client.get("/slowniki")
    assert strona.status_code == 200
    assert "Serwerownia A" in strona.text


def test_ta_sama_wartosc_inaczej_zapisana_nie_tworzy_drugiego_wpisu(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client, nazwa="SW-1", lokalizacja="Serwerownia A")
    _dodaj_sprzet(client, nazwa="SW-2", lokalizacja="  serwerownia   a ")

    with SessionLocal() as db:
        lokalizacje = db.execute(
            select(WpisSlownika).where(WpisSlownika.kategoria == "lokalizacja")
        ).scalars().all()
        assert [w.wartosc for w in lokalizacje] == ["Serwerownia A"]
        # Drugi sprzet dostaje pisownie ze slownika, nie swoja.
        assert {a.lokalizacja for a in db.execute(select(Asset)).scalars()} == {"Serwerownia A"}


def test_usuniecie_ze_slownika_nie_rusza_sprzetu(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client)

    with SessionLocal() as db:
        wpis_id = db.execute(
            select(WpisSlownika).where(WpisSlownika.kategoria == "lokalizacja")
        ).scalar_one().id

    csrf = _extract_csrf(client.get("/slowniki").text)
    assert client.post(f"/slowniki/{wpis_id}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303

    with SessionLocal() as db:
        assert db.get(WpisSlownika, wpis_id) is None
        assert db.execute(select(Asset)).scalar_one().lokalizacja == "Serwerownia A"


def test_slownik_nie_wychodzi_poza_firme(client, tenant_a, tenant_b, make_user):
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client, lokalizacja="Tajna serwerownia")
    client.post("/logout")

    _admin_firmy(client, tenant_b, make_user, email="admin@firma-b.pl")
    assert "Tajna serwerownia" not in client.get("/slowniki").text
    assert "Tajna serwerownia" not in client.get("/assets/nowy").text


# --- audytor globalny -------------------------------------------------------

def _zaloz_audytora(email="audyt@cmdb.pl", haslo="bardzo-dlugie-haslo") -> str:
    with SessionLocal() as db:
        konto = PortalUser(
            tenant_id=None,
            email=email,
            password_hash=hash_password(haslo),
            role="viewer",
            is_superadmin=False,
            is_global_viewer=True,
        )
        db.add(konto)
        db.commit()
        return konto.id


def test_audytor_widzi_kazda_firme_ale_nic_nie_zapisze(client, tenant_a, tenant_b, make_user):
    _admin_firmy(client, tenant_a, make_user)
    _dodaj_sprzet(client, nazwa="SPRZET-A")
    client.post("/logout")
    _admin_firmy(client, tenant_b, make_user, email="admin@firma-b.pl")
    _dodaj_sprzet(client, nazwa="SPRZET-B")
    client.post("/logout")

    _zaloz_audytora()
    _login(client, "audyt@cmdb.pl", "bardzo-dlugie-haslo")

    client.get(f"/switch-tenant?slug={tenant_a['slug']}", follow_redirects=False)
    assert "SPRZET-A" in client.get("/assets").text
    client.get(f"/switch-tenant?slug={tenant_b['slug']}", follow_redirects=False)
    strona_b = client.get("/assets")
    assert "SPRZET-B" in strona_b.text
    # Konto tylko do odczytu nie dostaje nawet przycisku dodawania.
    assert "Dodaj sprzet recznie" not in strona_b.text

    with SessionLocal() as db:
        asset_b = db.execute(
            select(Asset).where(Asset.tenant_id == tenant_b["id"])
        ).scalar_one().id

    csrf = _extract_csrf(client.get(f"/assets/{asset_b}").text)
    proba = client.post(
        f"/assets/{asset_b}/owner",
        data={"owner_id": "", "uzytkownik_id": "", "role_label": "podmieniona",
              "lokalizacja": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert proba.status_code == 403
    proba = client.post("/assets/nowy", data={"nazwa": "X", "typ": "siec", "csrf_token": csrf},
                        follow_redirects=False)
    assert proba.status_code == 403


def test_audytor_nie_wchodzi_do_panelu_administracyjnego(client, tenant_a):
    _zaloz_audytora()
    _login(client, "audyt@cmdb.pl", "bardzo-dlugie-haslo")
    assert client.get("/admin/firmy", follow_redirects=False).status_code == 403


def test_superadmin_zaklada_audytora_z_panelu(client, tenant_a, make_user):
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    odpowiedz = client.post(
        "/admin/users/globalne",
        data={"email": "audyt@cmdb.pl", "password": "bardzo-dlugie-haslo",
              "full_name": "Audytor", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        konto = db.execute(
            select(PortalUser).where(PortalUser.email == "audyt@cmdb.pl")
        ).scalar_one()
        assert konto.is_global_viewer is True
        assert konto.is_superadmin is False
        assert konto.tenant_id is None


def test_za_krotkie_haslo_audytora_odrzucone(client, tenant_a, make_user):
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    odpowiedz = client.post(
        "/admin/users/globalne",
        data={"email": "audyt@cmdb.pl", "password": "krotkie", "full_name": "",
              "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 400


# --- hasla ------------------------------------------------------------------

def test_administrator_firmy_zmienia_sobie_haslo(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/konto").text)

    odpowiedz = client.post(
        "/konto/haslo",
        data={"obecne": "bardzo-dlugie-haslo", "nowe": "jeszcze-dluzsze-haslo",
              "powtorzone": "jeszcze-dluzsze-haslo", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    assert odpowiedz.headers["location"] == "/login"

    client.post("/logout")
    _login(client, "admin@firma-a.pl", "jeszcze-dluzsze-haslo")


def test_zmiana_hasla_wymaga_dotychczasowego(client, tenant_a, make_user):
    """Sama zalogowana sesja nie wystarczy - inaczej porzucona przegladarka
    pozwalalaby przejac konto na stale."""
    _admin_firmy(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/konto").text)

    odpowiedz = client.post(
        "/konto/haslo",
        data={"obecne": "zle-haslo-zupelnie", "nowe": "jeszcze-dluzsze-haslo",
              "powtorzone": "jeszcze-dluzsze-haslo", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    assert "blad=" in odpowiedz.headers["location"]

    client.post("/logout")
    # Stare haslo nadal dziala, nowe nie.
    zle = client.post(
        "/login",
        data={"email": "admin@firma-a.pl", "password": "jeszcze-dluzsze-haslo"},
        follow_redirects=False,
    )
    assert zle.status_code == 401
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")


def test_za_krotkie_nowe_haslo_odrzucone(client, tenant_a, make_user):
    _admin_firmy(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/konto").text)
    odpowiedz = client.post(
        "/konto/haslo",
        data={"obecne": "bardzo-dlugie-haslo", "nowe": "krotkie", "powtorzone": "krotkie",
              "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "blad=" in odpowiedz.headers["location"]
    client.post("/logout")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")


def test_superadmin_ustawia_haslo_administratorowi_firmy(client, tenant_a, make_user):
    konto_id = make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    odpowiedz = client.post(
        f"/admin/users/{konto_id}/haslo",
        data={"password": "nowe-bardzo-dlugie-haslo", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    client.post("/logout")
    _login(client, "admin@firma-a.pl", "nowe-bardzo-dlugie-haslo")


def test_superadmin_ustawia_haslo_audytorowi_i_sobie(client, tenant_a, make_user):
    audytor_id = _zaloz_audytora()
    root_id = make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    for konto_id in (audytor_id, root_id):
        odpowiedz = client.post(
            f"/admin/users/{konto_id}/haslo",
            data={"password": "nowe-bardzo-dlugie-haslo", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert odpowiedz.status_code == 303

    client.post("/logout")
    _login(client, "audyt@cmdb.pl", "nowe-bardzo-dlugie-haslo")
    client.post("/logout")
    _login(client, "root@cmdb.pl", "nowe-bardzo-dlugie-haslo")


def test_administrator_firmy_nie_zmienia_hasel_innym(client, tenant_a, tenant_b, make_user):
    ofiara = make_user(tenant_b["id"], "admin@firma-b.pl", "bardzo-dlugie-haslo")
    _admin_firmy(client, tenant_a, make_user)
    csrf = _extract_csrf(client.get("/konto").text)

    odpowiedz = client.post(
        f"/admin/users/{ofiara}/haslo",
        data={"password": "przejete-bardzo-dlugie", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 403


def test_konto_dziala_dla_kont_bez_firmy(client, tenant_a, make_user):
    """Superadmin i audytor nie naleza do zadnej firmy, a haslo zmienic musza.

    Widok wlasnego konta celowo nie zalezy wiec od kontekstu firmy - inaczej
    konto globalne dostawaloby 403 na wlasnej stronie.
    """
    _zaloz_audytora()
    _login(client, "audyt@cmdb.pl", "bardzo-dlugie-haslo")
    strona = client.get("/konto")
    assert strona.status_code == 200
    assert "wszystkie firmy, tylko odczyt" in strona.text

    csrf = _extract_csrf(strona.text)
    odpowiedz = client.post(
        "/konto/haslo",
        data={"obecne": "bardzo-dlugie-haslo", "nowe": "jeszcze-dluzsze-haslo",
              "powtorzone": "jeszcze-dluzsze-haslo", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert odpowiedz.status_code == 303
    client.post("/logout")
    _login(client, "audyt@cmdb.pl", "jeszcze-dluzsze-haslo")
    client.post("/logout")

    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    assert client.get("/konto").status_code == 200


def test_audytor_nie_dostaje_formularzy_raportow(client, tenant_a, make_user):
    """Formularz, ktory i tak skonczy sie kodem 403, nie powinien byc pokazywany."""
    _zaloz_audytora()
    _login(client, "audyt@cmdb.pl", "bardzo-dlugie-haslo")
    client.get(f"/switch-tenant?slug={tenant_a['slug']}", follow_redirects=False)

    strona = client.get("/raporty")
    assert strona.status_code == 200
    assert "uprawnienia tylko do odczytu" in strona.text
    assert 'action="/raporty/definicje"' not in strona.text
    # Tresc informacyjna zostaje - audytor ma raporty ogladac.
    assert "Co zawiera ktory raport" in strona.text


def test_haslo_ustawia_sie_w_oknie_a_nie_w_tabeli(client, tenant_a, make_user):
    """Pole hasla widoczne w tabeli pokazuje je kazdemu, kto patrzy w ekran.

    Sam endpoint zostaje bez zmian - zmienia sie tylko to, ze formularz jest
    w oknie modalnym, a pole domyslnie zakryte.
    """
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")

    strona = client.get("/admin/firmy")
    assert strona.status_code == 200
    assert "data-okno-hasla" in strona.text
    assert 'data-haslo-akcja="/admin/users/' in strona.text
    # Zadnego pola hasla wpisywanego jawnie - ani w tabeli, ani w formularzach.
    assert 'name="password" type="text"' not in strona.text
    assert 'type="text" name="password"' not in strona.text
    assert strona.text.count('type="password"') >= 3


def test_superadmin_usuwa_konto_ale_nie_swoje(client, tenant_a, make_user):
    """Zakaz kasowania samego siebie jest tez ochrona przed zatrzasnieciem
    drzwi: skoro operacje wykonuje superadmin, a wlasnego konta ruszyc nie
    moze, zawsze zostaje przynajmniej jeden czynny superadmin."""
    obce = make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    moje = make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    wlasne = client.post(f"/admin/users/{moje}/usun", data={"csrf_token": csrf},
                         follow_redirects=False)
    assert wlasne.status_code == 400
    assert "wlasnego konta" in wlasne.text

    cudze = client.post(f"/admin/users/{obce}/usun", data={"csrf_token": csrf},
                        follow_redirects=False)
    assert cudze.status_code == 303

    with SessionLocal() as db:
        assert db.get(PortalUser, obce) is None
        assert db.get(PortalUser, moje) is not None
    # Usuniete konto przestaje sie logowac.
    client.post("/logout")
    odmowa = client.post(
        "/login", data={"email": "admin@firma-a.pl", "password": "bardzo-dlugie-haslo"},
        follow_redirects=False,
    )
    assert odmowa.status_code == 401


def test_wlasnego_konta_nie_da_sie_wylaczyc(client, tenant_a, make_user):
    moje = make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    odpowiedz = client.post(f"/admin/users/{moje}/active", data={"csrf_token": csrf},
                            follow_redirects=False)
    assert odpowiedz.status_code == 400
    with SessionLocal() as db:
        assert db.get(PortalUser, moje).is_active is True


def test_pomylkowo_zalozonego_superadmina_da_sie_usunac(client, tenant_a, make_user):
    """Wczesniej konto superadmina bylo nietykalne: nie dalo sie go ani
    wylaczyc, ani usunac, wiec pomylka przy zakladaniu zostawala w systemie
    na zawsze."""
    pomylka = make_user(None, "pomylka@cmdb.pl", "bardzo-dlugie-haslo")
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")
    csrf = _extract_csrf(client.get("/admin/firmy").text)

    assert client.post(f"/admin/users/{pomylka}/active", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    assert client.post(f"/admin/users/{pomylka}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.get(PortalUser, pomylka) is None


def test_akcje_kont_sa_przyciskami_a_wylaczanie_rozroznia_firme_od_konta(
    client, tenant_a, make_user
):
    """W jednym wierszu stalo "wylacz" (konto) obok "Wylacz" (cala firma)."""
    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    make_user(None, "root@cmdb.pl", "bardzo-dlugie-haslo")
    _login(client, "root@cmdb.pl", "bardzo-dlugie-haslo")

    strona = client.get("/admin/firmy").text
    assert "Wylacz firme" in strona
    assert "Wylacz konto" in strona
    assert 'action="/admin/users/' in strona and "/usun" in strona
    # Wlasne konto bez akcji, ktore odcielyby dostep.
    assert "to Twoje konto" in strona
