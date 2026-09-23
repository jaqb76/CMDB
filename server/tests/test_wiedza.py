"""Baza wiedzy: zapis z wersjami, dopasowanie maszyn, uprawnienia, izolacja.

Czesc testow odpowiada wprost kryteriom akceptacji przeniesionym z poprzedniego
systemu (hubzso) - ich nazwy zaczynaja sie od ``test_kryterium_``.
Przyklady nazw hostow i systemow sa wylacznie generyczne (*.example.local).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal, engine
from cmdb_server.models import (
    Asset,
    AssetCurrentReport,
    WiedzaArtykul,
    WiedzaPrzestrzen,
    WiedzaSlownik,
    WiedzaWersja,
    WiedzaZalacznik,
    WpisSlownika,
    utcnow,
)
from cmdb_server.security import issue_csrf_token
from cmdb_server.services import wiedza_dopasowanie as dopasowanie
from cmdb_server.services import wiedza_tresc as tresc

HASLO = "haslo-do-bazy-wiedzy-123"


# --- zaplecze ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def katalog_wiedzy(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "wiedza_dir", str(tmp_path / "wiedza"))
    return tmp_path / "wiedza"


def zaloguj(client, tenant, make_user, role="admin", email="wiedza@example.local"):
    uid = make_user(tenant["id"], email, HASLO, role)
    assert client.post("/login", data={"email": email, "password": HASLO},
                       follow_redirects=False).status_code == 303
    return issue_csrf_token(uid)


def maszyna(tenant_id, hostname, *, fqdn=None, os_name=None, typ="komputer", tagi=None,
            pakiety=None, lokalizacja=None) -> str:
    with SessionLocal() as db:
        lok_id = None
        if lokalizacja:
            wpis = WpisSlownika(tenant_id=tenant_id, kategoria="lokalizacja",
                                wartosc=lokalizacja, klucz=lokalizacja.lower())
            db.add(wpis)
            db.flush()
            lok_id = wpis.id
        asset = Asset(tenant_id=tenant_id, machine_id=f"m-{hostname}", hostname=hostname,
                      fqdn=fqdn, os_name=os_name, typ=typ, tags=tagi, lokalizacja_id=lok_id)
        db.add(asset)
        db.flush()
        if pakiety is not None:
            db.add(AssetCurrentReport(
                asset_id=asset.id, tenant_id=tenant_id, collected_at=utcnow(), payload_hash="x",
                payload={"software": {"packages": [{"name": p} for p in pakiety]}},
            ))
        db.commit()
        return asset.id


def formularz(csrf, **pola):
    dane = {
        "csrf_token": csrf, "tytul": "Restart usługi system-a",
        "streszczenie": "Co zrobić, gdy system-a przestaje odpowiadać.",
        "tresc": "## Kroki\n\n1. Zaloguj się na {hostname}\n2. Zrestartuj usługę",
        "kategoria": "runbooks", "przeglad_co_mies": "6",
    }
    dane.update(pola)
    return dane


def utworz(client, csrf, **pola) -> str:
    odpowiedz = client.post("/wiedza/nowy", data=formularz(csrf, **pola), follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text
    return odpowiedz.headers["location"].split("/wiedza/a/")[1].split("?")[0]


def pola_edycji(client, artykul_id) -> dict:
    """Stan formularza tak, jak go wysyla przegladarka po otwarciu edycji."""
    with SessionLocal() as db:
        a = db.get(WiedzaArtykul, artykul_id)
        tagi = ", ".join(w.wartosc for w in a.slownik if w.rodzaj == "tag")
        systemy = ", ".join(w.wartosc for w in a.slownik if w.rodzaj == "system")
        return {
            "tytul": a.tytul, "streszczenie": a.streszczenie, "tresc": a.tresc,
            "kategoria": a.kategoria, "przestrzen_id": a.przestrzen_id,
            "rodzic_id": a.rodzic_id or "", "tagi": tagi, "systemy": systemy,
            "warunek_pole": [w["pole"] for w in a.dotyczy or []],
            "warunek_wartosc": [w["wartosc"] for w in a.dotyczy or []],
            "notatki_zalacznikow": a.notatki_zalacznikow or "",
            "notatki_diagramu": a.notatki_diagramu or "",
            "wlasciciel": a.wlasciciel or "", "przeglad_co_mies": str(a.przeglad_co_mies),
            "na_dyzur": "1" if a.na_dyzur else "", "wersja": str(a.wersja),
        }


def zapisz(client, csrf, artykul_id, **zmiany):
    dane = {**pola_edycji(client, artykul_id), "csrf_token": csrf, **zmiany}
    return client.post(f"/wiedza/a/{artykul_id}", data=dane, follow_redirects=False)


def wersje(artykul_id) -> list[WiedzaWersja]:
    with SessionLocal() as db:
        return list(db.execute(select(WiedzaWersja).where(WiedzaWersja.artykul_id == artykul_id)
                               .order_by(WiedzaWersja.wersja)).scalars())


# --- kryteria akceptacji ---------------------------------------------------------------

def test_kryterium_utworzony_artykul_ma_wszystkie_pola(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(
        client, csrf, tagi="restart, klaster", systemy="system-a, host01.example.local",
        warunek_pole=["host", "os"], warunek_wartosc=["db0*", "Windows Server 2019"],
        notatki_zalacznikow="schemat w PDF", notatki_diagramu="diagrams/a.drawio",
        przeglad_co_mies="3", na_dyzur="1", kategoria="troubleshooting",
    )
    with SessionLocal() as db:
        a = db.get(WiedzaArtykul, artykul_id)
        assert a.tytul == "Restart usługi system-a"
        assert a.streszczenie.startswith("Co zrobić")
        assert "{hostname}" in a.tresc
        assert a.kategoria == "troubleshooting"
        assert sorted(a.tagi) == ["klaster", "restart"]
        assert sorted(a.systemy) == ["host01.example.local", "system-a"]
        assert [(w["pole"], w["wartosc"]) for w in a.dotyczy] == [
            ("host", "db0*"), ("os", "Windows Server 2019")]
        assert a.notatki_zalacznikow == "schemat w PDF"
        assert a.notatki_diagramu == "diagrams/a.drawio"
        assert a.przeglad_co_mies == 3 and a.przeglad_do is not None
        assert a.na_dyzur is True
        assert a.wlasciciel == "wiedza@example.local"
        # Pierwszy artykul zaklada przestrzen - nie wymaga konfiguracji.
        assert db.get(WiedzaPrzestrzen, a.przestrzen_id).nazwa == "Ogólna"
    assert [w.operacja for w in wersje(artykul_id)] == ["utworzenie"]


def test_kryterium_edycja_bez_zmian_nie_dodaje_wersji_a_jedno_pole_dodaje_jedna(
        client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf, tagi="restart")

    odpowiedz = zapisz(client, csrf, artykul_id)
    assert odpowiedz.status_code == 303
    assert "Bez+zmian" in odpowiedz.headers["location"] or "Bez%20zmian" in odpowiedz.headers["location"]
    assert len(wersje(artykul_id)) == 1

    assert zapisz(client, csrf, artykul_id, streszczenie="Nowe streszczenie").status_code == 303
    lista = wersje(artykul_id)
    assert len(lista) == 2
    assert lista[-1].zmiany == {"streszczenie": {
        "old": "Co zrobić, gdy system-a przestaje odpowiadać.", "new": "Nowe streszczenie"}}
    assert lista[-1].kto == "wiedza@example.local"

    # Zmiana samych tagow tez jest jedna wersja z jednym polem.
    assert zapisz(client, csrf, artykul_id, tagi="restart, nowy").status_code == 303
    assert list(wersje(artykul_id)[-1].zmiany) == ["tagi"]


def test_kryterium_usuniety_zalacznik_znika_z_dysku_i_metadanych(
        client, tenant_a, make_user, katalog_wiedzy):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf)
    odpowiedz = client.post(
        f"/wiedza/a/{artykul_id}/zalaczniki", data={"csrf_token": csrf},
        files=[("pliki", ("schemat.pdf", b"%PDF-1.4 udawany", "application/pdf"))],
        follow_redirects=False)
    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        zal = db.execute(select(WiedzaZalacznik)).scalar_one()
    plik = katalog_wiedzy / "zalaczniki" / zal.sciezka
    assert plik.read_bytes() == b"%PDF-1.4 udawany"
    # Nazwa od uzytkownika nie trafia na dysk.
    assert "schemat" not in plik.name

    pobrany = client.get(f"/wiedza/zalacznik/{zal.id}")
    assert pobrany.status_code == 200 and pobrany.content == b"%PDF-1.4 udawany"
    assert pobrany.headers["content-type"] == "application/octet-stream"

    odpowiedz = client.post(f"/wiedza/a/{artykul_id}/zalaczniki/{zal.id}/usun",
                            data={"csrf_token": csrf}, follow_redirects=False)
    assert odpowiedz.status_code == 303
    assert not plik.exists()
    with SessionLocal() as db:
        assert db.execute(select(WiedzaZalacznik)).first() is None
    ostatnia = wersje(artykul_id)[-1]
    assert ostatnia.zmiany == {"zalaczniki": {"old": ["schemat.pdf"], "new": []}}
    assert "schemat.pdf" in ostatnia.opis_zmiany


def test_za_duzy_zalacznik_jest_odrzucany(client, tenant_a, make_user, monkeypatch, katalog_wiedzy):
    monkeypatch.setattr(get_settings(), "wiedza_zalacznik_mb", 1)
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf)
    odpowiedz = client.post(
        f"/wiedza/a/{artykul_id}/zalaczniki", data={"csrf_token": csrf},
        files=[("pliki", ("duzy.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream"))],
        follow_redirects=False)
    assert "przekracza" in odpowiedz.headers["location"].replace("%20", " ") or \
        "przekracza" in client.get(odpowiedz.headers["location"]).text
    with SessionLocal() as db:
        assert db.execute(select(WiedzaZalacznik)).first() is None
    assert len(wersje(artykul_id)) == 1


def test_kryterium_tag_ze_skryptem_wyswietla_sie_jako_tekst(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    zlosliwy = "<script>alert(1)</script>"
    artykul_id = utworz(client, csrf, tagi=zlosliwy[:50], systemy="<img src=x onerror=alert(1)>",
                        tresc=f"Tekst {zlosliwy}\n\n:::uwaga {zlosliwy}\ntresc\n:::\n\n[x](javascript:alert(1))")
    for adres in ("/wiedza/artykuly", f"/wiedza/a/{artykul_id}", f"/wiedza/a/{artykul_id}/edycja",
                  f"/wiedza/a/{artykul_id}/historia", "/wiedza"):
        strona = client.get(adres).text
        assert "<script>alert(1)" not in strona, adres
        assert "<img src=x" not in strona, adres
    strona = client.get(f"/wiedza/a/{artykul_id}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in strona
    assert 'href="javascript:' not in strona


def test_kryterium_bez_prawa_zapisu_dostaje_403(client, tenant_a, make_user):
    # Artykul zaklada administrator, a potem loguje sie konto tylko do odczytu.
    csrf_admina = zaloguj(client, tenant_a, make_user, email="admin-wiedzy@example.local")
    artykul_id = utworz(client, csrf_admina)
    with SessionLocal() as db:
        przestrzen_id = db.get(WiedzaArtykul, artykul_id).przestrzen_id
    client.post("/logout")
    csrf = zaloguj(client, tenant_a, make_user, role="viewer", email="czytelnik@example.local")

    assert client.get(f"/wiedza/a/{artykul_id}").status_code == 200
    proby = [
        ("/wiedza/nowy", formularz(csrf)),
        (f"/wiedza/a/{artykul_id}", {**formularz(csrf), "tytul": "zmiana"}),
        (f"/wiedza/a/{artykul_id}/usun", {"csrf_token": csrf}),
        (f"/wiedza/a/{artykul_id}/przywroc", {"csrf_token": csrf}),
        (f"/wiedza/a/{artykul_id}/przejrzany", {"csrf_token": csrf}),
        (f"/wiedza/a/{artykul_id}/historia/1/przywroc", {"csrf_token": csrf}),
        (f"/wiedza/a/{artykul_id}/zalaczniki", {"csrf_token": csrf}),
        ("/wiedza/przestrzenie", {"csrf_token": csrf, "nazwa": "Sieć"}),
        (f"/wiedza/p/{przestrzen_id}", {"csrf_token": csrf, "nazwa": "Inna"}),
        (f"/wiedza/p/{przestrzen_id}/usun", {"csrf_token": csrf}),
    ]
    for adres, dane in proby:
        assert client.post(adres, data=dane, follow_redirects=False).status_code == 403, adres
    assert client.get("/wiedza/nowy").status_code == 403
    assert client.get(f"/wiedza/a/{artykul_id}/edycja").status_code == 403
    with SessionLocal() as db:
        assert db.get(WiedzaArtykul, artykul_id).tytul == "Restart usługi system-a"
    # Strona nie proponuje zapisu kontu, ktore go nie ma.
    assert f"/wiedza/a/{artykul_id}/edycja" not in client.get(f"/wiedza/a/{artykul_id}").text


def test_zapis_bez_tokenu_csrf_jest_odrzucany(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    assert client.post("/wiedza/nowy", data=formularz("zly-token"),
                       follow_redirects=False).status_code == 403


def test_kryterium_linki_dla_listy_nazw_jednym_zapytaniem(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    maszyna(tenant_a["id"], "db01", fqdn="db01.example.local")
    maszyna(tenant_a["id"], "web01", fqdn="web01.example.local")
    utworz(client, csrf, systemy="system-a", warunek_pole=["host"], warunek_wartosc=["db0*"])

    zapytania: list[str] = []

    def licz(conn, cursor, statement, *args):
        zapytania.append(statement)

    event.listen(engine, "before_cursor_execute", licz)
    try:
        with SessionLocal() as db:
            wynik = dopasowanie.nazwy_z_artykulami(
                db, tenant_a["id"], ["DB01.example.local", "web01", "system-a", "brak"])
    finally:
        event.remove(engine, "before_cursor_execute", licz)
    assert wynik == {"db01.example.local", "system-a"}
    assert len(zapytania) == 1

    # Trasa zachowuje nazwy w postaci podanej przez wywolujacego.
    odpowiedz = client.get("/wiedza/linki", params=[("nazwa", "DB01.example.local"),
                                                     ("nazwa", "web01"), ("nazwa", "system-a")])
    assert set(odpowiedz.json()) == {"DB01.example.local", "system-a"}
    assert odpowiedz.json()["system-a"] == "/wiedza/artykuly?system=system-a"
    assert client.post("/wiedza/linki", json={"systems": ["system-a", "x"]}).json() == {
        "system-a": "/wiedza/artykuly?system=system-a"}


# --- dopasowanie maszyn -------------------------------------------------------------------

def test_wystarczy_jeden_pasujacy_warunek(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    t = tenant_a["id"]
    ids = {
        "host": maszyna(t, "db02", fqdn="db02.example.local"),
        "os": maszyna(t, "srv07", os_name="Microsoft Windows Server 2019 Standard"),
        "soft": maszyna(t, "web01", pakiety=["nginx 1.24", "curl"]),
        "lok": maszyna(t, "pc03", lokalizacja="Serwerownia A"),
        "tag": maszyna(t, "pc04", tagi=["Dyzur", "x"]),
        "rodzaj": maszyna(t, "prn01", typ="drukarka"),
        "system": maszyna(t, "HOST01", fqdn="host01.example.local"),
        "zadna": maszyna(t, "inna", os_name="Ubuntu 24.04", pakiety=["apache2"], tagi=["dyzurny"]),
    }
    artykul_id = utworz(
        client, csrf, systemy="Host01.Example.Local",
        warunek_pole=["host", "os", "oprogramowanie", "lokalizacja", "etykieta", "rodzaj", "host"],
        warunek_wartosc=["db0?", "windows server 2019", "NGINX", "serwerownia", "dyzur",
                         "Drukarka / skaner", ""],
    )
    with SessionLocal() as db:
        # Pusty warunek pominiety, etykieta rodzaju zamieniona na klucz.
        assert [w["pole"] for w in db.get(WiedzaArtykul, artykul_id).dotyczy] == [
            "host", "os", "oprogramowanie", "lokalizacja", "etykieta", "rodzaj"]
        trafione = {tr.asset_id: tr.powody for tr in dopasowanie.trafienia(db, t, artykul_id=artykul_id)}
    assert set(trafione) == {v for k, v in ids.items() if k != "zadna"}
    assert trafione[ids["system"]] == [("nazwa", "Host01.Example.Local")]
    assert trafione[ids["rodzaj"]] == [("rodzaj", "drukarka")]

    # Karta maszyny: zakladka "Wiedza" z powodem, lista zasobow z licznikiem.
    karta = client.get(f"/assets/{ids['soft']}").text
    assert 'data-tab="wiedza"' in karta and "Restart usługi system-a" in karta
    assert "oprogramowanie <code>NGINX</code>" in karta
    assert "Restart usługi system-a" not in client.get(f"/assets/{ids['zadna']}").text
    lista = client.get("/assets").text
    assert f'href="/assets/{ids["os"]}#wiedza"' in lista
    assert f'href="/assets/{ids["zadna"]}#wiedza"' not in lista


def test_wzorzec_traktuje_procent_i_podkreslenie_doslownie(tenant_a):
    assert dopasowanie.wzorzec("host", "db_0*") == "db\\_0%"
    assert dopasowanie.wzorzec("os", "100%") == "%100\\%%"
    with pytest.raises(ValueError):
        dopasowanie.normalizuj_warunki([{"pole": "host", "wartosc": "*"}])
    with pytest.raises(ValueError):
        dopasowanie.normalizuj_warunki([{"pole": "nieznane", "wartosc": "x"}])


def test_podglad_dopasowania_w_edytorze(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    maszyna(tenant_a["id"], "db01")
    maszyna(tenant_a["id"], "db02")
    maszyna(tenant_a["id"], "web01")
    wynik = client.get("/wiedza/dopasowanie", params=[
        ("pole", "host"), ("wartosc", "db*"), ("systemy", "web01")]).json()
    assert wynik["liczba"] == 3
    assert [m["nazwa"] for m in wynik["maszyny"]] == ["db01", "db02", "web01"]
    assert client.get("/wiedza/dopasowanie", params=[("pole", "host"), ("wartosc", "*")]).status_code == 400


def test_artykul_nie_dopasowuje_maszyn_innej_firmy(client, tenant_a, tenant_b, make_user):
    obca = maszyna(tenant_b["id"], "db01", fqdn="db01.example.local")
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf, systemy="db01", warunek_pole=["host"], warunek_wartosc=["db*"])
    with SessionLocal() as db:
        assert dopasowanie.trafienia(db, tenant_a["id"], artykul_id=artykul_id) == []
        assert dopasowanie.liczniki(db, tenant_b["id"]) == {}
    # Kontekst z cudzej maszyny niczego nie podstawia.
    strona = client.get(f"/wiedza/a/{artykul_id}", params={"maszyna": obca}).text
    assert "kb-pole-wypelnione" not in strona

    client.post("/logout")
    zaloguj(client, tenant_b, make_user, email="obcy@example.local")
    assert client.get(f"/wiedza/a/{artykul_id}").status_code == 404
    assert "Restart usługi" not in client.get("/wiedza/artykuly").text
    assert client.get("/wiedza/linki", params={"nazwa": "db01"}).json() == {}


# --- tresc, historia, kosz ----------------------------------------------------------------

def test_kontekst_maszyny_wypelnia_pola(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    asset_id = maszyna(tenant_a["id"], "host01", fqdn="host01.example.local")
    artykul_id = utworz(client, csrf, systemy="host01")
    bez = client.get(f"/wiedza/a/{artykul_id}").text
    assert ">{hostname}</span>" in bez
    z = client.get(f"/wiedza/a/{artykul_id}", params={"maszyna": asset_id}).text
    assert 'kb-pole-wypelnione' in z and ">host01</span>" in z


def test_renderowanie_paneli_blokow_kodu_i_pol():
    html = str(tresc.renderuj(
        "```\n:::uwaga nie panel\n{hostname}\n```\n\n:::stop Uwaga <b>\nNie restartuj\n:::\n\n"
        "[link](http://{hostname}/)", {"hostname": "db01<x>"}))
    assert 'class="kb-panel kb-panel-stop"' in html
    assert "Uwaga &lt;b&gt;" in html
    # ":::" w bloku kodu zostaje tekstem, a pole w bloku kodu sie wypelnia.
    assert ":::uwaga nie panel" in html and "db01&lt;x&gt;" in html
    # Pole wewnatrz atrybutu nie jest podmieniane na znacznik.
    assert 'href="http://%7Bhostname%7D/"' in html


def test_wyszukiwanie_bez_polskich_znakow_z_fragmentem(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    utworz(client, csrf, tytul="Zakleszczenia w bazie", tresc="Gdy pojawi się zakleszczenie, sprawdź blokady.")
    utworz(client, csrf, tytul="Drukarka", tresc="Wymiana tonera.")
    strona = client.get("/wiedza/artykuly", params={"q": "zakleszczenie blokady"}).text
    assert "Zakleszczenia w bazie" in strona and "Drukarka" not in strona
    assert "<mark>blokady</mark>" in strona
    assert "Zakleszczenia w bazie" in client.get("/wiedza/artykuly", params={"q": "ZAKLESZ"}).text
    assert "Zakleszczenia w bazie" in client.get("/wiedza/artykuly", params={"q": "sprawdz"}).text
    assert "Drukarka" in client.get("/wiedza/artykuly", params={"q": "tonera"}).text


def test_filtry_listy_tag_system_kategoria(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    maszyna(tenant_a["id"], "db01", fqdn="db01.example.local")
    utworz(client, csrf, tytul="Artykul A", tagi="klaster", kategoria="runbooks")
    utworz(client, csrf, tytul="Artykul B", warunek_pole=["host"], warunek_wartosc=["db*"],
           kategoria="architecture")
    assert "Artykul A" in client.get("/wiedza/artykuly?tag=Klaster").text
    assert "Artykul B" not in client.get("/wiedza/artykuly?tag=klaster").text
    # System: artykul, ktorego regula obejmuje maszyne o tej nazwie.
    strona = client.get("/wiedza/artykuly?system=db01.example.local").text
    assert "Artykul B" in strona and "Artykul A" not in strona
    strona = client.get("/wiedza/artykuly?kategoria=architecture").text
    assert "Artykul B" in strona and "Artykul A" not in strona
    assert client.get("/wiedza/podpowiedzi?rodzaj=tag&q=kla").json() == ["klaster"]


def test_historia_tresci_i_przywracanie_wersji(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf, tresc="pierwsza")
    assert zapisz(client, csrf, artykul_id, tresc="druga").status_code == 303
    assert zapisz(client, csrf, artykul_id, tytul="Nowy tytuł").status_code == 303
    lista = wersje(artykul_id)
    assert [w.wersja for w in lista] == [1, 2, 3]
    assert lista[1].tresc_przed == "pierwsza" and lista[2].tresc_przed is None

    strona = client.get(f"/wiedza/a/{artykul_id}/historia?od=1&do=3").text
    assert "- pierwsza" in strona and "+ druga" in strona

    odpowiedz = client.post(f"/wiedza/a/{artykul_id}/historia/1/przywroc",
                            data={"csrf_token": csrf}, follow_redirects=False)
    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        a = db.get(WiedzaArtykul, artykul_id)
        assert a.tresc == "pierwsza" and a.wersja == 4 and a.tytul == "Nowy tytuł"
    assert wersje(artykul_id)[-1].opis_zmiany == "Przywrócono treść wersji 1"


def test_rownolegla_edycja_nie_nadpisuje_cudzych_zmian(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf)
    stary = pola_edycji(client, artykul_id)
    assert zapisz(client, csrf, artykul_id, tresc="zmiana kolegi").status_code == 303
    odpowiedz = client.post(f"/wiedza/a/{artykul_id}",
                            data={**stary, "csrf_token": csrf, "tresc": "moja zmiana"})
    assert odpowiedz.status_code == 409
    assert "moja zmiana" in odpowiedz.text
    with SessionLocal() as db:
        assert db.get(WiedzaArtykul, artykul_id).tresc == "zmiana kolegi"


def test_kosz_ukrywa_artykul_i_pozwala_przywrocic(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    asset_id = maszyna(tenant_a["id"], "host01")
    artykul_id = utworz(client, csrf, systemy="host01")
    assert client.post(f"/wiedza/a/{artykul_id}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    assert "Restart usługi" not in client.get("/wiedza/artykuly").text
    assert "Restart usługi" not in client.get(f"/assets/{asset_id}").text
    assert "Restart usługi" in client.get("/wiedza/kosz").text
    assert wersje(artykul_id)[-1].operacja == "usuniecie"
    # Edycja artykulu w koszu jest niemozliwa, podglad tak.
    assert client.get(f"/wiedza/a/{artykul_id}/edycja").status_code == 404
    assert client.get(f"/wiedza/a/{artykul_id}").status_code == 200

    assert client.post(f"/wiedza/a/{artykul_id}/przywroc", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    assert "Restart usługi" in client.get(f"/assets/{asset_id}").text
    assert wersje(artykul_id)[-1].operacja == "przywrocenie"


def test_walidacja_formularza(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    for pola, fragment in [
        ({"tytul": ""}, "tytuł jest wymagany"),
        ({"tytul": "x" * 201}, "najwyżej 200"),
        ({"streszczenie": ""}, "streszczenie jest wymagane"),
        ({"kategoria": "nieznana"}, "wybierz kategorię"),
        ({"tagi": "x" * 51}, "dłuższy niż 50"),
        ({"przeglad_co_mies": "5"}, "okres przeglądu"),
    ]:
        odpowiedz = client.post("/wiedza/nowy", data=formularz(csrf, **pola))
        assert odpowiedz.status_code == 400, pola
        assert fragment in odpowiedz.text, pola
    with SessionLocal() as db:
        assert db.execute(select(WiedzaArtykul)).first() is None


def test_drzewo_stron_i_przestrzenie(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    client.post("/wiedza/przestrzenie", data={"csrf_token": csrf, "nazwa": "Bazy danych"})
    client.post("/wiedza/przestrzenie", data={"csrf_token": csrf, "nazwa": "Sieć"})
    with SessionLocal() as db:
        przestrzenie = {p.nazwa: p.id for p in db.execute(select(WiedzaPrzestrzen)).scalars()}
    assert przestrzenie.keys() == {"Bazy danych", "Sieć"}
    # Druga przestrzen o tej samej nazwie (inna wielkosc liter) nie powstaje.
    client.post("/wiedza/przestrzenie", data={"csrf_token": csrf, "nazwa": "sieć "})
    with SessionLocal() as db:
        assert len(db.execute(select(WiedzaPrzestrzen)).all()) == 2

    rodzic = utworz(client, csrf, tytul="SQL Server", przestrzen_id=przestrzenie["Bazy danych"])
    dziecko = utworz(client, csrf, tytul="Runbooki SQL", przestrzen_id=przestrzenie["Bazy danych"],
                     rodzic_id=rodzic)
    wnuk = utworz(client, csrf, tytul="Awaria węzła", przestrzen_id=przestrzenie["Bazy danych"],
                  rodzic_id=dziecko)
    strona = client.get(f"/wiedza/a/{wnuk}").text
    assert "Runbooki SQL" in strona and "SQL Server" in strona

    # Strona nie moze byc podrzedna wobec wlasnej podstrony.
    odpowiedz = zapisz(client, csrf, rodzic, rodzic_id=wnuk)
    assert odpowiedz.status_code == 400 and "samej siebie" in odpowiedz.text
    # Rodzic z innej przestrzeni jest odrzucany.
    odpowiedz = client.post("/wiedza/nowy", data=formularz(
        csrf, przestrzen_id=przestrzenie["Sieć"], rodzic_id=rodzic))
    assert odpowiedz.status_code == 400

    # Przeniesienie strony zabiera podstrony do nowej przestrzeni.
    assert zapisz(client, csrf, dziecko, przestrzen_id=przestrzenie["Sieć"], rodzic_id="").status_code == 303
    with SessionLocal() as db:
        assert db.get(WiedzaArtykul, wnuk).przestrzen_id == przestrzenie["Sieć"]

    # Przestrzeni z artykulami nie da sie usunac, pusta znika.
    odpowiedz = client.post(f"/wiedza/p/{przestrzenie['Sieć']}/usun", data={"csrf_token": csrf},
                            follow_redirects=False)
    assert "Nie" in odpowiedz.headers["location"]
    client.post("/wiedza/przestrzenie", data={"csrf_token": csrf, "nazwa": "Pusta"})
    with SessionLocal() as db:
        pusta = db.execute(select(WiedzaPrzestrzen).where(WiedzaPrzestrzen.nazwa == "Pusta")).scalar_one()
    client.post(f"/wiedza/p/{pusta.id}/usun", data={"csrf_token": csrf})
    with SessionLocal() as db:
        assert db.get(WiedzaPrzestrzen, pusta.id) is None


def test_strony_panelu_renderuja_sie(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    asset_id = maszyna(tenant_a["id"], "host01")
    artykul_id = utworz(client, csrf, systemy="host01", na_dyzur="1")
    with SessionLocal() as db:
        przestrzen_id = db.get(WiedzaArtykul, artykul_id).przestrzen_id
    for adres in ("/wiedza", "/wiedza/artykuly", "/wiedza/przestrzenie", f"/wiedza/p/{przestrzen_id}",
                  "/wiedza/kosz", "/wiedza/nowy", f"/wiedza/nowy?maszyna={asset_id}",
                  f"/wiedza/a/{artykul_id}", f"/wiedza/a/{artykul_id}/edycja",
                  f"/wiedza/a/{artykul_id}/historia", f"/wiedza/artykuly?maszyna={asset_id}"):
        assert client.get(adres).status_code == 200, adres
    start = client.get("/wiedza").text
    assert "Przypięte na dyżur" in start and "Restart usługi system-a" in start
    # Menu obu ukladow prowadzi do bazy wiedzy.
    assert 'href="/wiedza"' in start
    client.cookies.set("cmdb_layout", "classic")
    assert 'href="/wiedza"' in client.get("/").text
    # Nowy artykul z karty maszyny ma jej nazwe wsrod systemow.
    assert 'value="host01"' in client.get(f"/wiedza/nowy?maszyna={asset_id}").text


def test_podglad_tresci_wymaga_tokenu(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    odpowiedz = client.post("/wiedza/podglad", data={"csrf_token": csrf, "tresc": ":::ok\n**tak**\n:::"})
    assert odpowiedz.status_code == 200 and "<strong>tak</strong>" in odpowiedz.text
    assert client.post("/wiedza/podglad", data={"tresc": "x"}).status_code == 403


def test_przeglad_przesuwa_termin_i_trafia_do_historii(client, tenant_a, make_user):
    from datetime import date, timedelta

    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf, przeglad_co_mies="3")
    with SessionLocal() as db:
        a = db.get(WiedzaArtykul, artykul_id)
        a.przeglad_do = date.today() - timedelta(days=5)
        db.commit()
    assert "do przeglądu" in client.get(f"/wiedza/a/{artykul_id}").text
    assert "Restart usługi system-a" in client.get("/wiedza").text.split('id="do-przegladu"')[1]
    client.post(f"/wiedza/a/{artykul_id}/przejrzany", data={"csrf_token": csrf})
    with SessionLocal() as db:
        assert db.get(WiedzaArtykul, artykul_id).przeglad_do > date.today()
    assert list(wersje(artykul_id)[-1].zmiany) == ["przeglad_do"]


def test_slownik_tylko_rosnie(client, tenant_a, make_user):
    csrf = zaloguj(client, tenant_a, make_user)
    artykul_id = utworz(client, csrf, tagi="Restart, restart , klaster")
    assert zapisz(client, csrf, artykul_id, tagi="").status_code == 303
    client.post(f"/wiedza/a/{artykul_id}/usun", data={"csrf_token": csrf})
    with SessionLocal() as db:
        tagi = db.execute(select(WiedzaSlownik).where(WiedzaSlownik.rodzaj == "tag")).scalars().all()
    # "Restart" i "restart " to ten sam wpis; po usunieciu tagu i artykulu
    # wpisy zostaja, zeby dalej sie podpowiadaly.
    assert sorted(w.klucz for w in tagi) == ["klaster", "restart"]
    assert client.get("/wiedza/podpowiedzi?rodzaj=tag").json() == ["klaster", "Restart"]


def test_kopia_zawiera_katalog_wiedzy():
    from cmdb_server.services import kopie

    assert kopie.KATALOGI["wiedza"] == "wiedza_dir"
    assert Path(get_settings().wiedza_dir).name == "wiedza"
