"""Biometria w aplikacji: rejestracja klucza, wyzwanie, podpis, odlaczanie.

Telefon zastepuje para kluczy EC P-256 z biblioteki cryptography - ten sam
algorytm i format (DER SPKI, podpis ECDSA/SHA-256 w DER), co Android Keystore.
"""
from __future__ import annotations

import base64
import time
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import AuditLog, Tenant, UrzadzenieMobilne, utcnow
from cmdb_server.services import biometria

from .test_tenant_isolation import _extract_csrf, _login


class Telefon:
    def __init__(self):
        self.klucz = ec.generate_private_key(ec.SECP256R1())

    @property
    def publiczny(self) -> str:
        return base64.b64encode(self.klucz.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def podpisz(self, tresc: str) -> str:
        return base64.b64encode(self.klucz.sign(tresc.encode(), ec.ECDSA(hashes.SHA256()))).decode()


def _zaloguj_haslem(client, email="ktos@firma-a.pl", haslo="haslo-lokalne-123"):
    odp = client.post("/api/v1/mobile/auth/login", json={"email": email, "password": haslo})
    assert odp.status_code == 200, odp.text
    return odp.json()


def _naglowek(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def konto(tenant_a, make_user):
    make_user(tenant_a["id"], "ktos@firma-a.pl", "haslo-lokalne-123")
    return tenant_a


def _wlacz(client, telefon, token, zastepuje=None):
    odp = client.post("/api/v1/mobile/devices", headers=_naglowek(token),
                      json={"name": "Pixel 8", "public_key": telefon.publiczny,
                            "replaces": zastepuje})
    assert odp.status_code == 200, odp.text
    return odp.json()["device"]["id"]


def _biometria(client, telefon, urzadzenie):
    wyzwanie = client.post("/api/v1/mobile/auth/challenge",
                           json={"device_id": urzadzenie}).json()["challenge"]
    return client.post("/api/v1/mobile/auth/biometric", json={
        "device_id": urzadzenie, "challenge": wyzwanie, "signature": telefon.podpisz(wyzwanie)})


def test_pelny_przebieg(client, konto):
    dane = _zaloguj_haslem(client)
    assert dane["policy"]["biometrics"] == "dozwolona"
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    odp = _biometria(client, telefon, urzadzenie)
    assert odp.status_code == 200, odp.text
    assert odp.json()["expires_in"] == 3600
    token = odp.json()["access_token"]
    assert client.get("/api/v1/mobile/me", headers=_naglowek(token)).status_code == 200
    with SessionLocal() as db:
        sposoby = [w.detail["sposob"] for w in db.execute(
            select(AuditLog).where(AuditLog.action == "mobile.login.ok")).scalars()]
    assert sorted(sposoby) == ["biometria", "lokalne"]


def test_cudzy_klucz_nie_przejdzie(client, konto):
    dane = _zaloguj_haslem(client)
    urzadzenie = _wlacz(client, Telefon(), dane["access_token"])
    odp = _biometria(client, Telefon(), urzadzenie)
    assert odp.status_code == 401
    assert odp.headers["x-cmdb-biometria"] == "podpis"


def test_wyzwanie_dziala_raz(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    wyzwanie = client.post("/api/v1/mobile/auth/challenge", json={"device_id": urzadzenie}).json()["challenge"]
    tresc = {"device_id": urzadzenie, "challenge": wyzwanie, "signature": telefon.podpisz(wyzwanie)}
    assert client.post("/api/v1/mobile/auth/biometric", json=tresc).status_code == 200
    odp = client.post("/api/v1/mobile/auth/biometric", json=tresc)
    assert odp.status_code == 401 and odp.headers["x-cmdb-biometria"] == "wyzwanie"


def test_wyzwanie_innego_urzadzenia(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    wyzwanie = client.post("/api/v1/mobile/auth/challenge", json={"device_id": "inne"}).json()["challenge"]
    odp = client.post("/api/v1/mobile/auth/biometric", json={
        "device_id": urzadzenie, "challenge": wyzwanie, "signature": telefon.podpisz(wyzwanie)})
    assert odp.status_code == 401


def test_rejestracja_tylko_po_swiezym_pelnym_logowaniu(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    token_biometrii = _biometria(client, telefon, urzadzenie).json()["access_token"]
    # Token z biometrii nie wystarcza, zeby dopisac kolejny telefon.
    odp = client.post("/api/v1/mobile/devices", headers=_naglowek(token_biometrii),
                      json={"name": "Obcy", "public_key": Telefon().publiczny})
    assert odp.status_code == 401


def test_odlaczenie_w_panelu_dziala_od_razu(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    token = _biometria(client, telefon, urzadzenie).json()["access_token"]
    _login(client, "ktos@firma-a.pl", "haslo-lokalne-123")
    strona = client.get("/konto").text
    assert "Pixel 8" in strona
    client.post(f"/konto/urzadzenia/{urzadzenie}/odlacz", data={"csrf_token": _extract_csrf(strona)})
    # Wydany juz token przestaje dzialac, a nowego sie nie dostanie.
    assert client.get("/api/v1/mobile/me", headers=_naglowek(token)).status_code == 401
    odp = _biometria(client, telefon, urzadzenie)
    assert odp.status_code == 401 and odp.headers["x-cmdb-biometria"] == "odlaczone"


def test_wyloguj_wszedzie_odlacza_telefony(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    _login(client, "ktos@firma-a.pl", "haslo-lokalne-123")
    client.post("/konto/wyloguj-wszystkie", data={"csrf_token": _extract_csrf(client.get("/konto").text)})
    assert _biometria(client, telefon, urzadzenie).status_code == 401


def test_po_okresie_wymagane_pelne_logowanie(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    with SessionLocal() as db:
        db.get(UrzadzenieMobilne, urzadzenie).ostatnie_pelne_logowanie = utcnow() - timedelta(days=31)
        db.commit()
    odp = _biometria(client, telefon, urzadzenie)
    assert odp.status_code == 401 and odp.headers["x-cmdb-biometria"] == "pelne_logowanie"
    # Po pelnym logowaniu aplikacja rejestruje nowy klucz w miejsce starego.
    dane = _zaloguj_haslem(client)
    nowy = Telefon()
    nowe = _wlacz(client, nowy, dane["access_token"], zastepuje=urzadzenie)
    assert _biometria(client, nowy, nowe).status_code == 200
    assert _biometria(client, telefon, urzadzenie).status_code == 401


def test_firma_wylacza_biometrie(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    with SessionLocal() as db:
        db.get(Tenant, konto["id"]).biometria = "wylaczona"
        db.commit()
    assert _biometria(client, telefon, urzadzenie).status_code == 401
    dane = _zaloguj_haslem(client)
    assert dane["policy"]["biometrics"] == "wylaczona"
    odp = client.post("/api/v1/mobile/devices", headers=_naglowek(dane["access_token"]),
                      json={"name": "x", "public_key": telefon.publiczny})
    assert odp.status_code == 403


def test_wylaczone_konto_nie_loguje_palcem(client, konto):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    with SessionLocal() as db:
        from cmdb_server.models import PortalUser
        db.execute(select(PortalUser)).scalar_one().is_active = False
        db.commit()
    assert _biometria(client, telefon, urzadzenie).status_code == 401


def test_zapis_po_biometrii_wymaga_swiezego_potwierdzenia(client, konto, monkeypatch):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    token = _biometria(client, telefon, urzadzenie).json()["access_token"]
    prawdziwy = time.time
    monkeypatch.setattr(time, "time", lambda: prawdziwy() + 600)
    odp = client.put("/api/v1/mobile/assets/nieistniejacy/assignment", headers=_naglowek(token),
                     json={})
    assert odp.status_code == 401 and odp.headers["x-cmdb-potwierdz"] == "biometria"


def test_zly_klucz_publiczny(client, konto):
    dane = _zaloguj_haslem(client)
    rsa_like = base64.b64encode(b"x" * 80).decode()
    odp = client.post("/api/v1/mobile/devices", headers=_naglowek(dane["access_token"]),
                      json={"name": "x", "public_key": rsa_like})
    assert odp.status_code == 400


def test_admin_odlacza_telefony(client, konto, make_user):
    dane = _zaloguj_haslem(client)
    telefon = Telefon()
    urzadzenie = _wlacz(client, telefon, dane["access_token"])
    make_user(None, "root@operator.pl", "haslo-superadmina-1")
    _login(client, "root@operator.pl", "haslo-superadmina-1")
    strona = client.get("/admin/konta").text
    assert "Odłącz telefony (1)" in strona
    with SessionLocal() as db:
        uid = db.get(UrzadzenieMobilne, urzadzenie).user_id
    client.post(f"/admin/users/{uid}/urzadzenia/odlacz", data={"csrf_token": _extract_csrf(strona)})
    assert _biometria(client, telefon, urzadzenie).status_code == 401
