"""Dell OpenManage Enterprise: serwery dopisane do istniejacych kart po Service Tagu."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AuditLog, NutanixObiekt
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, ustawienia_agenta, zarejestruj
from .test_review_improvements import enroll, send
from .test_vmware import odczyt as odczyt_vmware, polityka as polityka_vmware
from .test_vmware import wlacz as wlacz_vmware, wyslij as wyslij_vmware


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, **{"adres": "ome.firma.pl", "uzytkownik": "cmdb-ro", "nazwa": "OME DC1", **extra})
    odp = client.post(f"/assets/{asset_id}/ome", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="9" * 32):
    odp = client.get("/api/v1/agent/ome-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def serwer(tag, **zmiany):
    s = {"ext_id": tag, "nazwa": f"{tag.lower()}-idrac", "model": "PowerEdge R760", "zdrowie": "OK",
         "zasilanie": "ON", "polaczony": True, "idrac_ip": "10.70.0.11", "idrac_wersja": "7.10.30.00",
         "bios_wersja": "2.3.5", "cpu_model": "Intel(R) Xeon(R) Gold 6430", "gniazda": 2, "rdzenie": 64,
         "watki": 128, "ram_bajty": 1024 * 2**30, "mac": ["b0:26:28:aa:bb:01"],
         "dyski": [{"rozmiar_bajty": 2 * 2**40, "typ": "SSD SAS", "model": "KPM6", "numer_seryjny": "D1"},
                   {"rozmiar_bajty": 2 * 2**40, "typ": "SSD SAS", "model": "KPM6", "numer_seryjny": "D2"},
                   {"rozmiar_bajty": 2 * 2**40, "typ": "SSD SAS", "model": "KPM6", "numer_seryjny": "D3",
                    "stan": "Critical", "miejsce": "Disk 3 in Backplane 1"},
                   {"rozmiar_bajty": 750 * 2**30, "typ": "SSD PCIe", "model": "P4800X"}],
         "system": "VMware ESXi 8.0.3", "hostname_os": None,
         "pamiec": [{"slot": "DIMM A1", "rozmiar_bajty": 64 * 2**30, "typ": "DDR4", "predkosc": 3200,
                     "predkosc_robocza": 2933, "producent": "Hynix", "part": "HMAA8GR7", "numer_seryjny": "54C388A6"}],
         "porty": [{"port": "NIC.Integrated.1-1", "opis": "Broadcom BCM57414 25GbE", "mac": "b0:26:28:aa:bb:01",
                    "lacze": "Up", "predkosc_mbps": 25000}],
         "kontrolery": [{"nazwa": "PERC H750 Adapter", "opis": "RAID Controller in Slot 3", "firmware": "52.26.0",
                         "cache_mb": 8192, "dyski_wirtualne": 2, "uklady": "1 × RAID-0, 1 × RAID-1",
                         "wirtualne_z_bledem": ["VD1: Degraded"]}],
         "zasilacze": [{"nazwa": "Power Supply 2", "model": "PWR SPLY,750W", "moc_w": 750, "stan": "Failed"}],
         "karty_pcie": [{"slot": "Video.Embedded.1-1", "producent": "Matrox", "opis": "Integrated Matrox G200eW3"}],
         "licencje": [{"opis": "iDRAC9 Enterprise License", "typ": "Perpetual"}]}
    s.update(zmiany)
    return s


def odczyt(revision, urzadzenia):
    return {"protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 800,
            "wersja_pc": "4.1.0", "urzadzenia": urzadzenia}


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/ome", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user):
    enrolled = zarejestruj(client, tenant)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def test_konfiguracja_ome(client, tenant_a, make_user):
    enrolled, csrf, _ = _wlaczony(client, tenant_a, make_user)
    p = polityka(client, enrolled)["policy"]
    assert p["adres"] == "https://ome.firma.pl:443" and p["uzytkownik"] == "cmdb-ro" and "bez_tls" not in p
    with SessionLocal() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "ome.config_changed")).one()
    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=ome").text
    assert "Odczytuj OME z tej maszyny" in strona and "VIEWER" in strona and 'name="bez_tls"' not in strona
    # OME tylko po https.
    blad = client.post(f"/assets/{enrolled['asset_id']}/ome",
                       data=formularz(csrf, revision=polityka(client, enrolled)["revision"], adres="http://ome"),
                       follow_redirects=False)
    assert blad.headers["location"].endswith("nutanix_komunikat=adres#agent")


def test_serwer_z_ome_dopisuje_sie_do_hosta_esxi(client, tenant_a, make_user):
    """Host ESXi z vCenter ma numer seryjny ABC1234 - OME dopisuje sie do jego karty."""
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wlacz_vmware(client, csrf, enrolled["asset_id"])
    assert wyslij_vmware(client, enrolled, odczyt_vmware(polityka_vmware(client, enrolled)["revision"])) == "przyjeto"

    assert wyslij(client, enrolled, odczyt(rev, [serwer("ABC1234"), serwer("XYZ9876", idrac_ip="10.70.0.99",
                                                                          hostname_os="srv-backup")])) == "przyjeto"
    with SessionLocal() as db:
        esx = db.scalars(select(Asset).where(Asset.hostname == "esx01.firma.pl")).one()
        assert esx.zrodlo == "vmware"
        assert not db.scalars(select(Asset).where(Asset.zrodlo == "ome", Asset.serial_number == "ABC1234")).all()
        nowy = db.scalars(select(Asset).where(Asset.zrodlo == "ome")).one()
        assert (nowy.hostname, nowy.serial_number, nowy.manufacturer, nowy.typ) == ("srv-backup", "XYZ9876", "Dell", "komputer")
        assert ustawienia_agenta(db, enrolled["asset_id"], "ome").odczyt_liczby == {
            "serwery": 2, "dopasowane": 1, "nowe": 1, "wycofane": 0}

    karta = client.get(f"/assets/{esx.id}").text
    # Obie perspektywy na jednej karcie: wirtualizacja (vCenter) i sprzet (OME).
    assert "Identyfikator w vCenter" in karta and "Dell OME" in karta and "7.10.30.00" in karta
    assert "połączono z tą kartą po Service Tagu" in karta and "https://10.70.0.11" in karta
    # Dyski zgrupowane, a uszkodzony osobno z miejscem i numerem seryjnym.
    assert "3 × 2.0 TB" in karta and "1 × 750.0 GB" in karta
    assert "razem 4 dyski, 6.7 TB" in karta  # liczba z odmiana i laczna pojemnosc
    assert "<b>Critical</b>: Disk 3 in Backplane 1" in karta and "s/n D3" in karta
    # Zakladka "Sprzet (OME)" z pelnym inwentarzem i znacznikiem problemu.
    assert 'data-tab="ome">Sprzęt (OME) <span class="badge badge-warn"' in karta
    for tekst in ("DIMM A1", "2933 <span class=\"muted\">/ 3200</span> MT/s", "25.0 Gb/s", "PERC H750 Adapter",
                  "8.0 GB", "VD1: Degraded", "750 W", ">Failed<", "Integrated Matrox G200eW3", "iDRAC9 Enterprise License",
                  "Disk 3 in Backplane 1"):
        assert tekst in karta, tekst
    assert [w["nazwa"] for w in client.get("/szukaj.json", params={"q": "10.70.0.99"}).json()["wyniki"]] == ["srv-backup"]
    assert "OME DC1" not in client.get("/wirtualizacja").text  # OME to sprzet, nie zrodlo wirtualizacji


def test_instalacja_agenta_przepina_dane_ome(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rev, [serwer("SRV0002")]))
    # Na serwerze pojawia sie agent CMDB z tym samym numerem seryjnym.
    nowy_agent, raport = enroll(client, tenant_a, machine="serwer-z-ome-01")
    raport["identity"]["hostname"] = "SRV-APP02"
    raport["hardware"]["system"]["serial_number"] = "srv0002"
    assert send(client, nowy_agent, raport).status_code == 200
    wyslij(client, enrolled, odczyt(rev, [serwer("SRV0002")]))
    with SessionLocal() as db:
        z_ome = db.scalars(select(Asset).where(Asset.zrodlo == "ome")).one()
        assert z_ome.lifecycle == "wycofany" and z_ome.retired_reason == "ten sam Service Tag co karta: SRV-APP02"
        o = db.scalars(select(NutanixObiekt).where(NutanixObiekt.dostawca == "ome")).one()
        assert o.asset_id == nowy_agent["asset_id"]
    assert "Dell OME" in client.get(f"/assets/{nowy_agent['asset_id']}").text


def test_znikniecie_z_ome(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    nowy_agent, raport = enroll(client, tenant_a, machine="serwer-z-ome-02")
    raport["hardware"]["system"]["serial_number"] = "AGT0001"
    assert send(client, nowy_agent, raport).status_code == 200
    wyslij(client, enrolled, odczyt(rev, [serwer("AGT0001"), serwer("SAM0001")]))
    wyslij(client, enrolled, odczyt(rev, []))
    with SessionLocal() as db:
        assert db.scalars(select(Asset).where(Asset.zrodlo == "ome")).one().retired_reason == "zniknął z OME"
        # Karta z agentem zostaje aktywna - traci tylko sekcje OME.
        assert db.get(Asset, nowy_agent["asset_id"]).lifecycle == "aktywny"
    assert "Dell OME" not in client.get(f"/assets/{nowy_agent['asset_id']}").text
