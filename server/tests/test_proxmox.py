"""Proxmox VE: ta sama droga co vCenter, plus wezel z agentem i kontener LXC po nazwie."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetRelation, AuditLog, NutanixObiekt
from .test_monitoring import zaloguj
from .test_nutanix import formularz, naglowki, polityka as polityka_nutanix, ustawienia_agenta, zarejestruj
from .test_review_improvements import enroll, send

# smbios1 maszyny QEMU = UUID, ktory agent widzi w gosciu.
VM_Z_AGENTEM = "6f2e8c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"


def wlacz(client, csrf, asset_id, **extra):
    dane = formularz(csrf, **{"adres": "pve01.firma.pl", "uzytkownik": "cmdb@pve!cmdb", **extra})
    odp = client.post(f"/assets/{asset_id}/proxmox", data=dane, follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="e" * 32):
    odp = client.get("/api/v1/agent/proxmox-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, vm=None, hosty=None):
    return {
        "protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 700,
        "wersja_pc": "8.2.4",
        "klastry": [{"ext_id": "pve-dc1", "nazwa": "pve-dc1", "wersja": "8.2.4", "hipernadzorca": "Proxmox VE",
                     "liczba_hostow": 2}],
        "hosty": hosty if hosty is not None else [
            {"ext_id": "pve01", "nazwa": "pve01", "klaster_id": "pve-dc1", "ip": "10.20.0.11",
             "hipernadzorca": "Proxmox VE 8.2.4 (jądro 6.8.12-1-pve)", "cpu_model": "AMD EPYC 7313",
             "gniazda": 2, "rdzenie": 32, "watki": 64, "ram_bajty": 512 * 2**30},
            {"ext_id": "pve02", "nazwa": "pve02", "klaster_id": "pve-dc1", "ip": "10.20.0.12"},
        ],
        "vm": vm if vm is not None else [
            {"ext_id": "100", "nazwa": "app-01", "klaster_id": "pve-dc1", "host_id": "pve01", "stan": "ON",
             "bios_uuid": VM_Z_AGENTEM, "typ": "QEMU/KVM", "gniazda": 1, "rdzenie_na_gniazdo": 4},
            {"ext_id": "101", "nazwa": "win-01", "klaster_id": "pve-dc1", "host_id": "pve02", "stan": "OFF",
             "typ": "QEMU/KVM", "system": "Windows 11/2022/2025", "projekt": "produkcja",
             "bios_uuid": "0c53f7eb-0000-4000-8000-000000000101"},
            {"ext_id": "200", "nazwa": "dns-01", "klaster_id": "pve-dc1", "host_id": "pve01", "stan": "ON",
             "typ": "LXC", "system": "Debian",
             "karty": [{"mac": "bc:24:11:cc:dd:01", "ip": ["10.20.5.53"], "siec": "vmbr0"}]},
        ],
    }


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/proxmox", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


def _wlaczony(client, tenant, make_user):
    enrolled = zarejestruj(client, tenant, uuid=VM_Z_AGENTEM)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    return enrolled, csrf, polityka(client, enrolled)["revision"]


def _agent(client, tenant, maszyna, hostname):
    e, report = enroll(client, tenant, machine=maszyna)
    report["identity"]["hostname"] = hostname
    assert send(client, e, report).status_code == 200
    return e


def test_konfiguracja_proxmox_z_tokenem_api(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = wlacz(client, csrf, enrolled["asset_id"])
    assert odp.headers["location"].endswith("?funkcja=proxmox#agent")
    p = polityka(client, enrolled)
    assert p["policy"]["enabled"] is True and p["policy"]["adres"] == "https://pve01.firma.pl:8006"
    assert p["policy"]["uzytkownik"] == "cmdb@pve!cmdb" and "bez_tls" not in p["policy"]
    assert polityka_nutanix(client, enrolled)["policy"] == {"enabled": False, "test": False}
    with SessionLocal() as db:
        assert ustawienia_agenta(db, enrolled["asset_id"], "proxmox").haslo
        assert db.scalars(select(AuditLog).where(AuditLog.action == "proxmox.config_changed")).one()

    strona = client.get(f"/assets/{enrolled['asset_id']}?funkcja=proxmox").text
    assert "Odczytuj Proxmox VE z tej maszyny" in strona and "ID tokenu API" in strona
    assert "PVEAuditor" in strona and "pve-root-ca.pem" in strona
    assert f'/assets/{enrolled["asset_id"]}/proxmox/test' in strona

    # http nie przechodzi - Proxmox zawsze ma TLS.
    blad = client.post(f"/assets/{enrolled['asset_id']}/proxmox",
                       data=formularz(csrf, revision=p["revision"], adres="http://pve01:8006", bez_tls="true"),
                       follow_redirects=False)
    assert blad.headers["location"].endswith("?funkcja=proxmox&nutanix_komunikat=adres#agent")


def test_odczyt_proxmoxa_zaklada_drzewo_i_laczy_vm_z_agentem(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        obiekty = db.scalars(select(NutanixObiekt)).all()
        assert {o.dostawca for o in obiekty} == {"proxmox"}
        assert all("/" in o.ext_id for o in obiekty)  # przestrzen adresu Proxmoxa
        agent = db.get(Asset, enrolled["asset_id"])
        win = db.scalars(select(Asset).where(Asset.hostname == "win-01")).one()
        assert (win.zrodlo, win.manufacturer, win.model) == ("proxmox", "Proxmox", "QEMU/KVM")
        dns = db.scalars(select(Asset).where(Asset.hostname == "dns-01")).one()
        assert dns.model == "LXC" and dns.primary_ip == "10.20.5.53"
        klaster = db.scalars(select(Asset).where(Asset.typ == "klaster")).one()
        assert klaster.zrodlo == "proxmox" and klaster.hostname == "pve-dc1" and klaster.os_version == "8.2.4"
        relacje = {(r.source.hostname, r.kind, r.target.hostname, r.created_by)
                   for r in db.scalars(select(AssetRelation))}
        assert (agent.hostname, "vm_host", "pve01", "proxmox") in relacje
        assert ("win-01", "vm_host", "pve02", "proxmox") in relacje
        assert ("pve01", "host_cluster", "pve-dc1", "proxmox") in relacje
        assert ustawienia_agenta(db, enrolled["asset_id"], "proxmox").odczyt_liczby["vm_z_agentem"] == 1

    strona = client.get("/wirtualizacja").text
    assert "Proxmox VE" in strona and "pve-dc1" in strona and "pula produkcja" in strona
    karta = client.get(f"/assets/{win.id}").text
    assert "odczytany z Proxmox VE" in karta and "Identyfikator w Proxmox VE (VMID)" in karta
    assert "<dt>Typ</dt><dd>QEMU/KVM</dd>" in karta and "<dt>Pula</dt><dd>produkcja</dd>" in karta

    # Znikniecie z Proxmoxa wycofuje - z wlasnym powodem.
    assert wyslij(client, enrolled, odczyt(rev, vm=odczyt(rev)["vm"][:1])) == "przyjeto"
    with SessionLocal() as db:
        win = db.scalars(select(Asset).where(Asset.hostname == "win-01")).one()
        assert win.lifecycle == "wycofany" and win.retired_reason == "zniknęła z Proxmox VE"


def test_wezel_i_kontener_z_agentem_nie_dostaja_drugiej_karty(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    wezel = _agent(client, tenant_a, "pve-wezel-01", "pve01")
    kontener = _agent(client, tenant_a, "lxc-dns-01", "dns-01")
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        assert not db.scalars(select(Asset).where(Asset.zrodlo == "proxmox", Asset.typ == "host",
                                                  Asset.hostname == "pve01")).all()
        assert not db.scalars(select(Asset).where(Asset.zrodlo == "proxmox", Asset.hostname == "dns-01")).all()
        relacje = {(r.source_id, r.kind, r.target_id) for r in db.scalars(select(AssetRelation))}
        assert (kontener["asset_id"], "vm_host", wezel["asset_id"]) in relacje
        assert ustawienia_agenta(db, enrolled["asset_id"], "proxmox").odczyt_liczby["vm_z_agentem"] == 2
    assert "połączono z raportem agenta po nazwie hosta" in client.get(f"/assets/{kontener['asset_id']}").text
    assert "połączono z raportem agenta po UUID" in client.get(f"/assets/{enrolled['asset_id']}").text


def test_maszyna_qemu_z_uuid_nie_laczy_sie_po_nazwie(client, tenant_a, make_user):
    """Nazwa laczy tylko maszyny bez UUID (LXC) - VM z UUID czeka na agenta z tym UUID."""
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    obcy = _agent(client, tenant_a, "inny-win-01", "win-01")
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    with SessionLocal() as db:
        assert db.scalars(select(Asset).where(Asset.zrodlo == "proxmox", Asset.hostname == "win-01")).one()
        assert not db.scalars(select(AssetRelation).where(AssetRelation.source_id == obcy["asset_id"])).all()


def test_dwa_klastry_z_tymi_samymi_vmid(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    wlacz(client, csrf, enrolled["asset_id"], adres="pve-dr.firma.pl", nazwa="DR")
    lista = polityka(client, enrolled)["polaczenia"]
    rev_dr = next(p["revision"] for p in lista if p["nazwa"] == "DR")
    pol_dr = next(p["id"] for p in lista if p["nazwa"] == "DR")
    assert wyslij(client, enrolled, odczyt(rev)) == "przyjeto"
    assert wyslij(client, enrolled, dict(odczyt(rev_dr, vm=[dict(odczyt(rev_dr)["vm"][1], nazwa="win-dr")]),
                                         polaczenie_id=pol_dr)) == "przyjeto"
    with SessionLocal() as db:
        vm101 = db.scalars(select(NutanixObiekt).where(NutanixObiekt.rodzaj == "vm",
                                                        NutanixObiekt.ext_id.like("%/101"))).all()
        assert sorted(o.nazwa for o in vm101) == ["win-01", "win-dr"]
        assert db.scalars(select(Asset).where(Asset.hostname == "win-01")).one().lifecycle == "aktywny"


def test_test_polaczenia_podaje_wezly(client, tenant_a, make_user):
    enrolled, csrf, rev = _wlaczony(client, tenant_a, make_user)
    with SessionLocal() as db:
        pol = ustawienia_agenta(db, enrolled["asset_id"], "proxmox")
    client.post(f"/assets/{enrolled['asset_id']}/proxmox/test",
                data={"csrf_token": csrf, "polaczenie_id": pol.id}, follow_redirects=False)
    wynik = {"protocol": 1, "revision": rev, "polaczenie_id": pol.id, "rodzaj": "test", "ok": True,
             "wersja_pc": "8.2.4", "czas_ms": 40, "klastry": [],
             "hosty": [{"ext_id": "pve-solo", "nazwa": "pve-solo"}]}
    assert wyslij(client, enrolled, wynik) == "przyjeto"
    with SessionLocal() as db:
        opis = ustawienia_agenta(db, enrolled["asset_id"], "proxmox").test_opis
    assert opis == "polaczenie OK · Proxmox VE 8.2.4 · klastry: 0 · hosty: 1 · 40 ms"


def test_lista_zasobow_pokazuje_stan_i_przydzial_vm(client, tenant_a, make_user):
    enrolled, _, rev = _wlaczony(client, tenant_a, make_user)
    vm = odczyt(rev)["vm"]
    vm[1].update(gniazda=2, rdzenie_na_gniazdo=2, ram_bajty=4 * 2**30,
                 dyski=[{"rozmiar_bajty": 64 * 2**30}])
    assert wyslij(client, enrolled, odczyt(rev, vm=vm)) == "przyjeto"

    lista = client.get("/assets").text
    assert "<h1>Zasoby" in lista and "<th>Stan</th>" in lista
    assert '<span class="badge badge-ok">włączona</span>' in lista
    assert '<span class="badge">wyłączona</span>' in lista
    # VM bez agenta: podzespoly z przydzialu hypervisora.
    assert "4 vCPU" in lista and "4.0 GB RAM" in lista and "64.0 GB" in lista

    wlaczone = client.get("/assets?state=wlaczone").text
    assert "dns-01" in wlaczone and "win-01" not in wlaczone
    # Maszyna z agentem tez ma stan z platformy.
    assert "bastionjps" in wlaczone
    wylaczone = client.get("/assets?state=wylaczone").text
    assert "win-01" in wylaczone and "dns-01" not in wylaczone and "pve01" not in wylaczone
