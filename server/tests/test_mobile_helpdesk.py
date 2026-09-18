"""Helpdesk w API aplikacji Android.

Sprawdzamy trzy rzeczy naraz: czy telefon widzi to samo co panel, czy granica
firm trzyma sie takze tutaj (identyfikator zgloszenia w adresie jest
najkrotsza droga do cudzych danych) i czy zalacznik doczepiony na telefonie
faktycznie wychodzi do klienta, a nie tylko laduje w bazie.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    STATUS_OCZEKUJE,
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    Asset,
    HelpdeskUstawienia,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    ZalacznikWpisu,
    Zgloszenie,
)
from cmdb_server.security import hash_password
from cmdb_server.services import helpdesk, helpdesk_wysylka, sekrety

from .test_helpdesk_skrzynka import AtrapaSMTP

HASLO = "haslo-aplikacji-2026"
# Najmniejszy poprawny PNG - podglad sprawdza poczatek pliku, a nie nazwe.
PNG = bytes.fromhex("89504e470d0a1a0a") + b"reszta-obrazka"


@pytest.fixture
def smtp(monkeypatch):
    AtrapaSMTP.wyslane = []
    monkeypatch.setattr(helpdesk_wysylka, "_polaczenie", lambda konfiguracja: AtrapaSMTP())
    return AtrapaSMTP


@pytest.fixture
def zalaczniki(tmp_path, monkeypatch):
    """Zalaczniki testu ladują w katalogu tymczasowym, nie w repozytorium."""
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "helpdesk_dir", str(tmp_path))
    return tmp_path


def _firma(tenant_id: str, skrot: str, domeny: tuple[str, ...]) -> None:
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


def _skrzynka() -> None:
    with SessionLocal() as db:
        db.add(HelpdeskUstawienia(
            klucz="helpdesk", imap_host="imap.mojadomena.pl",
            smtp_host="smtp.mojadomena.pl", smtp_uzytkownik="helpdesk@mojadomena.pl",
            smtp_haslo_szyfr=sekrety.zaszyfruj("tajne"),
            nadawca="helpdesk@mojadomena.pl", nazwa_nadawcy="Helpdesk", aktywne=True,
        ))
        db.commit()


def _zgloszenie(tenant_id: str, temat: str = "Nie dziala VPN",
                email: str = "jan@bongo.pl") -> str:
    with SessionLocal() as db:
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_id, temat=temat, tresc="Od rana nie dziala.",
            zglaszajacy_email=email, message_id=f"<{temat[:5]}@bongo.pl>",
        )
        db.commit()
        return zgloszenie.id


def _naglowki(client, email: str, haslo: str = HASLO) -> dict:
    odpowiedz = client.post(
        "/api/v1/mobile/auth/login", json={"email": email, "password": haslo}
    )
    assert odpowiedz.status_code == 200, odpowiedz.text
    return {"Authorization": "Bearer " + odpowiedz.json()["access_token"]}


def _dane(**pola) -> dict:
    """Czesc ``dane`` multipartu - pola formularza jada jednym kawalkiem JSON."""
    return {"dane": json.dumps(pola)}


# --- zakres i dostep --------------------------------------------------------

def test_technik_widzi_zgloszenia_swoich_firm_a_nie_cudze(client, tenant_a, tenant_b):
    """Zakres wyznaczaja firmy helpdesku konta, a nie naglowek X-CMDB-Tenant."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    _zgloszenie(tenant_a["id"], "Awaria drukarki")
    _zgloszenie(tenant_b["id"], "Cudza sprawa", email="ala@kotki.pl")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])

    naglowki = _naglowki(client, "technik@mojadomena.pl")
    # Naglowek firmy wskazuje firme B, a mimo to widac wylacznie zgloszenia A:
    # helpdesk chodzi po firmach konta, nie po przelaczonej firmie aplikacji.
    naglowki["X-CMDB-Tenant"] = tenant_b["slug"]
    odpowiedz = client.get("/api/v1/mobile/helpdesk/tickets", headers=naglowki)

    assert odpowiedz.status_code == 200
    tematy = [pozycja["subject"] for pozycja in odpowiedz.json()["items"]]
    assert tematy == ["Awaria drukarki"]
    assert odpowiedz.json()["counters"]["open"] == 1


def test_konto_bez_helpdesku_dostaje_katalog_bez_dostepu(client, tenant_a, make_user):
    """Brak dostepu to stan konta, a nie awaria - katalog mowi o tym wprost."""
    make_user(tenant_a["id"], "zwykly@a.pl", HASLO)
    naglowki = _naglowki(client, "zwykly@a.pl")

    katalog = client.get("/api/v1/mobile/helpdesk/catalog", headers=naglowki)
    lista = client.get("/api/v1/mobile/helpdesk/tickets", headers=naglowki)

    assert katalog.status_code == 200
    assert katalog.json()["available"] is False
    # Same zgloszenia juz sa zamkniete - aplikacja ma ukryc zakladke, a nie
    # probowac czytac liste.
    assert lista.status_code == 403


def test_cudze_zgloszenie_jest_nie_do_odroznienia_od_nieistniejacego(client, tenant_a, tenant_b):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    cudze = _zgloszenie(tenant_b["id"], "Cudza sprawa", email="ala@kotki.pl")
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    karta = client.get(f"/api/v1/mobile/helpdesk/tickets/{cudze}", headers=naglowki)
    wymyslone = client.get("/api/v1/mobile/helpdesk/tickets/nie-ma-takiego", headers=naglowki)

    assert karta.status_code == 404
    assert wymyslone.status_code == 404
    assert karta.json()["detail"] == wymyslone.json()["detail"]


# --- zakladanie zgloszenia --------------------------------------------------

def test_zgloszenie_z_telefonu_ma_numer_watek_i_sprzet(client, tenant_a, smtp, zalaczniki):
    """Droga jest ta sama co w panelu: numer, pierwszy wpis, slad w historii."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _skrzynka()
    technik_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    with SessionLocal() as db:
        asset = Asset(tenant_id=tenant_a["id"], machine_id="a-1", hostname="LAPTOP-023")
        db.add(asset)
        db.commit()
        asset_id = asset.id

    naglowki = _naglowki(client, "technik@mojadomena.pl")
    odpowiedz = client.post(
        "/api/v1/mobile/helpdesk/tickets",
        headers=naglowki,
        data=_dane(
            tenant_id=tenant_a["id"], requester_email="Jan@Bongo.pl",
            requester_name="Jan Kowalski", subject="Brak dostepu do VPN",
            content="Po aktualizacji nie moze polaczyc sie z siecia.",
            type="incydent", source="telefon", asset_id=asset_id,
            assign_to_me=True, notify_customer=True,
        ),
        files=[("pliki", ("blad-vpn.png", PNG, "image/png"))],
    )

    assert odpowiedz.status_code == 201, odpowiedz.text
    assert odpowiedz.json()["number"] == "BON-1"
    with SessionLocal() as db:
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        assert zgloszenie.zglaszajacy_email == "jan@bongo.pl"
        assert zgloszenie.technik_id == technik_id
        assert zgloszenie.status == STATUS_W_TRAKCIE
        assert [asset.hostname for asset, _ in helpdesk.sprzet_zgloszenia(db, zgloszenie.id)] == ["LAPTOP-023"]

        pierwszy = helpdesk.pierwszy_wpis(db, zgloszenie.id)
        assert pierwszy.rodzaj == "od_klienta"
        plik = db.execute(select(ZalacznikWpisu)).scalar_one()
        assert plik.wpis_id == pierwszy.id
        assert plik.nazwa == "blad-vpn.png"
        assert (zalaczniki / "zalaczniki" / plik.sciezka).read_bytes() == PNG

    # Klient dostal numer, wiec ma na co odpisac.
    assert len(smtp.wyslane) == 1


def test_zgloszenie_na_adres_innej_firmy_jest_odrzucane(client, tenant_a, tenant_b):
    """Firme wybiera tu czlowiek, wiec tylko tutaj trzeba pilnowac domeny."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"], tenant_b["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        "/api/v1/mobile/helpdesk/tickets",
        headers=naglowki,
        data=_dane(
            tenant_id=tenant_a["id"], requester_email="ala@kotki.pl",
            subject="Pomylka", content="Adres nalezy do innej firmy.",
        ),
    )

    assert odpowiedz.status_code == 400
    assert "kotki.pl" in odpowiedz.json()["detail"]
    with SessionLocal() as db:
        assert db.execute(select(Zgloszenie)).first() is None


def test_zgloszenie_dla_nieobslugiwanej_firmy_jest_odrzucane(client, tenant_a, tenant_b):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        "/api/v1/mobile/helpdesk/tickets",
        headers=naglowki,
        data=_dane(
            tenant_id=tenant_b["id"], requester_email="ala@kotki.pl",
            subject="Cudza firma", content="Nie moje.",
        ),
    )

    assert odpowiedz.status_code == 403


# --- watek ------------------------------------------------------------------

def test_odpowiedz_wychodzi_z_zalacznikiem_i_przestawia_status(
    client, tenant_a, smtp, zalaczniki
):
    """Plik doczepiony na telefonie ma wyjsc RAZEM z odpowiedzia."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _skrzynka()
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/messages",
        headers=naglowki,
        data=_dane(content="Sprawdzam konfiguracje, w zalaczeniu instrukcja.",
                   kind=WPIS_DO_KLIENTA),
        files=[("pliki", ("instrukcja.png", PNG, "image/png"))],
    )

    assert odpowiedz.status_code == 201, odpowiedz.text
    assert odpowiedz.json()["sent"] is True
    assert len(smtp.wyslane) == 1
    nazwy = [czesc.get_filename() for czesc in smtp.wyslane[0].iter_attachments()]
    assert nazwy == ["instrukcja.png"]
    with SessionLocal() as db:
        zgloszenie = db.get(Zgloszenie, zgloszenie_id)
        # Napisalismy do klienta, wiec pilka jest po jego stronie.
        assert zgloszenie.status == STATUS_OCZEKUJE
        wpis = db.execute(
            select(WpisZgloszenia).where(WpisZgloszenia.rodzaj == WPIS_DO_KLIENTA)
        ).scalar_one()
        assert wpis.blad_wysylki is None
        assert wpis.wyslano_o is not None


def test_notatka_wewnetrzna_nie_wychodzi_do_klienta(client, tenant_a, smtp, zalaczniki):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _skrzynka()
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/messages",
        headers=naglowki,
        data=_dane(content="Sprawdzic polityke VPN.", kind=WPIS_WEWNETRZNY),
    )

    assert odpowiedz.status_code == 201
    assert smtp.wyslane == []
    with SessionLocal() as db:
        wpis = db.execute(
            select(WpisZgloszenia).where(WpisZgloszenia.rodzaj == WPIS_WEWNETRZNY)
        ).scalar_one()
        assert wpis.tresc == "Sprawdzic polityke VPN."
        # Pierwsza odpowiedz na sprawe zdejmuje z niej etykiete "nowe".
        assert db.get(Zgloszenie, zgloszenie_id).status == STATUS_W_TRAKCIE


def test_nieznany_rodzaj_wpisu_nie_przechodzi(client, tenant_a):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/messages",
        headers=naglowki,
        data=_dane(content="Cokolwiek", kind="system"),
    )

    assert odpowiedz.status_code == 400


def test_karta_pokazuje_watek_czekanie_i_czas(client, tenant_a, zalaczniki):
    """Karta ma dac telefonowi wszystko, co pokazuje ekran szczegolow."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    technik_id = _technik("technik@mojadomena.pl", [tenant_a["id"]])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    client.post(f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/technician",
                headers=naglowki, json={"technician_id": technik_id})
    client.post(f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/time",
                headers=naglowki, json={"minutes": 45, "description": "diagnostyka"})
    karta = client.get(f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}", headers=naglowki)

    assert karta.status_code == 200
    dane = karta.json()
    assert dane["ticket"]["number"] == "BON-1"
    assert dane["ticket"]["technician"] == "Wladek Nowak"
    # Zgloszenie z mailem klienta i bez naszej odpowiedzi czeka na nas.
    assert dane["ticket"]["waiting"] is True
    assert dane["ticket"]["waiting_since"]
    assert dane["time"]["total"] == 45
    assert dane["time"]["total_label"] == "45 min"
    rodzaje = [wpis["kind"] for wpis in dane["entries"]]
    assert rodzaje[0] == "od_klienta"
    assert "system" in rodzaje


def test_zamkniecie_zawiadamia_klienta(client, tenant_a, smtp):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _skrzynka()
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        f"/api/v1/mobile/helpdesk/tickets/{zgloszenie_id}/status",
        headers=naglowki,
        json={"status": STATUS_ZAMKNIETE, "summary": "Wymieniony certyfikat."},
    )

    assert odpowiedz.status_code == 200
    with SessionLocal() as db:
        assert db.get(Zgloszenie, zgloszenie_id).status == STATUS_ZAMKNIETE
    assert len(smtp.wyslane) == 1
    assert "Wymieniony certyfikat." in smtp.wyslane[0].get_content()


# --- zalaczniki -------------------------------------------------------------

def test_zalacznik_cudzego_zgloszenia_jest_niedostepny(client, tenant_a, tenant_b, zalaczniki):
    """Sprawdzamy firme zgloszenia, a nie sam identyfikator zalacznika."""
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    cudze = _zgloszenie(tenant_b["id"], "Cudza sprawa", email="ala@kotki.pl")
    with SessionLocal() as db:
        from cmdb_server.services import helpdesk_poczta as poczta

        wpis = helpdesk.pierwszy_wpis(db, cudze)
        plik = poczta.zapisz_zalaczniki(
            db, wpis, (poczta.Zalacznik(nazwa="tajne.png", typ_mime="image/png", dane=PNG),)
        )[0]
        db.commit()
        plik_id = plik.id

    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    pobranie = client.get(f"/api/v1/mobile/helpdesk/attachments/{plik_id}", headers=naglowki)
    podglad = client.get(
        f"/api/v1/mobile/helpdesk/attachments/{plik_id}/preview", headers=naglowki
    )

    assert pobranie.status_code == 404
    assert podglad.status_code == 404


def test_zalacznik_swojego_zgloszenia_pobiera_sie_i_ma_podglad(client, tenant_a, zalaczniki):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    zgloszenie_id = _zgloszenie(tenant_a["id"])
    with SessionLocal() as db:
        from cmdb_server.services import helpdesk_poczta as poczta

        wpis = helpdesk.pierwszy_wpis(db, zgloszenie_id)
        plik = poczta.zapisz_zalaczniki(
            db, wpis, (poczta.Zalacznik(nazwa="zrzut.png", typ_mime="image/png", dane=PNG),)
        )[0]
        db.commit()
        plik_id = plik.id

    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    pobranie = client.get(f"/api/v1/mobile/helpdesk/attachments/{plik_id}", headers=naglowki)
    podglad = client.get(
        f"/api/v1/mobile/helpdesk/attachments/{plik_id}/preview", headers=naglowki
    )

    assert pobranie.status_code == 200
    # Tresc od klienta nie ma prawa wykonac sie na telefonie, wiec pobranie
    # zawsze jest strumieniem bajtow.
    assert pobranie.headers["content-type"] == "application/octet-stream"
    assert pobranie.content == PNG
    assert podglad.status_code == 200
    assert podglad.headers["content-type"] == "image/png"


def test_zbyt_duzy_plik_nie_zaklada_zgloszenia(client, tenant_a, zalaczniki, monkeypatch):
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "helpdesk_zalacznik_mb", 1)
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    odpowiedz = client.post(
        "/api/v1/mobile/helpdesk/tickets",
        headers=naglowki,
        data=_dane(tenant_id=tenant_a["id"], requester_email="jan@bongo.pl",
                   subject="Za duzy plik", content="Zalacznik nie miesci sie w limicie."),
        files=[("pliki", ("wielki.bin", b"x" * (2 * 1024 * 1024), "application/octet-stream"))],
    )

    assert odpowiedz.status_code == 413
    with SessionLocal() as db:
        # Odrzucony plik nie zostawia zgloszenia zalozonego w polowie.
        assert db.execute(select(Zgloszenie)).first() is None


# --- firmy technika w aplikacji ---------------------------------------------

def test_technik_bez_wlasnej_firmy_widzi_firmy_z_helpdesku(client, tenant_a, tenant_b):
    """Technik nie nalezy do zadnej firmy - jego uprawnienie to dostepy helpdesku.

    Bez tego aplikacja pokazywala mu "Wybierz firmę / Brak dostępnych firm"
    i nie dalo sie z tego ekranu wyjsc, mimo nadanego dostepu.
    """
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    firmy = client.get("/api/v1/mobile/tenants", headers=naglowki)
    ja = client.get("/api/v1/mobile/me", headers=naglowki)

    assert firmy.status_code == 200
    assert [pozycja["id"] for pozycja in firmy.json()] == [tenant_a["id"]]
    # Jedyna firma wybiera sie sama, wiec aplikacja pomija ekran wyboru.
    assert ja.json()["tenant"]["id"] == tenant_a["id"]
    # Technik pracuje w swojej firmie z prawami administratora firmy.
    assert ja.json()["can_write"] is True


def test_technik_pracuje_w_firmie_wskazanej_naglowkiem(client, tenant_a, tenant_b):
    _firma(tenant_a["id"], "BON", ("bongo.pl",))
    _firma(tenant_b["id"], "KOT", ("kotki.pl",))
    _technik("technik@mojadomena.pl", [tenant_a["id"], tenant_b["id"]])
    naglowki = _naglowki(client, "technik@mojadomena.pl")

    with SessionLocal() as db:
        db.add(Asset(tenant_id=tenant_b["id"], machine_id="b-1", hostname="KOT-SRV"))
        db.add(Asset(tenant_id=tenant_a["id"], machine_id="a-1", hostname="BON-SRV"))
        db.commit()

    obie = client.get("/api/v1/mobile/tenants", headers=naglowki).json()
    maszyny = client.get(
        "/api/v1/mobile/assets", headers={**naglowki, "X-CMDB-Tenant": tenant_b["slug"]}
    )

    assert len(obie) == 2
    # Ewidencja idzie za wybrana firma, w odroznieniu od zgloszen, ktore
    # obejmuja wszystkie firmy technika naraz.
    assert [m["hostname"] for m in maszyny.json()["items"]] == ["KOT-SRV"]


def test_konto_bez_firmy_i_bez_helpdesku_dostaje_jasna_odmowe(client, tenant_a):
    # Konto bez wlasnej firmy i bez ani jednego dostepu helpdesku - czyli
    # technik, ktoremu nikt jeszcze nic nie nadal.
    _technik("nikt@mojadomena.pl", [])
    naglowki = _naglowki(client, "nikt@mojadomena.pl")

    firmy = client.get("/api/v1/mobile/tenants", headers=naglowki)
    pulpit = client.get("/api/v1/mobile/dashboard", headers=naglowki)

    assert firmy.json() == []
    assert pulpit.status_code == 403
    assert "nie jest przypisane" in pulpit.json()["detail"]
