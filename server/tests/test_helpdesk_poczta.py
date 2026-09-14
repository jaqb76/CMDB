"""Co helpdesk robi z przychodzaca poczta.

Najwazniejszy przypadek to odpowiedz klienta: ma dopisac sie do istniejacego
zgloszenia, a nie zalozyc drugie. Duplikat oznacza dwa watki tej samej sprawy,
dwoch technikow nieswiadomych siebie nawzajem i czas pracy rozbity na dwie
faktury - dlatego sprawdzamy wszystkie trzy drogi rozpoznania.

Testy chodza na surowych mailach, bez serwera pocztowego: inaczej tego
przypadku nie dalo by sie w ogole sprawdzic.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    NIEROZPOZNANA_CZEKA,
    NIEROZPOZNANA_ZIGNOROWANA,
    NierozpoznanaWiadomosc,
    Tenant,
    WpisZgloszenia,
    Zgloszenie,
)
from cmdb_server.services import helpdesk, helpdesk_poczta as poczta


def _firma(db, tenant_id, skrot, domeny):
    helpdesk.zapewnij_firme(db, db.get(Tenant, tenant_id), skrot)
    for domena in domeny:
        helpdesk.dodaj_domene(db, tenant_id, domena)


def _mail(
    nadawca="Jan Kowalski <jan@bongo.pl>", temat="Nie dziala drukarka",
    tresc="Od rana nie dziala drukarka w biurze.", message_id="<klient-1@bongo.pl>",
    in_reply_to=None, references=None, dodatkowe=(), html=False,
) -> bytes:
    naglowki = [
        f"From: {nadawca}",
        "To: helpdesk@mojadomena.pl",
        f"Subject: {temat}",
        f"Message-ID: {message_id}",
        "Date: Mon, 14 Sep 2026 09:10:00 +0200",
    ]
    if in_reply_to:
        naglowki.append(f"In-Reply-To: {in_reply_to}")
    if references:
        naglowki.append(f"References: {' '.join(references)}")
    naglowki.extend(dodatkowe)
    naglowki.append("MIME-Version: 1.0")
    naglowki.append(
        'Content-Type: text/html; charset="utf-8"' if html
        else 'Content-Type: text/plain; charset="utf-8"'
    )
    return ("\r\n".join(naglowki) + "\r\n\r\n" + tresc).encode("utf-8")


# --- czytanie wiadomosci ----------------------------------------------------

def test_odczyt_nadawcy_tematu_i_tresci():
    wiadomosc = poczta.przeczytaj(_mail())
    assert wiadomosc.nadawca == "jan@bongo.pl"
    assert wiadomosc.nadawca_nazwa == "Jan Kowalski"
    assert wiadomosc.temat == "Nie dziala drukarka"
    assert wiadomosc.tresc.startswith("Od rana")
    assert wiadomosc.domena == "bongo.pl"


def test_temat_z_polskimi_znakami_jest_rozkodowany():
    """Numer schowany w base64 nie dopasowalby sie do niczego."""
    zakodowany = "=?UTF-8?B?UmU6IFtCT04tMV0gWmdBb3N6ZW5pZQ==?="
    wiadomosc = poczta.przeczytaj(_mail(temat=zakodowany))
    assert wiadomosc.temat.startswith("Re: [BON-1]")


def test_tresc_html_bez_znacznikow():
    wiadomosc = poczta.przeczytaj(_mail(tresc="<p>Nie <b>dziala</b> drukarka</p>", html=True))
    assert "Nie dziala drukarka" in wiadomosc.tresc
    assert "<b>" not in wiadomosc.tresc


def test_autoodpowiedz_jest_rozpoznawana():
    urlop = poczta.przeczytaj(_mail(dodatkowe=["Auto-Submitted: auto-replied"]))
    czlowiek = poczta.przeczytaj(_mail(dodatkowe=["Auto-Submitted: no"]))
    daemon = poczta.przeczytaj(_mail(nadawca="mailer-daemon@bongo.pl"))
    assert urlop.automat is True
    assert czlowiek.automat is False
    assert daemon.automat is True


# --- kwalifikacja: nowe zgloszenie ------------------------------------------

def test_mail_z_domeny_firmy_zaklada_zgloszenie(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_NOWE
        assert wynik.zgloszenie.numer_pelny == "BON-1"
        assert wynik.zgloszenie.tenant_id == tenant_a["id"]
        assert wynik.zgloszenie.zglaszajacy_email == "jan@bongo.pl"


def test_prefiks_re_nie_wchodzi_do_tematu_zgloszenia(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(temat="Fwd: Re: Drukarka")))
        db.commit()
        assert wynik.zgloszenie.temat == "Drukarka"


def test_autoodpowiedz_z_domeny_klienta_nie_zaklada_zgloszenia(tenant_a):
    """"Jestem na urlopie do 15 wrzesnia" nie jest zgloszeniem awarii."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wynik = poczta.przyjmij(db, poczta.przeczytaj(
            _mail(temat="Automatyczna odpowiedz: urlop",
                  dodatkowe=["Auto-Submitted: auto-replied"])
        ))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_NIEROZPOZNANA
        assert db.execute(select(Zgloszenie)).scalars().all() == []
        czekajace = db.execute(select(NierozpoznanaWiadomosc)).scalars().all()
        assert len(czekajace) == 1 and czekajace[0].stan == NIEROZPOZNANA_CZEKA


# --- kwalifikacja: odpowiedz w watku ----------------------------------------

def test_odpowiedz_po_naglowku_in_reply_to_dopisuje_sie(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: Nie dziala drukarka", tresc="Po restarcie nadal nie dziala.",
            message_id="<klient-2@bongo.pl>", in_reply_to="<klient-1@bongo.pl>",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_DOPISZ
        assert db.execute(select(Zgloszenie)).scalars().all().__len__() == 1
        wpisy = list(db.execute(
            select(WpisZgloszenia)
            .where(WpisZgloszenia.rodzaj == "od_klienta")
            .order_by(WpisZgloszenia.utworzono)
        ).scalars())
        assert [w.tresc.split(".")[0] for w in wpisy] == [
            "Od rana nie dziala drukarka w biurze", "Po restarcie nadal nie dziala",
        ]


def test_odpowiedz_po_numerze_w_temacie_dopisuje_sie(tenant_a):
    """Klient odpowiada z innego adresu i bez naglowkow watku - zostaje numer."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            nadawca="prezes@bongo.pl", temat="Re: [BON-1] Nie dziala drukarka",
            tresc="Dopisuje sie do sprawy.", message_id="<inny-1@bongo.pl>",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_DOPISZ
        assert len(db.execute(select(Zgloszenie)).scalars().all()) == 1


def test_odpowiedz_po_references_dopisuje_sie(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Re: cos zupelnie innego", message_id="<klient-3@bongo.pl>",
            references=["<obcy@example.com>", "<klient-1@bongo.pl>"],
        )))
        db.commit()
        assert wynik.decyzja == poczta.DECYZJA_DOPISZ


def test_ten_sam_nadawca_i_temat_w_otwartej_sprawie_dopisuje_sie(tenant_a):
    """Klient kasuje numer z tematu i pisze z telefonu - bez tej proby
    powstalby duplikat tej samej sprawy."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Nie dziala drukarka", tresc="Ponawiam.", message_id="<klient-4@bongo.pl>",
        )))
        db.commit()
        assert wynik.decyzja == poczta.DECYZJA_DOPISZ


def test_inny_temat_tego_samego_nadawcy_to_nowa_sprawa(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            temat="Nie dziala VPN", message_id="<klient-5@bongo.pl>",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_NOWE
        assert wynik.zgloszenie.numer_pelny == "BON-2"


def test_watek_wygrywa_z_domena_nadawcy(tenant_a, tenant_b):
    """Odpowiedz w watku zostaje w swoim zgloszeniu, nawet gdy nadawca jest
    z domeny innej firmy - inaczej watek rozpadlby sie na dwie sprawy."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        _firma(db, tenant_b["id"], "KLE", ["klepsydra.pl"])
        poczta.przyjmij(db, poczta.przeczytaj(_mail()))
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            nadawca="serwis@klepsydra.pl", temat="Re: [BON-1] Nie dziala drukarka",
            message_id="<obcy-1@klepsydra.pl>",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_DOPISZ
        assert wynik.zgloszenie.tenant_id == tenant_a["id"]


# --- kwalifikacja: nierozpoznane i ignorowane -------------------------------

def test_nieznana_domena_czeka_i_nie_zaklada_zgloszenia(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wynik = poczta.przyjmij(db, poczta.przeczytaj(
            _mail(nadawca="anna.kowalska@gmail.com", message_id="<obca-1@gmail.com>")
        ))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_NIEROZPOZNANA
        czekajaca = db.execute(select(NierozpoznanaWiadomosc)).scalar_one()
        assert czekajaca.domena == "gmail.com"
        assert czekajaca.stan == NIEROZPOZNANA_CZEKA
        assert db.execute(select(Zgloszenie)).scalars().all() == []


def test_poczta_z_wlasnej_domeny_tez_jest_nierozpoznana(tenant_a):
    """Skrzynka sluzy wylacznie helpdeskowi - wlasna domena nie ma tu wyjatku."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            nadawca="ksiegowosc@mojadomena.pl", temat="Faktura do podpisu",
            message_id="<wewn-1@mojadomena.pl>",
        )))
        db.commit()
        assert wynik.decyzja == poczta.DECYZJA_NIEROZPOZNANA


def test_ignorowana_domena_nie_zasmieca_listy_ale_zostaje_w_bazie(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.ignoruj_domene(db, "sklep-online.pl", zalozyl="operator@mojadomena.pl")
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            nadawca="noreply@sklep-online.pl", temat="Twoja przesylka",
            message_id="<spam-1@sklep-online.pl>",
        )))
        db.commit()

        assert wynik.decyzja == poczta.DECYZJA_ZIGNORUJ
        wpis = db.execute(select(NierozpoznanaWiadomosc)).scalar_one()
        assert wpis.stan == NIEROZPOZNANA_ZIGNOROWANA
        regula = poczta.domena_ignorowana(db, "sklep-online.pl")
        assert regula.przechwycone == 1 and regula.ostatnia_wiadomosc is not None


def test_domeny_firmy_nie_da_sie_zignorowac(tenant_a):
    """Jedno klikniecie zamykaloby droge zgloszeniom klienta - po cichu,
    bo nikt nie dostaje odpowiedzi o odrzuceniu."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        db.commit()
        with pytest.raises(helpdesk.BladHelpdesku, match="zakladac zgloszenia"):
            poczta.ignoruj_domene(db, "bongo.pl")


def test_przywrocenie_domeny_otwiera_ja_z_powrotem(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        poczta.ignoruj_domene(db, "nowa-firma.pl")
        db.commit()

        assert poczta.przywroc_domene(db, "nowa-firma.pl") is True
        db.commit()

        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(
            nadawca="biuro@nowa-firma.pl", message_id="<nowa-1@nowa-firma.pl>"
        )))
        db.commit()
        assert wynik.decyzja == poczta.DECYZJA_NIEROZPOZNANA


def test_ta_sama_wiadomosc_dwa_razy_nie_dubluje_wpisu(tenant_a):
    """Pobieranie moze sie przerwac po odczycie, a przed oznaczeniem
    wiadomosci - ten sam Message-ID przyjdzie wtedy ponownie."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        surowa = _mail(nadawca="anna@gmail.com", message_id="<powtorka@gmail.com>")
        poczta.przyjmij(db, poczta.przeczytaj(surowa))
        poczta.przyjmij(db, poczta.przeczytaj(surowa))
        db.commit()

        assert len(db.execute(select(NierozpoznanaWiadomosc)).scalars().all()) == 1


def test_wiadomosc_bez_nadawcy_jest_pomijana(tenant_a):
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        wynik = poczta.przyjmij(db, poczta.przeczytaj(_mail(nadawca="")))
        db.commit()
        assert wynik.decyzja == poczta.DECYZJA_ZIGNORUJ
        assert db.execute(select(NierozpoznanaWiadomosc)).scalars().all() == []


def test_powod_decyzji_jest_zapisany_slowami(tenant_a):
    """Przy pytaniu "czemu ten mail nie zalozyl zgloszenia" powod jest jedyna
    odpowiedzia, ktora nie wymaga zgadywania."""
    with SessionLocal() as db:
        _firma(db, tenant_a["id"], "BON", ["bongo.pl"])
        db.commit()

        nowe = poczta.zakwalifikuj(db, poczta.przeczytaj(_mail()))
        obce = poczta.zakwalifikuj(db, poczta.przeczytaj(_mail(nadawca="x@gmail.com")))

        assert "bongo.pl" in nowe.powod and "Firma A" in nowe.powod
        assert "gmail.com" in obce.powod and "nie nalezy" in obce.powod
