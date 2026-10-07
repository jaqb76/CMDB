"""Ceph Dashboard: konfiguracja polaczenia, zapis klastra i relacja z regionem OpenStacka."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetRelation, AuditLog, NutanixObiekt
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, ustawienia_agenta, zarejestruj
from .test_openstack import odczyt as odczyt_os, polityka as polityka_os, wlacz as wlacz_os, wyslij as wyslij_os

FSID = "6f1c2a3e-5b7d-11ef-9c1a-0242ac120002"


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, **{"adres": "ceph-mgr.firma.pl", "uzytkownik": "cmdb-ro", "nazwa": "Ceph DC1", **extra})
    odp = client.post(f"/assets/{asset_id}/ceph", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="e" * 32):
    odp = client.get("/api/v1/agent/ceph-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, fsid=FSID, **zmiany):
    klaster = {"ext_id": fsid, "wersja": "18.2.4 reef", "zdrowie": "HEALTH_WARN",
               "ostrzezenia": ["WARN: 1 nearfull osd(s)"],
               "pojemnosc_bajty": 30 * 2**40, "zajete_bajty": 12 * 2**40, "wolne_bajty": 18 * 2**40,
               "liczba_osd": 3, "osd_up": 2, "osd_in": 3, "liczba_mon": 3, "mon_kworum": 3, "liczba_hostow": 2,
               "hosty": [{"nazwa": "ceph-osd01", "adres": "10.60.0.21", "role": ["mgr", "mon", "osd"], "osd": 2,
                          "wersja": "18.2.4 reef"},
                         {"nazwa": "ceph-osd02", "adres": "10.60.0.22", "role": ["osd"], "osd": 1}],
               "pule": [{"nazwa": "volumes", "typ": "replicated", "rozmiar": 3, "min_rozmiar": 2, "pg": 128,
                         "aplikacje": ["rbd"], "zajete_bajty": 3 * 2**40, "dostepne_bajty": 5 * 2**40},
                        {"nazwa": "scratch", "typ": "replicated", "rozmiar": 1, "aplikacje": ["rbd"]}],
               "osd": [{"id": 0, "host": "ceph-osd01", "klasa": "ssd", "rozmiar_bajty": 8 * 2**40, "zajete_bajty": 2**40,
                        "pg": 131, "up": True, "in": True},
                       {"id": 1, "host": "ceph-osd01", "klasa": "ssd", "rozmiar_bajty": 8 * 2**40, "zajete_bajty": 0,
                        "pg": 0, "up": False, "in": False, "stan": "autoout"}],
               "buckety": [{"nazwa": "loki-ruler", "wlasciciel": "od-prod-logs", "rozmiar_bajty": 9 * 2**30, "obiekty": 3,
                            "wersjonowanie": "off", "kwota_bajty": 10 * 2**30, "utworzono": "2025-07-08T11:24:59Z"}],
               "uzytkownicy_rgw": [{"uid": "ru-prod-tempo", "nazwa": "Tempo", "klucze_s3": 2, "klucze_swift": 0,
                                    "max_bucketow": 1, "zawieszony": True}],
               "bramy_rgw": [{"id": "coi.pod2-ceph011.wwsvtj", "host": "pod2-ceph011", "strefa": "data1", "port": 8080}],
               "cephfs": [{"nazwa": "CEPH_FS_PROD", "max_mds": 3, "aktywne_mds": 3, "pule_danych": [21],
                           "pula_metadanych": 22}]}
    klaster.update(zmiany)
    return {"protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 400,
            "wersja_pc": "18.2.4 reef", "klastry": [klaster]}


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/ceph", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user, **extra):
    enrolled = zarejestruj(client, tenant)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"], **extra)
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def test_konfiguracja_ceph_i_http_po_zgodzie(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = wlacz(client, csrf, enrolled["asset_id"], adres="http://10.60.0.10:8080")
    assert odp.headers["location"].endswith("nutanix_komunikat=tls#agent")
    wlacz(client, csrf, enrolled["asset_id"], adres="http://10.60.0.10:8080", bez_tls="true")
    p = polityka(client, enrolled)["policy"]
    assert p["adres"] == "http://10.60.0.10:8080" and p["bez_tls"] is True and p["uzytkownik"] == "cmdb-ro"
    assert "projekt" not in p  # pola OpenStacka nie dotycza Cepha
    with SessionLocal() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "ceph.config_changed")).one()
    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=ceph").text
    assert "Odczytuj Ceph Dashboard z tej maszyny" in strona and "read-only" in strona
    assert 'name="bez_tls"' in strona and 'name="projekt"' not in strona


def test_domyslny_port_dashboardu(client, tenant_a, make_user):
    enrolled, _, _ = _wlaczony(client, tenant_a, make_user)
    assert polityka(client, enrolled)["policy"]["adres"] == "https://ceph-mgr.firma.pl:8443"


def test_odczyt_zaklada_klaster_ceph(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        klaster = db.scalars(select(Asset).where(Asset.zrodlo == "ceph")).one()
        assert (klaster.typ, klaster.hostname, klaster.serial_number) == ("magazyn", "Ceph DC1", FSID)
        assert klaster.os_version == "18.2.4 reef"
        o = db.scalars(select(NutanixObiekt).where(NutanixObiekt.dostawca == "ceph")).one()
        assert o.rodzaj == "ceph" and o.ext_id == FSID and len(o.dane["pule"]) == 2
        assert ustawienia_agenta(db, enrolled["asset_id"], "ceph").odczyt_liczby == {
            "klastry": 1, "hosty": 2, "pule": 2, "osd": 3, "nowe": 1, "wycofane": 0}
    karta = client.get(f"/assets/{klaster.id}").text
    assert "Pamięć masowa" in karta and "HEALTH_WARN" in karta and "1 nearfull osd(s)" in karta
    assert "ceph-osd01" in karta and "mgr, mon, osd" in karta and "bez kopii" in karta and FSID in karta
    assert "down: 1" in karta and "odczytany z Ceph" in karta
    # Zakladka Ceph: OSD (zle na gorze, zestawienie host x klasa), buckety, uzytkownicy, bramy, CephFS.
    assert 'data-tab="ceph">Ceph <span class="badge badge-warn"' in karta
    # Sekcje zwiniete, tabele sortowalne, rozmiary sortowane po bajtach.
    zakladka = karta.split('data-panel="ceph"', 1)[1].split("</section>", 1)[0]
    assert zakladka.count('<details class="panel wirt-klaster sekcja-karty">') == 5 and "<details open" not in zakladka
    assert 'class="table sortowalna" id="ceph-osd"' in zakladka and f'data-sort="{8 * 2**40}"' in zakladka
    for tekst in ("OSD w złym stanie: 1", "osd.1 na ceph-osd01 — autoout", "16.0 TB", "Wszystkie OSD",
                  "loki-ruler", "≥90%", "ru-prod-tempo", "zawieszony", "2 / 0", "coi.pod2-ceph011.wwsvtj",
                  "CEPH_FS_PROD", "3 / 3"):
        assert tekst in karta, tekst
    with SessionLocal() as db:
        assert db.scalars(select(NutanixObiekt).where(NutanixObiekt.dostawca == "ceph")).one().dane["osd"][1]["in"] is False
    assert "Pamięć masowa ·" in karta or "Pamięć masowa\n" in karta  # nazwa rodzaju, nie surowy klucz
    assert "klaster pamięci masowej" in karta and "Po instalacji" not in karta
    strona = client.get("/wirtualizacja").text
    assert "Pamięć masowa" in strona and "Ceph DC1" in strona and "zajęte 40.0%" in strona
    assert "scratch" in strona
    # Globalna wyszukiwarka znajduje klaster po nazwie wezla i po fsid.
    assert [w["nazwa"] for w in client.get("/szukaj.json", params={"q": "ceph-osd02"}).json()["wyniki"]] == ["Ceph DC1"]
    assert [w["nazwa"] for w in client.get("/szukaj.json", params={"q": FSID[:13]}).json()["wyniki"]] == ["Ceph DC1"]
    assert "Ceph" in client.get(f"/assets/{enrolled['asset_id']}?funkcja=ceph").text


def test_znikniecie_klastra_wycofuje_a_powrot_przywraca(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev))
    inny = "11111111-2222-3333-4444-555555555555"
    wyslij(client, enrolled, odczyt(rev, fsid=inny))  # Dashboard wskazuje teraz inny klaster
    with SessionLocal() as db:
        stary = db.scalars(select(Asset).where(Asset.serial_number == FSID)).one()
        assert stary.lifecycle == "wycofany" and stary.retired_reason == "zniknął z Ceph Dashboard"
    wyslij(client, enrolled, odczyt(rev))
    with SessionLocal() as db:
        assert db.scalars(select(Asset).where(Asset.serial_number == FSID)).one().lifecycle == "aktywny"


def test_relacja_region_openstacka_do_klastra_ceph(client, tenant_a, make_user):
    """Cinder podaje fsid Cepha - region laczy sie z klastrem, w dowolnej kolejnosci odczytow."""
    enrolled, csrf, rev_ceph = _wlaczony(client, tenant_a, make_user)
    wlacz_os(client, csrf, enrolled["asset_id"])
    rev_os = polityka_os(client, enrolled)["revision"]
    dane_os = odczyt_os(rev_os)
    dane_os["klastry"][0]["magazyny"] = [{"fsid": FSID, "pula": "volumes", "backend": "rbd"}]

    assert wyslij_os(client, enrolled, dane_os) == "przyjeto"  # OpenStack pierwszy - Cepha jeszcze nie ma
    with SessionLocal() as db:
        assert not db.scalars(select(AssetRelation).where(AssetRelation.kind == "cluster_storage")).all()
    assert wyslij(client, enrolled, odczyt(rev_ceph)) == "przyjeto"  # Ceph dochodzi - relacja powstaje
    with SessionLocal() as db:
        [r] = db.scalars(select(AssetRelation).where(AssetRelation.kind == "cluster_storage")).all()
        assert (r.source.hostname, r.target.hostname, r.created_by) == ("Openstack G1 DC1", "Ceph DC1", "openstack")
    assert "korzysta: Openstack G1 DC1" in client.get("/wirtualizacja").text

    # Cinder przestal korzystac z tego Cepha - wykryta relacja znika.
    dane_os["klastry"][0]["magazyny"] = []
    wyslij_os(client, enrolled, dane_os)
    with SessionLocal() as db:
        assert not db.scalars(select(AssetRelation).where(AssetRelation.kind == "cluster_storage")).all()


def test_reczna_relacja_do_cepha_zostaje(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev))
    with SessionLocal() as db:
        ceph_id = db.scalars(select(Asset.id).where(Asset.zrodlo == "ceph")).one()
    odp = client.post("/relacje", data={"csrf_token": csrf, "source_id": enrolled["asset_id"],
                                        "target_id": ceph_id, "kind": "cluster_storage"}, follow_redirects=False)
    assert odp.status_code in (200, 303), odp.text
    wyslij(client, enrolled, odczyt(rev))  # kolejny odczyt nie rusza recznej relacji
    with SessionLocal() as db:
        [r] = db.scalars(select(AssetRelation).where(AssetRelation.kind == "cluster_storage")).all()
        assert r.source_id == enrolled["asset_id"] and r.created_by != "openstack"


def test_migracja_warunku_rodzaju_relacji():
    """Baza sprzed cluster_storage dostaje nowy warunek przy starcie (create_all go nie zmienia)."""
    from sqlalchemy import inspect, text

    from cmdb_server import db as baza

    with baza.engine.begin() as conn:
        conn.execute(text("ALTER TABLE asset_relations DROP CONSTRAINT ck_relation_kind"))
        conn.execute(text("ALTER TABLE asset_relations ADD CONSTRAINT ck_relation_kind "
                          "CHECK (kind IN ('vm_host', 'host_cluster', 'application_server'))"))
    baza._popraw_rodzaje_relacji()
    warunek = next(w["sqltext"] for w in inspect(baza.engine).get_check_constraints("asset_relations")
                   if w["name"] == "ck_relation_kind")
    assert "cluster_storage" in warunek and "vm_host" in warunek
    baza._popraw_rodzaje_relacji()  # drugi raz nic nie robi
