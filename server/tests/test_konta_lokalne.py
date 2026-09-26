"""Konta lokalne: zaproszenia, reset hasla, weryfikacja dwuetapowa, domeny AD."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    JednorazowyLink,
    KatalogTozsamosci,
    PortalUser,
    Tenant,
)
from cmdb_server.services import konta_lokalne, sekrety

from .test_tenant_isolation import _extract_csrf, _login


def _konto(email):
    with SessionLocal() as db:
        return db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()


@pytest.fixture
def superadmin(client, make_user):
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    _login(client, "root@operator.pl", "haslo-superadmina-1")
    return client


def _csrf(client, adres="/admin/konta"):
    return _extract_csrf(client.get(adres).text)


def _link_z_odpowiedzi(html):
    return re.search(r'<code class="token-value">([^<]+)</code>', html).group(1)


# --- TOTP -------------------------------------------------------------------

def test_totp_zgodny_z_rfc6238():
    # Wektor testowy z RFC 6238 (SHA1): sekret "12345678901234567890", T=59 -> 94287082.
    import base64
    sekret = base64.b32encode(b"12345678901234567890").decode()
    assert konta_lokalne._kod(sekret, 59 // 30) == "287082"


def test_totp_nie_przyjmie_tego_samego_kodu_dwa_razy():
    sekret = konta_lokalne.nowy_sekret()
    kod = konta_lokalne.kod_teraz(sekret, 1_000_000)
    krok = konta_lokalne.sprawdz_kod(sekret, kod, None, czas=1_000_000)
    assert krok is not None
    assert konta_lokalne.sprawdz_kod(sekret, kod, krok, czas=1_000_000) is None


# --- zaproszenia ------------------------------------------------------------

def test_zaproszenie_zaklada_konto_z_haslem_ustawionym_przez_osobe(superadmin, client, tenant_a):
    odp = superadmin.post("/admin/zaproszenia", data={
        "csrf_token": _csrf(superadmin), "email": "Serwis@DrukPol.pl", "full_name": "Serwis",
        "zakres": "firma", "tenant_id": tenant_a["id"], "role": "viewer"})
    assert odp.status_code == 200
    link = _link_z_odpowiedzi(odp.text)
    assert "/zaproszenie/" in link
    assert "Oczekujące zaproszenia" in odp.text
    sciezka = link.split("testserver")[-1]
    client.cookies.clear()
    assert "Zaproszenie dla serwis@drukpol.pl" in client.get(sciezka).text
    odp = client.post(sciezka, data={"nowe": "krotkie", "powtorzone": "krotkie"})
    assert odp.status_code == 400
    odp = client.post(sciezka, data={"nowe": "haslo-serwisu-123", "powtorzone": "haslo-serwisu-123"},
                      follow_redirects=False)
    assert odp.status_code == 303 and odp.headers["location"] == "/"
    konto = _konto("serwis@drukpol.pl")
    assert konto.tenant_id == tenant_a["id"] and konto.role == "viewer"
    assert konto.zrodlo == "lokalne"
    # Link dziala raz.
    client.cookies.clear()
    assert client.get(sciezka).status_code == 404
    _login(client, "serwis@drukpol.pl", "haslo-serwisu-123")


def test_zaproszenie_na_adres_w_domenie_ad_odrzucone(superadmin, tenant_a):
    with SessionLocal() as db:
        db.add(KatalogTozsamosci(tenant_id=tenant_a["id"], nazwa="AD ABC",
                                 serwery="ldaps://dc", base_dn="DC=abc", bind_dn="svc",
                                 domeny=["abc.pl"]))
        db.commit()
    odp = superadmin.post("/admin/zaproszenia", data={
        "csrf_token": _csrf(superadmin), "email": "jan@abc.pl", "zakres": "firma",
        "tenant_id": tenant_a["id"], "role": "viewer"})
    assert odp.status_code == 400
    assert "domenie katalogu AD" in odp.text
    odp = superadmin.post("/admin/konta", data={
        "csrf_token": _csrf(superadmin), "email": "jan@abc.pl", "password": "haslo-lokalne-123",
        "zakres": "firma", "tenant_id": tenant_a["id"], "role": "viewer"})
    assert odp.status_code == 400


def test_odwolane_zaproszenie_nie_dziala(superadmin, client):
    odp = superadmin.post("/admin/zaproszenia", data={
        "csrf_token": _csrf(superadmin), "email": "tech@operator.pl", "zakres": "technik"})
    sciezka = _link_z_odpowiedzi(odp.text).split("testserver")[-1]
    with SessionLocal() as db:
        link_id = db.execute(select(JednorazowyLink.id)).scalar_one()
    superadmin.post(f"/admin/zaproszenia/{link_id}/odwolaj", data={"csrf_token": _csrf(superadmin)})
    client.cookies.clear()
    assert client.get(sciezka).status_code == 404


# --- reset hasla ------------------------------------------------------------

def test_zapomniane_haslo_ta_sama_odpowiedz_dla_kazdego_adresu(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "stare-haslo-12345")
    a = client.post("/haslo/zapomniane", data={"email": "ktos@firma-a.pl"})
    b = client.post("/haslo/zapomniane", data={"email": "nikt@firma-a.pl"})
    assert a.status_code == b.status_code == 200
    assert "Jeśli pod tym adresem jest konto" in a.text and "Jeśli pod tym adresem jest konto" in b.text
    with SessionLocal() as db:
        linki = db.execute(select(JednorazowyLink)).scalars().all()
    assert [l.email for l in linki] == ["ktos@firma-a.pl"]


def test_link_do_hasla_od_admina_ustawia_haslo_i_wylogowuje(superadmin, client, tenant_a,
                                                            make_user):
    uid = make_user(tenant_a["id"], "ktos@firma-a.pl", "stare-haslo-12345")
    odp = superadmin.post(f"/admin/users/{uid}/link-hasla", data={"csrf_token": _csrf(superadmin)})
    sciezka = _link_z_odpowiedzi(odp.text).split("testserver")[-1]
    assert "/haslo/nowe/" in sciezka
    client.cookies.clear()
    odp = client.post(sciezka, data={"nowe": "nowe-haslo-12345", "powtorzone": "nowe-haslo-12345"},
                      follow_redirects=False)
    assert odp.status_code == 303
    assert "zmienione" in odp.headers["location"]
    assert client.post("/login", data={"email": "ktos@firma-a.pl", "password": "stare-haslo-12345"},
                       follow_redirects=False).status_code == 401
    _login(client, "ktos@firma-a.pl", "nowe-haslo-12345")


def test_najwyzej_trzy_linki_na_godzine(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "stare-haslo-12345")
    for _ in range(5):
        client.post("/haslo/zapomniane", data={"email": "ktos@firma-a.pl"})
    with SessionLocal() as db:
        assert len(db.execute(select(JednorazowyLink)).scalars().all()) == 3


# --- weryfikacja dwuetapowa -------------------------------------------------

def _wlacz_totp(email):
    sekret = konta_lokalne.nowy_sekret()
    with SessionLocal() as db:
        konto = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one()
        konto.totp_szyfr = sekrety.zaszyfruj(sekret)
        konto.totp_wlaczone = True
        db.commit()
    return sekret


def test_logowanie_z_kodem(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    sekret = _wlacz_totp("ktos@firma-a.pl")
    odp = client.post("/login", data={"email": "ktos@firma-a.pl", "password": "haslo-lokalne-123"},
                      follow_redirects=False)
    assert odp.headers["location"] == "/login/kod"
    # Bez kodu nie ma sesji.
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/login/kod", data={"kod": "000000"}).status_code == 401
    odp = client.post("/login/kod", data={"kod": konta_lokalne.kod_teraz(sekret)},
                      follow_redirects=False)
    assert odp.status_code == 303 and odp.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_firma_wymusza_2fa_konfiguracja_przy_logowaniu(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    with SessionLocal() as db:
        db.get(Tenant, tenant_a["id"]).wymagaj_mfa = True
        db.commit()
    _login(client, "ktos@firma-a.pl", "haslo-lokalne-123")
    strona = client.get("/login/kod")
    assert "Ustaw weryfikację dwuetapową" in strona.text
    with SessionLocal() as db:
        konto = db.execute(select(PortalUser).where(PortalUser.email == "ktos@firma-a.pl")).scalar_one()
        sekret = sekrety.odszyfruj(konto.totp_szyfr)
    odp = client.post("/login/kod", data={"kod": konta_lokalne.kod_teraz(sekret)},
                      follow_redirects=False)
    assert odp.headers["location"] == "/"
    assert _konto("ktos@firma-a.pl").totp_wlaczone


def test_wlaczenie_2fa_na_stronie_konta(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    _login(client, "ktos@firma-a.pl", "haslo-lokalne-123")
    csrf = _extract_csrf(client.get("/konto").text)
    client.post("/konto/mfa/nowy", data={"csrf_token": csrf})
    strona = client.get("/konto?mfa_konfiguracja=1")
    assert "Zeskanuj kod" in strona.text
    sekret = sekrety.odszyfruj(_konto("ktos@firma-a.pl").totp_szyfr)
    client.post("/konto/mfa/potwierdz", data={"csrf_token": csrf,
                                              "kod": konta_lokalne.kod_teraz(sekret)})
    assert _konto("ktos@firma-a.pl").totp_wlaczone


def test_mobilne_logowanie_z_kodem(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    sekret = _wlacz_totp("ktos@firma-a.pl")
    dane = {"email": "ktos@firma-a.pl", "password": "haslo-lokalne-123"}
    odp = client.post("/api/v1/mobile/auth/login", json=dane)
    assert odp.status_code == 401 and odp.headers["x-cmdb-mfa"] == "wymagany"
    odp = client.post("/api/v1/mobile/auth/login",
                      json={**dane, "code": konta_lokalne.kod_teraz(sekret)})
    assert odp.status_code == 200, odp.text


def test_admin_zdejmuje_2fa(superadmin, tenant_a, make_user):
    uid = make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    _wlacz_totp("ktos@firma-a.pl")
    superadmin.post(f"/admin/users/{uid}/mfa-reset", data={"csrf_token": _csrf(superadmin)})
    assert not _konto("ktos@firma-a.pl").totp_wlaczone


def test_ustawienia_firmy_bez_pol_logowania_nie_zmieniaja_ich(superadmin, tenant_a):
    odp = superadmin.post(f"/admin/tenants/{tenant_a['id']}/ustawienia", data={
        "csrf_token": _csrf(superadmin, f"/admin/tenants/{tenant_a['id']}/ustawienia"),
        "name": "Firma A", "slug": "firma-a"}, follow_redirects=False)
    assert odp.status_code == 303
    with SessionLocal() as db:
        assert db.get(Tenant, tenant_a["id"]).logowanie_lokalne is True
    superadmin.post(f"/admin/tenants/{tenant_a['id']}/ustawienia", data={
        "csrf_token": _csrf(superadmin, f"/admin/tenants/{tenant_a['id']}/ustawienia"),
        "name": "Firma A", "slug": "firma-a", "pola_logowania": "1", "wymagaj_mfa": "true",
        "logowanie_lokalne": "true"})
    with SessionLocal() as db:
        firma = db.get(Tenant, tenant_a["id"])
        assert firma.wymagaj_mfa and firma.logowanie_lokalne
