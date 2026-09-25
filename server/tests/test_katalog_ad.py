"""Logowanie kontem AD i uprawnienia z grup.

Prawdziwego AD w testach nie ma - klienta katalogu zastepuje atrapa. Testujemy
logike CMDB: rozpoznanie katalogu po domenie, role z grup, granice firmy,
zakladanie kont i ich synchronizacje.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    AuditLog,
    HelpdeskDostep,
    KatalogTozsamosci,
    MapowanieGrupy,
    PortalUser,
    Tenant,
)
from cmdb_server.services import katalog as ldap
from cmdb_server.services import sekrety, tozsamosc
from cmdb_server.services.katalog import BladKatalogu, Grupa, WpisKatalogu

from .test_tenant_isolation import _extract_csrf, _login

SID_ADMINI = "S-1-5-21-1000-2000-3000-1101"
SID_IT = "S-1-5-21-1000-2000-3000-1102"
SID_SUPER = "S-1-5-21-9000-9000-9000-500"
SID_HD = "S-1-5-21-9000-9000-9000-600"


class AtrapaKatalogu:
    """Katalog w pamieci: login -> (haslo, wpis)."""

    osoby: dict[str, tuple[str, WpisKatalogu]] = {}
    awaria = False
    wywolania: list[str] = []

    def __init__(self, kat):
        self.kat = kat

    def _sprawdz_awarie(self):
        if AtrapaKatalogu.awaria:
            raise BladKatalogu("serwer nie odpowiada")

    def _szukaj(self, login):
        _, nazwa = ldap.rozbierz_login(login)
        for klucz, (haslo, wpis) in self.osoby.items():
            if klucz == nazwa.lower() or (wpis.email and wpis.email == login.lower()):
                return haslo, wpis
        return None

    def sprawdz_haslo(self, login, haslo):
        AtrapaKatalogu.wywolania.append(f"haslo:{login}")
        self._sprawdz_awarie()
        trafienie = self._szukaj(login)
        if trafienie is None or not haslo or trafienie[0] != haslo:
            return None
        return trafienie[1]

    def znajdz(self, login):
        self._sprawdz_awarie()
        trafienie = self._szukaj(login)
        return trafienie[1] if trafienie else None

    def znajdz_po_guid(self, guid):
        self._sprawdz_awarie()
        for _, wpis in self.osoby.values():
            if wpis.guid == guid:
                return wpis
        return None

    def szukaj_grup(self, fraza):
        self._sprawdz_awarie()
        return [Grupa(SID_ADMINI, "CMDB-Administratorzy", "CN=CMDB-Administratorzy,DC=abc,DC=local")]

    def sprawdz_polaczenie(self):
        self._sprawdz_awarie()
        return "Połączono z atrapą."


def osoba(login, guid, grupy, haslo="Haslo-AD-1", aktywne=True, email=None):
    wpis = WpisKatalogu(guid=guid, dn=f"CN={login},DC=abc,DC=local", login=login,
                        email=email or f"{login}@abc.pl", nazwa=login.title(), aktywne=aktywne,
                        grupy=[Grupa(s, s) for s in grupy])
    AtrapaKatalogu.osoby[login] = (haslo, wpis)
    return wpis


@pytest.fixture(autouse=True)
def atrapa(monkeypatch):
    AtrapaKatalogu.osoby = {}
    AtrapaKatalogu.awaria = False
    AtrapaKatalogu.wywolania = []
    monkeypatch.setattr(ldap, "fabryka_klienta", AtrapaKatalogu)
    yield AtrapaKatalogu


def _katalog(tenant_id, domeny, mapowania=(), bez_grupy="odmowa"):
    with SessionLocal() as db:
        kat = KatalogTozsamosci(
            tenant_id=tenant_id, nazwa="AD", serwery="ldaps://dc1.abc.local",
            base_dn="DC=abc,DC=local", bind_dn="svc", bind_haslo_szyfr=sekrety.zaszyfruj("x"),
            domeny=list(domeny), bez_grupy=bez_grupy,
        )
        db.add(kat)
        db.flush()
        for identyfikator, rola, *reszta in mapowania:
            db.add(MapowanieGrupy(katalog_id=kat.id, identyfikator=identyfikator,
                                  nazwa=identyfikator[-4:], rola=rola,
                                  tenant_id=reszta[0] if reszta else None))
        db.commit()
        return kat.id


@pytest.fixture
def katalog_abc(tenant_a):
    return _katalog(tenant_a["id"], ["abc.pl", "abc\\"],
                    [(SID_ADMINI, "admin"), (SID_IT, "viewer")])


def _konto(email):
    with SessionLocal() as db:
        return db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()


# --- pomocnicze -------------------------------------------------------------

def test_rozbior_loginu():
    assert ldap.rozbierz_login("Jan.K@ABC.pl") == ("abc.pl", "Jan.K")
    assert ldap.rozbierz_login("ABC\\jkowalski") == ("abc\\", "jkowalski")
    assert ldap.rozbierz_login("jkowalski") == ("", "jkowalski")


def test_sid_w_obie_strony():
    sid = "S-1-5-21-3623811015-3361044348-30300820-1013"
    filtr = ldap.sid_do_filtra(sid)
    dane = bytes(int(x, 16) for x in filtr.split("\\")[1:])
    assert ldap.sid_z_bajtow(dane) == sid


def test_polaczenie_bez_szyfrowania_odrzucone():
    with pytest.raises(ValueError):
        ldap.sprawdz_adresy("ldap://dc1.abc.local", starttls=False)
    assert ldap.sprawdz_adresy("ldap://dc1.abc.local", starttls=True)
    assert ldap.sprawdz_adresy("ldaps://dc1:636, ldaps://dc2:636", starttls=False) == [
        "ldaps://dc1:636", "ldaps://dc2:636"]


def test_login_escapowany_w_filtrze():
    kat = KatalogTozsamosci(serwery="ldaps://x", base_dn="DC=x", bind_dn="x", domeny=["abc.pl"])
    filtr = ldap.KlientLdap3(kat)._filtr("*)(uid=*))(|(uid=*@abc.pl")
    assert "*)(uid=*" not in filtr
    assert "\\2a\\29\\28uid=\\2a" in filtr


# --- logowanie --------------------------------------------------------------

def test_pierwsze_logowanie_zaklada_konto_z_rola_z_grupy(client, katalog_abc, tenant_a):
    osoba("jan", "g-jan", [SID_ADMINI, SID_IT])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    konto = _konto("jan@abc.pl")
    assert konto.zrodlo == "ad" and konto.zewnetrzny_id == "g-jan"
    assert konto.role == "admin" and konto.tenant_id == tenant_a["id"]
    assert not konto.is_superadmin
    # Konto z AD nie ma hasla CMDB - lokalnie nie zaloguje sie niczym.
    assert konto.password_hash == "!"
    assert client.get("/").status_code == 200


def test_login_netbios(client, katalog_abc):
    osoba("anna", "g-anna", [SID_IT])
    _login(client, "ABC\\anna", "Haslo-AD-1")
    assert _konto("anna@abc.pl").role == "viewer"


def test_osoba_bez_grupy_nie_wchodzi(client, katalog_abc):
    osoba("obcy", "g-obcy", ["S-1-5-21-1-2-3-513"])
    odp = client.post("/login", data={"email": "obcy@abc.pl", "password": "Haslo-AD-1"},
                      follow_redirects=False)
    assert odp.status_code == 403
    assert "nie ma dostępu do CMDB" in odp.text
    assert _konto("obcy@abc.pl") is None


def test_bez_grupy_z_prawem_odczytu(client, tenant_a):
    _katalog(tenant_a["id"], ["abc.pl"], bez_grupy="viewer")
    osoba("ktos", "g-ktos", [])
    _login(client, "ktos@abc.pl", "Haslo-AD-1")
    assert _konto("ktos@abc.pl").role == "viewer"


def test_zle_haslo_komunikat_ogolny(client, katalog_abc):
    osoba("jan", "g-jan", [SID_ADMINI])
    odp = client.post("/login", data={"email": "jan@abc.pl", "password": "zle"},
                      follow_redirects=False)
    assert odp.status_code == 401
    assert "Nieprawidłowy login lub hasło." in odp.text


def test_puste_haslo_nie_trafia_do_ad():
    with SessionLocal() as db:
        wynik = tozsamosc.uwierzytelnij(db, "jan@abc.pl", "")
    assert wynik.user is None


def test_puste_haslo_nie_pyta_katalogu(client, katalog_abc):
    osoba("jan", "g-jan", [SID_ADMINI])
    with SessionLocal() as db:
        wynik = tozsamosc.uwierzytelnij(db, "jan@abc.pl", "")
    assert wynik.user is None and wynik.powod == "haslo"
    assert AtrapaKatalogu.wywolania == []


def test_awaria_katalogu_to_nie_zle_haslo(client, katalog_abc, atrapa):
    osoba("jan", "g-jan", [SID_ADMINI])
    atrapa.awaria = True
    odp = client.post("/login", data={"email": "jan@abc.pl", "password": "Haslo-AD-1"},
                      follow_redirects=False)
    assert odp.status_code == 503
    assert "serwerem domeny" in odp.text


def test_podpowiedz_sposobu_logowania(client, katalog_abc):
    assert client.get("/login/sposob", params={"login": "x@abc.pl"}).json() == {
        "sposob": "ad", "firma": "Firma A"}
    assert client.get("/login/sposob", params={"login": "ABC\\x"}).json()["sposob"] == "ad"
    assert client.get("/login/sposob", params={"login": "x@gmail.com"}).json() == {
        "sposob": "lokalne"}


def test_konto_lokalne_w_domenie_katalogu_przechodzi_na_ad(client, katalog_abc, tenant_a,
                                                           make_user):
    make_user(tenant_a["id"], "jan@abc.pl", "stare-haslo-lokalne-123", role="viewer")
    osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    konto = _konto("jan@abc.pl")
    assert konto.zrodlo == "ad" and konto.role == "admin"
    with SessionLocal() as db:
        assert db.execute(select(PortalUser)).scalars().all().__len__() == 1


def test_logowanie_lokalne_wylaczone_w_firmie(client, katalog_abc, tenant_a, make_user):
    make_user(tenant_a["id"], "serwis@drukpol.pl", "haslo-serwisu-12345", role="viewer")
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    with SessionLocal() as db:
        db.get(Tenant, tenant_a["id"]).logowanie_lokalne = False
        db.commit()
    odp = client.post("/login", data={"email": "serwis@drukpol.pl",
                                      "password": "haslo-serwisu-12345"}, follow_redirects=False)
    assert odp.status_code == 403
    assert "wyłącznie kontem domenowym" in odp.text
    # Superadmin nie nalezy do firmy - jego to nie dotyczy.
    _login(client, "root@operator.pl", "haslo-superadmina-1")


def test_logowanie_mobilne_przez_ad(client, katalog_abc):
    osoba("jan", "g-jan", [SID_IT])
    odp = client.post("/api/v1/mobile/auth/login",
                      json={"email": "jan@abc.pl", "password": "Haslo-AD-1"})
    assert odp.status_code == 200, odp.text
    assert odp.json()["user"]["role"] == "viewer"
    odp = client.post("/api/v1/mobile/auth/login",
                      json={"email": "jan@abc.pl", "password": "zle"})
    assert odp.status_code == 401


# --- granica firmy ----------------------------------------------------------

def test_katalog_firmy_nie_daje_superadmina_nawet_z_bazy(client, tenant_a):
    # Mapowanie, ktorego panel by nie przepuscil, wstawione wprost do bazy.
    _katalog(tenant_a["id"], ["abc.pl"], [(SID_SUPER, "superadmin"), (SID_IT, "viewer")])
    osoba("cwaniak", "g-cw", [SID_SUPER, SID_IT])
    _login(client, "cwaniak@abc.pl", "Haslo-AD-1")
    konto = _konto("cwaniak@abc.pl")
    assert not konto.is_superadmin and not konto.is_global_viewer
    assert konto.role == "viewer"


def test_katalog_operatora_daje_superadmina_i_technika(client, tenant_a, tenant_b):
    _katalog(None, ["operator.pl"], [(SID_SUPER, "superadmin"),
                                     (SID_HD, "helpdesk", tenant_a["id"]),
                                     (SID_HD, "helpdesk", tenant_b["id"])])
    osoba("szef", "g-szef", [SID_SUPER], email="szef@operator.pl")
    osoba("tech", "g-tech", [SID_HD], email="tech@operator.pl")
    _login(client, "szef@operator.pl", "Haslo-AD-1")
    assert _konto("szef@operator.pl").is_superadmin
    client.cookies.clear()
    _login(client, "tech@operator.pl", "Haslo-AD-1")
    tech = _konto("tech@operator.pl")
    assert tech.tenant_id is None and not tech.is_superadmin
    with SessionLocal() as db:
        firmy = set(db.execute(select(HelpdeskDostep.tenant_id)
                               .where(HelpdeskDostep.user_id == tech.id)).scalars())
    assert firmy == {tenant_a["id"], tenant_b["id"]}


# --- synchronizacja ---------------------------------------------------------

def _sync(kat_id):
    with SessionLocal() as db:
        return tozsamosc.synchronizuj(db, db.get(KatalogTozsamosci, kat_id))


def test_wylaczenie_w_ad_odbiera_dostep_i_sesje(client, katalog_abc):
    wpis = osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    assert client.get("/", follow_redirects=False).status_code == 200
    wpis.aktywne = False
    wynik = _sync(katalog_abc)
    assert wynik["odebrane"] == 1
    assert not _konto("jan@abc.pl").is_active
    assert client.get("/", follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.execute(select(AuditLog).where(
            AuditLog.action == "katalog.dostep_odebrany")).scalar_one_or_none()


def test_usuniecie_z_grupy_zmienia_role_i_uniewaznia_sesje(client, katalog_abc):
    wpis = osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    wpis.grupy = [Grupa(SID_IT, SID_IT)]
    assert _sync(katalog_abc)["zmienione"] == 1
    assert _konto("jan@abc.pl").role == "viewer"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_awaria_katalogu_nie_rusza_kont(client, katalog_abc, atrapa):
    osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    atrapa.awaria = True
    wynik = _sync(katalog_abc)
    assert wynik["blad"]
    assert _konto("jan@abc.pl").is_active
    assert client.get("/", follow_redirects=False).status_code == 200
    with SessionLocal() as db:
        assert db.get(KatalogTozsamosci, katalog_abc).ostatni_blad


def test_osoba_usunieta_z_ad_traci_dostep(client, katalog_abc):
    osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    del AtrapaKatalogu.osoby["jan"]
    _sync(katalog_abc)
    assert not _konto("jan@abc.pl").is_active


def test_reczne_wylaczenie_nie_jest_cofane_przez_synchronizacje(client, katalog_abc, make_user):
    osoba("jan", "g-jan", [SID_ADMINI])
    _login(client, "jan@abc.pl", "Haslo-AD-1")
    client.cookies.clear()
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    _login(client, "root@operator.pl", "haslo-superadmina-1")
    konto = _konto("jan@abc.pl")
    csrf = _extract_csrf(client.get("/admin/konta").text)
    client.post(f"/admin/users/{konto.id}/active", data={"csrf_token": csrf},
                follow_redirects=False)
    _sync(katalog_abc)
    assert not _konto("jan@abc.pl").is_active
    client.cookies.clear()
    odp = client.post("/login", data={"email": "jan@abc.pl", "password": "Haslo-AD-1"},
                      follow_redirects=False)
    assert odp.status_code == 403


def test_synchronizacja_nie_zabiera_recznych_dostepow_helpdesku(client, tenant_a, tenant_b):
    kat = _katalog(None, ["operator.pl"], [(SID_HD, "helpdesk", tenant_a["id"])])
    wpis = osoba("tech", "g-tech", [SID_HD], email="tech@operator.pl")
    _login(client, "tech@operator.pl", "Haslo-AD-1")
    tech = _konto("tech@operator.pl")
    with SessionLocal() as db:
        db.add(HelpdeskDostep(user_id=tech.id, tenant_id=tenant_b["id"], nadal="root"))
        db.commit()
    wpis.grupy = [Grupa(SID_HD, SID_HD), Grupa("S-1-5-21-0-0-0-1", "x")]
    _sync(kat)
    with SessionLocal() as db:
        firmy = set(db.execute(select(HelpdeskDostep.tenant_id)
                               .where(HelpdeskDostep.user_id == tech.id)).scalars())
    assert firmy == {tenant_a["id"], tenant_b["id"]}


# --- panel superadmina ------------------------------------------------------

@pytest.fixture
def superadmin(client, make_user):
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    _login(client, "root@operator.pl", "haslo-superadmina-1")
    return client


def _csrf(client, adres="/admin/katalogi"):
    return _extract_csrf(client.get(adres).text)


def test_panel_dodaje_katalog_i_mapowanie(superadmin, tenant_a):
    c = superadmin
    odp = c.post("/admin/katalogi", data={
        "csrf_token": _csrf(c), "tenant_id": tenant_a["id"], "serwery": "ldaps://dc1.abc.local:636",
        "base_dn": "DC=abc,DC=local", "bind_dn": "svc@abc.local", "bind_haslo": "tajne",
        "domeny": "abc.pl, ABC\\", "bez_grupy": "odmowa",
    }, follow_redirects=False)
    assert odp.status_code == 303
    with SessionLocal() as db:
        kat = db.execute(select(KatalogTozsamosci)).scalar_one()
        assert kat.domeny == ["abc.pl", "abc\\"]
        assert kat.bind_haslo_szyfr and "tajne" not in kat.bind_haslo_szyfr
        kat_id = kat.id
    strona = c.get(f"/admin/katalogi/{kat_id}", params={"szukaj": "CMDB"})
    assert "CMDB-Administratorzy" in strona.text
    odp = c.post(f"/admin/katalogi/{kat_id}/mapowania", data={
        "csrf_token": _csrf(c), "identyfikator": SID_ADMINI, "nazwa": "CMDB-Administratorzy",
        "rola": "admin"}, follow_redirects=False)
    assert odp.status_code == 303
    # Granica firmy: katalog firmy nie przyjmie roli superadmina.
    odp = c.post(f"/admin/katalogi/{kat_id}/mapowania", data={
        "csrf_token": _csrf(c), "identyfikator": SID_SUPER, "rola": "superadmin"},
        follow_redirects=False)
    assert "nie+jest+dost" in odp.headers["location"] or "blad=" in odp.headers["location"]
    with SessionLocal() as db:
        assert [m.rola for m in db.execute(select(MapowanieGrupy)).scalars()] == ["admin"]

    osoba("jan", "g-jan", [SID_ADMINI])
    strona = c.get(f"/admin/katalogi/{kat_id}", params={"sprawdz": "jan"})
    assert "Wynik: Administrator firmy" in strona.text


def test_panel_odrzuca_ldap_bez_szyfrowania_i_zajeta_domene(superadmin, tenant_a, tenant_b):
    c = superadmin
    dane = {"csrf_token": _csrf(c), "tenant_id": tenant_a["id"], "serwery": "ldap://dc1:389",
            "base_dn": "DC=abc", "bind_dn": "svc", "bind_haslo": "x", "domeny": "abc.pl"}
    odp = c.post("/admin/katalogi", data=dane, follow_redirects=False)
    assert "blad=" in odp.headers["location"]
    dane["serwery"] = "ldaps://dc1"
    assert "blad=" not in c.post("/admin/katalogi", data=dane,
                                  follow_redirects=False).headers["location"]
    dane.update(tenant_id=tenant_b["id"], csrf_token=_csrf(c))
    odp = c.post("/admin/katalogi", data=dane, follow_redirects=False)
    assert "blad=" in odp.headers["location"]


def test_konto_z_ad_nie_ma_edycji_roli_ani_hasla(superadmin, katalog_abc):
    osoba("jan", "g-jan", [SID_ADMINI])
    with SessionLocal() as db:
        kat = db.get(KatalogTozsamosci, katalog_abc)
        tozsamosc.zastosuj(db, kat, AtrapaKatalogu.osoby["jan"][1],
                           tozsamosc.ustal_uprawnienia(kat, AtrapaKatalogu.osoby["jan"][1]))
        db.commit()
    konto = _konto("jan@abc.pl")
    c = superadmin
    strona = c.get("/admin/konta")
    assert "Uprawnienia wynikają z grup w AD" in strona.text
    odp = c.post(f"/admin/users/{konto.id}/zakres", data={
        "csrf_token": _csrf(c, "/admin/konta"), "zakres": "superadmin", "role": "admin"})
    assert odp.status_code == 400
    odp = c.post(f"/admin/users/{konto.id}/haslo", data={
        "csrf_token": _csrf(c, "/admin/konta"), "password": "nowe-haslo-123456"})
    assert odp.status_code == 400
    assert not _konto("jan@abc.pl").is_superadmin


def test_synchronizacja_z_panelu(superadmin, katalog_abc):
    c = superadmin
    odp = c.post(f"/admin/katalogi/{katalog_abc}/synchronizuj",
                 data={"csrf_token": _csrf(c)}, follow_redirects=False)
    assert "komunikat=" in odp.headers["location"]
    odp = c.post(f"/admin/katalogi/{katalog_abc}/sprawdz",
                 data={"csrf_token": _csrf(c)}, follow_redirects=False)
    assert "komunikat=" in odp.headers["location"]
