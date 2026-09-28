"""Stan portalu: statystyki ruchu, zasoby, baza i blokady logowania."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import AuditLog, BlokadaLogowania, StatystykaRuchu, utcnow
from cmdb_server.services import ruch, stan_portalu

from .test_tenant_isolation import _extract_csrf, _login


def test_grupy_adresow():
    assert ruch.grupa("/api/v1/inventory") == "agenty"
    assert ruch.grupa("/api/v1/mobile/assets/1") == "mobilna"
    assert ruch.grupa("/admin/firmy") == "administracja"
    assert ruch.grupa("/static/app.css") == "statyczne"
    assert ruch.grupa("/assets") == "portal"


def test_procesy_sumuja_sie_w_bazie():
    """Kazdy proces roboczy dopisuje swoje liczby do tego samego wiersza."""
    chwila = utcnow()
    ruch.zanotuj("/assets", 200, 40, chwila)
    ruch.zanotuj("/assets", 500, 3000, chwila)
    with SessionLocal() as db:
        ruch.zrzuc(db)
    ruch.zanotuj("/assets", 404, 120, chwila)
    with SessionLocal() as db:
        ruch.zrzuc(db)
        wiersz = db.execute(select(StatystykaRuchu).where(
            StatystykaRuchu.grupa == "portal")).scalar_one()
    assert wiersz.liczba == 3
    assert wiersz.bledy_5xx == 1 and wiersz.bledy_4xx == 1
    assert wiersz.maks_ms == 3000
    assert (wiersz.do_50, wiersz.do_250, wiersz.powyzej) == (1, 1, 1)


def test_percentyl_z_przedzialow():
    przedzialy = {"do_50": 90, "do_100": 5, "do_1000": 5}
    assert ruch.percentyl(przedzialy, 0.5) == 50
    assert ruch.percentyl(przedzialy, 0.95) == 100
    assert ruch.percentyl(przedzialy, 0.99) == 1000
    assert ruch.percentyl({}, 0.95) is None


def test_middleware_liczy_zapytania(client):
    with SessionLocal() as db:
        ruch.zrzuc(db)
        db.query(StatystykaRuchu).delete()
        db.commit()
    client.get("/login")
    client.get("/api/v1/health")      # sprawdzanie zywotnosci nie jest ruchem
    with SessionLocal() as db:
        ruch.zrzuc(db)
        grupy = {w.grupa: w.liczba for w in db.execute(select(StatystykaRuchu)).scalars()}
    assert grupy.get("logowanie", 0) >= 1
    assert "agenty" not in grupy


def test_zasoby_i_baza_sie_licza():
    zasoby = stan_portalu.zasoby()
    assert zasoby["procesory"] >= 1
    with SessionLocal() as db:
        baza = stan_portalu.baza(db)
    assert baza["rozmiar"] > 0
    assert baza["polaczenia"] >= 1


def _blokada(klucz, godzin=1, licznik=5):
    with SessionLocal() as db:
        db.add(BlokadaLogowania(klucz=klucz, licznik=licznik, ostatnia_proba=utcnow(),
                                blokada_do=utcnow() + timedelta(hours=godzin)))
        db.commit()


def test_strona_pokazuje_i_zdejmuje_blokady(client, tenant_a, make_user):
    _blokada("konto:ofiara@firma.pl")
    _blokada("ip:203.0.113.7")
    make_user(None, "root-stan@cmdb.pl", "haslo-do-testow-123")
    _login(client, "root-stan@cmdb.pl", "haslo-do-testow-123")

    strona = client.get("/admin/portal").text
    assert "Stan portalu" in strona
    assert "ofiara@firma.pl" in strona and "203.0.113.7" in strona
    assert "Ruch według części portalu" in strona
    assert 'style="' not in strona.split("<main")[1]

    odpowiedz = client.post("/admin/portal/odblokuj", data={
        "klucz": "konto:ofiara@firma.pl", "csrf_token": _extract_csrf(strona)},
        follow_redirects=False)
    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        assert db.get(BlokadaLogowania, "konto:ofiara@firma.pl") is None
        assert db.get(BlokadaLogowania, "ip:203.0.113.7") is not None
        assert db.execute(select(AuditLog).where(
            AuditLog.action == "login.unblocked")).scalar_one()


def test_odblokowanie_wymaga_csrf(client, tenant_a, make_user):
    _blokada("konto:x@firma.pl")
    make_user(None, "root-csrf@cmdb.pl", "haslo-do-testow-123")
    _login(client, "root-csrf@cmdb.pl", "haslo-do-testow-123")
    odpowiedz = client.post("/admin/portal/odblokuj", data={"klucz": "konto:x@firma.pl"},
                            follow_redirects=False)
    assert odpowiedz.status_code in (400, 403)
    with SessionLocal() as db:
        assert db.get(BlokadaLogowania, "konto:x@firma.pl") is not None


def test_strona_tylko_dla_administratora_glownego(client, tenant_a, make_user):
    make_user(tenant_a["id"], "firma-stan@firma.pl", "haslo-do-testow-123")
    _login(client, "firma-stan@firma.pl", "haslo-do-testow-123")
    assert client.get("/admin/portal").status_code == 403
