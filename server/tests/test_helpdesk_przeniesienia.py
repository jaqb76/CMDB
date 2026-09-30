"""Transfer firmy: realne transakcje PostgreSQL, granice dostepu i poczta."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    Asset, AuditLog, CzasPracy, HelpdeskFirma, NierozpoznanaWiadomosc, PortalUser,
    PrzeniesienieZgloszenia, Tenant, WpisSlownika, WpisZgloszenia, ZalacznikWpisu,
    Zgloszenie, ZgloszenieSprzet, STATUS_W_TRAKCIE, WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY, utcnow,
)
from cmdb_server.services import helpdesk, helpdesk_przeniesienia as transfer
from cmdb_server.services import helpdesk_poczta as poczta, helpdesk_wysylka

from .test_helpdesk_ui import HASLO, _firma, _technik, _zgloszenie, _skrzynka
from .test_tenant_isolation import _login, _extract_csrf
from .test_helpdesk_poczta import _mail
from .test_helpdesk_skrzynka import AtrapaSMTP
from .test_mobile_helpdesk import _naglowki


@pytest.fixture
def sprawa(tenant_a, tenant_b, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "helpdesk_dir", str(tmp_path))
    a, b = tenant_a["id"], tenant_b["id"]
    _firma(a, "ALF", ["alfa.pl"])
    _firma(b, "BET", ["beta.pl"])
    technik = _technik("technik@operator.pl", [a, b])
    stary = _technik("stary@operator.pl", [a])
    nowy = _technik("nowy@operator.pl", [b])
    z_id = _zgloszenie(a, email="jan@alfa.pl")
    _zgloszenie(b, temat="Istniejaca sprawa", email="inna@beta.pl")
    with SessionLocal() as db:
        kontakt = WpisSlownika(tenant_id=b, kategoria="osoba", wartosc="Anna Nowak",
                              klucz="anna-nowak", atrybuty={"email": "anna@beta.pl"})
        db.add(kontakt)
        sprzet = Asset(tenant_id=a, hostname="ALFA-LAP-23", machine_id="transfer-laptop")
        db.add(sprzet)
        db.flush()
        z = db.get(Zgloszenie, z_id)
        helpdesk.podepnij_sprzet(db, z, sprzet)
        helpdesk.przypisz(db, z, db.get(PortalUser, stary), "test")
        helpdesk.zmien_status(db, z, STATUS_W_TRAKCIE, "test")
        helpdesk.dodaj_czas(db, z, db.get(PortalUser, stary), 35, "Diagnoza")
        pierwszy = helpdesk.pierwszy_wpis(db, z_id)
        pierwszy.dw = ["szef@alfa.pl"]
        pierwszy.message_id = "<stary-watek@alfa.pl>"
        wpis = helpdesk.dopisz_wiadomosc(db, z, rodzaj=WPIS_WEWNETRZNY,
            tresc="Notatka tylko dla technikow", autor=db.get(PortalUser, stary))
        db.add(ZalacznikWpisu(wpis_id=pierwszy.id, zgloszenie_id=z_id, nazwa="obraz.png",
            typ_mime="image/png", sciezka="obraz.png", rozmiar=11))
        db.flush()
        zalacznik = db.scalar(select(ZalacznikWpisu).where(ZalacznikWpisu.zgloszenie_id == z_id))
        db.commit()
        wynik = dict(a=a, b=b, id=z_id, technik=technik, stary=stary, nowy=nowy,
                     kontakt=kontakt.id, sprzet=sprzet.id, zalacznik=zalacznik.id,
                     notatka=wpis.id, pierwszy=pierwszy.id, utworzono=z.utworzono,
                     numer=z.numer_pelny)
    (tmp_path / "obraz.png").write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"png")
    return wynik


@pytest.fixture
def smtp(monkeypatch):
    AtrapaSMTP.wyslane = []
    monkeypatch.setattr(helpdesk_wysylka, "_polaczenie", lambda _: AtrapaSMTP())
    return AtrapaSMTP


def _wybor(s, **zmiany):
    return dict(tenant_id=s["b"], kontakt_id=s["kontakt"], technik_id=s["nowy"],
                powod="Błędnie przypisana firma") | zmiany


def _przygotuj(s, user_id=None, **zmiany):
    with SessionLocal() as db:
        return transfer.przygotuj(db, db.get(PortalUser, user_id or s["technik"]),
                                 db.get(Zgloszenie, s["id"]), **_wybor(s, **zmiany))["token"]


def _przenies(s, **zmiany):
    token = _przygotuj(s, **zmiany)
    with SessionLocal() as db:
        transfer.zatwierdz(db, db.get(PortalUser, s["technik"]), s["id"], token)
        db.commit()
    return token


def _ui_podglad(client, s, **zmiany):
    url = f'/helpdesk/zgloszenie/{s["id"]}/przenies'
    csrf = _extract_csrf(client.get(url).text)
    odpowiedz = client.post(url + "/podglad", data={"csrf_token": csrf, **_wybor(s, **zmiany)})
    assert odpowiedz.status_code == 200, odpowiedz.text
    token = re.search(r'name="potwierdzenie" value="([^"]+)"', odpowiedz.text)
    assert token is not None, odpowiedz.text
    return url, csrf, token.group(1)


def test_transfer_przenosi_calosc_bez_kopii_i_kolizji_numeru(sprawa):
    s = sprawa
    _przenies(s)
    with SessionLocal() as db:
        z = db.get(Zgloszenie, s["id"])
        assert z.tenant_id == s["b"]
        assert z.numer_pelny == s["numer"] == "ALF-1"
        assert z.numer == 2  # BET-1 juz istnialo; numer publiczny sie nie zmienia.
        assert z.utworzono == s["utworzono"] and z.status == STATUS_W_TRAKCIE
        assert z.technik_id == s["nowy"] and z.zglaszajacy_email == "anna@beta.pl"
        assert db.get(WpisZgloszenia, s["notatka"]).rodzaj == WPIS_WEWNETRZNY
        assert db.get(WpisZgloszenia, s["pierwszy"]).autor_email == "jan@alfa.pl"
        assert db.get(WpisZgloszenia, s["pierwszy"]).dw == ["szef@alfa.pl"]
        assert db.get(ZalacznikWpisu, s["zalacznik"]).sciezka == "obraz.png"
        assert db.get(Asset, s["sprzet"]).tenant_id == s["a"]
        assert not db.scalars(select(ZgloszenieSprzet).where(ZgloszenieSprzet.zgloszenie_id == z.id)).all()
        czas = db.scalar(select(CzasPracy).where(CzasPracy.zgloszenie_id == z.id))
        assert (czas.tenant_id, czas.minuty, czas.technik_id) == (s["b"], 35, s["stary"])
        zapis = db.scalar(select(PrzeniesienieZgloszenia))
        assert (zapis.firma_z, zapis.firma_do, zapis.autor) == (s["a"], s["b"], "technik@operator.pl")
        assert zapis.szczegoly["sprzet_odpiety"] == [s["sprzet"]]
        assert zapis.szczegoly["kontakt_z"]["email"] == "jan@alfa.pl"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "helpdesk.zgloszenie.przeniesienie")).target == z.id
        nastepne = helpdesk.utworz_zgloszenie(db, tenant_id=s["b"], temat="Nowe", tresc="Nowe",
                                            zglaszajacy_email="anna@beta.pl")
        assert nastepne.numer_pelny == "BET-3"


@pytest.mark.parametrize("email", ["technik@operator.pl", "root@operator.pl"])
def test_www_podglad_potwierdzenie_i_historia(client, sprawa, make_user, email):
    if email.startswith("root"):
        make_user(None, email, HASLO)
    _login(client, email, HASLO)
    url, csrf, token = _ui_podglad(client, sprawa)
    # Sam podglad nie zapisuje transferu.
    with SessionLocal() as db:
        assert db.get(Zgloszenie, sprawa["id"]).tenant_id == sprawa["a"]
    response = client.post(url, data={"csrf_token": csrf, "potwierdzenie": token,
                                     "potwierdzam": "1"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and "przeniesiono zgłoszenie" in page.text
    assert "ALF-1" in page.text and "anna@beta.pl" in page.text


def test_lista_firm_i_opcje_nie_ujawniaja_cudzych_danych(client, sprawa):
    s = sprawa
    with SessionLocal() as db:
        firma_c = Tenant(name="TAJNA-FIRMA-C", slug="tajna-c")
        db.add(firma_c); db.flush()
        helpdesk.zapewnij_firme(db, firma_c, "TAJ")
        c = firma_c.id; db.commit()
    _login(client, "technik@operator.pl", HASLO)
    url = f'/helpdesk/zgloszenie/{s["id"]}/przenies'
    page = client.get(url)
    assert "TAJNA-FIRMA-C" not in page.text
    assert client.get(url + "/opcje", params={"tenant_id": c}).status_code == 403
    osoby = client.get(url + "/opcje", params={"tenant_id": s["b"]}).json()
    assert [k["id"] for k in osoby["contacts"]] == [s["kontakt"]]
    assert s["stary"] not in {k["id"] for k in osoby["technicians"]}
    assert {s["technik"], s["nowy"]} <= {k["id"] for k in osoby["technicians"]}


@pytest.mark.parametrize("user_key", ["stary", "nowy"])
def test_usluga_wymaga_uprawnien_do_obu_firm(sprawa, user_key):
    with pytest.raises(transfer.BrakDostepu):
        _przygotuj(sprawa, user_id=sprawa[user_key])


@pytest.mark.parametrize("zmiana", ["audytor", "viewer", "konto_firmy", "nieaktywne"])
def test_sam_podglad_lub_konto_firmy_nie_uprawnia(sprawa, zmiana):
    with SessionLocal() as db:
        user = db.get(PortalUser, sprawa["technik"])
        if zmiana == "audytor": user.is_global_viewer = True
        if zmiana == "viewer":
            user.role = "viewer"
            helpdesk.odbierz_dostep(db, user.id, sprawa["a"])
            helpdesk.odbierz_dostep(db, user.id, sprawa["b"])
        if zmiana == "konto_firmy": user.tenant_id = sprawa["a"]
        if zmiana == "nieaktywne": user.is_active = False
        db.commit()
    with pytest.raises(transfer.BrakDostepu):
        _przygotuj(sprawa)


def test_dostepy_helpdesku_daja_prawo_technika_niezaleznie_od_ogolnej_roli(sprawa):
    with SessionLocal() as db:
        db.get(PortalUser, sprawa["technik"]).role = "viewer"
        db.commit()
    _przenies(sprawa)
    with SessionLocal() as db:
        assert db.get(Zgloszenie, sprawa["id"]).tenant_id == sprawa["b"]


@pytest.mark.parametrize("pole", ["kontakt_id", "technik_id", "tenant_id", "powod"])
def test_odrzuca_niespojny_wybor_bez_czesciowych_zapisow(sprawa, pole):
    s = sprawa
    if pole == "kontakt_id":
        with SessionLocal() as db:
            k = WpisSlownika(tenant_id=s["a"], kategoria="osoba", wartosc="Jan", klucz="jan",
                             atrybuty={"email": "jan@alfa.pl"})
            db.add(k); db.commit(); wartosc = k.id
    else:
        wartosc = {"technik_id": s["stary"], "tenant_id": s["a"], "powod": "   "}[pole]
    with pytest.raises(helpdesk.BladHelpdesku):
        _przygotuj(s, **{pole: wartosc})
    with SessionLocal() as db:
        assert db.get(Zgloszenie, s["id"]).tenant_id == s["a"]
        assert db.get(HelpdeskFirma, s["b"]).licznik == 1
        assert not db.scalars(select(PrzeniesienieZgloszenia)).all()


@pytest.mark.parametrize("zmiana", ["dostep_z", "dostep_do", "firma", "helpdesk", "kontakt", "zgloszenie"])
def test_zmiana_po_podgladzie_blokuje_zatwierdzenie(sprawa, zmiana):
    s = sprawa; token = _przygotuj(s)
    with SessionLocal() as db:
        if zmiana == "dostep_z": helpdesk.odbierz_dostep(db, s["technik"], s["a"])
        elif zmiana == "dostep_do": helpdesk.odbierz_dostep(db, s["technik"], s["b"])
        elif zmiana == "firma": db.get(Tenant, s["b"]).is_active = False
        elif zmiana == "helpdesk": db.get(HelpdeskFirma, s["b"]).aktywna = False
        elif zmiana == "kontakt": db.get(WpisSlownika, s["kontakt"]).atrybuty = {"email": "inna@beta.pl"}
        else: helpdesk.zmien_status(db, db.get(Zgloszenie, s["id"]), "oczekuje", "inny technik")
        db.commit()
    with SessionLocal() as db, pytest.raises(helpdesk.BladHelpdesku):
        transfer.zatwierdz(db, db.get(PortalUser, s["technik"]), s["id"], token)
    with SessionLocal() as db:
        assert db.get(Zgloszenie, s["id"]).tenant_id == s["a"]
        assert db.get(HelpdeskFirma, s["b"]).licznik == 1


def test_csrf_potwierdzenie_i_powtorzenie_zadania(client, sprawa):
    _login(client, "technik@operator.pl", HASLO)
    url, csrf, token = _ui_podglad(client, sprawa)
    assert client.post(url, data={"potwierdzenie": token, "potwierdzam": "1"}).status_code == 403
    assert client.post(url, data={"csrf_token": csrf, "potwierdzenie": token}).status_code == 400
    assert client.post(url, data={"csrf_token": csrf, "potwierdzenie": token + "x", "potwierdzam": "1"}).status_code == 409
    data = {"csrf_token": csrf, "potwierdzenie": token, "potwierdzam": "1"}
    assert client.post(url, data=data, follow_redirects=False).status_code == 303
    assert client.post(url, data=data).status_code == 409
    with SessionLocal() as db:
        assert len(db.scalars(select(PrzeniesienieZgloszenia)).all()) == 1


def test_token_innego_konta_i_wygasly_nie_przechodzi(sprawa, monkeypatch):
    import itsdangerous.timed
    s = sprawa; token = _przygotuj(s)
    with SessionLocal() as db, pytest.raises(transfer.BrakDostepu):
        transfer.zatwierdz(db, db.get(PortalUser, s["nowy"]), s["id"], token)
    czas = itsdangerous.timed.TimestampSigner.get_timestamp
    monkeypatch.setattr(itsdangerous.timed.TimestampSigner, "get_timestamp", lambda self: czas(self) + 1000)
    with SessionLocal() as db, pytest.raises(transfer.Konflikt):
        transfer.zatwierdz(db, db.get(PortalUser, s["technik"]), s["id"], token)


def test_po_transferze_stare_linki_www_i_mobilne_nie_daja_dostepu(client, sprawa):
    s = sprawa; _przenies(s)
    for email, kod in [("stary@operator.pl", 404), ("nowy@operator.pl", 200)]:
        _login(client, email, HASLO)
        assert client.get(f'/helpdesk/zgloszenie/{s["id"]}').status_code == kod
        for suffix in ["", "/podglad"]:
            assert client.get(f'/helpdesk/zalacznik/{s["zalacznik"]}{suffix}').status_code == kod
        headers = _naglowki(client, email, HASLO)
        assert client.get(f'/api/v1/mobile/helpdesk/tickets/{s["id"]}', headers=headers).status_code == kod
        for suffix in ["", "/preview"]:
            assert client.get(f'/api/v1/mobile/helpdesk/attachments/{s["zalacznik"]}{suffix}', headers=headers).status_code == kod


def test_api_mobilne_uzywa_tych_samych_uprawnien_i_potwierdzenia(client, sprawa):
    s = sprawa; headers = _naglowki(client, "technik@operator.pl", HASLO)
    url = f'/api/v1/mobile/helpdesk/tickets/{s["id"]}/transfer'
    options = client.get(url + "/options", headers=headers).json()
    assert [f["id"] for f in options["companies"]] == [s["b"]]
    preview = client.post(url + "/preview", headers=headers, json={"tenant_id": s["b"],
        "contact_id": s["kontakt"], "technician_id": s["nowy"], "reason": "Zła firma"})
    assert preview.status_code == 200, preview.text
    token = preview.json()["confirmation_token"]
    assert client.post(url, headers=headers, json={"confirmation_token": token}).status_code == 400
    response = client.post(url, headers=headers, json={"confirmation_token": token, "confirmed": True})
    assert response.status_code == 200 and response.json()["tenant_id"] == s["b"]
    assert response.json()["number"] == s["numer"]


def test_nowi_odbiorcy_bez_starych_dw_i_naglowkow(sprawa, smtp):
    s = sprawa; _skrzynka(); _przenies(s)
    assert smtp.wyslane == []
    with SessionLocal() as db:
        helpdesk_wysylka.odpowiedz_klientowi(db, db.get(Zgloszenie, s["id"]), "Nowa odpowiedź",
                                          db.get(PortalUser, s["technik"]))
        db.commit()
    mail = smtp.wyslane[-1]
    assert mail["To"] == "anna@beta.pl"
    assert mail["Cc"] is None and mail["In-Reply-To"] is None and mail["References"] is None


def test_mail_starej_firmy_trafia_do_weryfikacji_nowej_do_watku(sprawa):
    s = sprawa; _przenies(s)
    with SessionLocal() as db:
        stary = poczta.przyjmij(db, poczta.przeczytaj(_mail(nadawca="jan@alfa.pl", temat="Re: [ALF-1]",
                         in_reply_to="<stary-watek@alfa.pl>", message_id="<po-transferze@alfa.pl>")))
        assert stary.decyzja == poczta.DECYZJA_NIEROZPOZNANA
        nowy = poczta.przyjmij(db, poczta.przeczytaj(_mail(nadawca="anna@beta.pl", temat="Re: [ALF-1]",
                         in_reply_to="<stary-watek@alfa.pl>", message_id="<po-transferze@beta.pl>")))
        assert nowy.decyzja == poczta.DECYZJA_DOPISZ and nowy.zgloszenie.id == s["id"]
        db.commit()
        assert db.scalar(select(NierozpoznanaWiadomosc).where(
            NierozpoznanaWiadomosc.message_id == "<po-transferze@alfa.pl>")) is not None


def test_dw_po_transferze_filtruje_obce_domeny(sprawa, smtp):
    s = sprawa; _skrzynka(); _przenies(s)
    with SessionLocal() as db:
        poczta.przyjmij(db, poczta.przeczytaj(_mail(nadawca="anna@beta.pl", temat="Re: [ALF-1]",
            message_id="<nowy-dw@beta.pl>", dodatkowe=["Cc: szef@beta.pl, szef@alfa.pl, obcy@gmail.com"])))
        db.commit()
        helpdesk_wysylka.odpowiedz_klientowi(db, db.get(Zgloszenie, s["id"]), "Odpowiedź", db.get(PortalUser, s["technik"]))
        db.commit()
    assert smtp.wyslane[-1]["Cc"] == "szef@beta.pl"


def test_bez_kontaktu_wstrzymuje_wysylke_i_pozwala_ustawic_go_pozniej(client, sprawa, smtp):
    s = sprawa; _skrzynka(); _przenies(s, kontakt_id="")
    with SessionLocal() as db, pytest.raises(helpdesk_wysylka.BladWysylki):
        helpdesk_wysylka.odpowiedz_klientowi(db, db.get(Zgloszenie, s["id"]), "Nie wysyłaj",
                                          db.get(PortalUser, s["technik"]))
    assert smtp.wyslane == []
    _login(client, "technik@operator.pl", HASLO)
    url = f'/helpdesk/zgloszenie/{s["id"]}'
    csrf = _extract_csrf(client.get(url).text)
    assert client.post(url + "/kontakt", data={"csrf_token": csrf, "kontakt_id": s["kontakt"]},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.get(Zgloszenie, s["id"]).zglaszajacy_email == "anna@beta.pl"


def test_niewyslana_stara_wiadomosc_nie_wychodzi_do_nowej_firmy(sprawa, smtp):
    s = sprawa; _skrzynka()
    with SessionLocal() as db:
        w = helpdesk.dopisz_wiadomosc(db, db.get(Zgloszenie, s["id"]), rodzaj=WPIS_DO_KLIENTA,
                                    tresc="Stara niewysłana odpowiedź", autor=db.get(PortalUser, s["stary"]))
        db.commit(); wpis_id = w.id
    _przenies(s)
    with SessionLocal() as db, pytest.raises(helpdesk_wysylka.BladWysylki):
        helpdesk_wysylka.wyslij_wpis(db, db.get(Zgloszenie, s["id"]), db.get(WpisZgloszenia, wpis_id))
    assert smtp.wyslane == []


def test_blad_w_srodku_operacji_wycofuje_wszystkie_zmiany(sprawa, monkeypatch):
    s = sprawa; token = _przygotuj(s)
    def awaria(*args, **kwargs): raise RuntimeError("symulowana awaria zapisu audytu")
    monkeypatch.setattr(transfer, "audit", awaria)
    with SessionLocal() as db, pytest.raises(RuntimeError):
        transfer.zatwierdz(db, db.get(PortalUser, s["technik"]), s["id"], token)
    with SessionLocal() as db:
        assert db.get(Zgloszenie, s["id"]).tenant_id == s["a"]
        assert db.get(HelpdeskFirma, s["b"]).licznik == 1
        assert db.scalar(select(CzasPracy).where(CzasPracy.zgloszenie_id == s["id"])).tenant_id == s["a"]
        assert db.scalar(select(ZgloszenieSprzet).where(ZgloszenieSprzet.zgloszenie_id == s["id"])) is not None
        assert not db.scalars(select(PrzeniesienieZgloszenia)).all()


def test_rownoczesne_zatwierdzenia_wykonuja_tylko_jeden_transfer(sprawa):
    s = sprawa; token = _przygotuj(s); wystartowal = Event()
    def druga_proba():
        with SessionLocal() as db:
            user = db.get(PortalUser, s["technik"])
            wystartowal.set()
            with pytest.raises(transfer.Konflikt):
                transfer.zatwierdz(db, user, s["id"], token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal() as db:
            transfer.zatwierdz(db, db.get(PortalUser, s["technik"]), s["id"], token)
            future = executor.submit(druga_proba)
            assert wystartowal.wait(5)
            db.commit()
        future.result(timeout=10)
    with SessionLocal() as db:
        assert len(db.scalars(select(PrzeniesienieZgloszenia)).all()) == 1
