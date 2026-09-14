"""Helpdesk: domena wskazuje firme, firma nadaje numer.

Te dwie reguly decyduja o wszystkim, co dzieje sie pozniej: zla firma to zle
zafakturowany czas, a powtorzony numer to odpowiedz klienta doklejona do
cudzego zgloszenia. Dlatego sprawdzamy je tutaj, na czystych funkcjach, a nie
dopiero przez ekran.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    WPIS_SYSTEM,
    WPIS_WEWNETRZNY,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    Zgloszenie,
)
from cmdb_server.services import helpdesk


def _firma(db, tenant_id, skrot=None, domeny=()):
    tenant = db.get(Tenant, tenant_id)
    wpis = helpdesk.zapewnij_firme(db, tenant, skrot)
    for domena in domeny:
        helpdesk.dodaj_domene(db, tenant_id, domena)
    return wpis


def _technik(db, tenant_id, email, nazwa=None):
    from cmdb_server.security import hash_password

    user = PortalUser(
        tenant_id=None, email=email, full_name=nazwa,
        password_hash=hash_password("haslo-testowe-123"), role="admin",
    )
    db.add(user)
    db.flush()
    helpdesk.nadaj_dostep(db, user.id, tenant_id)
    return user


# --- domeny -----------------------------------------------------------------

def test_domena_wskazuje_firme(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        db.commit()

        firma = helpdesk.firma_dla_adresu(db, "Jan.Kowalski@BONGO.PL")
        assert firma is not None and firma.id == tenant_a["id"]


def test_domena_nieznana_nie_wskazuje_niczego(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        db.commit()
        assert helpdesk.firma_dla_adresu(db, "anna@gmail.com") is None


def test_firma_moze_miec_kilka_domen(tenant_a):
    """Po konsolidacji poczta przychodzi z dwoch domen i obie sa tej firmy."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "KLE", ["klepsydra.pl", "klepsydra.com.pl"])
        db.commit()

        for adres in ("biuro@klepsydra.pl", "it@klepsydra.com.pl"):
            firma = helpdesk.firma_dla_adresu(db, adres)
            assert firma is not None and firma.id == tenant_a["id"]


def test_domena_nie_moze_nalezec_do_dwoch_firm(tenant_a, tenant_b):
    """Blad musi nazwac firme, ktora domene juz ma - inaczej trzeba jej szukac."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        _firma(db, tenant_b["id"], "KLE")
        db.commit()

        with pytest.raises(helpdesk.BladHelpdesku, match="Firma A"):
            helpdesk.dodaj_domene(db, tenant_b["id"], "BONGO.pl")


def test_ta_sama_domena_dwa_razy_tej_samej_firmie_nie_jest_bledem(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        pierwsza = helpdesk.wlasciciel_domeny(db, "bongo.pl")
        druga = helpdesk.dodaj_domene(db, tenant_a["id"], " Bongo.PL. ")
        assert druga.id == pierwsza.id


# --- numeracja --------------------------------------------------------------

def test_numeracja_idzie_osobno_dla_kazdej_firmy(tenant_a, tenant_b):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON")
        _firma(db, tenant_b["id"], "KLE")
        db.commit()

        assert helpdesk.nadaj_numer(db, tenant_a["id"]) == (1, "BON-1")
        assert helpdesk.nadaj_numer(db, tenant_a["id"]) == (2, "BON-2")
        # Druga firma zaczyna od jedynki - liczniki sie nie widza.
        assert helpdesk.nadaj_numer(db, tenant_b["id"]) == (1, "KLE-1")
        db.commit()


def test_numer_nie_wraca_po_skasowaniu_zgloszenia(tenant_a):
    """Licznik jest ostatnim nadanym numerem, nie liczba zgloszen: numer, ktory
    poszedl juz w mailu do klienta, nie moze trafic do drugiej sprawy."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="Nie dziala",
            zglaszajacy_email="jan@bongo.pl",
        )
        db.commit()
        assert zgloszenie.numer_pelny == "BON-1"

        db.delete(zgloszenie)
        db.commit()

        assert helpdesk.nadaj_numer(db, tenant_a["id"])[1] == "BON-2"
        db.commit()


def test_skrot_proponowany_z_nazwy_bez_ogonkow():
    assert helpdesk.proponuj_skrot("Bongo") == "BON"
    assert helpdesk.proponuj_skrot("ŁKS") == "LKS"
    assert helpdesk.proponuj_skrot("Żółw sp. z o.o.") == "ZOL"


def test_zajety_skrot_dostaje_cyfre(tenant_a, tenant_b):
    with SessionLocal() as db:
        pierwsza = _firma(db, tenant_a["id"], "BON")
        db.commit()
        druga = helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_b["id"]), "BON")
        db.commit()

        assert pierwsza.skrot == "BON"
        assert druga.skrot != "BON"


def test_rozpoznanie_numeru_z_tematu():
    assert helpdesk.rozpoznaj_numer("Re: [BON-123] Nie dziala drukarka") == "BON-123"
    assert helpdesk.rozpoznaj_numer("FW: [kle-88] Outlook") == "KLE-88"
    assert helpdesk.rozpoznaj_numer("Zwykly temat bez numeru") is None
    assert helpdesk.rozpoznaj_numer(None) is None


# --- zgloszenia i watek -----------------------------------------------------

def test_zgloszenie_zaczyna_sie_wiadomoscia_klienta(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Nie dziala drukarka",
            tresc="Od rana nie dziala drukarka w biurze.",
            zglaszajacy_email="Jan@Bongo.pl", zglaszajacy_nazwa="Jan Kowalski",
            message_id="<klient-1@bongo.pl>",
        )
        db.commit()

        wpisy = list(db.execute(
            select(WpisZgloszenia)
            .where(WpisZgloszenia.zgloszenie_id == zgloszenie.id)
            .order_by(WpisZgloszenia.utworzono)
        ).scalars())

        assert zgloszenie.zglaszajacy_email == "jan@bongo.pl"
        assert [w.rodzaj for w in wpisy] == ["od_klienta", WPIS_SYSTEM]
        assert wpisy[0].message_id == "<klient-1@bongo.pl>"
        assert "BON-1" in wpisy[1].tresc


def test_znajdz_po_numerze_z_tematu_odpowiedzi(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        db.commit()

        numer = helpdesk.rozpoznaj_numer("Re: [BON-1] Drukarka")
        znalezione = helpdesk.znajdz_po_numerze(db, numer)
        assert znalezione is not None and znalezione.id == zgloszenie.id


def test_zmiana_statusu_i_przypisania_zostawia_slad(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        lukasz = _technik(db, tenant_a["id"], "lukasz@mojadomena.pl", "Lukasz Mazur")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.przypisz(db, zgloszenie, wladek, autor="Wladek Nowak")
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor="Wladek Nowak")
        helpdesk.przypisz(db, zgloszenie, lukasz, autor="Wladek Nowak")
        db.commit()

        slady = [w.tresc for w in db.execute(
            select(WpisZgloszenia).where(
                WpisZgloszenia.zgloszenie_id == zgloszenie.id,
                WpisZgloszenia.rodzaj == WPIS_SYSTEM,
            ).order_by(WpisZgloszenia.utworzono)
        ).scalars()]

        assert any("przypisane: Wladek Nowak" in s for s in slady)
        assert any("status: Nowe -> W trakcie" in s for s in slady)
        assert any("przekazane: Lukasz Mazur" in s for s in slady)


def test_zamkniecie_zapisuje_date_i_cofniecie_ja_kasuje(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.zmien_status(db, zgloszenie, STATUS_ZAMKNIETE, autor="operator")
        assert zgloszenie.zamkniete_o is not None

        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor="operator")
        assert zgloszenie.zamkniete_o is None
        db.commit()


def test_komentarz_wewnetrzny_to_ten_sam_watek_innego_rodzaju(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        wpis = helpdesk.dopisz_wiadomosc(
            db, zgloszenie, rodzaj=WPIS_WEWNETRZNY,
            tresc="Sprawdze sterownik.", autor=wladek,
        )
        db.commit()

        assert wpis.rodzaj == WPIS_WEWNETRZNY
        assert wpis.autor_id == wladek.id
        assert zgloszenie.ostatnia_aktywnosc == wpis.utworzono


def test_nieznany_rodzaj_wpisu_jest_bledem(tenant_a):
    """Rodzaj decyduje, czy tresc wyjdzie do klienta - literowka nie moze
    przejsc jako 'cos posredniego'."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        with pytest.raises(helpdesk.BladHelpdesku):
            helpdesk.dopisz_wiadomosc(db, zgloszenie, rodzaj="do_kilenta", tresc="x")


# --- czas pracy -------------------------------------------------------------

def test_czas_sumuje_sie_i_rozbija_na_technikow(tenant_a):
    """Sedno modulu: jedno zgloszenie, kilku technikow, kazdy ze swoimi minutami."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        lukasz = _technik(db, tenant_a["id"], "lukasz@mojadomena.pl", "Lukasz Mazur")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.dodaj_czas(db, zgloszenie, wladek, 30, "Diagnostyka drukarki")
        helpdesk.dodaj_czas(db, zgloszenie, lukasz, 45, "Sprawdzenie sieci")
        helpdesk.dodaj_czas(db, zgloszenie, wladek, 20, "Wymiana sterownika")
        db.commit()

        suma, udzialy = helpdesk.czas_zgloszenia(db, zgloszenie.id)
        assert suma == 95
        assert helpdesk.formatuj_czas(suma) == "1 h 35 min"
        assert [(u.nazwa, u.minuty) for u in udzialy] == [
            ("Wladek Nowak", 50), ("Lukasz Mazur", 45),
        ]


def test_czas_musi_byc_dodatni(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        with pytest.raises(helpdesk.BladHelpdesku):
            helpdesk.dodaj_czas(db, zgloszenie, wladek, 0)


def test_formatowanie_czasu():
    assert helpdesk.formatuj_czas(0) == "-"
    assert helpdesk.formatuj_czas(None) == "-"
    assert helpdesk.formatuj_czas(45) == "45 min"
    assert helpdesk.formatuj_czas(60) == "1 h 00 min"
    assert helpdesk.formatuj_czas(95) == "1 h 35 min"


# --- dostep -----------------------------------------------------------------

def test_technik_widzi_tylko_przypisane_firmy(tenant_a, tenant_b):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON")
        _firma(db, tenant_b["id"], "KLE")
        ewa = _technik(db, tenant_a["id"], "ewa@mojadomena.pl", "Ewa Kruk")
        db.commit()

        assert helpdesk.firmy_technika(db, ewa) == [tenant_a["id"]]
        assert helpdesk.ma_dostep(db, ewa, tenant_a["id"]) is True
        assert helpdesk.ma_dostep(db, ewa, tenant_b["id"]) is False


def test_superadmin_widzi_wszystkie_firmy(tenant_a, tenant_b, make_user):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON")
        _firma(db, tenant_b["id"], "KLE")
        db.commit()
    make_user(None, "operator@mojadomena.pl", "haslo-testowe-123")

    with SessionLocal() as db:
        operator = db.execute(
            select(PortalUser).where(PortalUser.email == "operator@mojadomena.pl")
        ).scalar_one()
        assert helpdesk.prowadzi_helpdesk(operator) is True
        assert set(helpdesk.firmy_technika(db, operator)) == {tenant_a["id"], tenant_b["id"]}


def test_nie_da_sie_przypisac_zgloszenia_technikowi_bez_dostepu(tenant_a, tenant_b):
    """Przypisanie jest tez nadaniem wgladu w dane firmy - musi je sprawdzac."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        _firma(db, tenant_b["id"], "KLE")
        obcy = _technik(db, tenant_b["id"], "obcy@mojadomena.pl", "Obcy Technik")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        with pytest.raises(helpdesk.BladHelpdesku, match="dostepu"):
            helpdesk.przypisz(db, zgloszenie, obcy, autor="operator")


def test_odebranie_dostepu_nie_kasuje_historii(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.dodaj_czas(db, zgloszenie, wladek, 30, "Diagnostyka")
        db.commit()

        assert helpdesk.odbierz_dostep(db, wladek.id, tenant_a["id"]) is True
        db.commit()

        suma, udzialy = helpdesk.czas_zgloszenia(db, zgloszenie.id)
        assert suma == 30 and udzialy[0].nazwa == "Wladek Nowak"
        assert db.get(Zgloszenie, zgloszenie.id) is not None


# --- sprzet zgloszenia i karta napraw ---------------------------------------

def _osoba(db, tenant_id, imie, email):
    from cmdb_server.models import WpisSlownika

    wpis = WpisSlownika(
        tenant_id=tenant_id, kategoria="osoba", wartosc=imie, klucz=imie.lower(),
        atrybuty={"imie_nazwisko": imie, "email": email},
    )
    db.add(wpis)
    db.flush()
    return wpis


def _sprzet(db, tenant_id, hostname, uzytkownik_id=None, typ="komputer"):
    from cmdb_server.models import Asset

    asset = Asset(
        tenant_id=tenant_id, machine_id=f"machine-{hostname}", hostname=hostname,
        typ=typ, uzytkownik_id=uzytkownik_id,
    )
    db.add(asset)
    db.flush()
    return asset


def test_sprzet_zglaszajacego_podpina_sie_sam(tenant_a):
    """Domyslne powiazanie idzie po uzytkowniku sprzetu, nie po opiekunie:
    laptop prezesa ma opiekuna w IT, a awarie zglasza prezes."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        jan = _osoba(db, tenant_a["id"], "Jan Kowalski", "jan@bongo.pl")
        _sprzet(db, tenant_a["id"], "LAPTOP-023", jan.id)
        # Sprzet innej osoby nie moze sie doczepic.
        _sprzet(db, tenant_a["id"], "LAPTOP-999", _osoba(db, tenant_a["id"], "Ewa Nowak", "ewa@bongo.pl").id)

        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Nie dziala drukarka", tresc="",
            zglaszajacy_email="Jan@Bongo.pl",
        )
        db.commit()

        podpiety = helpdesk.sprzet_zgloszenia(db, zgloszenie.id)
        assert [(a.hostname, w.zrodlo) for a, w in podpiety] == [("LAPTOP-023", "automat")]


def test_kilka_sprzetow_uzytkownika_zostaje_do_decyzji_technika(tenant_a):
    """Dopisanie monitora i telefonu do zgloszenia o VPN zasmiecilo by ich karty
    napraw - przy kilku kandydatach wybiera czlowiek."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        jan = _osoba(db, tenant_a["id"], "Jan Kowalski", "jan@bongo.pl")
        _sprzet(db, tenant_a["id"], "LAPTOP-023", jan.id)
        _sprzet(db, tenant_a["id"], "MONITOR-11", jan.id, typ="monitor")

        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="VPN zrywa", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        db.commit()

        assert helpdesk.sprzet_zgloszenia(db, zgloszenie.id) == []
        kandydaci = helpdesk.sprzet_zglaszajacego(db, tenant_a["id"], "jan@bongo.pl")
        assert [a.hostname for a in kandydaci] == ["LAPTOP-023", "MONITOR-11"]


def test_nieznany_nadawca_nie_dostaje_zadnego_sprzetu(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        _sprzet(db, tenant_a["id"], "LAPTOP-023")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="ktos@bongo.pl",
        )
        db.commit()
        assert helpdesk.sprzet_zgloszenia(db, zgloszenie.id) == []


def test_technik_podpina_i_odpina_sprzet_recznie(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        drukarka = _sprzet(db, tenant_a["id"], "DRUKARKA-01", typ="drukarka")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        helpdesk.podepnij_sprzet(db, zgloszenie, drukarka, dodal=helpdesk.opis_osoby(wladek))
        db.commit()

        assert [a.hostname for a, _ in helpdesk.sprzet_zgloszenia(db, zgloszenie.id)] == ["DRUKARKA-01"]
        slady = [w.tresc for w in db.execute(
            select(WpisZgloszenia).where(
                WpisZgloszenia.zgloszenie_id == zgloszenie.id,
                WpisZgloszenia.rodzaj == WPIS_SYSTEM,
            )
        ).scalars()]
        assert any("podpiety sprzet: DRUKARKA-01" in s for s in slady)

        assert helpdesk.odepnij_sprzet(db, zgloszenie, drukarka.id, autor="Wladek Nowak") is True
        db.commit()
        assert helpdesk.sprzet_zgloszenia(db, zgloszenie.id) == []


def test_sprzet_innej_firmy_nie_da_sie_podpiac(tenant_a, tenant_b):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        _firma(db, tenant_b["id"], "KLE")
        obcy = _sprzet(db, tenant_b["id"], "OBCY-01")
        zgloszenie = helpdesk.utworz_zgloszenie(
            db, tenant_id=tenant_a["id"], temat="Drukarka", tresc="",
            zglaszajacy_email="jan@bongo.pl",
        )
        with pytest.raises(helpdesk.BladHelpdesku, match="innej firmy"):
            helpdesk.podepnij_sprzet(db, zgloszenie, obcy)


def test_karta_napraw_sprzetu_zbiera_jego_zgloszenia_i_czas(tenant_a):
    """Pytanie, ktorego dzis w CMDB nie da sie zadac: ile razy ta drukarka
    sie psula i ile czasu na nia poszlo."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wladek = _technik(db, tenant_a["id"], "wladek@mojadomena.pl", "Wladek Nowak")
        drukarka = _sprzet(db, tenant_a["id"], "DRUKARKA-01", typ="drukarka")

        for temat, minuty in (("Nie drukuje", 30), ("Zacina papier", 45), ("Brak tonera", 15)):
            zgloszenie = helpdesk.utworz_zgloszenie(
                db, tenant_id=tenant_a["id"], temat=temat, tresc="",
                zglaszajacy_email="jan@bongo.pl",
            )
            helpdesk.podepnij_sprzet(db, zgloszenie, drukarka)
            helpdesk.dodaj_czas(db, zgloszenie, wladek, minuty)
        db.commit()

        historia = helpdesk.zgloszenia_sprzetu(db, drukarka.id)
        assert [z.temat for z in historia] == ["Brak tonera", "Zacina papier", "Nie drukuje"]
        assert helpdesk.czas_sprzetu(db, drukarka.id) == 90
        assert helpdesk.formatuj_czas(helpdesk.czas_sprzetu(db, drukarka.id)) == "1 h 30 min"
