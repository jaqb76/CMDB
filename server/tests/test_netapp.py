"""NetApp ONTAP: konfiguracja polaczenia i zapis klastra (pamiec masowa)."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AuditLog, NutanixObiekt
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, ustawienia_agenta, zarejestruj

UUID = "9f1b6c2e-1a2b-11ee-8c3d-00a098e1c2d3"


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, **{"adres": "https://172.16.2.137/sysmgr/v4/", "uzytkownik": "cmdb-ro", **extra})
    odp = client.post(f"/assets/{asset_id}/netapp", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="8" * 32):
    odp = client.get("/api/v1/agent/netapp-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, **zmiany):
    k = {"ext_id": UUID, "nazwa": "pod01-netapp01", "wersja": "9.12.1P3", "numer_seryjny": "1-80-000011",
         "ip": "172.16.2.137",
         "wezly": [{"nazwa": "pod01-netapp01-01", "model": "AFF-A400", "numer_seryjny": "951913000123",
                    "czas_pracy_s": 8640000, "partner_ha": "pod01-netapp01-02"},
                   {"nazwa": "pod01-netapp01-02", "model": "AFF-A400", "numer_seryjny": "951913000124"}],
         "agregaty": [{"nazwa": "aggr1", "wezel": "pod01-netapp01-01", "rozmiar_bajty": 50 * 2**40,
                       "zajete_bajty": 45 * 2**40, "dostepne_bajty": 5 * 2**40, "dyski": 23, "raid": "raid_dp"}],
         "svm": [{"nazwa": "svm_nfs", "protokoly": ["nfs", "iscsi"], "ip": ["10.70.1.10"]}],
         "wolumeny": [{"nazwa": "vol_vmware01", "svm": "svm_nfs", "agregat": "aggr1", "sciezka": "/vol_vmware01",
                       "rozmiar_bajty": 10 * 2**40, "zajete_bajty": int(9.5 * 2**40)}],
         "luny": [{"nazwa": "/vol/vol_lun/lun0", "svm": "svm_nfs", "rozmiar_bajty": 2**40, "zmapowany": False}],
         "dyski": [{"nazwa": "1.0.0", "numer_seryjny": "S4ABC0000", "model": "X4011WBORA3T8NTF", "typ": "ssd",
                    "rozmiar_bajty": 3840000000000, "polka": "SHFMS123", "zatoka": 0},
                   {"nazwa": "1.0.3", "numer_seryjny": "S4ABC0003", "model": "X4011WBORA3T8NTF", "typ": "ssd",
                    "rozmiar_bajty": 3840000000000, "polka": "SHFMS123", "zatoka": 3, "stan": "broken"}],
         "polki": [{"nazwa": "1.0", "model": "NS224", "dyski": 24}],
         "interfejsy": [{"nazwa": "lif_nfs1", "ip": "10.70.1.10", "maska": "24", "svm": "svm_nfs"}],
         "snapmirror": [{"zrodlo": "svm_nfs:vol_vmware01", "cel": "svm_dr:vol_dst", "stan": "snapmirrored",
                         "zdrowy": False, "opoznienie": "PT26H"}]}
    k.update(zmiany)
    return {"protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 900,
            "wersja_pc": "9.12.1P3", "klastry": [k]}


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/netapp", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user):
    enrolled = zarejestruj(client, tenant)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def test_konfiguracja_netapp(client, tenant_a, make_user):
    enrolled, _, _ = _wlaczony(client, tenant_a, make_user)
    p = polityka(client, enrolled)["policy"]
    assert p["adres"] == "https://172.16.2.137/sysmgr/v4" and "bez_tls" not in p  # sciezke agent pomija
    with SessionLocal() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "netapp.config_changed")).one()
    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=netapp").text
    assert "Odczytuj ONTAP z tej maszyny" in strona and "-role readonly" in strona and "Ceph Dashboard" not in strona


def test_odczyt_zaklada_klaster_netapp(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        k = db.scalars(select(Asset).where(Asset.zrodlo == "netapp")).one()
        assert (k.typ, k.hostname, k.manufacturer, k.model) == ("magazyn", "pod01-netapp01", "NetApp", "AFF-A400")
        assert (k.os_name, k.os_version, k.serial_number, k.primary_ip) == ("ONTAP", "9.12.1P3", "1-80-000011", "172.16.2.137")
        assert db.scalars(select(NutanixObiekt).where(NutanixObiekt.dostawca == "netapp")).one().ext_id == UUID
        assert ustawienia_agenta(db, enrolled["asset_id"], "netapp").odczyt_liczby == {
            "klastry": 1, "wezly": 2, "wolumeny": 1, "dyski": 2, "nowe": 1, "wycofane": 0}
    karta = client.get(f"/assets/{k.id}").text
    # Przeglad: problemy i pojemnosc.
    assert "NetApp ONTAP" in karta and "wymaga uwagi" in karta and "agregat aggr1 zajęty w 90.0%" in karta
    assert "dyski w złym stanie: 1" in karta and "relacje SnapMirror niezdrowe: 1" in karta
    # Zakladka NetApp: zwiniete, sortowalne sekcje.
    assert 'data-tab="netapp">NetApp <span class="badge badge-warn"' in karta
    zakladka = karta.split('data-panel="netapp"', 1)[1].split("</section>", 1)[0]
    assert zakladka.count('<details class="panel wirt-klaster sekcja-karty">') == 9 and "<details open" not in zakladka
    for tekst in ("951913000123", "100 dni", "raid_dp", "NFS, ISCSI", "/vol_vmware01", "zajęte ≥90%: 1",
                  "/vol/vol_lun/lun0", "1.0.3 (półka SHFMS123, zatoka 3) — broken", "2 × 3.5 TB SSD", "NS224",
                  "10.70.1.10/24", "niezdrowa", "26h"):
        assert tekst in zakladka, tekst
    strona = client.get("/wirtualizacja").text
    assert "Pamięć masowa" in strona and "pod01-netapp01" in strona and "ONTAP 9.12.1P3" in strona
    assert [w["nazwa"] for w in client.get("/szukaj.json", params={"q": "S4ABC0003"}).json()["wyniki"]] == ["pod01-netapp01"]


def test_znikniecie_klastra_netapp(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev))
    wyslij(client, enrolled, odczyt(rev, ext_id="00000000-0000-0000-0000-000000000001", nazwa="inny"))
    with SessionLocal() as db:
        stary = db.scalars(select(Asset).where(Asset.hostname == "pod01-netapp01")).one()
        assert stary.lifecycle == "wycofany" and stary.retired_reason == "zniknął z ONTAP"
