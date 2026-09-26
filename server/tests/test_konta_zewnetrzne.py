"""Logowanie kontem Google/Microsoft/GitHub - wylacznie z zaproszenia.

Dostawcow zastepuje atrapa ``zapytanie_http``: zwraca tokeny i dane osoby,
tak jak zrobilby to prawdziwy endpoint.
"""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import PortalUser, Tenant, TozsamoscZewnetrzna
from cmdb_server.services import zewnetrzne

from .test_tenant_isolation import _extract_csrf, _login


def _jwt(dane: dict) -> str:
    srodek = base64.urlsafe_b64encode(json.dumps(dane).encode()).rstrip(b"=").decode()
    return f"x.{srodek}.y"


class Dostawca:
    """Atrapa: pamieta, kim ma byc osoba przy nastepnym logowaniu."""

    osoba = {"sub": "g-1", "email": "jan@gmail.com"}
    nonce = None
    wywolania: list = []

    @classmethod
    def zapytanie(cls, metoda, adres, dane=None, naglowki=None):
        cls.wywolania.append((metoda, adres, dane))
        if "googleapis" in adres:
            return {"id_token": _jwt({
                "iss": "https://accounts.google.com", "aud": "google-id", "exp": time.time() + 300,
                "nonce": cls.nonce, "email_verified": True, **cls.osoba})}
        if "github.com/login" in adres:
            return {"access_token": "gho_x"}
        if adres.endswith("/user"):
            return {"id": 4242, "login": "jan"}
        if adres.endswith("/user/emails"):
            return [{"email": "jan@users.noreply.github.com", "primary": False, "verified": True},
                    {"email": "Jan@Example.com", "primary": True, "verified": True}]
        raise AssertionError(adres)


@pytest.fixture(autouse=True)
def dostawcy(monkeypatch):
    ustawienia = get_settings()
    monkeypatch.setattr(ustawienia, "google_client_id", "google-id")
    monkeypatch.setattr(ustawienia, "google_client_secret", SecretStr("google-secret"))
    monkeypatch.setattr(ustawienia, "github_client_id", "github-id")
    monkeypatch.setattr(ustawienia, "github_client_secret", SecretStr("github-secret"))
    monkeypatch.setattr(zewnetrzne, "zapytanie_http", Dostawca.zapytanie)
    Dostawca.osoba = {"sub": "g-1", "email": "jan@gmail.com"}
    Dostawca.wywolania = []
    yield


def _przejdz(client, adres_startu, podmien_state=None):
    """Symuluje przegladarke: start -> dostawca -> powrot z kodem."""
    odp = client.get(adres_startu, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    parametry = parse_qs(urlsplit(odp.headers["location"]).query)
    Dostawca.nonce = parametry.get("nonce", [None])[0]
    assert parametry["code_challenge_method"] == ["S256"]
    state = podmien_state or parametry["state"][0]
    return client.get("/login/zewn/callback", params={"code": "kod-1", "state": state},
                      follow_redirects=False)


def _zaproszenie(client, make_user, tenant_id, email="jan@firma-a.pl"):
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    _login(client, "root@operator.pl", "haslo-superadmina-1")
    odp = client.post("/admin/zaproszenia", data={
        "csrf_token": _extract_csrf(client.get("/admin/konta").text), "email": email,
        "zakres": "firma", "tenant_id": tenant_id, "role": "viewer"})
    link = re.search(r'<code class="token-value">([^<]+)</code>', odp.text).group(1)
    client.cookies.clear()
    return link.rsplit("/", 1)[1]


def test_przyciski_tylko_wlaczonych_dostawcow(client):
    strona = client.get("/login").text
    assert "Zaloguj przez Google" in strona and "Zaloguj przez GitHub" in strona
    assert "Zaloguj przez Microsoft" not in strona
    assert client.get("/login/zewn/microsoft", follow_redirects=False).status_code == 404


def test_obce_konto_google_nie_ma_dostepu(client):
    odp = _przejdz(client, "/login/zewn/google")
    assert odp.status_code == 403
    assert "nie ma dostępu do CMDB" in odp.text
    assert "jan@gmail.com" in odp.text
    with SessionLocal() as db:
        assert db.execute(select(PortalUser)).first() is None


def test_zaproszenie_przyjete_kontem_google_i_ponowne_logowanie(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    assert "Użyj konta Google" in client.get(f"/zaproszenie/{token}").text
    odp = _przejdz(client, f"/login/zewn/google?zaproszenie={token}")
    assert odp.status_code == 303 and odp.headers["location"] == "/"
    with SessionLocal() as db:
        konto = db.execute(select(PortalUser).where(PortalUser.email == "jan@firma-a.pl")).scalar_one()
        assert konto.tenant_id == tenant_a["id"] and konto.password_hash == "!"
        powiazanie = db.execute(select(TozsamoscZewnetrzna)).scalar_one()
        assert (powiazanie.dostawca, powiazanie.sub) == ("google", "g-1")
    client.cookies.clear()
    odp = _przejdz(client, "/login/zewn/google")
    assert odp.status_code == 303 and odp.headers["location"] == "/"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_wiazanie_po_sub_a_nie_po_emailu(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    _przejdz(client, f"/login/zewn/google?zaproszenie={token}")
    client.cookies.clear()
    # Inne konto Google z TYM SAMYM adresem - nie wchodzi.
    Dostawca.osoba = {"sub": "g-obcy", "email": "jan@gmail.com"}
    assert _przejdz(client, "/login/zewn/google").status_code == 403


def test_podrobiony_state_odrzucony(client):
    odp = _przejdz(client, "/login/zewn/google", podmien_state="cudzy")
    assert odp.status_code == 400
    assert not any("googleapis" in w[1] for w in Dostawca.wywolania)


def test_nonce_musi_pasowac(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    odp = client.get(f"/login/zewn/google?zaproszenie={token}", follow_redirects=False)
    state = parse_qs(urlsplit(odp.headers["location"]).query)["state"][0]
    Dostawca.nonce = "inny"
    odp = client.get("/login/zewn/callback", params={"code": "k", "state": state})
    assert odp.status_code == 400
    assert "nie pasuje" in odp.text


def test_github_bierze_zweryfikowany_glowny_adres(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    odp = _przejdz(client, f"/login/zewn/github?zaproszenie={token}")
    assert odp.status_code == 303
    with SessionLocal() as db:
        powiazanie = db.execute(select(TozsamoscZewnetrzna)).scalar_one()
    assert (powiazanie.sub, powiazanie.email) == ("4242", "jan@example.com")


def test_powiazanie_z_konta_lokalnego_i_odlaczenie(client, tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    _login(client, "ktos@firma-a.pl", "haslo-lokalne-123")
    odp = _przejdz(client, "/login/zewn/google?cel=powiaz")
    assert odp.status_code == 303 and odp.headers["location"].startswith("/konto?info=")
    strona = client.get("/konto").text
    assert "jan@gmail.com" in strona
    with SessionLocal() as db:
        powiazanie_id = db.execute(select(TozsamoscZewnetrzna.id)).scalar_one()
    client.post(f"/konto/zewn/{powiazanie_id}/odlacz",
                data={"csrf_token": _extract_csrf(strona)})
    with SessionLocal() as db:
        assert db.execute(select(TozsamoscZewnetrzna)).first() is None


def test_nie_odlaczy_jedynego_sposobu_logowania(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    _przejdz(client, f"/login/zewn/google?zaproszenie={token}")
    strona = client.get("/konto").text
    with SessionLocal() as db:
        powiazanie_id = db.execute(select(TozsamoscZewnetrzna.id)).scalar_one()
    odp = client.post(f"/konto/zewn/{powiazanie_id}/odlacz",
                      data={"csrf_token": _extract_csrf(strona)}, follow_redirects=False)
    assert "blad=" in odp.headers["location"]
    with SessionLocal() as db:
        assert db.execute(select(TozsamoscZewnetrzna)).first() is not None


def test_firma_tylko_ad_blokuje_konta_zewnetrzne(client, tenant_a, make_user):
    token = _zaproszenie(client, make_user, tenant_a["id"])
    _przejdz(client, f"/login/zewn/google?zaproszenie={token}")
    client.cookies.clear()
    with SessionLocal() as db:
        db.get(Tenant, tenant_a["id"]).logowanie_lokalne = False
        db.commit()
    assert _przejdz(client, "/login/zewn/google").status_code == 403


def test_aplikacja_mobilna_loguje_kontem_zewnetrznym_z_pkce(client, tenant_a, make_user):
    import hashlib
    import secrets as s

    token = _zaproszenie(client, make_user, tenant_a["id"])
    _przejdz(client, f"/login/zewn/google?zaproszenie={token}")
    client.cookies.clear()
    weryfikator = s.token_urlsafe(48)
    wyzwanie = base64.urlsafe_b64encode(hashlib.sha256(weryfikator.encode()).digest()).rstrip(b"=").decode()
    odp = _przejdz(client, f"/login/zewn/google?cel=mobile&wyzwanie={wyzwanie}")
    assert odp.status_code == 303
    adres = odp.headers["location"]
    assert adres.startswith("pl.hubzso.cmdb:/logowanie?kod=")
    kod = adres.split("kod=")[1]
    # Bez wlasciwego weryfikatora kod nic nie daje.
    assert client.post("/api/v1/mobile/auth/kod",
                       json={"code": kod, "verifier": "x" * 43}).status_code == 401
    odp = client.post("/api/v1/mobile/auth/kod", json={"code": kod, "verifier": weryfikator})
    assert odp.status_code == 200, odp.text
    assert odp.json()["user"]["email"] == "jan@firma-a.pl"
    # Kod dziala raz.
    assert client.post("/api/v1/mobile/auth/kod",
                       json={"code": kod, "verifier": weryfikator}).status_code == 401


def test_sposoby_logowania_dla_aplikacji(client):
    dane = client.get("/api/v1/mobile/auth/sposoby").json()
    assert {d["key"] for d in dane["external"]} == {"google", "github"}


def test_id_token_microsoft_wymaga_wlasciwego_wydawcy(monkeypatch):
    ustawienia = get_settings()
    monkeypatch.setattr(ustawienia, "microsoft_client_id", "ms-id")
    monkeypatch.setattr(ustawienia, "microsoft_client_secret", SecretStr("ms-secret"))
    d = zewnetrzne.DOSTAWCY["microsoft"]
    dobry = {"iss": "https://login.microsoftonline.com/9188040d-6c67-4c5b-b112-36a304b66dad/v2.0",
             "aud": "ms-id", "exp": time.time() + 60, "nonce": "n", "sub": "abc"}
    assert zewnetrzne._sprawdz_id_token(d, _jwt(dobry), "n")["sub"] == "abc"
    for zly in ({"iss": "https://evil.example/v2.0"}, {"aud": "inna-aplikacja"},
                {"exp": time.time() - 3600}, {"nonce": "inny"}):
        with pytest.raises(zewnetrzne.BladZewnetrzny):
            zewnetrzne._sprawdz_id_token(d, _jwt({**dobry, **zly}), "n")
