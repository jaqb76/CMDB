"""Kontrakt bezpieczeństwa API aplikacji mobilnej."""
from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset


def _login(client, email="admin@a.pl", password="bardzo-dlugie-haslo"):
    response = client.post(
        "/api/v1/mobile/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_mobile_login_and_dashboard_use_same_tenant(client, tenant_a, tenant_b, make_user):
    make_user(tenant_a["id"], "admin@a.pl", "bardzo-dlugie-haslo")
    with SessionLocal() as db:
        db.add(Asset(tenant_id=tenant_a["id"], machine_id="a-1", hostname="A-ONE"))
        db.add(Asset(tenant_id=tenant_b["id"], machine_id="b-1", hostname="B-SECRET"))
        db.commit()

    headers = _login(client)
    dashboard = client.get("/api/v1/mobile/dashboard", headers=headers)
    assets = client.get("/api/v1/mobile/assets", headers=headers)

    assert dashboard.status_code == 200
    assert dashboard.json()["total"] == 1
    assert [row["hostname"] for row in assets.json()["items"]] == ["A-ONE"]


def test_mobile_viewer_cannot_write(client, tenant_a, make_user):
    make_user(tenant_a["id"], "viewer@a.pl", "bardzo-dlugie-haslo", role="viewer")
    with SessionLocal() as db:
        asset = Asset(tenant_id=tenant_a["id"], machine_id="a-1", hostname="A-ONE")
        db.add(asset); db.commit(); asset_id = asset.id

    headers = _login(client, "viewer@a.pl")
    response = client.put(
        f"/api/v1/mobile/assets/{asset_id}/assignment",
        headers=headers,
        json={"role_label": "serwer baz danych"},
    )
    assert response.status_code == 403


def test_browser_session_without_mobile_kind_is_rejected(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@a.pl", "bardzo-dlugie-haslo")
    browser = client.post("/login", data={"email": "admin@a.pl", "password": "bardzo-dlugie-haslo"})
    cookie = browser.history[0].cookies.get("cmdb_session")
    response = client.get("/api/v1/mobile/dashboard", headers={"Authorization": f"Bearer {cookie}"})
    assert response.status_code == 401
