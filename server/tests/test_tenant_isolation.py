"""Izolacja firm - najwazniejsza wlasciwosc bezpieczenstwa systemu.

Sprawdzamy zarowno sciezke agenta (token firmowy), jak i panelu (sesja).
"""
from __future__ import annotations

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset
from sqlalchemy import select

from .factories import build_report
from .test_agent_api import enroll


def _login(client, email: str, password: str):
    response = client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return response


def test_agents_of_two_tenants_land_in_separate_scopes(client, tenant_a, tenant_b):
    token_a = enroll(client, tenant_a["token"], machine_id="maszyna-a-0001", hostname="A-01").json()[
        "agent_token"
    ]
    token_b = enroll(client, tenant_b["token"], machine_id="maszyna-b-0001", hostname="B-01").json()[
        "agent_token"
    ]

    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_a}"},
        json=build_report(machine_id="maszyna-a-0001", hostname="A-01"),
    )
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_b}"},
        json=build_report(machine_id="maszyna-b-0001", hostname="B-01"),
    )

    with SessionLocal() as db:
        assets = db.execute(select(Asset)).scalars().all()
        assert len(assets) == 2
        by_tenant = {a.tenant_id: a.hostname for a in assets}
        assert by_tenant[tenant_a["id"]] == "A-01"
        assert by_tenant[tenant_b["id"]] == "B-01"


def test_agent_cannot_hijack_machine_of_other_tenant(client, tenant_a, tenant_b):
    """Ten sam machine_id w dwoch firmach to dwie odrebne maszyny."""
    token_a = enroll(client, tenant_a["token"], machine_id="wspolny-id-0001").json()["agent_token"]
    token_b = enroll(client, tenant_b["token"], machine_id="wspolny-id-0001").json()["agent_token"]

    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_a}"},
        json=build_report(machine_id="wspolny-id-0001", hostname="A-KOLIZJA"),
    )
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_b}"},
        json=build_report(machine_id="wspolny-id-0001", hostname="B-KOLIZJA"),
    )

    with SessionLocal() as db:
        assets = db.execute(select(Asset)).scalars().all()
        assert len(assets) == 2
        assert {a.tenant_id for a in assets} == {tenant_a["id"], tenant_b["id"]}


def test_portal_user_sees_only_own_company(client, tenant_a, tenant_b, make_user):
    token_a = enroll(client, tenant_a["token"], machine_id="maszyna-a-0001", hostname="A-01").json()[
        "agent_token"
    ]
    token_b = enroll(client, tenant_b["token"], machine_id="maszyna-b-0001", hostname="B-01").json()[
        "agent_token"
    ]
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_a}"},
        json=build_report(machine_id="maszyna-a-0001", hostname="A-01"),
    )
    client.post(
        "/api/v1/inventory",
        headers={"Authorization": f"Bearer {token_b}"},
        json=build_report(machine_id="maszyna-b-0001", hostname="B-01"),
    )

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    listing = client.get("/assets")
    assert listing.status_code == 200
    assert "A-01" in listing.text
    assert "B-01" not in listing.text

    with SessionLocal() as db:
        asset_b = db.execute(
            select(Asset).where(Asset.tenant_id == tenant_b["id"])
        ).scalar_one()

    # Bezposrednie wejscie na URL cudzej maszyny: 404, nie 403 - nie zdradzamy istnienia.
    assert client.get(f"/assets/{asset_b.id}").status_code == 404
    assert client.get(f"/assets/{asset_b.id}/raw.json").status_code == 404

    export = client.get("/export/assets.json").json()
    assert [a["hostname"] for a in export["assets"]] == ["A-01"]
    assert export["tenant"] == "firma-a"


def test_owner_from_other_tenant_cannot_be_assigned(client, tenant_a, tenant_b, make_user):
    from cmdb_server.models import Owner

    enroll(client, tenant_a["token"], machine_id="maszyna-a-0001", hostname="A-01")
    with SessionLocal() as db:
        foreign_owner = Owner(
            tenant_id=tenant_b["id"], full_name="Obcy Opiekun", email="obcy@firma-b.pl"
        )
        db.add(foreign_owner)
        db.commit()
        foreign_owner_id = foreign_owner.id
        asset_a = db.execute(select(Asset).where(Asset.tenant_id == tenant_a["id"])).scalar_one()
        asset_a_id = asset_a.id

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    csrf = _extract_csrf(client.get(f"/assets/{asset_a_id}").text)
    response = client.post(
        f"/assets/{asset_a_id}/owner",
        data={"owner_id": foreign_owner_id, "role_label": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_viewer_cannot_modify(client, tenant_a, make_user):
    enroll(client, tenant_a["token"], machine_id="maszyna-a-0001", hostname="A-01")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset)).scalar_one().id

    make_user(tenant_a["id"], "viewer@firma-a.pl", "bardzo-dlugie-haslo", role="viewer")
    _login(client, "viewer@firma-a.pl", "bardzo-dlugie-haslo")

    csrf = _extract_csrf(client.get(f"/assets/{asset_id}").text)
    response = client.post(
        f"/assets/{asset_id}/owner",
        data={"owner_id": "", "role_label": "cokolwiek", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_csrf_token_required(client, tenant_a, make_user):
    enroll(client, tenant_a["token"], machine_id="maszyna-a-0001", hostname="A-01")
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset)).scalar_one().id

    make_user(tenant_a["id"], "admin@firma-a.pl", "bardzo-dlugie-haslo")
    _login(client, "admin@firma-a.pl", "bardzo-dlugie-haslo")

    response = client.post(
        f"/assets/{asset_id}/owner",
        data={"owner_id": "", "role_label": "bez-csrf"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_anonymous_is_redirected_to_login(client):
    for path in ("/", "/assets", "/owners", "/tokens", "/audit"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]
