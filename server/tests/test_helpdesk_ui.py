"""Panel helpdesku: tablica, karta zgloszenia, poczta operatora i raporty.

Sprawdzamy tu dwie rzeczy naraz: czy ekran robi to, co obiecuje, i czy granica
firm trzyma sie takze przez formularze - bo identyfikator zgloszenia w adresie
jest najlatwiejsza droga do cudzych danych.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    STATUS_NOWE,
    STATUS_W_TRAKCIE,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    Asset,
    CzasPracy,
    HelpdeskUstawienia,
    IgnorowanaDomena,
    NierozpoznanaWiadomosc,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    Zgloszenie,
)
from cmdb_server.security import hash_password
from cmdb_server.services import helpdesk, helpdesk_wysylka, sekrety

from .test_helpdesk_skrzynka import AtrapaSMTP
from .test_tenant_isolation import _extract_csrf, _login

HASLO = "haslo-panelu-2026"


@pytest.fixture
def smtp(monkeypatch):
    AtrapaSMTP.wyslane = []
    monkeypatch.setattr(helpdesk_wysylka, "_polaczenie", lambda konfiguracja: AtrapaSMTP())
    return AtrapaSMTP


def _firma(tenant_id: str, skrot: str = "BON", domeny=("bongo.pl",)) -> None:
    with SessionLocal() as db:
        helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_id), skrot)
        for domena in domeny:
            helpdesk.dodaj_domene(db, tenant_id, domena)
        db.commit()


def _technik(email: str, firmy: list[str], nazwa: str = "Wladek Nowak") -> str:
    with SessionLocal() as db:
        user = PortalUser(
            tenant_id=None, email=email, full_name=nazwa,
            password_hash=hash_password(HASLO), role="admin",
        )
        db.add(user)
        db.flush()
        for tenant_id in firmy:
            helpdesk.nadaj_dostep(db, user.id, tenant_id)
        db.commit()
        return user.id


def _zgloszenie(tenant_id: str, temat: str = "Nie dziala drukarka",
                email: str = "jan@bongo.pl") -> str:
    with SessionLocal() as db:
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_id, temat=temat, tresc="Od rana nie dziala.",
            zglaszajacy_email=email, message_id=f"<{temat[:5]}@bongo.pl>",
        )
        db.commit()
        return zgloszenie.id


def _skrzynka() -> None:
    with SessionLocal() as db:
        db.add(HelpdeskUstawienia(
            klucz="helpdesk", imap_host="imap.mojadomena.pl",
            smtp_host="smtp.mojadomena.pl", smtp_uzytkownik="helpdesk@mojadomena.pl",
            smtp_haslo_szyfr=sekrety.zaszyfruj("tajne"),
            nadawca="helpdesk@mojadomena.pl", nazwa_nadawcy="Helpdesk", aktywne=True,
        ))
        db.commit()


def _csrf(client, sciezka: str) -> str:
    return _extract_csrf(client.get(sciezka).text)


# --- tablica i granica firm -------------------------------------------------

def test_tablica_pokazuje_zgloszenia_firm_technika(client, tenant_a, tenant_b):
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    _firma(tenant_b["id"], "KLE", ["klepsydra.pl"])
    _zgloszenie(tenant_a["id"], "Drukarka Bongo")
    _zgloszenie(tenant_b["id"], "Dysk Klepsydra", "biuro@klepsydra.pl")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    strona = client.get("/helpdesk").text
    assert "Drukarka Bongo" in strona
    assert "Dysk Klepsydra" not in strona


def test_cudze_zgloszenie_odpowiada_tak_samo_jak_nieistniejace(client, tenant_a, tenant_b):
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    _firma(tenant_b["id"], "KLE", ["klepsydra.pl"])
    obce = _zgloszenie(tenant_b["id"], "Dysk", "biuro@klepsydra.pl")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert client.get(f"/helpdesk/zgloszenie/{obce}").status_code == 404
    assert client.get("/helpdesk/zgloszenie/nie-ma-takiego").status_code == 404


def test_konto_bez_dostepow_nie_wchodzi_do_helpdesku(client, tenant_a, make_user):
    """Administrator firmy nie jest technikiem helpdesku - to narzedzie operatora."""
    make_user(tenant_a["id"], "admin@firma-a.pl", HASLO, role="admin")
    _login(client, "admin@firma-a.pl", HASLO)

    assert client.get("/helpdesk").status_code == 403
    assert "/helpdesk" not in client.get("/assets").text


def test_menu_pokazuje_zgloszenia_technikowi(client, tenant_a):
    _firma(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert 'href="/helpdesk"' in client.get("/assets").text


def test_filtr_po_statusie_i_szukanie_po_numerze(client, tenant_a):
    _firma(tenant_a["id"])
    _zgloszenie(tenant_a["id"], "Drukarka")
    _zgloszenie(tenant_a["id"], "VPN zrywa")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    assert "VPN zrywa" in client.get("/helpdesk?widok=lista&szukaj=BON-2").text
    assert "Drukarka" not in client.get("/helpdesk?widok=lista&szukaj=BON-2").text
    assert "Drukarka" in client.get("/helpdesk?widok=lista&stan=nowe").text
    assert "Drukarka" not in client.get("/helpdesk?widok=lista&stan=zamkniete").text


# --- karta zgloszenia -------------------------------------------------------

def test_komentarz_wewnetrzny_zostaje_w_zgloszeniu(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_WEWNETRZNY,
        "tresc": "Sprawdze sterownik.",
    }, follow_redirects=False)

    assert smtp.wyslane == []
    strona = client.get(sciezka).text
    assert "Sprawdze sterownik." in strona
    assert "tylko dla techników" in strona


def test_odpowiedz_do_klienta_wychodzi_i_rusza_status(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_DO_KLIENTA,
        "tresc": "Prosze zrestartowac drukarke.",
    }, follow_redirects=False)

    assert len(smtp.wyslane) == 1
    assert smtp.wyslane[0]["To"] == "jan@bongo.pl"
    assert "BON-1" in smtp.wyslane[0]["Subject"]
    with SessionLocal() as db:
        # Ktos odpisal, wiec sprawa przestaje byc "nowa" - inaczej pierwsza
        # kolumna tablicy zbiera zgloszenia, ktorymi ktos sie juz zajmuje.
        assert db.get(Zgloszenie, zgloszenie_id).status == STATUS_W_TRAKCIE


def test_pusta_wiadomosc_jest_odrzucana(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    odpowiedz = client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_WEWNETRZNY, "tresc": "   ",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400


def test_awaria_smtp_zostawia_tresc_w_watku(client, tenant_a, monkeypatch):
    import smtplib

    monkeypatch.setattr(
        helpdesk_wysylka, "_polaczenie",
        lambda konfiguracja: AtrapaSMTP(blad=smtplib.SMTPException("serwer nie odpowiada")),
    )
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_DO_KLIENTA,
        "tresc": "Prosze zrestartowac.",
    }, follow_redirects=False)

    strona = client.get(sciezka).text
    assert "Prosze zrestartowac." in strona
    assert "nie wysłano" in strona


def test_przypisanie_status_i_czas_pracy(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    technik_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)
    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"

    client.post(f"{sciezka}/technik", data={
        "csrf_token": _csrf(client, sciezka), "technik_id": technik_id,
    }, follow_redirects=False)
    client.post(f"{sciezka}/status", data={
        "csrf_token": _csrf(client, sciezka), "stan": "oczekuje",
    }, follow_redirects=False)
    client.post(f"{sciezka}/czas", data={
        "csrf_token": _csrf(client, sciezka), "minuty": "30 min", "opis": "Diagnostyka",
    }, follow_redirects=False)

    with SessionLocal() as db:
        zgloszenie = db.get(Zgloszenie, zgloszenie_id)
        assert zgloszenie.technik_id == technik_id
        assert zgloszenie.status == "oczekuje"
        czas = db.execute(select(CzasPracy)).scalar_one()
        assert czas.minuty == 30 and czas.technik_id == technik_id
    assert "1 h" not in client.get(sciezka).text.split("Czas pracy")[1][:200]


def test_czas_zapisuje_sie_zawsze_na_konto_piszacego(client, tenant_a, smtp):
    """Czasu kolegi nikt za niego nie wpisze - formularz nie ma nawet takiego pola."""
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]], "Wladek Nowak")
    lukasz = _technik("lukasz@mojadomena.pl", [tenant_a["id"]], "Lukasz Mazur")
    _login(client, "lukasz@mojadomena.pl", HASLO)

    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"
    client.post(f"{sciezka}/czas", data={
        "csrf_token": _csrf(client, sciezka), "minuty": "45",
    }, follow_redirects=False)

    with SessionLocal() as db:
        assert db.execute(select(CzasPracy)).scalar_one().technik_id == lukasz


def test_podpiecie_i_odpiecie_sprzetu(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    with SessionLocal() as db:
        asset = Asset(tenant_id=tenant_a["id"], machine_id="id-druk", hostname="DRUKARKA-01")
        db.add(asset)
        db.commit()
        asset_id = asset.id
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)
    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"

    client.post(f"{sciezka}/sprzet", data={
        "csrf_token": _csrf(client, sciezka), "asset_id": asset_id, "akcja": "podepnij",
    }, follow_redirects=False)
    assert "DRUKARKA-01" in client.get(sciezka).text

    client.post(f"{sciezka}/sprzet", data={
        "csrf_token": _csrf(client, sciezka), "asset_id": asset_id, "akcja": "odepnij",
    }, follow_redirects=False)
    with SessionLocal() as db:
        assert helpdesk.sprzet_zgloszenia(db, zgloszenie_id) == []


def test_zalacznik_pobiera_tylko_technik_tej_firmy(client, tenant_a, tenant_b, monkeypatch,
                                                   tmp_path, smtp):
    from cmdb_server.services import helpdesk_poczta as poczta

    monkeypatch.setattr(poczta, "katalog_zalacznikow", lambda: tmp_path)
    _firma(tenant_a["id"])
    _firma(tenant_b["id"], "KLE", ["klepsydra.pl"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])

    with SessionLocal() as db:
        wpis = helpdesk.pierwszy_wpis(db, zgloszenie_id)
        poczta.zapisz_zalaczniki(db, wpis, (
            poczta.Zalacznik(nazwa="zrzut.png", typ_mime="image/png", dane=b"PNG-udawany"),
        ))
        db.commit()

    with SessionLocal() as db:
        from cmdb_server.models import ZalacznikWpisu

        zalacznik_id = db.execute(select(ZalacznikWpisu.id)).scalars().one()

    _technik("swoj@mojadomena.pl", [tenant_a["id"]])
    _technik("obcy@mojadomena.pl", [tenant_b["id"]])

    _login(client, "swoj@mojadomena.pl", HASLO)
    odpowiedz = client.get(f"/helpdesk/zalacznik/{zalacznik_id}")
    assert odpowiedz.status_code == 200
    assert odpowiedz.content == b"PNG-udawany"

    client.post("/logout")
    _login(client, "obcy@mojadomena.pl", HASLO)
    assert client.get(f"/helpdesk/zalacznik/{zalacznik_id}").status_code == 404


# --- ekrany superadmina -----------------------------------------------------

def _nierozpoznana(nadawca="anna@gmail.com", temat="Prosba o pomoc") -> str:
    with SessionLocal() as db:
        wpis = NierozpoznanaWiadomosc(
            nadawca_email=nadawca, domena=nadawca.split("@")[1], temat=temat,
            tresc="Nie dziala mi poczta.", message_id=f"<{nadawca}>",
        )
        db.add(wpis)
        db.commit()
        return wpis.id


def test_technik_nie_wchodzi_do_ekranow_superadmina(client, tenant_a):
    _firma(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)

    for sciezka in ("/helpdesk/nierozpoznane", "/helpdesk/firmy", "/helpdesk/skrzynka"):
        assert client.get(sciezka).status_code == 403, sciezka


def test_superadmin_tworzy_zgloszenie_z_nierozpoznanej(client, tenant_a, make_user):
    _firma(tenant_a["id"])
    wiadomosc_id = _nierozpoznana()
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    client.post(f"/helpdesk/nierozpoznane/{wiadomosc_id}", data={
        "csrf_token": _csrf(client, "/helpdesk/nierozpoznane"),
        "akcja": "utworz", "tenant_id": tenant_a["id"],
    }, follow_redirects=False)

    with SessionLocal() as db:
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        assert zgloszenie.numer_pelny == "BON-1"
        assert zgloszenie.zglaszajacy_email == "anna@gmail.com"
        wiadomosc = db.get(NierozpoznanaWiadomosc, wiadomosc_id)
        assert wiadomosc.stan == "przypisana" and wiadomosc.zgloszenie_id == zgloszenie.id
        # Domena NIE zostaje dodana do firmy - jednorazowa decyzja nie otwiera
        # helpdesku dla calego gmail.com.
        assert helpdesk.wlasciciel_domeny(db, "gmail.com") is None


def test_superadmin_ignoruje_domene_i_ja_przywraca(client, tenant_a, make_user):
    _firma(tenant_a["id"])
    wiadomosc_id = _nierozpoznana("noreply@sklep-online.pl", "Twoja przesylka")
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    client.post(f"/helpdesk/nierozpoznane/{wiadomosc_id}", data={
        "csrf_token": _csrf(client, "/helpdesk/nierozpoznane"), "akcja": "zignoruj_domene",
    }, follow_redirects=False)

    with SessionLocal() as db:
        regula = db.execute(select(IgnorowanaDomena)).scalar_one()
        assert regula.domena == "sklep-online.pl"
        regula_id = regula.id
        # Wiadomosc nie znika - zmienia stan.
        assert db.get(NierozpoznanaWiadomosc, wiadomosc_id).stan == "zignorowana"

    client.post(f"/helpdesk/domeny/{regula_id}/przywroc", data={
        "csrf_token": _csrf(client, "/helpdesk/nierozpoznane"),
    }, follow_redirects=False)
    with SessionLocal() as db:
        assert db.execute(select(IgnorowanaDomena)).scalars().all() == []


def test_domeny_firmy_nie_da_sie_zignorowac_z_ekranu(client, tenant_a, make_user):
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    wiadomosc_id = _nierozpoznana("ktos@bongo.pl", "Cos")
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    odpowiedz = client.post(f"/helpdesk/nierozpoznane/{wiadomosc_id}", data={
        "csrf_token": _csrf(client, "/helpdesk/nierozpoznane"), "akcja": "zignoruj_domene",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400


def test_superadmin_wlacza_firme_i_nadaje_dostep(client, tenant_a, make_user):
    technik_id = _technik("technik@mojadomena.pl", [])
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    client.post(f"/helpdesk/firmy/{tenant_a['id']}/wlacz", data={
        "csrf_token": _csrf(client, "/helpdesk/firmy"), "skrot": "BON",
    }, follow_redirects=False)
    client.post(f"/helpdesk/firmy/{tenant_a['id']}/domena", data={
        "csrf_token": _csrf(client, "/helpdesk/firmy"), "domena": "Bongo.PL",
    }, follow_redirects=False)
    client.post("/helpdesk/dostepy", data={
        "csrf_token": _csrf(client, "/helpdesk/firmy"),
        "user_id": technik_id, "tenant_id": tenant_a["id"], "akcja": "nadaj",
    }, follow_redirects=False)

    with SessionLocal() as db:
        assert helpdesk.firma_dla_adresu(db, "jan@bongo.pl").id == tenant_a["id"]
        technik = db.get(PortalUser, technik_id)
        assert helpdesk.ma_dostep(db, technik, tenant_a["id"]) is True


def test_zajeta_domena_konczy_sie_bledem_z_nazwa_firmy(client, tenant_a, tenant_b, make_user):
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    _firma(tenant_b["id"], "KLE", domeny=())
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    odpowiedz = client.post(f"/helpdesk/firmy/{tenant_b['id']}/domena", data={
        "csrf_token": _csrf(client, "/helpdesk/firmy"), "domena": "bongo.pl",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    assert "Firma A" in odpowiedz.text


def test_zapis_skrzynki_nie_kasuje_zapisanego_hasla(client, tenant_a, make_user):
    """Pola hasel wracaja z formularza puste - gdyby puste znaczylo "skasuj",
    kazdy zapis ustawien odlaczalby skrzynke."""
    _skrzynka()
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    client.post("/helpdesk/skrzynka", data={
        "csrf_token": _csrf(client, "/helpdesk/skrzynka"),
        "imap_host": "imap.inny.pl", "smtp_host": "smtp.mojadomena.pl",
        "nadawca": "helpdesk@mojadomena.pl", "imap_haslo": "", "smtp_haslo": "",
        "aktywne": "1",
    }, follow_redirects=False)

    with SessionLocal() as db:
        ustawienia = db.get(HelpdeskUstawienia, "helpdesk")
        assert ustawienia.imap_host == "imap.inny.pl"
        assert sekrety.odszyfruj(ustawienia.smtp_haslo_szyfr) == "tajne"


# --- raporty ----------------------------------------------------------------

def _czas(tenant_id: str, zgloszenie_id: str, email: str, minuty: int) -> None:
    with SessionLocal() as db:
        technik = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one()
        zgloszenie = db.get(Zgloszenie, zgloszenie_id)
        helpdesk.dodaj_czas(db, zgloszenie, technik, minuty, "prace")
        db.commit()


def test_raport_firmy_rozbija_czas_na_technikow(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]], "Wladek Nowak")
    _technik("lukasz@mojadomena.pl", [tenant_a["id"]], "Lukasz Mazur")
    _czas(tenant_a["id"], zgloszenie_id, "wladek@mojadomena.pl", 50)
    _czas(tenant_a["id"], zgloszenie_id, "lukasz@mojadomena.pl", 45)
    _login(client, "wladek@mojadomena.pl", HASLO)

    strona = client.get(f"/helpdesk/raporty/firma?firma={tenant_a['id']}").text
    assert "1 h 35 min" in strona
    assert "Wladek Nowak" in strona and "Lukasz Mazur" in strona
    # Jedno zgloszenie u dwoch technikow liczy sie raz.
    assert ">1<" in strona.split("zgłoszenia z czasem pracy")[0][-200:]


def test_raport_technika_pokazuje_jego_firmy(client, tenant_a, tenant_b, smtp):
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    _firma(tenant_b["id"], "KLE", ["klepsydra.pl"])
    pierwsze = _zgloszenie(tenant_a["id"], "Drukarka")
    drugie = _zgloszenie(tenant_b["id"], "Dysk", "biuro@klepsydra.pl")
    _technik("wladek@mojadomena.pl", [tenant_a["id"], tenant_b["id"]], "Wladek Nowak")
    _czas(tenant_a["id"], pierwsze, "wladek@mojadomena.pl", 30)
    _czas(tenant_b["id"], drugie, "wladek@mojadomena.pl", 90)
    _login(client, "wladek@mojadomena.pl", HASLO)

    strona = client.get("/helpdesk/raporty/technik").text
    assert "2 h 00 min" in strona
    assert "Firma A" in strona and "Firma B" in strona


def test_eksport_csv_i_xlsx(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]], "Wladek Nowak")
    _czas(tenant_a["id"], zgloszenie_id, "wladek@mojadomena.pl", 95)
    _login(client, "wladek@mojadomena.pl", HASLO)

    csv_ = client.get(f"/helpdesk/raporty/firma?firma={tenant_a['id']}&eksport=csv")
    assert csv_.status_code == 200
    assert "attachment" in csv_.headers["content-disposition"]
    tresc = csv_.content.decode("utf-8-sig")
    assert "Grupa;Zgloszenie" in tresc
    assert "BON-1" in tresc and "1 h 35 min" in tresc

    xlsx = client.get(f"/helpdesk/raporty/firma?firma={tenant_a['id']}&eksport=xlsx")
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"          # arkusz to archiwum ZIP
    assert "spreadsheetml" in xlsx.headers["content-type"]


def test_raport_nie_pokazuje_firm_spoza_dostepu(client, tenant_a, tenant_b, smtp):
    """Technik oglada wlasny czas, ale nazwy firm, do ktorych nie ma dostepu,
    nie moga pojawic sie w jego raporcie."""
    _firma(tenant_a["id"], "BON", ["bongo.pl"])
    _firma(tenant_b["id"], "KLE", ["klepsydra.pl"])
    pierwsze = _zgloszenie(tenant_a["id"], "Drukarka")
    drugie = _zgloszenie(tenant_b["id"], "Dysk", "biuro@klepsydra.pl")
    _technik("wladek@mojadomena.pl", [tenant_a["id"], tenant_b["id"]], "Wladek Nowak")
    _czas(tenant_a["id"], pierwsze, "wladek@mojadomena.pl", 30)
    _czas(tenant_b["id"], drugie, "wladek@mojadomena.pl", 90)

    with SessionLocal() as db:
        wladek = db.execute(
            select(PortalUser).where(PortalUser.email == "wladek@mojadomena.pl")
        ).scalar_one()
        helpdesk.odbierz_dostep(db, wladek.id, tenant_b["id"])
        db.commit()

    _login(client, "wladek@mojadomena.pl", HASLO)
    strona = client.get("/helpdesk/raporty/technik").text
    assert "Firma A" in strona
    assert "Firma B" not in strona


def test_zgloszenie_zamkniete_liczy_sie_w_raporcie(client, tenant_a, smtp):
    """Czas nie znika razem z zamknieciem sprawy - faktura obejmuje wlasnie te."""
    _firma(tenant_a["id"])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("wladek@mojadomena.pl", [tenant_a["id"]], "Wladek Nowak")
    _czas(tenant_a["id"], zgloszenie_id, "wladek@mojadomena.pl", 40)
    with SessionLocal() as db:
        helpdesk.zmien_status(db, db.get(Zgloszenie, zgloszenie_id), "zamkniete", autor="test")
        db.commit()

    _login(client, "wladek@mojadomena.pl", HASLO)
    assert "40 min" in client.get(f"/helpdesk/raporty/firma?firma={tenant_a['id']}").text


def test_watek_pokazuje_wpisy_w_kolejnosci(client, tenant_a, smtp):
    _firma(tenant_a["id"])
    _skrzynka()
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    _login(client, "technik@mojadomena.pl", HASLO)
    sciezka = f"/helpdesk/zgloszenie/{zgloszenie_id}"

    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_WEWNETRZNY, "tresc": "Notatka",
    }, follow_redirects=False)
    client.post(f"{sciezka}/wiadomosc", data={
        "csrf_token": _csrf(client, sciezka), "rodzaj": WPIS_DO_KLIENTA, "tresc": "Odpowiedz",
    }, follow_redirects=False)

    strona = client.get(sciezka).text
    assert strona.index("Od rana nie dziala.") < strona.index("Notatka") < strona.index("Odpowiedz")
    with SessionLocal() as db:
        rodzaje = [w.rodzaj for w in db.execute(
            select(WpisZgloszenia)
            .where(WpisZgloszenia.zgloszenie_id == zgloszenie_id)
            .order_by(WpisZgloszenia.utworzono)
        ).scalars()]
    assert rodzaje[0] == "od_klienta" and WPIS_WEWNETRZNY in rodzaje and WPIS_DO_KLIENTA in rodzaje
    assert STATUS_NOWE not in rodzaje


def test_zapis_skrzynki_bez_wypelnionych_wyborow_nie_wywala_zapisu(client, tenant_a, make_user):
    """Pola wymagane przez baze musza dostac wartosc domyslna, a nie pustke -
    inaczej zapis konczy sie bledem zamiast zapisanymi ustawieniami."""
    make_user(None, "operator@mojadomena.pl", HASLO)
    _login(client, "operator@mojadomena.pl", HASLO)

    odpowiedz = client.post("/helpdesk/skrzynka", data={
        "csrf_token": _csrf(client, "/helpdesk/skrzynka"),
        "imap_host": "imap.mojadomena.pl", "imap_folder": "",
        "imap_szyfrowanie": "", "imap_po_pobraniu": "", "smtp_szyfrowanie": "",
        "nadawca": "helpdesk@mojadomena.pl",
    }, follow_redirects=False)

    assert odpowiedz.status_code == 303
    with SessionLocal() as db:
        ustawienia = db.get(HelpdeskUstawienia, "helpdesk")
        assert ustawienia.imap_folder == "INBOX"
        assert ustawienia.imap_szyfrowanie == "ssl"
