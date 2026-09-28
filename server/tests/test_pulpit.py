"""Pulpit: kafelki, wykresy i tabela zasobow."""
from __future__ import annotations

from datetime import timedelta

from cmdb_server.db import SessionLocal
from cmdb_server.models import ZRODLO_AGENT, ZRODLO_RECZNE, Asset, utcnow

from .test_cve import _pakiet, _raport, kanal_debian  # noqa: F401
from .test_tenant_isolation import _login


def _zasoby(tenant_id: str) -> None:
    teraz = utcnow()
    with SessionLocal() as db:
        db.add_all([
            Asset(tenant_id=tenant_id, machine_id="m1", hostname="srv-nowy", os_family="linux",
                  os_name="Ubuntu 24.04", agent_version="11.10.0", zrodlo=ZRODLO_AGENT, last_seen=teraz),
            Asset(tenant_id=tenant_id, machine_id="m2", hostname="ws-stary", os_family="windows",
                  agent_version="11.9.2", zrodlo=ZRODLO_AGENT, last_seen=teraz - timedelta(days=5)),
            Asset(tenant_id=tenant_id, machine_id="reczne:1", hostname="drukarka-12", typ="drukarka",
                  zrodlo=ZRODLO_RECZNE, last_seen=teraz - timedelta(hours=1)),
        ])
        db.commit()


def test_pulpit_pokazuje_podsumowanie(client, tenant_a, make_user):
    _zasoby(tenant_a["id"])
    make_user(tenant_a["id"], "pulpit@firma.pl", "haslo-do-testow-123")
    _login(client, "pulpit@firma.pl", "haslo-do-testow-123")

    strona = client.get("/").text
    assert "Podsumowanie zasobów" in strona
    assert "Wersje agentów" in strona
    assert "Szybkie filtry" in strona
    # 11.10.0 jest nowsza od 11.9.2, choc tekstowo wypada dalej.
    assert strona.index(">11.10.0<") < strona.index(">11.9.2<")
    assert "Najbardziej podatne maszyny" in strona
    # Wykresy sa w SVG, bez stylow wpisanych w strone (CSP: style-src 'self').
    assert "<svg" in strona
    assert 'style="' not in strona.split('<main class="content">')[1]


def test_pulpit_pokazuje_najbardziej_podatne(client, tenant_a, make_user, kanal_debian):
    from cmdb_server.models import AssetCurrentReport

    with SessionLocal() as db:
        for nazwa, wersja in (("deb-dziurawy", "7.88.1-10+deb12u14"), ("deb-czysty", "7.88.1-10+deb12u16")):
            maszyna = Asset(tenant_id=tenant_a["id"], machine_id=nazwa, hostname=nazwa,
                            os_family="linux", zrodlo=ZRODLO_AGENT)
            db.add(maszyna)
            db.flush()
            db.add(AssetCurrentReport(asset_id=maszyna.id, tenant_id=tenant_a["id"], collected_at=utcnow(),
                                      payload=_raport([_pakiet("curl", wersja)]), payload_hash="x"))
        db.commit()
    make_user(tenant_a["id"], "cve@firma.pl", "haslo-do-testow-123")
    _login(client, "cve@firma.pl", "haslo-do-testow-123")

    strona = client.get("/?odswiez=1").text
    lista = strona.split("Najbardziej podatne maszyny")[1]
    assert "deb-dziurawy" in lista
    assert "#podatnosci" in lista
    assert "deb-czysty" not in lista.split("</table>")[0]
    assert "Sprawdzono 2 maszyn" in lista


def test_filtr_bez_agenta(client, tenant_a, make_user):
    _zasoby(tenant_a["id"])
    make_user(tenant_a["id"], "filtr@firma.pl", "haslo-do-testow-123")
    _login(client, "filtr@firma.pl", "haslo-do-testow-123")

    lista = client.get("/assets?zrodlo=bez_agenta").text
    assert "drukarka-12" in lista
    assert "srv-nowy" not in lista
