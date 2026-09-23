"""Zakladka "Agent" i dodatkowe funkcjonalnosci na karcie maszyny."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, MonitorUslugi, OdbiorFunkcjiAgenta, utcnow
from .test_discovery_policy import form
from .test_monitoring import dodaj_cel, zaloguj, zarejestruj_agenta

NONCE = "b" * 32


def pobierz_skaner(client, enrolled, nonce=NONCE):
    return client.get("/api/v1/agent/discovery-policy?nonce=" + nonce,
                      headers={"Authorization": "Bearer " + enrolled["agent_token"]})


def pobierz_monitorowanie(client, enrolled, nonce=NONCE):
    return client.get("/api/v1/agent/monitoring-policy?nonce=" + nonce,
                      headers={"Authorization": "Bearer " + enrolled["agent_token"]})


def karta(client, asset_id, funkcja=""):
    adres = f"/assets/{asset_id}" + (f"?funkcja={funkcja}" if funkcja else "")
    odpowiedz = client.get(adres)
    assert odpowiedz.status_code == 200, odpowiedz.text
    return odpowiedz.text


def test_karta_ma_zakladke_agent_z_podzakladkami(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    zaloguj(client, tenant_a, make_user)
    tresc = karta(client, enrolled["asset_id"])
    assert 'data-tab="agent"' in tresc and 'data-panel="agent"' in tresc
    assert "Dodatkowe funkcjonalności" in tresc and "Funkcje podstawowe" in tresc
    # Domyslnie pierwsza podzakladka: skaner, z formularzem polityki.
    assert f'action="/assets/{enrolled["asset_id"]}/discovery-policy"' in tresc
    assert f'href="/assets/{enrolled["asset_id"]}?funkcja=monitorowanie#agent"' in tresc

    tresc = karta(client, enrolled["asset_id"], "monitorowanie")
    assert "Ta maszyna niczego jeszcze nie sprawdza" in tresc
    assert f'/monitoring?wykonawca={enrolled["asset_id"]}#nowy-cel' in tresc
    assert "discovery-policy" not in tresc.split('data-panel="agent"')[1].split("</section>")[0]


def test_nieznana_podzakladka_pokazuje_pierwsza(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    zaloguj(client, tenant_a, make_user)
    tresc = karta(client, enrolled["asset_id"], "cos-innego")
    assert f'action="/assets/{enrolled["asset_id"]}/discovery-policy"' in tresc


def test_wpis_reczny_nie_ma_zakladki_agent(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        wpis = Asset(tenant_id=tenant_a["id"], machine_id="reczny-1", hostname="drukarka-1",
                     zrodlo="reczne", typ="drukarka")
        db.add(wpis)
        db.commit()
        asset_id = wpis.id
    tresc = karta(client, asset_id)
    assert 'data-tab="agent"' not in tresc and "Dodatkowe funkcjonalności" not in tresc


def test_zmiana_skanera_czeka_do_odebrania_przez_agenta(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    asset_id = enrolled["asset_id"]

    # Wylaczony i nigdy niepobrany skaner na nic nie czeka.
    assert "data-funkcje-czekaja" not in karta(client, asset_id)

    zapis = client.post(f"/assets/{asset_id}/discovery-policy", data=form(csrf), follow_redirects=False)
    assert zapis.status_code == 303
    assert zapis.headers["location"] == f"/assets/{asset_id}?funkcja=skaner#agent"
    tresc = karta(client, asset_id)
    assert "data-funkcje-czekaja" in tresc and "czeka na odebranie" in tresc

    rewizja = pobierz_skaner(client, enrolled).json()["revision"]
    tresc = karta(client, asset_id)
    assert "data-funkcje-czekaja" not in tresc
    assert "agent ma aktualną konfigurację" in tresc

    # Kolejna zmiana - znowu czeka, bo agent ma poprzednia rewizje.
    client.post(f"/assets/{asset_id}/discovery-policy",
                data=form(csrf, rewizja, enabled="false"), follow_redirects=False)
    assert "data-funkcje-czekaja" in karta(client, asset_id)


def test_nowy_cel_monitorowania_czeka_do_odebrania(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    zaloguj(client, tenant_a, make_user)
    asset_id = enrolled["asset_id"]
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    tresc = karta(client, asset_id, "monitorowanie")
    assert "data-funkcje-czekaja" in tresc and "1 cel" in tresc

    assert pobierz_monitorowanie(client, enrolled).status_code == 200
    assert "data-funkcje-czekaja" not in karta(client, asset_id, "monitorowanie")

    # "Sprawdz teraz" zmienia odpowiedz, ale nie konfiguracje - nie moze
    # zapalac "czeka na odebranie" przy kazdym kliknieciu.
    with SessionLocal() as db:
        db.get(MonitorUslugi, monitor_id).wymuszone_o = utcnow()
        db.commit()
    assert "data-funkcje-czekaja" not in karta(client, asset_id, "monitorowanie")


def test_odbior_zapisuje_sie_dla_wlasciwej_firmy(client, tenant_a, tenant_b):
    a = zarejestruj_agenta(client, tenant_a, machine="maszyna-a")
    b = zarejestruj_agenta(client, tenant_b, machine="maszyna-b")
    pobierz_skaner(client, a)
    pobierz_monitorowanie(client, b, nonce="c" * 32)
    pobierz_monitorowanie(client, b, nonce="d" * 32)
    with SessionLocal() as db:
        wiersze = {(o.asset_id, o.funkcja): o.tenant_id for o in db.scalars(select(OdbiorFunkcjiAgenta))}
    assert wiersze == {(a["asset_id"], "skaner"): tenant_a["id"],
                       (b["asset_id"], "monitorowanie"): tenant_b["id"]}


def test_dawny_adres_polityki_przekierowuje_na_karte(client, tenant_a, tenant_b, make_user):
    a = zarejestruj_agenta(client, tenant_a, machine="maszyna-a")
    b = zarejestruj_agenta(client, tenant_b, machine="maszyna-b")
    zaloguj(client, tenant_a, make_user)
    odp = client.get(f"/assets/{a['asset_id']}/discovery-policy", follow_redirects=False)
    assert odp.status_code == 303
    assert odp.headers["location"] == f"/assets/{a['asset_id']}?funkcja=skaner#agent"
    assert client.get(f"/assets/{b['asset_id']}/discovery-policy",
                      follow_redirects=False).status_code == 404


def test_lista_sprzetu_filtruje_po_funkcji(client, tenant_a, make_user):
    skaner = zarejestruj_agenta(client, tenant_a, machine="maszyna-skaner")
    monitor = zarejestruj_agenta(client, tenant_a, machine="maszyna-monitor")
    zarejestruj_agenta(client, tenant_a, machine="maszyna-nic")
    _, csrf = zaloguj(client, tenant_a, make_user)
    client.post(f"/assets/{skaner['asset_id']}/discovery-policy", data=form(csrf), follow_redirects=False)
    dodaj_cel(tenant_a["id"], monitor["asset_id"])
    with SessionLocal() as db:
        nazwy = dict(db.execute(select(Asset.id, Asset.hostname)).all())

    def na_liscie(funkcja):
        tresc = client.get(f"/assets?funkcja={funkcja}").text
        return {n for i, n in nazwy.items() if f'href="/assets/{i}"' in tresc}

    assert na_liscie("skaner") == {nazwy[skaner["asset_id"]]}
    assert na_liscie("monitorowanie") == {nazwy[monitor["asset_id"]]}
    # Nieznana wartosc nie zawaza listy do zera - po prostu nie filtruje.
    assert na_liscie("nieznana") == set(nazwy.values())


def test_przycisk_z_zakladki_wybiera_maszyne_w_formularzu_celu(client, tenant_a, make_user):
    zarejestruj_agenta(client, tenant_a, machine="maszyna-1")
    druga = zarejestruj_agenta(client, tenant_a, machine="maszyna-2")
    zaloguj(client, tenant_a, make_user)
    tresc = client.get(f"/monitoring?wykonawca={druga['asset_id']}").text
    assert f'<option value="{druga["asset_id"]}" selected>' in tresc
    assert 'id="nowy-cel"' in tresc

