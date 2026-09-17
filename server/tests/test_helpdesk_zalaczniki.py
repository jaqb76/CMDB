"""Regresja obrazow wklejonych w tresc maila, bez dostepu do skrzynki."""
from __future__ import annotations

import base64
import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

from cmdb_server.services import helpdesk_poczta as poczta


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aPioAAAAASUVORK5CYII="
)
PDF = b"%PDF-1.4 przykladowy zalacznik"


def _mail_z_obrazem(*, nazwa="blad.png", alternatywa=True):
    mail = EmailMessage()
    mail["From"] = "jan@bongo.pl"
    mail["To"] = "helpdesk@operator.pl"
    mail["Subject"] = "Blad na ekranie"
    mail["Message-ID"] = "<obraz-1@bongo.pl>"
    html = '<p>Blad na ekranie: <img src="cid:obraz-1"></p>'
    if alternatywa:
        mail.set_content("Blad na ekranie")
        mail.add_alternative(html, subtype="html")
        czesc_html = mail.get_payload()[1]
    else:
        mail.set_content(html, subtype="html")
        czesc_html = mail
    czesc_html.add_related(
        PNG, maintype="image", subtype="png", cid="<obraz-1>",
        filename=nazwa, disposition="inline",
    )
    return mail


class TestZalacznikiPoczty(unittest.TestCase):
    def setUp(self):
        ustawienia = patch.object(
            poczta, "get_settings",
            return_value=SimpleNamespace(helpdesk_zalacznik_mb=1),
        )
        ustawienia.start()
        self.addCleanup(ustawienia.stop)

    def test_wklejony_obraz_z_nazwa_lub_samym_cid(self):
        for alternatywa in (True, False):
            for nazwa in ("blad.png", None):
                with self.subTest(alternatywa=alternatywa, nazwa=nazwa):
                    mail = _mail_z_obrazem(nazwa=nazwa, alternatywa=alternatywa)
                    wynik = poczta.przeczytaj(mail.as_bytes())
                    self.assertEqual(len(wynik.zalaczniki), 1)
                    obraz = wynik.zalaczniki[0]
                    self.assertEqual(obraz.dane, PNG)
                    self.assertEqual(obraz.typ_mime, "image/png")
                    self.assertEqual(obraz.nazwa, nazwa or "zalacznik")
                    self.assertEqual(poczta.do_podgladu(obraz.typ_mime, obraz.dane), "image/png")

    def test_wklejony_obraz_i_pdf_zostaja_razem(self):
        mail = _mail_z_obrazem()
        mail.add_attachment(PDF, maintype="application", subtype="pdf", filename="raport.pdf")
        wynik = poczta.przeczytaj(mail.as_bytes())
        self.assertEqual(
            [(z.nazwa, z.dane) for z in wynik.zalaczniki],
            [("blad.png", PNG), ("raport.pdf", PDF)],
        )

    def test_obraz_bez_content_disposition(self):
        mail = _mail_z_obrazem(nazwa=None)
        obraz = mail.get_payload()[1].get_payload()[1]
        del obraz["Content-Disposition"]
        wynik = poczta.przeczytaj(mail.as_bytes())
        self.assertEqual([z.dane for z in wynik.zalaczniki], [PNG])

    def test_wersje_tresci_nie_staja_sie_zalacznikami(self):
        mail = EmailMessage()
        mail.set_content("Opis problemu")
        mail.add_alternative("<p>Opis problemu</p>", subtype="html")
        wynik = poczta.przeczytaj(mail.as_bytes())
        self.assertEqual(wynik.tresc, "Opis problemu")
        self.assertEqual(wynik.zalaczniki, ())

    def test_zwykle_zalaczniki_tekstowe_i_pdf_nadal_sa_odczytywane(self):
        mail = EmailMessage()
        mail.set_content("Opis problemu")
        mail.add_attachment("Szczegoly bledu", subtype="plain", filename="log.txt")
        mail.add_attachment("<p>Raport</p>", subtype="html", filename="raport.html")
        mail.add_attachment(PDF, maintype="application", subtype="pdf", filename="raport.pdf")
        wynik = poczta.przeczytaj(mail.as_bytes())
        self.assertEqual([z.nazwa for z in wynik.zalaczniki], ["log.txt", "raport.html", "raport.pdf"])

    def test_limit_dotyczy_rowniez_wklejonego_obrazu(self):
        mail = _mail_z_obrazem()
        obraz = mail.get_payload()[1].get_payload()[1]
        obraz.set_content(
            PNG + b"x" * (1024 * 1024), maintype="image", subtype="png",
            cid="<obraz-1>", disposition="inline", filename="duzy.png",
        )
        mail.add_attachment(PDF, maintype="application", subtype="pdf", filename="raport.pdf")
        with self.assertLogs(poczta.log, level="WARNING"):
            wynik = poczta.przeczytaj(mail.as_bytes())
        self.assertEqual([z.nazwa for z in wynik.zalaczniki], ["raport.pdf"])


if __name__ == "__main__":
    unittest.main()
