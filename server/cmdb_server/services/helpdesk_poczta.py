"""Co helpdesk robi z przychodzaca wiadomoscia.

Skrzynka jest jedna i wspolna dla wszystkich klientow, wiec kazdy mail trzeba
najpierw zakwalifikowac. Kolejnosc pytan nie jest dowolna:

1. Czy to odpowiedz w istniejacym watku? Wtedy firma i numer sa juz znane
   i nie ma czego rozpoznawac - nawet gdy klient napisze z innego adresu.
2. Czy domena nadawcy wskazuje firme? Wtedy powstaje nowe zgloszenie.
3. Nie wskazuje - wiadomosc czeka w nierozpoznanych i nikt nie dostaje
   odpowiedzi.

Rozpoznawanie jest tu oddzielone od IMAP-a i od bazy w tym sensie, ze
``przeczytaj`` przerabia surowa wiadomosc na fakty, a ``zakwalifikuj``
podejmuje decyzje. Dzieki temu obie da sie sprawdzic testem na zwyklym
napisie, bez serwera pocztowego - a to jest jedyny sposob, zeby test
odpowiedzi klienta w ogole byl mozliwy.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    NIEROZPOZNANA_CZEKA,
    NIEROZPOZNANA_ZIGNOROWANA,
    STATUS_OCZEKUJE,
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    IgnorowanaDomena,
    Tenant,
    WpisZgloszenia,
    ZalacznikWpisu,
    Zgloszenie,
    utcnow,
)
from . import helpdesk

log = logging.getLogger(__name__)

# Naglowki, po ktorych poznaje sie wiadomosc wysylana przez automat.
# Odpowiadanie na nie konczy sie petla dwoch systemow piszacych do siebie
# w kolko, a takiej korespondencji nikt nie przerwie w nocy.
NAGLOWKI_AUTOMATU = (
    "auto-submitted",       # RFC 3834 - wszystko poza "no" znaczy automat
    "x-autoreply",
    "x-autorespond",
    "x-auto-response-suppress",
)
NADAWCY_AUTOMATU = ("mailer-daemon@", "postmaster@", "noreply@", "no-reply@")

# Ile dni wstecz szukamy zgloszenia po samym nadawcy i temacie. Klient bywa
# uprzejmy i pisze "jeszcze raz w tej samej sprawie" bez cytowania numeru;
# po tygodniu ta sama fraza znaczy juz zwykle nowa sprawe.
DNI_DOPASOWANIA_TEMATU = 7

_PREFIKSY_ODPOWIEDZI = re.compile(r"^(?:\s*(?:re|odp|fw|fwd|pd)\s*:\s*)+", re.IGNORECASE)


def _tekst_naglowka(wartosc) -> str:
    """Naglowek rozkodowany do zwyklego napisu.

    Temat po polsku przychodzi zakodowany (=?UTF-8?B?...?=) i bez rozkodowania
    trafialby w takiej postaci na liste zgloszen - a numer w temacie schowany
    w base64 nie dopasowalby sie do niczego.
    """
    if wartosc is None:
        return ""
    try:
        return str(make_header(decode_header(str(wartosc)))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(wartosc).strip()


def _tresc(wiadomosc: EmailMessage) -> str:
    """Tresc jako czysty tekst.

    Bierzemy czesc text/plain, a gdy jej nie ma - html odarty ze znacznikow.
    Zalaczniki zbiera osobna funkcja; tu chodzi o to, zeby ich obecnosc nie
    przeszkodzila w odczytaniu tresci.
    """
    czesc = None
    if wiadomosc.is_multipart():
        czesc = wiadomosc.get_body(preferencelist=("plain", "html"))
    else:
        czesc = wiadomosc

    if czesc is None:
        return ""
    try:
        tresc = czesc.get_content()
    except (LookupError, UnicodeDecodeError):
        surowe = czesc.get_payload(decode=True) or b""
        tresc = surowe.decode("utf-8", errors="replace")

    if (czesc.get_content_type() or "").endswith("html"):
        tresc = re.sub(r"<br\s*/?>|</p>", "\n", tresc, flags=re.IGNORECASE)
        tresc = re.sub(r"<[^>]+>", "", tresc)
    return tresc.strip()


def _bez_prefiksow(temat: str) -> str:
    """Temat bez "Re:", "Odp:" i "Fwd:" - do porownywania watkow."""
    return _PREFIKSY_ODPOWIEDZI.sub("", temat or "").strip()


@dataclass(frozen=True)
class Zalacznik:
    """Plik z wiadomosci. Nazwa pochodzi od klienta, wiec jest tylko opisem."""

    nazwa: str
    typ_mime: str | None
    dane: bytes

    @property
    def rozmiar(self) -> int:
        return len(self.dane)


@dataclass(frozen=True)
class Wiadomosc:
    """Fakty wyjete z surowego maila. Bez ocen i bez decyzji."""

    nadawca: str
    nadawca_nazwa: str | None
    temat: str
    tresc: str
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...] = ()
    dw: tuple[str, ...] = ()
    zalaczniki: tuple[Zalacznik, ...] = ()
    data: datetime | None = None
    automat: bool = False
    naglowki: dict[str, str] = field(default_factory=dict)

    @property
    def domena(self) -> str:
        return helpdesk.domena_adresu(self.nadawca)


def przeczytaj(surowa: bytes) -> Wiadomosc:
    """Surowy mail (RFC 822) na fakty."""
    wiadomosc = message_from_bytes(surowa, policy=policy.default)

    nadawcy = getaddresses([str(wiadomosc.get("From", ""))])
    nazwa, adres = (nadawcy[0] if nadawcy else ("", ""))

    referencje = tuple(
        znacznik for znacznik in re.findall(r"<[^>]+>", str(wiadomosc.get("References", "")))
    )
    data = None
    if wiadomosc.get("Date"):
        try:
            data = parsedate_to_datetime(str(wiadomosc["Date"]))
        except (TypeError, ValueError):
            data = None
    if data is not None and data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)

    kopia = tuple(
        adres_dw.strip().lower()
        for _, adres_dw in getaddresses([str(wiadomosc.get("Cc", ""))])
        if adres_dw and "@" in adres_dw
    )

    return Wiadomosc(
        nadawca=adres.strip().lower(),
        nadawca_nazwa=_tekst_naglowka(nazwa) or None,
        temat=_tekst_naglowka(wiadomosc.get("Subject")),
        tresc=_tresc(wiadomosc),
        message_id=(str(wiadomosc["Message-ID"]).strip() if wiadomosc.get("Message-ID") else None),
        in_reply_to=(str(wiadomosc["In-Reply-To"]).strip() if wiadomosc.get("In-Reply-To") else None),
        references=referencje,
        dw=kopia,
        zalaczniki=_zalaczniki(wiadomosc),
        data=data,
        automat=_czy_automat(wiadomosc, adres),
        naglowki={
            klucz: str(wartosc) for klucz, wartosc in wiadomosc.items()
            if klucz.lower() in ("from", "to", "cc", "subject", "date", "message-id",
                                 "in-reply-to", "references", "auto-submitted")
        },
    )


def _zalaczniki(wiadomosc: EmailMessage) -> tuple[Zalacznik, ...]:
    """Pliki z wiadomosci, bez czesci tekstowych skladajacych sie na tresc.

    Zbyt duzy plik pomijamy z ostrzezeniem w dzienniku, ale reszty wiadomosci
    nie odrzucamy: zgloszenie z trescia i bez jednego zalacznika jest lepsze
    niz brak zgloszenia.
    """
    if not wiadomosc.is_multipart():
        return ()

    limit = get_settings().helpdesk_zalacznik_mb * 1024 * 1024
    zebrane: list[Zalacznik] = []
    for czesc in wiadomosc.iter_attachments():
        dane = czesc.get_payload(decode=True)
        if not dane:
            continue
        if len(dane) > limit:
            log.warning(
                "pomijam zalacznik %s (%d B) - powyzej limitu %d B",
                czesc.get_filename(), len(dane), limit,
            )
            continue
        zebrane.append(Zalacznik(
            nazwa=_tekst_naglowka(czesc.get_filename()) or "zalacznik",
            typ_mime=czesc.get_content_type(),
            dane=dane,
        ))
    return tuple(zebrane)


def _czy_automat(wiadomosc, nadawca: str) -> bool:
    for naglowek in NAGLOWKI_AUTOMATU:
        wartosc = wiadomosc.get(naglowek)
        if wartosc is None:
            continue
        # RFC 3834: "no" znaczy, ze wiadomosc napisal czlowiek.
        if naglowek == "auto-submitted" and str(wartosc).strip().lower() == "no":
            continue
        return True
    if str(wiadomosc.get("Precedence", "")).strip().lower() in ("bulk", "auto_reply", "list"):
        return True
    return any((nadawca or "").lower().startswith(prefiks) for prefiks in NADAWCY_AUTOMATU)


# --- kwalifikacja -----------------------------------------------------------

DECYZJA_DOPISZ = "dopisz"
DECYZJA_NOWE = "nowe"
DECYZJA_NIEROZPOZNANA = "nierozpoznana"
DECYZJA_ZIGNORUJ = "zignoruj"


@dataclass(frozen=True)
class Kwalifikacja:
    """Co zrobic z wiadomoscia i dlaczego.

    Powod nie jest ozdoba: przy pytaniu "czemu ten mail nie zalozyl
    zgloszenia" jest jedyna odpowiedzia, ktora nie wymaga zgadywania.
    """

    decyzja: str
    powod: str
    zgloszenie: Zgloszenie | None = None
    firma: Tenant | None = None


def zakwalifikuj(db: Session, wiadomosc: Wiadomosc) -> Kwalifikacja:
    """Decyzja o jednej wiadomosci. Niczego nie zapisuje."""
    if not wiadomosc.nadawca or "@" not in wiadomosc.nadawca:
        return Kwalifikacja(DECYZJA_ZIGNORUJ, "wiadomosc bez czytelnego nadawcy")

    zgloszenie = dopasuj_watek(db, wiadomosc)
    if zgloszenie is not None:
        return Kwalifikacja(
            DECYZJA_DOPISZ,
            f"odpowiedz w watku {zgloszenie.numer_pelny}",
            zgloszenie=zgloszenie,
            firma=db.get(Tenant, zgloszenie.tenant_id),
        )

    firma = helpdesk.firma_dla_adresu(db, wiadomosc.nadawca)
    if firma is not None:
        if wiadomosc.automat:
            # Autoodpowiedz z domeny klienta nie jest zgloszeniem. Zakladanie
            # zgloszen z "Jestem na urlopie do 15 wrzesnia" konczy sie tablica
            # pelna spraw, ktorych nikt nie zglaszal.
            return Kwalifikacja(
                DECYZJA_NIEROZPOZNANA,
                "wiadomosc automatyczna z domeny klienta - nie zaklada zgloszenia",
                firma=firma,
            )
        return Kwalifikacja(DECYZJA_NOWE, f"domena {wiadomosc.domena} -> {firma.name}", firma=firma)

    if domena_ignorowana(db, wiadomosc.domena):
        return Kwalifikacja(DECYZJA_ZIGNORUJ, f"domena {wiadomosc.domena} jest ignorowana")

    return Kwalifikacja(
        DECYZJA_NIEROZPOZNANA, f"domena {wiadomosc.domena} nie nalezy do zadnej firmy"
    )


def dopasuj_watek(db: Session, wiadomosc: Wiadomosc) -> Zgloszenie | None:
    """Zgloszenie, do ktorego nalezy ta wiadomosc.

    Trzy proby, od najpewniejszej: naglowki watku, numer w temacie, wreszcie
    ten sam nadawca z tym samym tematem w otwartej sprawie sprzed tygodnia.
    Ostatnia jest najslabsza i dlatego jest ostatnia - ale bez niej klient,
    ktory skasuje numer z tematu i odpowie z telefonu, zaklada duplikat.
    """
    znaczniki = [z for z in (wiadomosc.in_reply_to, *wiadomosc.references) if z]
    if znaczniki:
        zgloszenie = db.execute(
            select(Zgloszenie)
            .join(WpisZgloszenia, WpisZgloszenia.zgloszenie_id == Zgloszenie.id)
            .where(WpisZgloszenia.message_id.in_(znaczniki))
            .order_by(Zgloszenie.utworzono.desc())
        ).scalars().first()
        if zgloszenie is not None:
            return zgloszenie

    z_tematu = helpdesk.znajdz_po_numerze(db, helpdesk.rozpoznaj_numer(wiadomosc.temat))
    if z_tematu is not None:
        return z_tematu

    return _dopasuj_po_temacie(db, wiadomosc)


def _dopasuj_po_temacie(db: Session, wiadomosc: Wiadomosc) -> Zgloszenie | None:
    from datetime import timedelta

    from ..models import STATUSY_OTWARTE

    temat = _bez_prefiksow(wiadomosc.temat)
    if not temat:
        return None

    granica = utcnow() - timedelta(days=DNI_DOPASOWANIA_TEMATU)
    return db.execute(
        select(Zgloszenie)
        .where(
            Zgloszenie.zglaszajacy_email == wiadomosc.nadawca,
            Zgloszenie.status.in_(STATUSY_OTWARTE),
            Zgloszenie.utworzono >= granica,
            Zgloszenie.temat == temat,
        )
        .order_by(Zgloszenie.utworzono.desc())
    ).scalars().first()


# --- ignorowane domeny ------------------------------------------------------

def domena_ignorowana(db: Session, domena: str) -> IgnorowanaDomena | None:
    czysta = helpdesk.normalizuj_domene(domena)
    if not czysta:
        return None
    return db.execute(
        select(IgnorowanaDomena).where(IgnorowanaDomena.domena == czysta)
    ).scalar_one_or_none()


def ignoruj_domene(db: Session, domena: str, zalozyl: str | None = None) -> IgnorowanaDomena:
    """Zaklada regule. Domeny nalezacej do firmy nie da sie zignorowac.

    Bez tego zastrzezenia jedno klikniecie zamykaloby droge zgloszeniom
    klienta - po cichu, bo nikt nie dostaje odpowiedzi o odrzuceniu.
    """
    czysta = helpdesk.normalizuj_domene(domena)
    if not czysta:
        raise helpdesk.BladHelpdesku("pusta domena")

    wlasciciel = helpdesk.wlasciciel_domeny(db, czysta)
    if wlasciciel is not None:
        firma = db.get(Tenant, wlasciciel.tenant_id)
        raise helpdesk.BladHelpdesku(
            f"domena {czysta} nalezy do firmy {firma.name if firma else '?'} "
            "- jej poczta ma zakladac zgloszenia"
        )

    istniejaca = domena_ignorowana(db, czysta)
    if istniejaca is not None:
        return istniejaca

    regula = IgnorowanaDomena(domena=czysta, zalozyl=zalozyl)
    db.add(regula)
    db.flush()
    return regula


def przywroc_domene(db: Session, domena: str) -> bool:
    """Zdejmuje regule. Wiadomosci juz przechwycone zostaja tam, gdzie sa."""
    regula = domena_ignorowana(db, domena)
    if regula is None:
        return False
    db.delete(regula)
    return True


def odnotuj_przechwycenie(db: Session, regula: IgnorowanaDomena) -> None:
    """Licznik przy regule - po nim widac pomylke.

    Regula, ktora przechwycila 200 wiadomosci w tydzien, albo trafia idealnie,
    albo zjada czyjas poczte. Bez licznika nie widac ani jednego, ani drugiego.
    """
    regula.przechwycone += 1
    regula.ostatnia_wiadomosc = utcnow()


# --- zapis wyniku kwalifikacji ---------------------------------------------

def zapisz_nierozpoznana(
    db: Session, wiadomosc: Wiadomosc, *, stan: str = NIEROZPOZNANA_CZEKA
):
    """Odklada wiadomosc na liste do przejrzenia (albo od razu do zignorowanych).

    Duplikatow nie zakladamy: ten sam Message-ID moze przyjsc drugi raz, gdy
    pobieranie przerwie sie po odczycie, a przed oznaczeniem wiadomosci.
    """
    from ..models import NierozpoznanaWiadomosc

    if wiadomosc.message_id:
        istniejaca = db.execute(
            select(NierozpoznanaWiadomosc).where(
                NierozpoznanaWiadomosc.message_id == wiadomosc.message_id
            )
        ).scalar_one_or_none()
        if istniejaca is not None:
            return istniejaca

    wpis = NierozpoznanaWiadomosc(
        nadawca_email=wiadomosc.nadawca,
        nadawca_nazwa=wiadomosc.nadawca_nazwa,
        domena=wiadomosc.domena,
        temat=wiadomosc.temat[:500] if wiadomosc.temat else None,
        tresc=wiadomosc.tresc,
        message_id=wiadomosc.message_id,
        naglowki=wiadomosc.naglowki,
        otrzymano=wiadomosc.data or utcnow(),
        stan=stan,
    )
    db.add(wpis)
    db.flush()
    return wpis


def _po_odpowiedzi_klienta(db: Session, zgloszenie: Zgloszenie) -> None:
    """Odpowiedz klienta wraca pilka na strone technika.

    Zamkniete: sprawa otwiera sie z powrotem. Zamkniete zgloszenia sa poza
    tablica, wiec wiadomosc doklejona do takiego watku nie trafilaby nikomu
    na oczy - sprawa wygladalaby na zalatwiona, a klient czekalby na
    odpowiedz, ktorej nikt nie pisze.

    Oczekuje: czekalismy wlasnie na te wiadomosc, wiec zgloszenie wraca do
    realizacji. Bez tego "Oczekuje" zbieralo by sprawy, na ktore klient juz
    odpisal, i technik musialby przegladac te kolumne recznie.

    Nowe i W trakcie zostawiamy: dopisek klienta do sprawy, ktorej nikt
    jeszcze nie wzial, nie robi z niej sprawy w toku.
    """
    if zgloszenie.status == STATUS_ZAMKNIETE:
        helpdesk.zdarzenie(
            db, zgloszenie, "zgłoszenie otwarte ponownie odpowiedzią klienta", autor="system"
        )
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor="system")
    elif zgloszenie.status == STATUS_OCZEKUJE:
        helpdesk.zdarzenie(
            db, zgloszenie, "klient odpowiedział - zgłoszenie wraca do realizacji",
            autor="system",
        )
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor="system")


# --- zalaczniki na dysku ----------------------------------------------------

def katalog_zalacznikow() -> Path:
    return Path(get_settings().helpdesk_dir) / "zalaczniki"


_DOZWOLONE_ROZSZERZENIE = re.compile(r"^[a-z0-9]{1,8}$")


def _rozszerzenie(zalacznik: Zalacznik) -> str:
    """Rozszerzenie pliku na dysku. Bierzemy je z typu MIME albo z nazwy.

    Nazwa od klienta nie dotyka systemu plikow - moze zawierac sciezke,
    dwukropek albo sto znakow. Rozszerzenie przepuszczamy tylko wtedy, gdy
    sklada sie z liter i cyfr.
    """
    z_typu = mimetypes.guess_extension(zalacznik.typ_mime or "") or ""
    kandydat = (z_typu or Path(zalacznik.nazwa).suffix).lstrip(".").lower()
    return f".{kandydat}" if _DOZWOLONE_ROZSZERZENIE.fullmatch(kandydat) else ".bin"


def zapisz_zalaczniki(
    db: Session, wpis: WpisZgloszenia, zalaczniki: tuple[Zalacznik, ...]
) -> list[ZalacznikWpisu]:
    """Zapisuje pliki na dysk i opisuje je w bazie.

    Nazwa pliku na dysku powstaje z identyfikatora wpisu i licznika - nazwa
    z maila zostaje wylacznie jako opis. Blad zapisu jednego pliku nie moze
    przewrocic calego odbioru poczty: wiadomosc jest juz w bazie i wazniejsza
    od zalacznika.
    """
    if not zalaczniki:
        return []

    katalog = katalog_zalacznikow() / wpis.zgloszenie_id
    zapisane: list[ZalacznikWpisu] = []
    for numer, zalacznik in enumerate(zalaczniki, start=1):
        wzgledna = f"{wpis.zgloszenie_id}/{wpis.id}-{numer}{_rozszerzenie(zalacznik)}"
        try:
            katalog.mkdir(parents=True, exist_ok=True)
            (katalog_zalacznikow() / wzgledna).write_bytes(zalacznik.dane)
        except OSError as blad:
            log.error("nie zapisalem zalacznika %s: %s", zalacznik.nazwa, blad)
            continue

        wpis_zalacznika = ZalacznikWpisu(
            wpis_id=wpis.id,
            zgloszenie_id=wpis.zgloszenie_id,
            nazwa=zalacznik.nazwa[:255],
            typ_mime=zalacznik.typ_mime,
            rozmiar=zalacznik.rozmiar,
            sciezka=wzgledna,
        )
        db.add(wpis_zalacznika)
        zapisane.append(wpis_zalacznika)

    db.flush()
    return zapisane


def przyjmij(db: Session, wiadomosc: Wiadomosc) -> Kwalifikacja:
    """Kwalifikuje wiadomosc i zapisuje jej skutek. Nic nie wysyla.

    Wysylka potwierdzenia jest osobnym krokiem, bo zapis do bazy musi sie udac
    niezaleznie od tego, czy serwer SMTP akurat odpowiada.
    """
    wynik = zakwalifikuj(db, wiadomosc)

    if wynik.decyzja == DECYZJA_DOPISZ and wynik.zgloszenie is not None:
        wpis = helpdesk.dopisz_wiadomosc(
            db, wynik.zgloszenie,
            rodzaj="od_klienta",
            tresc=wiadomosc.tresc,
            autor_email=wiadomosc.nadawca,
            autor_nazwa=wiadomosc.nadawca_nazwa,
            message_id=wiadomosc.message_id,
            in_reply_to=wiadomosc.in_reply_to,
            dw=list(wiadomosc.dw),
        )
        zapisz_zalaczniki(db, wpis, wiadomosc.zalaczniki)
        _po_odpowiedzi_klienta(db, wynik.zgloszenie)
        return wynik

    if wynik.decyzja == DECYZJA_NOWE and wynik.firma is not None:
        zgloszenie = helpdesk.utworz_zgloszenie(
            db,
            tenant_id=wynik.firma.id,
            temat=_bez_prefiksow(wiadomosc.temat) or "(bez tematu)",
            tresc=wiadomosc.tresc,
            zglaszajacy_email=wiadomosc.nadawca,
            zglaszajacy_nazwa=wiadomosc.nadawca_nazwa,
            message_id=wiadomosc.message_id,
            dw=list(wiadomosc.dw),
        )
        pierwszy = helpdesk.pierwszy_wpis(db, zgloszenie.id)
        if pierwszy is not None:
            zapisz_zalaczniki(db, pierwszy, wiadomosc.zalaczniki)
        return Kwalifikacja(wynik.decyzja, wynik.powod, zgloszenie=zgloszenie, firma=wynik.firma)

    if wynik.decyzja == DECYZJA_ZIGNORUJ:
        regula = domena_ignorowana(db, wiadomosc.domena)
        if regula is not None:
            odnotuj_przechwycenie(db, regula)
            zapisz_nierozpoznana(db, wiadomosc, stan=NIEROZPOZNANA_ZIGNOROWANA)
        return wynik

    zapisz_nierozpoznana(db, wiadomosc)
    return wynik
