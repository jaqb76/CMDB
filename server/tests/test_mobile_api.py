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


def test_mobile_dictionary_contract_follows_tenant_schema(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@a.pl", "bardzo-dlugie-haslo")
    headers = _login(client)

    categories = client.get("/api/v1/mobile/dictionaries", headers=headers)
    schema = client.get("/api/v1/mobile/dictionaries/osoba/schema", headers=headers)

    assert categories.status_code == 200
    assert {item["key"] for item in categories.json()} >= {"osoba", "lokalizacja"}
    assert schema.status_code == 200
    assert schema.json()["category"] == "osoba"
    assert schema.json()["fields"]
    assert {"key", "label", "type", "required"} <= set(schema.json()["fields"][0])


def test_mobile_assignment_returns_all_editable_values(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@a.pl", "bardzo-dlugie-haslo")
    with SessionLocal() as db:
        asset = Asset(tenant_id=tenant_a["id"], machine_id="a-2", hostname="A-TWO")
        db.add(asset); db.commit(); asset_id = asset.id

    response = client.put(
        f"/api/v1/mobile/assets/{asset_id}/assignment",
        headers=_login(client),
        json={"role_label": "serwer baz danych", "place": "szafa A/12"},
    )

    assert response.status_code == 200
    assert response.json()["role_label"] == "serwer baz danych"
    assert response.json()["place"] == "szafa A/12"


def test_mobile_report_can_be_created_and_updated(client, tenant_a, make_user):
    make_user(tenant_a["id"], "admin@a.pl", "bardzo-dlugie-haslo")
    headers = _login(client)
    catalog = client.get("/api/v1/mobile/reports/catalog", headers=headers)
    assert catalog.status_code == 200
    column = catalog.json()["columns"][0]["key"]

    created = client.post("/api/v1/mobile/reports", headers=headers, json={
        "name": "Tygodniowy", "type": "sprzet", "frequency": "tygodniowo",
        "recipients": "cmdb@example.pl", "active": True,
        "send_to_vendors": False, "columns": [column],
    })
    assert created.status_code == 201

    updated = client.put(f"/api/v1/mobile/reports/{created.json()['id']}", headers=headers, json={
        "name": "Miesieczny", "type": "sprzet", "frequency": "miesiecznie",
        "recipients": "cmdb@example.pl", "active": False,
        "send_to_vendors": True, "columns": [column],
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "Miesieczny"
    assert updated.json()["active"] is False
    assert updated.json()["send_to_vendors"] is True


def test_dictionary_wrong_category_and_foreign_tenant_cannot_delete(client, tenant_a, tenant_b, make_user):
    from cmdb_server.models import WpisSlownika
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    with SessionLocal() as db:
        rows = [WpisSlownika(tenant_id=t['id'], kategoria='dzial', wartosc='IT', klucz='it')
                for t in (tenant_a, tenant_b)]
        db.add_all(rows); db.commit()
        own, foreign = [row.id for row in rows]
    headers = _login(client)
    assert client.delete(f'/api/v1/mobile/dictionaries/osoba/{own}', headers=headers).status_code == 404
    assert client.delete(f'/api/v1/mobile/dictionaries/dzial/{foreign}', headers=headers).status_code == 404
    with SessionLocal() as db:
        assert db.get(WpisSlownika, own) is not None
        assert db.get(WpisSlownika, foreign) is not None
    assert client.delete(f'/api/v1/mobile/dictionaries/dzial/{own}', headers=headers).status_code == 204


def test_assets_filter_and_paginate_on_server(client, tenant_a, tenant_b, make_user):
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    with SessionLocal() as db:
        db.add_all([Asset(tenant_id=tenant_a['id'], machine_id=f'a-{i}', hostname=f'HOST-{i:03}',
                          os_family='linux' if i % 2 else 'windows') for i in range(105)])
        db.add(Asset(tenant_id=tenant_b['id'], machine_id='b-secret', hostname='HOST-SECRET', os_family='linux'))
        db.commit()
    headers = _login(client)
    page = client.get('/api/v1/mobile/assets?page=3&page_size=50', headers=headers).json()
    assert page['total'] == 105 and len(page['items']) == 5
    result = client.get('/api/v1/mobile/assets?q=HOST-103&os_family=linux&unassigned=true', headers=headers).json()
    assert [a['hostname'] for a in result['items']] == ['HOST-103']
    assert client.get('/api/v1/mobile/assets?q=SECRET', headers=headers).json()['total'] == 0


def test_report_validation_and_write_permissions(client, tenant_a, tenant_b, make_user):
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    make_user(tenant_a['id'], 'viewer@a.pl', 'bardzo-dlugie-haslo', role='viewer')
    make_user(tenant_b['id'], 'admin@b.pl', 'bardzo-dlugie-haslo')
    admin = _login(client)
    viewer = _login(client, 'viewer@a.pl')
    foreign = _login(client, 'admin@b.pl')
    body = dict(name='Inventory', type='sprzet', frequency='tygodniowo', recipients='cmdb@example.pl')
    assert client.post('/api/v1/mobile/reports', headers=viewer, json=body).status_code == 403
    created = client.post('/api/v1/mobile/reports', headers=admin, json=body)
    assert created.status_code == 201
    path = '/api/v1/mobile/reports/' + created.json()['id']
    for hdr, status in ((viewer, 403), (foreign, 404)):
        assert client.put(path, headers=hdr, json=body).status_code == status
        assert client.delete(path, headers=hdr).status_code == status
    for invalid in (dict(name='   '), dict(columns=['not-a-column']), dict(recipients='invalid')):
        assert client.put(path, headers=admin, json={**body, **invalid}).status_code == 400
    assert client.get('/api/v1/mobile/reports', headers=admin).json()[0]['name'] == 'Inventory'
    assert client.delete(path, headers=admin).status_code == 204


def test_mobile_tenant_selection_respects_roles(client, tenant_a, tenant_b, make_user):
    from cmdb_server.models import PortalUser
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    make_user(None, 'super@example.pl', 'bardzo-dlugie-haslo')
    auditor_id = make_user(None, 'audit@example.pl', 'bardzo-dlugie-haslo', role='viewer')
    with SessionLocal() as db:
        auditor = db.get(PortalUser, auditor_id)
        auditor.is_superadmin = False; auditor.is_global_viewer = True
        db.commit()
    admin = _login(client)
    assert [t['id'] for t in client.get('/api/v1/mobile/tenants', headers=admin).json()] == [tenant_a['id']]
    assert client.get('/api/v1/mobile/me', headers={**admin, 'X-CMDB-Tenant': tenant_b['slug']}).json()['tenant']['id'] == tenant_a['id']
    for email, can_write in [('super@example.pl', True), ('audit@example.pl', False)]:
        headers = _login(client, email)
        assert len(client.get('/api/v1/mobile/tenants', headers=headers).json()) == 2
        assert client.get('/api/v1/mobile/dashboard', headers=headers).status_code == 400
        scoped = {**headers, 'X-CMDB-Tenant': tenant_b['slug']}
        me = client.get('/api/v1/mobile/me', headers=scoped).json()
        assert me['tenant']['id'] == tenant_b['id'] and me['can_write'] is can_write
        if not can_write:
            assert client.post('/api/v1/mobile/reports', headers=scoped, json={
                'name': 'Denied', 'type': 'sprzet', 'recipients': 'cmdb@example.pl',
            }).status_code == 403


def test_audit_excludes_other_tenants_and_global_events(client, tenant_a, tenant_b, make_user):
    from cmdb_server.models import AuditLog
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    with SessionLocal() as db:
        db.add_all([AuditLog(tenant_id=t, actor='test', action='test', target=label)
                    for t, label in [(tenant_a['id'], 'own'), (tenant_b['id'], 'foreign'), (None, 'global')]])
        db.commit()
    events = client.get('/api/v1/mobile/audit', headers=_login(client)).json()
    assert {e['target'] for e in events} == {'own'}


def test_dynamic_dictionary_crud_and_validation(client, tenant_a, make_user):
    from cmdb_server.models import SchematSlownika
    make_user(tenant_a['id'], 'admin@a.pl', 'bardzo-dlugie-haslo')
    headers = _login(client)
    with SessionLocal() as db:
        db.add(SchematSlownika(tenant_id=tenant_a['id'], kategoria='dzial', definicja={
            'kategoria': 'dzial', 'wersja': 1, 'pola': [
                {'klucz': 'skrot', 'etykieta': 'Skrót', 'wymagane': True, 'w_etykiecie': True, 'typ': 'tekst'},
                {'klucz': 'aktywny', 'etykieta': 'Aktywny', 'typ': 'logiczna'},
                {'klucz': 'numer', 'etykieta': 'Numer', 'typ': 'liczba', 'min': 1, 'max': 10},
            ],
        }))
        db.commit()
    path = '/api/v1/mobile/dictionaries/dzial'
    assert client.post(path, headers=headers, json={'attributes': {'numer': '99'}}).status_code == 400
    created = client.post(path, headers=headers, json={'attributes': {'skrot': 'IT', 'aktywny': False, 'numer': '2'}})
    assert created.status_code == 201
    assert created.json()['value'] == 'IT'
    assert created.json()['attributes']['aktywny'] is False
    updated = client.put(path + '/' + created.json()['id'], headers=headers, json={'attributes': {'skrot': 'OPS', 'aktywny': True, 'numer': '3'}})
    assert updated.status_code == 200 and updated.json()['value'] == 'OPS'
