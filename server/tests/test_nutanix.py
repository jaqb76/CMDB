"""Nutanix Prism Central: konfiguracja, polityka dla agenta i wlaczanie odczytu do ewidencji."""
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    Asset, AssetChange, AssetRelation, AuditLog, NutanixObiekt, NutanixUstawienia,
)
from cmdb_server.services import nutanix
from .test_monitoring import zaloguj
from .test_review_improvements import enroll, send

VM_Z_AGENTEM = "0c53f7eb-5cd6-44d4-7a4c-f16f4fce86bc"
VM_BEZ_AGENTA = "11111111-2222-3333-4444-555555555555"
KLASTER = "00061c8e-aaaa-bbbb-cccc-000000000001"
HOST_A = "aaaaaaaa-0000-0000-0000-00000000000a"
HOST_B = "bbbbbbbb-0000-0000-0000-00000000000b"


def naglowki(enrolled):
    return {"Authorization": "Bearer " + enrolled["agent_token"]}


def zarejestruj(client, tenant, machine="bastion-1", uuid=None, hostname="bastionjps"):
    enrolled, report = enroll(client, tenant, machine=machine)
    report["identity"]["hostname"] = hostname
    if uuid:
        report["hardware"]["system"]["uuid"] = uuid.upper()
        report["hardware"]["system"]["serial_number"] = uuid.upper()
    assert send(client, enrolled, report).status_code == 200
    return enrolled


def formularz(csrf, revision="unassigned", **extra):
    return {"csrf_token": csrf, "revision": revision, "wlaczona": "true",
            "adres": "prism.firma.pl", "uzytkownik": "cmdb-ro", "haslo": "tajne-haslo-1",
            "ca_pem": "", "interwal_minut": "60", **extra}


def wlacz(client, csrf, asset_id, **extra):
    odp = client.post(f"/assets/{asset_id}/nutanix", data=formularz(csrf, **extra), follow_redirects=False)
    assert odp.status_code == 303, odp.text
    return odp


def polityka(client, enrolled, nonce="a" * 32):
    odp = client.get("/api/v1/agent/nutanix-policy?nonce=" + nonce, headers=naglowki(enrolled))
    assert odp.status_code == 200, odp.text
    return odp.json()


def odczyt(revision, vm=None, hosty=None, **extra):
    return {
        "protocol": 1, "revision": revision, "rodzaj": "odczyt", "ok": True, "czas_ms": 3800,
        "wersja_pc": "pc.2024.3.1",
        "klastry": [{"ext_id": KLASTER, "nazwa": "RCHO-AHV-01", "wersja": "6.10.1", "liczba_hostow": 2}],
        "hosty": hosty if hosty is not None else [
            {"ext_id": HOST_A, "nazwa": "NTNX-A", "klaster_id": KLASTER, "model": "NX-3170-G8",
             "numer_seryjny": "24SM3A410012", "ram_bajty": 768 * 2**30, "hipernadzorca": "AHV 20230302"},
            {"ext_id": HOST_B, "nazwa": "NTNX-B", "klaster_id": KLASTER, "model": "NX-3170-G8"},
        ],
        "vm": vm if vm is not None else [
            {"ext_id": VM_Z_AGENTEM, "nazwa": "bastionjps", "klaster_id": KLASTER, "host_id": HOST_A,
             "stan": "ON", "gniazda": 2, "rdzenie_na_gniazdo": 2, "ram_bajty": 8 * 2**30},
            {"ext_id": VM_BEZ_AGENTA, "nazwa": "sql01", "klaster_id": KLASTER, "host_id": HOST_A,
             "stan": "ON", "system": "Windows Server 2019",
             "karty": [{"mac": "50:6b:8d:00:00:01", "ip": ["10.20.0.50"]}]},
        ],
        **extra,
    }


def wyslij(client, enrolled, dane):
    odp = client.post("/api/v1/agent/nutanix", headers=naglowki(enrolled), json=dane)
    assert odp.status_code == 200, odp.text
    return odp.json()["wynik"]


# --- konfiguracja i polityka ------------------------------------------------

def test_wylaczona_funkcja_nie_wydaje_niczego(client, tenant_a):
    enrolled = zarejestruj(client, tenant_a)
    odp = polityka(client, enrolled)
    assert odp["policy"] == {"enabled": False, "test": False}
    assert odp["asset_id"] == enrolled["asset_id"] and odp["nonce"] == "a" * 32
    assert client.get("/api/v1/agent/nutanix-policy?nonce=" + "a" * 32).status_code == 401


def test_zapis_konfiguracji_szyfruje_haslo_i_trafia_do_audytu(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = wlacz(client, csrf, enrolled["asset_id"])
    assert odp.headers["location"].endswith("?funkcja=nutanix#agent")
    with SessionLocal() as db:
        row = db.get(NutanixUstawienia, enrolled["asset_id"])
        assert row.adres == "https://prism.firma.pl:9440"
        assert row.haslo and "tajne" not in row.haslo
        wpis = db.scalars(select(AuditLog).where(AuditLog.action == "nutanix.config_changed")).one()
        assert "tajne" not in str(wpis.detail) and wpis.detail["haslo_zmienione"] is True

    p = polityka(client, enrolled)
    assert p["policy"]["enabled"] is True and p["policy"]["haslo"] == "tajne-haslo-1"
    assert p["policy"]["adres"] == "https://prism.firma.pl:9440"
    assert p["policy"]["interwal_sekund"] == 3600

    # Puste haslo przy kolejnym zapisie zostawia poprzednie.
    wlacz(client, csrf, enrolled["asset_id"], revision=p["revision"], haslo="", interwal_minut="15")
    p2 = polityka(client, enrolled, nonce="b" * 32)
    assert p2["policy"]["haslo"] == "tajne-haslo-1" and p2["policy"]["interwal_sekund"] == 900
    assert p2["revision"] != p["revision"]


def test_bledny_adres_i_konflikt_rewizji(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    odp = client.post(f"/assets/{enrolled['asset_id']}/nutanix",
                      data=formularz(csrf, adres="http://prism.firma.pl"), follow_redirects=False)
    assert odp.headers["location"].endswith("?funkcja=nutanix&nutanix_komunikat=adres#agent")
    assert "Niepoprawny adres Prism Central" in client.get(odp.headers["location"]).text
    # Dowolny tekst w adresie nie trafia na strone - tylko znane kody.
    obcy = client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix&nutanix_komunikat=zadzwon").text
    assert "zadzwon" not in obcy.split('data-panel="agent"')[1]
    with SessionLocal() as db:
        assert db.get(NutanixUstawienia, enrolled["asset_id"]) is None
    wlacz(client, csrf, enrolled["asset_id"])
    assert client.post(f"/assets/{enrolled['asset_id']}/nutanix", data=formularz(csrf)).status_code == 409
    assert client.post(f"/assets/{enrolled['asset_id']}/nutanix",
                       data=formularz(csrf, interwal_minut="1")).status_code in (409, 422)


def test_widz_i_obca_firma_nie_zmieniaja_konfiguracji(client, tenant_a, tenant_b, make_user):
    a = zarejestruj(client, tenant_a, machine="maszyna-a-1")
    b = zarejestruj(client, tenant_b, machine="maszyna-b-1")
    _, csrf = zaloguj(client, tenant_a, make_user, role="viewer")
    assert client.post(f"/assets/{a['asset_id']}/nutanix", data=formularz(csrf)).status_code == 403
    client.cookies.clear()
    _, csrf = zaloguj(client, tenant_a, make_user, email="admin2@example.com")
    assert client.post(f"/assets/{b['asset_id']}/nutanix", data=formularz(csrf)).status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(NutanixUstawienia)) is None


def test_test_polaczenia_jedzie_do_agenta_i_wraca_z_wynikiem(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    # Konfiguracja zapisana, ale odczyt wylaczony - test i tak ma przejsc.
    wlacz(client, csrf, enrolled["asset_id"], wlaczona="false")
    assert polityka(client, enrolled)["policy"]["enabled"] is False
    assert client.post(f"/assets/{enrolled['asset_id']}/nutanix/test",
                       data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    p = polityka(client, enrolled, nonce="c" * 32)
    assert p["policy"]["test"] is True and p["policy"]["enabled"] is False
    assert p["policy"]["haslo"] == "tajne-haslo-1"
    assert "Test zlecony" in client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix").text

    assert wyslij(client, enrolled, {"protocol": 1, "revision": p["revision"], "rodzaj": "test",
                                     "ok": True, "wersja_pc": "pc.2024.3.1", "czas_ms": 120,
                                     "klastry": [{"ext_id": KLASTER, "nazwa": "K"}]}) == "przyjeto"
    assert polityka(client, enrolled, nonce="d" * 32)["policy"] == {"enabled": False, "test": False}
    tresc = client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix").text
    assert "polaczenie OK" in tresc and "pc.2024.3.1" in tresc


# --- synchronizacja ---------------------------------------------------------

def _wlaczony(client, tenant, make_user, uuid=VM_Z_AGENTEM):
    enrolled = zarejestruj(client, tenant, uuid=uuid)
    _, csrf = zaloguj(client, tenant, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    return enrolled, polityka(client, enrolled)["revision"]


def test_odczyt_zaklada_klaster_hosty_i_vm_oraz_laczy_z_agentem(client, tenant_a, make_user):
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt(rewizja)) == "przyjeto"
    with SessionLocal() as db:
        zasoby = {a.hostname: a for a in db.scalars(select(Asset))}
        # Maszyna z agentem nie dostala drugiego wpisu.
        assert [a.zrodlo for a in zasoby.values() if a.hostname == "bastionjps"] == ["agent"]
        assert zasoby["sql01"].zrodlo == "nutanix" and zasoby["sql01"].typ == "vm"
        assert zasoby["sql01"].primary_ip == "10.20.0.50"
        assert zasoby["NTNX-A"].typ == "host" and zasoby["RCHO-AHV-01"].typ == "klaster"
        relacje = {(r.source.hostname, r.target.hostname, r.kind) for r in db.scalars(select(AssetRelation))}
        assert relacje == {
            ("NTNX-A", "RCHO-AHV-01", "host_cluster"), ("NTNX-B", "RCHO-AHV-01", "host_cluster"),
            ("bastionjps", "NTNX-A", "vm_host"), ("sql01", "NTNX-A", "vm_host"),
        }
        row = db.get(NutanixUstawienia, enrolled["asset_id"])
        assert row.odczyt_ok and row.odczyt_liczby["vm_z_agentem"] == 1 and row.odczyt_liczby["vm"] == 2

    karta = client.get(f"/assets/{enrolled['asset_id']}").text
    assert "Wirtualizacja" in karta and "połączono z raportem agenta po UUID" in karta
    widok = client.get("/wirtualizacja").text
    assert "RCHO-AHV-01" in widok and "sql01" in widok and "NTNX-A" in widok


def test_uuid_z_odwrocona_kolejnoscia_bajtow_tez_sie_laczy(client, tenant_a, make_user):
    odwrocony = "ebf7530c-d65c-d444-7a4c-f16f4fce86bc"
    assert odwrocony in nutanix.warianty_uuid(VM_Z_AGENTEM)
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user, uuid=odwrocony)
    wyslij(client, enrolled, odczyt(rewizja))
    with SessionLocal() as db:
        assert db.scalar(select(Asset).where(Asset.hostname == "bastionjps", Asset.zrodlo == "nutanix")) is None


def test_migracja_vm_przepina_host_i_trafia_do_historii(client, tenant_a, make_user):
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rewizja))
    dane = odczyt(rewizja)
    dane["vm"][1]["host_id"] = HOST_B
    wyslij(client, enrolled, dane)
    with SessionLocal() as db:
        sql = db.scalar(select(Asset).where(Asset.hostname == "sql01"))
        hosty = [r.target.hostname for r in db.scalars(select(AssetRelation).where(
            AssetRelation.source_id == sql.id, AssetRelation.kind == "vm_host"))]
        assert hosty == ["NTNX-B"]
        zmiana = db.scalars(select(AssetChange).where(AssetChange.asset_id == sql.id)).one()
        assert (zmiana.old_value, zmiana.new_value) == ("NTNX-A", "NTNX-B")


def test_znikniecie_wycofuje_wpis_a_powrot_przywraca(client, tenant_a, make_user):
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rewizja))
    dane = odczyt(rewizja)
    dane["vm"] = dane["vm"][:1]
    wyslij(client, enrolled, dane)
    with SessionLocal() as db:
        sql = db.scalar(select(Asset).where(Asset.hostname == "sql01"))
        assert sql.lifecycle == "wycofany" and sql.retired_by == "nutanix"
        assert db.scalar(select(AssetRelation).where(AssetRelation.source_id == sql.id)) is None
        # Maszyna z agentem nigdy nie jest wycofywana przez odczyt.
        assert db.get(Asset, enrolled["asset_id"]).lifecycle == "aktywny"
    wyslij(client, enrolled, odczyt(rewizja))
    with SessionLocal() as db:
        sql = db.scalar(select(Asset).where(Asset.hostname == "sql01"))
        assert sql.lifecycle == "aktywny" and sql.retired_by is None


def test_nieudany_odczyt_niczego_nie_wycofuje(client, tenant_a, make_user):
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rewizja))
    wyslij(client, enrolled, {"protocol": 1, "revision": rewizja, "rodzaj": "odczyt", "ok": False,
                              "blad": "HTTP 401: nieprawidlowe dane logowania"})
    with SessionLocal() as db:
        assert db.scalar(select(Asset).where(Asset.hostname == "sql01")).lifecycle == "aktywny"
        row = db.get(NutanixUstawienia, enrolled["asset_id"])
        assert row.odczyt_ok is False and "401" in row.odczyt_blad


def test_odczyt_ze_starej_konfiguracji_jest_pomijany(client, tenant_a, make_user):
    enrolled, _ = _wlaczony(client, tenant_a, make_user)
    assert wyslij(client, enrolled, odczyt("inna-rewizja")) == "pominieto"
    with SessionLocal() as db:
        assert db.scalar(select(NutanixObiekt)) is None


def test_vm_bez_agenta_po_instalacji_agenta_laczy_sie_z_jego_karta(client, tenant_a, make_user):
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rewizja))
    # Na sql01 instalujemy agenta - jego UUID rowna sie ext_id VM.
    sql_agent = zarejestruj(client, tenant_a, machine="maszyna-sql-1", uuid=VM_BEZ_AGENTA, hostname="sql01")
    wyslij(client, enrolled, odczyt(rewizja))
    with SessionLocal() as db:
        wpisy = db.scalars(select(Asset).where(Asset.hostname == "sql01")).all()
        stary = next(a for a in wpisy if a.zrodlo == "nutanix")
        assert stary.lifecycle == "wycofany" and "sql01" in stary.retired_reason
        obiekt = db.scalar(select(NutanixObiekt).where(NutanixObiekt.ext_id == VM_BEZ_AGENTA))
        assert obiekt.asset_id == sql_agent["asset_id"]


def test_odczyt_innej_firmy_nie_dotyka_cudzych_maszyn(client, tenant_a, tenant_b, make_user):
    # Maszyna z tym samym UUID w firmie B nie moze zostac polaczona z odczytem firmy A.
    zarejestruj(client, tenant_b, machine="maszyna-b-1", uuid=VM_BEZ_AGENTA, hostname="obca")
    enrolled, rewizja = _wlaczony(client, tenant_a, make_user)
    wyslij(client, enrolled, odczyt(rewizja))
    with SessionLocal() as db:
        obiekt = db.scalar(select(NutanixObiekt).where(NutanixObiekt.ext_id == VM_BEZ_AGENTA))
        assert db.get(Asset, obiekt.asset_id).tenant_id == tenant_a["id"]
        assert all(o.tenant_id == tenant_a["id"] for o in db.scalars(select(NutanixObiekt)))


def test_filtr_listy_i_czeka_na_odebranie(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    zarejestruj(client, tenant_a, machine="maszyna-inna", hostname="inna")
    _, csrf = zaloguj(client, tenant_a, make_user)
    wlacz(client, csrf, enrolled["asset_id"])
    assert "data-funkcje-czekaja" in client.get(f"/assets/{enrolled['asset_id']}").text
    polityka(client, enrolled)
    assert "data-funkcje-czekaja" not in client.get(f"/assets/{enrolled['asset_id']}").text
    lista = client.get("/assets?funkcja=nutanix").text
    assert "bastionjps" in lista and ">inna<" not in lista


def test_odczytaj_teraz_zleca_odczyt_az_do_wyniku(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    wlacz(client, csrf, enrolled["asset_id"], wlaczona="false")
    # Przy wylaczonym odczycie nie ma czego zlecac.
    odp = client.post(f"/assets/{enrolled['asset_id']}/nutanix/odczyt",
                      data={"csrf_token": csrf}, follow_redirects=False)
    assert odp.headers["location"].endswith("nutanix_komunikat=wylaczona#agent")

    rewizja = polityka(client, enrolled)["revision"]
    wlacz(client, csrf, enrolled["asset_id"], revision=rewizja, haslo="")
    p = polityka(client, enrolled, nonce="b" * 32)
    assert p["policy"]["odczyt_teraz"] is False
    assert "Odczytaj teraz" in client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix").text

    assert client.post(f"/assets/{enrolled['asset_id']}/nutanix/odczyt",
                       data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert polityka(client, enrolled, nonce="c" * 32)["policy"]["odczyt_teraz"] is True
    assert "Odczyt zlecony" in client.get(f"/assets/{enrolled['asset_id']}?funkcja=nutanix").text

    wyslij(client, enrolled, odczyt(p["revision"]))
    assert polityka(client, enrolled, nonce="d" * 32)["policy"]["odczyt_teraz"] is False
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "nutanix.read_requested")) is not None


def test_widz_nie_zleca_odczytu(client, tenant_a, make_user):
    enrolled = zarejestruj(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user, role="viewer")
    assert client.post(f"/assets/{enrolled['asset_id']}/nutanix/odczyt",
                       data={"csrf_token": csrf}).status_code == 403
