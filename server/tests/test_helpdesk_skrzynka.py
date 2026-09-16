"""Skrzynka helpdesku: odbior IMAP i wysylka SMTP.

Serwera pocztowego w tescie nie ma i byc nie moze, wiec podstawiamy atrapy
polaczen. Sprawdzamy to, co dzieje sie POMIEDZY skrzynka a baza: czy
wiadomosc zostaje oznaczona dopiero po zapisaniu, czy jedna wadliwa nie
zatrzymuje reszty, czy odpowiedz niesie naglowki, po ktorych wroci do swojego
zgloszenia, i czy komentarz wewnetrzny nie ma jak wyjsc na zewnatrz.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    STATUS_NOWE,
    STATUS_OCZEKUJE,
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    HelpdeskUstawienia,
    NierozpoznanaWiadomosc,
    PortalUser,
    Tenant,
    ZalacznikWpisu,
    Zgloszenie,
)
from cmdb_server.security import hash_password

from .test_tenant_isolation import _login
from cmdb_server.services import helpdesk, helpdesk_imap, helpdesk_wysylka, sekrety
from cmdb_server.services import helpdesk_poczta as poczta


# --- przygotowanie ----------------------------------------------------------

def _skrzynka(db, **zmiany) -> HelpdeskUstawienia:
    ustawienia = HelpdeskUstawienia(
        klucz="helpdesk",
        imap_host="imap.mojadomena.pl", imap_uzytkownik="helpdesk@mojadomena.pl",
        imap_haslo_szyfr=sekrety.zaszyfruj("tajne"),
        smtp_host="smtp.mojadomena.pl", smtp_uzytkownik="helpdesk@mojadomena.pl",
        smtp_haslo_szyfr=sekrety.zaszyfruj("tajne"),
        nadawca="helpdesk@mojadomena.pl", nazwa_nadawcy="Helpdesk",
        stopka="Pozdrawiamy, Helpdesk", aktywne=True,
    )
    for pole, wartosc in zmiany.items():
        setattr(ustawienia, pole, wartosc)
    db.add(ustawienia)
    db.flush()
    return ustawienia


def _firma(db, tenant_id, skrot="BON", domeny=("bongo.pl",)):
    helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_id), skrot)
    for domena in domeny:
        helpdesk.dodaj_domene(db, tenant_id, domena)


def _technik(db, tenant_id, email="wladek@mojadomena.pl", nazwa="Wladek Nowak"):
    user = PortalUser(
        tenant_id=None, email=email, full_name=nazwa,
        password_hash=hash_password("haslo-testowe-123"), role="admin",
    )
    db.add(user)
    db.flush()
    helpdesk.nadaj_dostep(db, user.id, tenant_id)
    return user


def _mail(temat="Nie dziala drukarka", nadawca="Jan Kowalski <jan@bongo.pl>",
          tresc="Od rana nie dziala drukarka.", message_id="<k1@bongo.pl>",
          dw=None, zalaczniki=(), in_reply_to=None) -> bytes:
    from email.message import EmailMessage

    wiadomosc = EmailMessage()
    wiadomosc["From"] = nadawca
    wiadomosc["To"] = "helpdesk@mojadomena.pl"
    wiadomosc["Subject"] = temat
    wiadomosc["Message-ID"] = message_id
    wiadomosc["Date"] = "Mon, 14 Sep 2026 09:10:00 +0200"
    if dw:
        wiadomosc["Cc"] = ", ".join(dw)
    if in_reply_to:
        wiadomosc["In-Reply-To"] = in_reply_to
    wiadomosc.set_content(tresc)
    for nazwa_pliku, typ, dane in zalaczniki:
        glowny, _, podtyp = typ.partition("/")
        wiadomosc.add_attachment(dane, maintype=glowny, subtype=podtyp, filename=nazwa_pliku)
    return wiadomosc.as_bytes()


class AtrapaIMAP:
    """Skrzynka w pamieci. Notuje, co zostalo z wiadomosciami zrobione."""

    def __init__(self, wiadomosci: dict[bytes, bytes], psuj_fetch: set[bytes] | None = None):
        self.wiadomosci = wiadomosci
        self.psuj_fetch = psuj_fetch or set()
        self.oznaczone: list[tuple[bytes, str]] = []
        self.skopiowane: list[tuple[bytes, str]] = []
        self.zalogowany = False
        self.folder: str | None = None

    def login(self, uzytkownik, haslo):
        self.zalogowany = True
        return "OK", [b""]

    def select(self, folder):
        self.folder = folder
        return "OK", [b"1"]

    def search(self, charset, kryterium):
        return "OK", [b" ".join(self.wiadomosci)]

    def fetch(self, identyfikator, czesci):
        if identyfikator in self.psuj_fetch:
            return "NO", [None]
        return "OK", [(b"1 (RFC822)", self.wiadomosci[identyfikator]), b")"]

    def store(self, identyfikator, polecenie, flaga):
        self.oznaczone.append((identyfikator, flaga))
        return "OK", [b""]

    def copy(self, identyfikator, folder):
        self.skopiowane.append((identyfikator, folder))
        return "OK", [b""]

    def expunge(self):
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "OK", [b""]


class AtrapaSMTP:
    """Serwer wysylkowy w pamieci."""

    wyslane: list = []

    def __init__(self, blad: Exception | None = None):
        self.blad = blad

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def login(self, uzytkownik, haslo):
        return None

    def send_message(self, wiadomosc):
        if self.blad is not None:
            raise self.blad
        AtrapaSMTP.wyslane.append(wiadomosc)


@pytest.fixture
def smtp(monkeypatch):
    AtrapaSMTP.wyslane = []
    monkeypatch.setattr(helpdesk_wysylka, "_polaczenie", lambda konfiguracja: AtrapaSMTP())
    return AtrapaSMTP


# --- odbior IMAP ------------------------------------------------------------

def test_obieg_zaklada_zgloszenie_i_oznacza_wiadomosc(client, tenant_a, monkeypatch, smtp):
    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

        wynik = helpdesk_imap.pobierz(db)

        assert wynik["nowe"] == 1
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        assert zgloszenie.numer_pelny == "BON-1"
        # Oznaczenie idzie PO zapisie: inaczej awaria miedzy jednym a drugim
        # gubi zgloszenie, a wiadomosc zostaje przeczytana.
        assert atrapa.oznaczone == [(b"1", "\\Seen")]
        assert atrapa.zalogowany is True


def test_wadliwa_wiadomosc_nie_zatrzymuje_reszty(client, tenant_a, monkeypatch, smtp):
    """Skrzynka ma isc dalej: jedna wiadomosc, ktorej nie da sie przyjac,
    nie moze wstrzymac zgloszen, ktore czekaja za nia."""
    atrapa = AtrapaIMAP({b"1": _mail(message_id="<a@bongo.pl>"),
                         b"2": _mail(message_id="<b@bongo.pl>", temat="Druga sprawa")},
                        psuj_fetch={b"1"})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

        wynik = helpdesk_imap.pobierz(db)

        assert wynik["pobrane"] == 1
        tematy = [z.temat for z in db.execute(select(Zgloszenie)).scalars()]
        assert tematy == ["Druga sprawa"]
        # Wiadomosc, ktorej nie pobralismy, zostaje nieoznaczona i wroci.
        assert atrapa.oznaczone == [(b"2", "\\Seen")]


def test_przenoszenie_do_folderu_zamiast_oznaczania(client, tenant_a, monkeypatch, smtp):
    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db, imap_po_pobraniu="przenies", imap_folder_docelowy="Helpdesk")
        _firma(db, tenant_a["id"])
        db.commit()

        helpdesk_imap.pobierz(db)

        assert atrapa.skopiowane == [(b"1", "Helpdesk")]
        assert atrapa.oznaczone == [(b"1", "\\Deleted")]


def test_blad_polaczenia_widac_w_panelu(client, tenant_a, monkeypatch):
    """Skrzynka, ktora przestala sie logowac, wyglada z panelu tak samo jak
    skrzynka, do ktorej nikt nie napisal - i to jest awaria cicha."""
    def wybuch(konfiguracja):
        raise OSError("connection refused")

    monkeypatch.setattr(helpdesk_imap, "_polaczenie", wybuch)

    with SessionLocal() as db:
        _skrzynka(db)
        db.commit()

        wynik = helpdesk_imap.pobierz(db)

        assert "connection refused" in wynik["blad"]
        assert "connection refused" in db.get(HelpdeskUstawienia, "helpdesk").ostatni_blad


def test_nieskonfigurowana_skrzynka_nie_probuje_sie_laczyc(client):
    with SessionLocal() as db:
        assert "pominiete" in helpdesk_imap.pobierz(db)


def test_udany_obieg_kasuje_poprzedni_blad(client, tenant_a, monkeypatch, smtp):
    atrapa = AtrapaIMAP({})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db, ostatni_blad="stara awaria")
        db.commit()

        helpdesk_imap.pobierz(db)

        konfiguracja = db.get(HelpdeskUstawienia, "helpdesk")
        assert konfiguracja.ostatni_blad is None
        assert konfiguracja.ostatnie_pobranie is not None


# --- zalaczniki -------------------------------------------------------------

def test_zalacznik_ladu_je_na_dysku_i_w_watku(client, tenant_a, monkeypatch, smtp, tmp_path):
    monkeypatch.setattr(
        "cmdb_server.services.helpdesk_poczta.katalog_zalacznikow", lambda: tmp_path
    )
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        db.commit()

        poczta.przyjmij(db, poczta.przeczytaj(_mail(
            zalaczniki=[("zrzut ekranu.png", "image/png", b"\x89PNG-udawany")]
        )))
        db.commit()

        zalacznik = db.execute(select(ZalacznikWpisu)).scalar_one()
        assert zalacznik.nazwa == "zrzut ekranu.png"
        assert zalacznik.rozmiar == len(b"\x89PNG-udawany")
        assert (tmp_path / zalacznik.sciezka).read_bytes() == b"\x89PNG-udawany"


def test_podglad_dostaje_tylko_prawdziwy_obrazek(client, tenant_a, monkeypatch, smtp, tmp_path):
    """Typ pliku deklaruje nadawca, wiec sam naglowek nie wystarcza.

    Zrzut ekranu ma sie pokazac w watku od razu - to czesto cala tresc
    zgloszenia. Ale "image/png", ktore w srodku jest czyms innym, wraca do
    pobierania: podglad dostaja wylacznie pliki zgodne z wlasnym naglowkiem.
    """
    monkeypatch.setattr(
        "cmdb_server.services.helpdesk_poczta.katalog_zalacznikow", lambda: tmp_path
    )
    prawdziwy_png = b"\x89PNG\r\n\x1a\n" + b"reszta pliku"
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        db.commit()
        poczta.przyjmij(db, poczta.przeczytaj(_mail(zalaczniki=[
            ("zrzut.png", "image/png", prawdziwy_png),
            ("podszywacz.png", "image/png", b"<html>wcale nie obrazek"),
            ("logo.svg", "image/svg+xml", b"<svg onload=alert(1)></svg>"),
        ])))
        db.commit()
        _technik(db, tenant_a["id"])
        db.commit()
        pliki = {z.nazwa: z.id for z in db.execute(select(ZalacznikWpisu)).scalars()}

    _login(client, "wladek@mojadomena.pl", "haslo-testowe-123")

    odp = client.get(f"/helpdesk/zalacznik/{pliki['zrzut.png']}/podglad")
    assert odp.status_code == 200
    assert odp.headers["content-type"].startswith("image/png")
    assert odp.content == prawdziwy_png

    # Plik, ktory tylko udaje obrazek, oraz SVG (potrafi nosic skrypty) -
    # bez podgladu. Pobrac je nadal mozna, bo nic nie ginie.
    assert client.get(f"/helpdesk/zalacznik/{pliki['podszywacz.png']}/podglad").status_code == 404
    assert client.get(f"/helpdesk/zalacznik/{pliki['logo.svg']}/podglad").status_code == 404
    assert client.get(f"/helpdesk/zalacznik/{pliki['logo.svg']}").status_code == 200


def test_nazwa_pliku_od_klienta_nie_dotyka_sciezki(client, tenant_a, monkeypatch, smtp, tmp_path):
    """Klient moze przyslac "..\\..\\etc\\passwd" - nazwa z maila jest opisem,
    a nie miejscem na dysku."""
    monkeypatch.setattr(
        "cmdb_server.services.helpdesk_poczta.katalog_zalacznikow", lambda: tmp_path
    )
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        db.commit()

        poczta.przyjmij(db, poczta.przeczytaj(_mail(
            zalaczniki=[("../../../etc/passwd", "text/plain", b"root:x:0:0")]
        )))
        db.commit()

        zalacznik = db.execute(select(ZalacznikWpisu)).scalar_one()
        assert ".." not in zalacznik.sciezka
        assert (tmp_path / zalacznik.sciezka).is_file()


def test_zbyt_duzy_zalacznik_nie_przewraca_zgloszenia(client, tenant_a, monkeypatch, smtp, tmp_path):
    monkeypatch.setattr(
        "cmdb_server.services.helpdesk_poczta.katalog_zalacznikow", lambda: tmp_path
    )
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "helpdesk_zalacznik_mb", 0)
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            zalaczniki=[("wielki.iso", "application/octet-stream", b"x" * 1024)]
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_NOWE
        assert db.execute(select(ZalacznikWpisu)).scalars().all() == []


# --- odpowiedz na zamkniete i DW --------------------------------------------

def test_odpowiedz_otwiera_zamkniete_zgloszenie(client, tenant_a, smtp):
    """Zamkniete zgloszenia sa poza tablica - wiadomosc doklejona do takiego
    watku nie trafilaby nikomu na oczy."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        helpdesk.zmien_status(db, zgloszenie, STATUS_ZAMKNIETE, autor="operator")
        db.commit()

        poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: [BON-1] Nie dziala drukarka", message_id="<k2@bongo.pl>",
            tresc="Znowu to samo.",
        )))
        db.commit()

        db.refresh(zgloszenie)
        assert zgloszenie.status == STATUS_W_TRAKCIE


def test_odpowiedz_zdejmuje_zgloszenie_z_oczekiwania(client, tenant_a, smtp):
    """Czekalismy wlasnie na te wiadomosc - sprawa wraca do realizacji.

    Bez tego kolumna "Oczekuje" zbieralaby sprawy, na ktore klient juz odpisal,
    i technik musialby przegladac ja recznie w poszukiwaniu odpowiedzi.
    """
    with SessionLocal() as db:
        _firma(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        helpdesk.zmien_status(db, zgloszenie, STATUS_OCZEKUJE, autor="operator")
        db.commit()

        poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: [BON-1] Nie dziala drukarka", message_id="<k3@bongo.pl>",
            tresc="Zrestartowalem, dalej nie dziala.",
        )))
        db.commit()

        db.refresh(zgloszenie)
        assert zgloszenie.status == STATUS_W_TRAKCIE


def test_dw_klienta_wraca_w_odpowiedzi_technika(client, tenant_a, smtp):
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(
            _mail(dw=["prezes@bongo.pl", "helpdesk@mojadomena.pl", "jan@bongo.pl"])
        ))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, "Prosze zrestartowac.", wladek)
        db.commit()

        wyslana = smtp.wyslane[-1]
        assert wyslana["To"] == "jan@bongo.pl"
        # Wlasny adres i sam zglaszajacy wypadaja z kopii: pierwszy wrocilby
        # do nas, drugi dostalby wiadomosc dwa razy.
        assert wyslana["Cc"] == "prezes@bongo.pl"


def test_dw_bierze_sie_z_ostatniej_wiadomosci_klienta(client, tenant_a, smtp):
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail(dw=["prezes@bongo.pl"])))
        poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: [BON-1] Nie dziala drukarka", message_id="<k2@bongo.pl>",
            dw=["ksiegowosc@bongo.pl"],
        )))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, "Sprawdzam.", wladek)
        db.commit()

        assert smtp.wyslane[-1]["Cc"] == "ksiegowosc@bongo.pl"


# --- wysylka ----------------------------------------------------------------

def test_odpowiedz_niesie_numer_i_naglowki_watku(client, tenant_a, smtp):
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        wpis = helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, "Prosze zrestartowac.", wladek)
        db.commit()

        wyslana = smtp.wyslane[-1]
        assert wyslana["Subject"] == "Re: [BON-1] Nie dziala drukarka"
        assert wyslana["In-Reply-To"] == "<k1@bongo.pl>"
        assert "Pozdrawiamy, Helpdesk" in wyslana.get_content()
        # Message-ID wychodzacej wiadomosci zapisujemy, bo klient przysle go
        # z powrotem w In-Reply-To.
        assert wpis.message_id == wyslana["Message-ID"]
        assert wpis.wyslano_o is not None and wpis.blad_wysylki is None


def test_odpowiedz_klienta_na_nasza_wiadomosc_wraca_do_zgloszenia(client, tenant_a, smtp):
    """Pelne kolko: wysylamy, klient odpowiada na to, co dostal, i trafia
    z powrotem do swojej sprawy."""
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        wpis = helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, "Prosze zrestartowac.", wladek)
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: zupelnie zmieniony temat", message_id="<k9@bongo.pl>",
            in_reply_to=wpis.message_id, tresc="Nadal nie dziala.",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_DOPISZ
        assert wynik.zgloszenie.id == zgloszenie.id


def test_komentarz_wewnetrzny_nie_ma_jak_wyjsc(client, tenant_a, smtp):
    """Rodzaj wpisu sprawdzamy w jedynym miejscu, przez ktore tresc wychodzi
    na zewnatrz - nie polegamy na tym, ze kazdy widok o tym pamieta."""
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        wpis = helpdesk.dopisz_wiadomosc(
            db, zgloszenie, rodzaj=WPIS_WEWNETRZNY, tresc="Sprawdze sterownik.", autor=wladek
        )
        db.commit()

        with pytest.raises(helpdesk_wysylka.BladWysylki, match="nie wychodzi"):
            helpdesk_wysylka.wyslij_wpis(db, zgloszenie, wpis)
        assert smtp.wyslane == []


def test_nieudana_wysylka_zostawia_tresc_w_watku(client, tenant_a, monkeypatch):
    """Awaria SMTP nie moze skasowac odpowiedzi, ktora technik wlasnie napisal."""
    import smtplib

    monkeypatch.setattr(
        helpdesk_wysylka, "_polaczenie",
        lambda konfiguracja: AtrapaSMTP(blad=smtplib.SMTPException("serwer nie odpowiada")),
    )
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        wladek = _technik(db, tenant_a["id"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        with pytest.raises(helpdesk_wysylka.BladWysylki):
            helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, "Prosze zrestartowac.", wladek)
        db.commit()

        niewyslane = helpdesk_wysylka.niewyslane(db, zgloszenie.id)
        assert len(niewyslane) == 1
        assert niewyslane[0].tresc == "Prosze zrestartowac."
        assert "serwer nie odpowiada" in niewyslane[0].blad_wysylki


# --- potwierdzenie przyjecia ------------------------------------------------

def test_nowe_zgloszenie_dostaje_potwierdzenie_z_numerem(client, tenant_a, monkeypatch, smtp):
    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

        helpdesk_imap.pobierz(db)

        wyslana = smtp.wyslane[-1]
        assert "BON-1" in wyslana.get_content()
        assert wyslana["To"] == "jan@bongo.pl"
        # Potwierdzenie jest wiadomoscia do klienta, wiec widnieje w watku -
        # technik ma widziec, co dokladnie klient dostal.
        zgloszenie = db.execute(select(Zgloszenie)).scalar_one()
        wpisy = [w.rodzaj for w in db.execute(
            select(helpdesk.WpisZgloszenia).where(
                helpdesk.WpisZgloszenia.zgloszenie_id == zgloszenie.id
            )
        ).scalars()]
        assert WPIS_DO_KLIENTA in wpisy


def test_potwierdzenie_zostawia_zgloszenie_nowym(client, tenant_a, monkeypatch, smtp):
    """Automat pisze do klienta, ale nikt nie zaczal jeszcze pracy.

    Gdyby potwierdzenie ustawialo "Oczekuje", kolumna "Nowe" na tablicy bylaby
    zawsze pusta i nie dalo by sie odroznic sprawy swiezej od takiej, ktora
    ktos juz obejrzal. Status rusza dopiero odpowiedz napisana przez technika.
    """
    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

        helpdesk_imap.pobierz(db)

        assert smtp.wyslane, "potwierdzenie mialo pojsc"
        assert db.execute(select(Zgloszenie)).scalar_one().status == STATUS_NOWE


def test_odpowiedz_w_watku_nie_dostaje_potwierdzenia(client, tenant_a, monkeypatch, smtp):
    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)
    with SessionLocal() as db:
        helpdesk_imap.pobierz(db)
    ile_po_pierwszej = len(smtp.wyslane)

    atrapa2 = AtrapaIMAP({b"2": _mail(temat="Re: [BON-1] Nie dziala drukarka",
                                      message_id="<k2@bongo.pl>")})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa2)
    with SessionLocal() as db:
        wynik = helpdesk_imap.pobierz(db)

    assert wynik["dopisane"] == 1
    assert len(smtp.wyslane) == ile_po_pierwszej


def test_wylaczone_potwierdzenie_nic_nie_wysyla(client, tenant_a, monkeypatch, smtp):
    atrapa = AtrapaIMAP({b"1": _mail()})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db, potwierdzenie_wlaczone=False)
        _firma(db, tenant_a["id"])
        db.commit()

        helpdesk_imap.pobierz(db)

        assert smtp.wyslane == []


def test_nierozpoznana_domena_nie_dostaje_zadnej_odpowiedzi(client, tenant_a, monkeypatch, smtp):
    """Cisza jest tu celowa: odpowiadanie nieznanym nadawcom potwierdza im,
    ze adres istnieje i jest czytany."""
    atrapa = AtrapaIMAP({b"1": _mail(nadawca="anna@gmail.com", message_id="<obca@gmail.com>")})
    monkeypatch.setattr(helpdesk_imap, "_polaczenie", lambda konfiguracja: atrapa)

    with SessionLocal() as db:
        _skrzynka(db)
        _firma(db, tenant_a["id"])
        db.commit()

        wynik = helpdesk_imap.pobierz(db)

        assert wynik["nierozpoznane"] == 1
        assert smtp.wyslane == []
        assert db.execute(select(NierozpoznanaWiadomosc)).scalar_one().domena == "gmail.com"
