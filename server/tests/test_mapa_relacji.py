"""Mapa relacji: dane grafu, izolacja firm i bezpieczne osadzenie nazw."""
from pathlib import Path

from cmdb_server.db import SessionLocal
from cmdb_server.models import Asset, AssetRelation
from .test_monitoring import zaloguj


def zasob(tenant_id, nazwa, typ, zrodlo="reczne"):
    with SessionLocal() as db:
        a = Asset(tenant_id=tenant_id, machine_id=f"test:{nazwa}", hostname=nazwa, typ=typ, zrodlo=zrodlo)
        db.add(a)
        db.commit()
        return a.id


def relacja(tenant_id, z, do, rodzaj):
    with SessionLocal() as db:
        db.add(AssetRelation(tenant_id=tenant_id, source_id=z, target_id=do, kind=rodzaj))
        db.commit()


def dane_mapy(client, zapytanie=""):
    odp = client.get("/relacje/mapa.json" + zapytanie)
    assert odp.status_code == 200, odp.text
    return odp.json()


def test_mapa_zawiera_graf_firmy_i_zaczyna_od_klastra(client, tenant_a, tenant_b, make_user):
    t = tenant_a["id"]
    klaster = zasob(t, "pod02", "klaster", "nutanix")
    host = zasob(t, "pod02-nutanix02", "host", "nutanix")
    vm = zasob(t, "FUDO-pod02", "vm", "nutanix")
    app = zasob(t, "FUDO PAM", "aplikacja")
    zasob(t, "samotny", "komputer")
    relacja(t, host, klaster, "host_cluster")
    relacja(t, vm, host, "vm_host")
    relacja(t, app, vm, "application_server")
    obcy = zasob(tenant_b["id"], "obcy", "klaster")
    zaloguj(client, tenant_a, make_user)

    odp = client.get("/relacje/mapa")
    assert odp.status_code == 200
    assert f'data-mapa-zrodlo="/relacje/mapa.json?zasob={klaster}&amp;kierunek=w-dol"' in odp.text
    dane = dane_mapy(client)
    assert {w["nazwa"]: w["kolumna"] for w in dane["wezly"]} == {
        "pod02": 0, "pod02-nutanix02": 1, "FUDO-pod02": 2, "FUDO PAM": 3}
    assert dane["zasob"] == klaster and dane["kierunek"] == "w-dol"
    assert len(dane["krawedzie"]) == 3
    assert 'src="/static/mapa.js' in odp.text

    dane = dane_mapy(client, f"?zasob={vm}&kierunek=w-gore")
    assert dane["zasob"] == vm and dane["kierunek"] == "w-gore"
    assert client.get(f"/relacje/mapa?zasob={obcy}").status_code == 404
    assert client.get(f"/relacje/mapa.json?zasob={obcy}").status_code == 404
    assert client.get("/relacje/mapa?kierunek=bokiem").status_code == 422


def test_nazwa_zasobu_trafia_tylko_do_danych(client, tenant_a, make_user):
    """Nazwa z raportu nie moze trafic do HTML strony - mapa wstawia ja przez textContent."""
    t = tenant_a["id"]
    zly = zasob(t, "</script><script>alert(1)</script>", "vm")
    host = zasob(t, "host-1", "host")
    relacja(t, zly, host, "vm_host")
    zaloguj(client, tenant_a, make_user)
    assert "<script>alert(1)" not in client.get("/relacje/mapa").text
    assert any(w["nazwa"].startswith("</script>") for w in dane_mapy(client)["wezly"])
    assert ".innerHTML" not in (Path(__file__).resolve().parent.parent
                               / "cmdb_server" / "static" / "mapa.js").read_text(encoding="utf-8")


def test_bez_relacji_jest_podpowiedz_i_odnosniki(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    assert "Nie ma jeszcze żadnych relacji" in client.get("/relacje/mapa").text
    assert 'href="/relacje/mapa"' in client.get("/relacje").text
