"""Konta panelu w jednym miejscu.

Powod powstania ekranu: konta byly pokazywane POD firmami, a technik helpdesku
do zadnej firmy nie nalezy - po nadaniu mu firm znikal z widoku i nie dalo sie
go ani wylaczyc, ani usunac. Tu sprawdzamy, ze widac kazde konto niezaleznie
od tego, skad bierze sie jego uprawnienie.
"""
from __future__ import annotations

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, PortalUser, Tenant, Zgloszenie
from cmdb_server.security import hash_password
from cmdb_server.services import helpdesk

from .test_tenant_isolation import _extract_csrf, _login

HASLO = "haslo-administracji-2026"


def _superadmin(client, make_user):
    make_user(None, "szef@mojadomena.pl", HASLO)
    _login(client, "szef@mojadomena.pl", HASLO)


def _csrf(client) -> str:
    return _extract_csrf(client.get("/admin/konta").text)


def _technik(email: str, firmy: list[str], nazwa: str = "Technik Testowy") -> str:
    with SessionLocal() as db:
        konto = PortalUser(
            tenant_id=None, email=email, full_name=nazwa,
            password_hash=hash_password(HASLO), role="admin",
        )
        db.add(konto)
        db.flush()
        for tenant_id in firmy:
            helpdesk.nadaj_dostep(db, konto.id, tenant_id)
        db.commit()
        return konto.id


# --- widocznosc -------------------------------------------------------------

def test_lista_pokazuje_konta_kazdego_rodzaju(client, tenant_a, make_user):
    """Sedno zgloszenia: technik bez firmy tez ma tu byc."""
    make_user(tenant_a["id"], "admin@firma-a.pl", HASLO, role="admin")
    make_user(tenant_a["id"], "widz@firma-a.pl", HASLO, role="viewer")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _superadmin(client, make_user)

    strona = client.get("/admin/konta").text
    for adres in ("admin@firma-a.pl", "widz@firma-a.pl",
                  "technik@mojadomena.pl", "szef@mojadomena.pl"):
        assert adres in strona, adres
    assert "technik helpdesku" in strona


def test_konto_nie_znika_po_nadaniu_mu_firm(client, tenant_a, tenant_b, make_user):
    """Dokladnie przypadek ze zgloszenia: viewer firmy dostaje firmy helpdesku
    i przestaje byc kontem firmowym - ma nadal byc widoczny."""
    make_user(tenant_a["id"], "widz@firma-a.pl", HASLO, role="viewer")
    _superadmin(client, make_user)
    with SessionLocal() as db:
        konto_id = db.execute(
            select(PortalUser.id).where(PortalUser.email == "widz@firma-a.pl")
        ).scalar_one()

    client.post(f"/admin/users/{konto_id}/zakres", data={
        "csrf_token": _csrf(client), "zakres": "technik", "role": "admin",
    }, follow_redirects=False)
    client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_a["id"], "akcja": "nadaj",
    }, follow_redirects=False)

    strona = client.get("/admin/konta").text
    assert "widz@firma-a.pl" in strona
    assert "Firma A" in strona
    with SessionLocal() as db:
        konto = db.get(PortalUser, konto_id)
        assert konto.tenant_id is None
        assert helpdesk.ma_dostep(db, konto, tenant_a["id"]) is True


def test_ekran_tylko_dla_superadmina(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@firma-a.pl", HASLO, role="admin")
    _login(client, "admin@firma-a.pl", HASLO)
    assert client.get("/admin/konta", follow_redirects=False).status_code == 403


# --- zakladanie kont --------------------------------------------------------

def test_zakladanie_technika_i_przydzial_firm(client, tenant_a, make_user):
    """Technika nie dalo sie dotad zalozyc wcale - panel umial tworzyc konto
    firmy albo audytora globalnego."""
    _superadmin(client, make_user)

    client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "Wladek@mojadomena.pl",
        "full_name": "Wladek Nowak", "password": "haslo-technika-2026",
        "zakres": "technik", "role": "admin",
    }, follow_redirects=False)

    with SessionLocal() as db:
        konto = db.execute(
            select(PortalUser).where(PortalUser.email == "wladek@mojadomena.pl")
        ).scalar_one()
        assert konto.tenant_id is None
        assert konto.is_superadmin is False and konto.is_global_viewer is False
        konto_id = konto.id

    client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_a["id"], "akcja": "nadaj",
    }, follow_redirects=False)

    # Nowy technik loguje sie i widzi zgloszenia swojej firmy.
    client.post("/logout")
    _login(client, "wladek@mojadomena.pl", "haslo-technika-2026")
    assert client.get("/helpdesk").status_code == 200


def test_zakladanie_konta_firmy_i_audytora(client, tenant_a, make_user):
    _superadmin(client, make_user)

    client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "ksiegowa@firma-a.pl",
        "password": "haslo-ksiegowej-2026", "zakres": "firma",
        "tenant_id": tenant_a["id"], "role": "viewer",
    }, follow_redirects=False)
    client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "audytor@mojadomena.pl",
        "password": "haslo-audytora-2026", "zakres": "audytor", "role": "admin",
    }, follow_redirects=False)

    with SessionLocal() as db:
        ksiegowa = db.execute(
            select(PortalUser).where(PortalUser.email == "ksiegowa@firma-a.pl")
        ).scalar_one()
        audytor = db.execute(
            select(PortalUser).where(PortalUser.email == "audytor@mojadomena.pl")
        ).scalar_one()

    assert ksiegowa.tenant_id == tenant_a["id"] and ksiegowa.role == "viewer"
    assert audytor.is_global_viewer is True
    # Audytor niczego nie zmienia w zadnej firmie, wiec rola "admin" z formularza
    # nie moze mu sie przykleic.
    assert audytor.role == "viewer"


def test_krotkie_haslo_i_zajety_adres_sa_odrzucane(client, tenant_a, make_user):
    _superadmin(client, make_user)

    krotkie = client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "ktos@mojadomena.pl",
        "password": "krotkie", "zakres": "technik",
    }, follow_redirects=False)
    assert krotkie.status_code == 400

    zajety = client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "szef@mojadomena.pl",
        "password": "haslo-dostatecznie-dlugie", "zakres": "technik",
    }, follow_redirects=False)
    assert zajety.status_code == 400


# --- zmiana rodzaju konta ---------------------------------------------------

def test_zmiana_rodzaju_uniewaznia_sesje_konta(client, tenant_a, make_user):
    """Odebrane prawo ma przestac dzialac natychmiast, a nie z wygasnieciem
    ciasteczka."""
    konto_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    with SessionLocal() as db:
        przed = db.get(PortalUser, konto_id).session_version

    _superadmin(client, make_user)
    client.post(f"/admin/users/{konto_id}/zakres", data={
        "csrf_token": _csrf(client), "zakres": "firma",
        "tenant_id": tenant_a["id"], "role": "viewer",
    }, follow_redirects=False)

    with SessionLocal() as db:
        konto = db.get(PortalUser, konto_id)
        assert konto.tenant_id == tenant_a["id"]
        assert konto.role == "viewer"
        assert konto.session_version > przed


def test_wylaczone_konto_traci_dostep_natychmiast(client, tenant_a, make_user):
    konto_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    klient_technika = client
    _login(klient_technika, "technik@mojadomena.pl", HASLO)
    assert klient_technika.get("/helpdesk").status_code == 200
    klient_technika.post("/logout")

    _superadmin(client, make_user)
    client.post(f"/admin/users/{konto_id}/active", data={
        "csrf_token": _csrf(client), "powrot": "/admin/konta",
    }, follow_redirects=False)

    with SessionLocal() as db:
        assert db.get(PortalUser, konto_id).is_active is False


def test_wlasnego_konta_nie_da_sie_ruszyc(client, make_user):
    _superadmin(client, make_user)
    with SessionLocal() as db:
        moje = db.execute(
            select(PortalUser.id).where(PortalUser.email == "szef@mojadomena.pl")
        ).scalar_one()

    for sciezka in (f"/admin/users/{moje}/active", f"/admin/users/{moje}/usun"):
        odpowiedz = client.post(sciezka, data={"csrf_token": _csrf(client)},
                                follow_redirects=False)
        assert odpowiedz.status_code == 400, sciezka


# --- usuwanie ---------------------------------------------------------------

def test_zbedne_konto_da_sie_usunac(client, tenant_a, make_user):
    konto_id = _technik("pomylka@mojadomena.pl", [tenant_a["id"]])
    _superadmin(client, make_user)

    odpowiedz = client.post(f"/admin/users/{konto_id}/usun", data={
        "csrf_token": _csrf(client), "powrot": "/admin/konta",
    }, follow_redirects=False)

    assert odpowiedz.status_code == 303
    assert odpowiedz.headers["location"].startswith("/admin/konta")
    with SessionLocal() as db:
        assert db.get(PortalUser, konto_id) is None


def test_konta_z_czasem_pracy_nie_da_sie_usunac(client, tenant_a, make_user):
    """Te minuty sa podstawa faktury - odmowa mowi, co zrobic zamiast usuwania."""
    konto_id = _technik("wladek@mojadomena.pl", [tenant_a["id"]])
    with SessionLocal() as db:
        helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_a["id"]), "BON")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.dodaj_czas(db, zgloszenie, db.get(PortalUser, konto_id), 30, "prace")
        db.commit()

    _superadmin(client, make_user)
    odpowiedz = client.post(f"/admin/users/{konto_id}/usun", data={
        "csrf_token": _csrf(client), "powrot": "/admin/konta",
    }, follow_redirects=False)

    assert odpowiedz.status_code == 400
    assert "Wyłącz konto" in odpowiedz.text
    with SessionLocal() as db:
        assert db.get(PortalUser, konto_id) is not None
        # Historia zostaje nietknieta.
        assert db.execute(select(Zgloszenie)).scalar_one() is not None

    # Za to wylaczenie dziala i zostawia historie.
    client.post(f"/admin/users/{konto_id}/active", data={
        "csrf_token": _csrf(client), "powrot": "/admin/konta",
    }, follow_redirects=False)
    with SessionLocal() as db:
        assert db.get(PortalUser, konto_id).is_active is False


def test_ekran_ostrzega_przed_usunieciem_konta_z_czasem(client, tenant_a, make_user):
    konto_id = _technik("wladek@mojadomena.pl", [tenant_a["id"]])
    with SessionLocal() as db:
        helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_a["id"]), "BON")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.dodaj_czas(db, zgloszenie, db.get(PortalUser, konto_id), 30)
        db.commit()

    _superadmin(client, make_user)
    strona = client.get("/admin/konta").text
    assert "1 wpisów czasu pracy" in strona
    assert "bez usuwania" in strona


def test_usuniecie_konta_nie_rusza_danych_firmy(client, tenant_a, make_user):
    with SessionLocal() as db:
        db.add(Asset(tenant_id=tenant_a["id"], machine_id="id-a1", hostname="A-01"))
        db.commit()
    konto_id = _technik("pomylka@mojadomena.pl", [tenant_a["id"]])
    _superadmin(client, make_user)

    client.post(f"/admin/users/{konto_id}/usun", data={
        "csrf_token": _csrf(client), "powrot": "/admin/konta",
    }, follow_redirects=False)

    with SessionLocal() as db:
        assert db.execute(select(Asset)).scalar_one().hostname == "A-01"


def test_odebranie_firmy_z_listy_kont(client, tenant_a, tenant_b, make_user):
    konto_id = _technik("technik@mojadomena.pl", [tenant_a["id"], tenant_b["id"]])
    _superadmin(client, make_user)

    client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_b["id"], "akcja": "odbierz",
    }, follow_redirects=False)

    with SessionLocal() as db:
        konto = db.get(PortalUser, konto_id)
        assert helpdesk.ma_dostep(db, konto, tenant_a["id"]) is True
        assert helpdesk.ma_dostep(db, konto, tenant_b["id"]) is False


# --- czytelnosc formularza --------------------------------------------------

def test_kazde_pole_ma_etykiete_i_opis_rodzaju(client, tenant_a, make_user):
    """Trzy listy jedna pod druga bez podpisow nie mowia, co robia - a rola
    przy audytorze czy firma przy superadminie nie znacza nic."""
    _superadmin(client, make_user)
    strona = client.get("/admin/konta").text

    assert "Rodzaj konta" in strona
    assert "Rola w tej firmie" in strona
    # Opis wybranego rodzaju jedzie z serwera, wiec widac go takze bez JavaScriptu.
    assert "Ogląda wszystkie firmy i nie zmienia" in strona
    assert 'data-pola="firma rola"' in strona


def test_audytor_nie_dostanie_firmy_helpdesku(client, tenant_a, make_user):
    """Audytor globalny nigdzie nie zapisuje, wiec firma nic by mu nie dala -
    nadanie wygladaloby jak nadanie uprawnien technika, a nie dawaloby ich."""
    _superadmin(client, make_user)
    client.post("/admin/konta", data={
        "csrf_token": _csrf(client), "email": "audytor@mojadomena.pl",
        "password": "haslo-audytora-2026", "zakres": "audytor",
    }, follow_redirects=False)
    with SessionLocal() as db:
        konto_id = db.execute(
            select(PortalUser.id).where(PortalUser.email == "audytor@mojadomena.pl")
        ).scalar_one()

    odpowiedz = client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_a["id"], "akcja": "nadaj",
    }, follow_redirects=False)

    assert odpowiedz.status_code == 400
    assert "technika" in odpowiedz.text
    with SessionLocal() as db:
        assert helpdesk.ma_dostep(db, db.get(PortalUser, konto_id), tenant_a["id"]) is False


def test_superadminowi_nie_nadaje_sie_firm(client, tenant_a, make_user):
    _superadmin(client, make_user)
    make_user(None, "drugi@mojadomena.pl", HASLO)
    with SessionLocal() as db:
        konto_id = db.execute(
            select(PortalUser.id).where(PortalUser.email == "drugi@mojadomena.pl")
        ).scalar_one()

    odpowiedz = client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_a["id"], "akcja": "nadaj",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    assert "wszystkie firmy" in odpowiedz.text


def test_konto_niespojne_jest_nazwane(client, tenant_a, make_user):
    """Stan odziedziczony po starszych danych: audytor z firmami helpdesku.
    Lepiej go nazwac, niz pozwolic komus liczyc, ze technik dziala."""
    with SessionLocal() as db:
        konto = PortalUser(
            tenant_id=None, email="dziwne@mojadomena.pl",
            password_hash=hash_password(HASLO), role="viewer", is_global_viewer=True,
        )
        db.add(konto)
        db.flush()
        # Wpis zakladamy z pominieciem reguly - tak wlasnie wygladaja stare dane.
        from cmdb_server.models import HelpdeskDostep

        db.add(HelpdeskDostep(user_id=konto.id, tenant_id=tenant_a["id"]))
        db.commit()

    _superadmin(client, make_user)
    strona = client.get("/admin/konta").text
    assert "firmy helpdesku i uprawnienie audytora" in strona


def test_konto_firmy_nie_dostanie_firmy_helpdesku(client, tenant_a, tenant_b, make_user):
    """Konto z wlasna firma i firmami helpdesku mialoby dwa zrodla uprawnien
    i nie dalo by sie powiedziec, czym wlasciwie jest."""
    make_user(tenant_a["id"], "admin@firma-a.pl", HASLO, role="admin")
    _superadmin(client, make_user)
    with SessionLocal() as db:
        konto_id = db.execute(
            select(PortalUser.id).where(PortalUser.email == "admin@firma-a.pl")
        ).scalar_one()

    odpowiedz = client.post(f"/admin/users/{konto_id}/helpdesk", data={
        "csrf_token": _csrf(client), "tenant_id": tenant_b["id"], "akcja": "nadaj",
    }, follow_redirects=False)

    assert odpowiedz.status_code == 400
    assert "technika helpdesku" in odpowiedz.text
    # Ekran mowi to samo, zanim ktos kliknie.
    assert "zmień jego rodzaj na technika" in client.get("/admin/konta").text


def test_pola_niepasujace_do_rodzaju_nie_zaslaniaja_formularza(client, tenant_a, make_user):
    """Superadmin nie ma firmy, audytor nie ma roli - te pola sa w formularzu,
    ale opisane danymi, po ktorych przegladarka je chowa i wylacza."""
    _superadmin(client, make_user)
    strona = client.get("/admin/konta").text

    assert 'data-konto-pole="firma"' in strona
    assert 'data-konto-pole="rola"' in strona
    # Tylko konto firmy potrzebuje obu pol - reszta rodzajow zadnego.
    assert 'data-pola="firma rola"' in strona
    assert 'data-pola=""' in strona
