"""Helpdesk: firmy, domeny, numeracja i zgloszenia.

Jedna skrzynka pocztowa obsluguje wszystkich klientow, wiec o tym, czyje jest
zgloszenie, decyduje domena nadawcy - a dopiero firma nadaje numer. Ta
kolejnosc jest tu wymuszona konstrukcyjnie: numer powstaje wylacznie w
``nadaj_numer`` i wylacznie dla wskazanej firmy.

Modul celowo nie wie nic o poczcie ani o HTTP. Odbior IMAP i wysylka SMTP
korzystaja z tych funkcji, ale nie odwrotnie - dzieki temu regule "domena ->
firma -> numer" da sie sprawdzic testem bez serwera pocztowego.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import (
    STATUS_NOWE,
    STATUS_ZAMKNIETE,
    WPIS_DO_KLIENTA,
    WPIS_OD_KLIENTA,
    WPIS_SYSTEM,
    WPIS_WEWNETRZNY,
    Asset,
    CzasPracy,
    HelpdeskDomena,
    HelpdeskDostep,
    HelpdeskFirma,
    PortalUser,
    Tenant,
    WpisSlownika,
    WpisZgloszenia,
    Zgloszenie,
    ZgloszenieSprzet,
    utcnow,
)


class BladHelpdesku(RuntimeError):
    """Blad, ktorego tresc mozna pokazac uzytkownikowi."""


# Znacznik numeru w temacie: "Re: [BON-123] Nie dziala drukarka".
# Skrot ma 2-8 znakow, bo z jednej litery nie da sie zrobic czytelnego numeru,
# a osiem to juz nie skrot.
WZORZEC_NUMERU = re.compile(r"\[([A-Za-z]{2,8})-(\d{1,9})\]")


# --- domeny -----------------------------------------------------------------

def normalizuj_domene(domena: str) -> str:
    """Domena do postaci porownywalnej: male litery, bez spacji i kropki na koncu."""
    czysta = (domena or "").strip().lower().rstrip(".")
    if czysta.startswith("@"):
        czysta = czysta[1:]
    return czysta


def domena_adresu(email: str) -> str:
    """Sama domena z adresu. Pusty napis, gdy adres jest bez sensu."""
    _, _, domena = (email or "").strip().lower().rpartition("@")
    return normalizuj_domene(domena)


def wlasciciel_domeny(db: Session, domena: str) -> HelpdeskDomena | None:
    czysta = normalizuj_domene(domena)
    if not czysta:
        return None
    return db.execute(
        select(HelpdeskDomena).where(HelpdeskDomena.domena == czysta)
    ).scalar_one_or_none()


def firma_dla_adresu(db: Session, email: str) -> Tenant | None:
    """Firma wskazana przez domene nadawcy. None, gdy domeny nikt nie zglosil."""
    wpis = wlasciciel_domeny(db, domena_adresu(email))
    return None if wpis is None else db.get(Tenant, wpis.tenant_id)


def obca_firma_adresu(db: Session, email: str, tenant_id: str) -> Tenant | None:
    """Firma, do ktorej nalezy domena adresu, gdy to NIE jest wskazana firma.

    Sluzy zgloszeniom zakladanym recznie: tam firme wybiera technik, a nie
    domena, wiec da sie wpisac adres z cudzej domeny. Taka pomylka nie zostaje
    w jednym zgloszeniu - odpowiedz z tego adresu wroci poczta i zalozy sprawe
    tej drugiej firmie, wiec lepiej zatrzymac ja przy zakladaniu.

    Adres z domeny, ktorej nikt nie zglosil (prywatna skrzynka pracownika),
    nie jest pomylka - zwracamy None i zgloszenie powstaje.
    """
    firma = firma_dla_adresu(db, email)
    return None if firma is None or firma.id == tenant_id else firma


def dodaj_domene(db: Session, tenant_id: str, domena: str, dodal: str | None = None) -> HelpdeskDomena:
    """Przypisuje domene do firmy.

    Ta sama domena nie moze nalezec do dwoch firm - inaczej nie dalo by sie
    powiedziec, czyje jest zgloszenie. Blad nazywa firme, ktora domene juz ma:
    "zajeta" bez wskazania kto ja zajal zmusza do szukania po tabelach.
    """
    czysta = normalizuj_domene(domena)
    if not czysta or "." not in czysta:
        raise BladHelpdesku(f"'{domena}' nie wyglada na domene")

    istniejaca = wlasciciel_domeny(db, czysta)
    if istniejaca is not None:
        if istniejaca.tenant_id == tenant_id:
            return istniejaca
        wlasciciel = db.get(Tenant, istniejaca.tenant_id)
        nazwa = wlasciciel.name if wlasciciel else istniejaca.tenant_id
        raise BladHelpdesku(f"domena {czysta} nalezy juz do firmy {nazwa}")

    wpis = HelpdeskDomena(tenant_id=tenant_id, domena=czysta, dodal=dodal)
    db.add(wpis)
    db.flush()
    return wpis


def domeny_firmy(db: Session, tenant_id: str) -> list[HelpdeskDomena]:
    return list(db.execute(
        select(HelpdeskDomena)
        .where(HelpdeskDomena.tenant_id == tenant_id)
        .order_by(HelpdeskDomena.domena)
    ).scalars())


# --- firmy i numeracja ------------------------------------------------------

def proponuj_skrot(nazwa: str) -> str:
    """Skrot firmy z jej nazwy: LKS z "LKS", BON z "Bongo", GLO z "Gloria".

    Ogonki zdejmujemy, bo skrot idzie w temacie maila i w numerze zgloszenia -
    a te przechodza przez systemy, ktore z "Ł" robia znak zapytania.
    """
    bez_ogonkow = unicodedata.normalize("NFKD", nazwa or "")
    # Polskie "ł" nie ma postaci rozlozonej, wiec NFKD go nie ruszy.
    bez_ogonkow = bez_ogonkow.replace("ł", "l").replace("Ł", "L")
    litery = [z for z in bez_ogonkow.upper() if z.isascii() and z.isalnum()]
    skrot = "".join(litery[:3])
    return skrot or "HD"


def zapewnij_firme(
    db: Session, tenant: Tenant, skrot: str | None = None
) -> HelpdeskFirma:
    """Wlacza firme do helpdesku, nadajac jej skrot numeru.

    Skrot jest unikalny w calym systemie. Gdy proponowany jest zajety,
    doklejamy cyfre - dwie firmy o podobnych nazwach zdarzaja sie czesto,
    a numer musi wskazywac jedna.
    """
    wpis = db.get(HelpdeskFirma, tenant.id)
    if wpis is not None:
        return wpis

    kandydat = (skrot or proponuj_skrot(tenant.name)).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,8}", kandydat):
        raise BladHelpdesku(
            f"skrot '{kandydat}' musi miec 2-8 znakow, same litery lub cyfry bez ogonkow"
        )

    ostateczny, licznik = kandydat, 1
    while db.execute(
        select(HelpdeskFirma).where(HelpdeskFirma.skrot == ostateczny)
    ).scalar_one_or_none() is not None:
        licznik += 1
        ostateczny = f"{kandydat[:6]}{licznik}"

    wpis = HelpdeskFirma(tenant_id=tenant.id, skrot=ostateczny)
    db.add(wpis)
    db.flush()
    return wpis


def numer_pelny(skrot: str, numer: int) -> str:
    return f"{skrot}-{numer}"


def nadaj_numer(db: Session, tenant_id: str) -> tuple[int, str]:
    """Kolejny numer firmy. Jedyne miejsce, w ktorym numer powstaje.

    Zwiekszenie i odczyt ida jednym UPDATE ... RETURNING, wiec dwa procesy
    odbierajace poczte rownoczesnie nie nadadza tego samego numeru dwom
    zgloszeniom - drugi czeka na blokade wiersza, nie na uprzejmosc pierwszego.
    """
    wiersz = db.execute(
        update(HelpdeskFirma)
        .where(HelpdeskFirma.tenant_id == tenant_id)
        .values(licznik=HelpdeskFirma.licznik + 1)
        .returning(HelpdeskFirma.skrot, HelpdeskFirma.licznik)
    ).one_or_none()
    if wiersz is None:
        raise BladHelpdesku("firma nie jest wlaczona do helpdesku")
    skrot, numer = wiersz
    return numer, numer_pelny(skrot, numer)


def rozpoznaj_numer(temat: str | None) -> str | None:
    """Numer zgloszenia z tematu maila, w postaci "BON-123"."""
    if not temat:
        return None
    trafienie = WZORZEC_NUMERU.search(temat)
    if trafienie is None:
        return None
    return f"{trafienie.group(1).upper()}-{int(trafienie.group(2))}"


def znajdz_po_numerze(db: Session, numer: str | None) -> Zgloszenie | None:
    if not numer:
        return None
    return db.execute(
        select(Zgloszenie).where(Zgloszenie.numer_pelny == numer.strip().upper())
    ).scalar_one_or_none()


# --- dostep technikow -------------------------------------------------------

def prowadzi_helpdesk(user: PortalUser) -> bool:
    """Czy konto zarzadza helpdeskiem: domenami, skrzynka i dostepami technikow.

    Robi to superadmin i tylko on. Nadanie technikowi firmy jest jednoczesnie
    otwarciem mu kartoteki tej firmy w CMDB, wiec nie moze tego zrobic nikt,
    kto sam nie ma prawa przekraczac granicy firm - a administrator firmy
    ("operator" w slowniku CMDB) go nie ma.
    """
    return bool(user.is_superadmin)


def firmy_technika(db: Session, user: PortalUser) -> list[str]:
    """Identyfikatory firm, ktore technik obsluguje.

    Superadmin dostaje wszystkie firmy, bo prowadzi caly helpdesk. Zwykle konto
    portalu bez zadnego dostepu helpdeskowego dostaje pusta liste - a nie
    swoja wlasna firme: dostep do panelu firmy nie jest tym samym co prawo do
    czytania jej zgloszen.
    """
    if prowadzi_helpdesk(user):
        return list(db.execute(select(Tenant.id).where(Tenant.is_active.is_(True))).scalars())
    return list(db.execute(
        select(HelpdeskDostep.tenant_id).where(HelpdeskDostep.user_id == user.id)
    ).scalars())


def ma_dostep(db: Session, user: PortalUser, tenant_id: str) -> bool:
    if prowadzi_helpdesk(user):
        return True
    return db.execute(
        select(HelpdeskDostep.id).where(
            HelpdeskDostep.user_id == user.id,
            HelpdeskDostep.tenant_id == tenant_id,
        )
    ).scalar_one_or_none() is not None


def nadaj_dostep(db: Session, user_id: str, tenant_id: str, nadal: str | None = None) -> HelpdeskDostep:
    """Przydziela kontu firme helpdesku.

    Dwa rodzaje kont odmawiaja przyjecia firmy, bo nic by z niej nie mialy:
    superadmin obsluguje juz wszystkie, a audytor globalny nie zapisuje nigdzie
    (odbiera mu to tenant_context_for). Nadanie firmy audytorowi wygladaloby
    jak nadanie uprawnien technika, a nie dawaloby ich wcale - lepiej odmowic
    i powiedziec, co zrobic zamiast tego.
    """
    konto = db.get(PortalUser, user_id)
    if konto is None:
        raise BladHelpdesku("nie znaleziono konta")
    if konto.is_superadmin:
        raise BladHelpdesku(
            f"{konto.email} jest superadminem i obsługuje wszystkie firmy — "
            "nie trzeba mu ich nadawać."
        )
    if konto.is_global_viewer:
        raise BladHelpdesku(
            f"{konto.email} jest audytorem globalnym i niczego nie zmienia w firmach. "
            "Zmień najpierw rodzaj konta na technika helpdesku."
        )
    if konto.tenant_id:
        # Konto firmy pracuje w swojej jednej firmie. Dolozenie mu firm
        # helpdeskowych dawaloby konto o dwoch roznych zrodlach uprawnien -
        # i nie dalo by sie powiedziec, czym ono wlasciwie jest.
        raise BladHelpdesku(
            f"{konto.email} jest kontem firmy. Zmień najpierw jego rodzaj na "
            "technika helpdesku — technik nie należy do żadnej firmy."
        )

    istniejacy = db.execute(
        select(HelpdeskDostep).where(
            HelpdeskDostep.user_id == user_id, HelpdeskDostep.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if istniejacy is not None:
        return istniejacy
    wpis = HelpdeskDostep(user_id=user_id, tenant_id=tenant_id, nadal=nadal)
    db.add(wpis)
    db.flush()
    return wpis


def odbierz_dostep(db: Session, user_id: str, tenant_id: str) -> bool:
    """Odebranie firmy nie rusza historii - technik przestaje ja tylko widziec."""
    wpis = db.execute(
        select(HelpdeskDostep).where(
            HelpdeskDostep.user_id == user_id, HelpdeskDostep.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if wpis is None:
        return False
    db.delete(wpis)
    return True


# --- zgloszenia -------------------------------------------------------------

def utworz_zgloszenie(
    db: Session,
    *,
    tenant_id: str,
    temat: str,
    tresc: str,
    zglaszajacy_email: str,
    zglaszajacy_nazwa: str | None = None,
    typ: str | None = None,
    technik_id: str | None = None,
    message_id: str | None = None,
    dw: list[str] | None = None,
    zrodlo: str = "e-mail",
    autor: str = "system",
) -> Zgloszenie:
    """Zaklada zgloszenie razem z pierwszym wpisem w watku."""
    numer, pelny = nadaj_numer(db, tenant_id)
    teraz = utcnow()
    zgloszenie = Zgloszenie(
        tenant_id=tenant_id,
        numer=numer,
        numer_pelny=pelny,
        temat=(temat or "(bez tematu)").strip()[:500],
        status=STATUS_NOWE,
        typ=typ,
        zglaszajacy_email=zglaszajacy_email.strip().lower(),
        zglaszajacy_nazwa=zglaszajacy_nazwa,
        technik_id=technik_id,
        utworzono=teraz,
        ostatnia_aktywnosc=teraz,
    )
    db.add(zgloszenie)
    db.flush()

    db.add(WpisZgloszenia(
        zgloszenie_id=zgloszenie.id,
        rodzaj=WPIS_OD_KLIENTA,
        autor_email=zgloszenie.zglaszajacy_email,
        autor_nazwa=zglaszajacy_nazwa,
        tresc=tresc or "",
        message_id=message_id,
        dw=dw or None,
        utworzono=teraz,
    ))
    # Sesja nie ma autoflush, wiec bez tego pierwszy wpis nie istnieje jeszcze
    # dla zapytan - a to do niego dopina sie zalaczniki z tej samej wiadomosci.
    db.flush()
    zdarzenie(db, zgloszenie, f"zgłoszenie utworzone ({zrodlo}) jako {pelny}", autor=autor)
    podepnij_sprzet_zglaszajacego(db, zgloszenie)
    return zgloszenie


def zdarzenie(
    db: Session, zgloszenie: Zgloszenie, opis: str, autor: str | None = None,
    autor_id: str | None = None,
) -> WpisZgloszenia:
    """Wpis systemowy: przypisanie, zmiana statusu, dopisany czas.

    Zdarzenia leza w tym samym watku co wiadomosci, bo historia zgloszenia ma
    byc jedna lista czytana od gory do dolu.
    """
    wpis = WpisZgloszenia(
        zgloszenie_id=zgloszenie.id,
        rodzaj=WPIS_SYSTEM,
        autor_id=autor_id,
        autor_nazwa=autor,
        tresc=opis,
        utworzono=utcnow(),
    )
    db.add(wpis)
    return wpis


def dopisz_wiadomosc(
    db: Session,
    zgloszenie: Zgloszenie,
    *,
    rodzaj: str,
    tresc: str,
    autor: PortalUser | None = None,
    autor_email: str | None = None,
    autor_nazwa: str | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    dw: list[str] | None = None,
) -> WpisZgloszenia:
    """Dokleja wpis do watku i odswieza date ostatniej aktywnosci."""
    if rodzaj not in (WPIS_OD_KLIENTA, WPIS_DO_KLIENTA, WPIS_WEWNETRZNY):
        raise BladHelpdesku(f"nieznany rodzaj wpisu: {rodzaj}")

    wpis = WpisZgloszenia(
        zgloszenie_id=zgloszenie.id,
        rodzaj=rodzaj,
        autor_id=autor.id if autor else None,
        autor_email=autor_email or (autor.email if autor else None),
        autor_nazwa=autor_nazwa or (autor.full_name if autor else None),
        tresc=tresc or "",
        message_id=message_id,
        in_reply_to=in_reply_to,
        dw=dw or None,
        utworzono=utcnow(),
    )
    db.add(wpis)
    db.flush()
    zgloszenie.ostatnia_aktywnosc = wpis.utworzono
    return wpis


def przypisz(
    db: Session, zgloszenie: Zgloszenie, technik: PortalUser | None, autor: str
) -> None:
    """Zmiana odpowiedzialnego. Zawsze zostawia slad, bo to jest historia sprawy."""
    if technik is not None and not ma_dostep(db, technik, zgloszenie.tenant_id):
        raise BladHelpdesku(
            f"{technik.email} nie ma dostepu do firmy tego zgloszenia"
        )
    poprzedni = zgloszenie.technik_id
    zgloszenie.technik_id = technik.id if technik else None
    zgloszenie.ostatnia_aktywnosc = utcnow()

    if technik is None:
        zdarzenie(db, zgloszenie, "zgłoszenie bez przypisanego technika", autor=autor)
    elif poprzedni is None:
        zdarzenie(db, zgloszenie, f"zgłoszenie przypisane: {opis_osoby(technik)}", autor=autor)
    else:
        zdarzenie(db, zgloszenie, f"zgłoszenie przekazane: {opis_osoby(technik)}", autor=autor)


def zmien_status(db: Session, zgloszenie: Zgloszenie, status: str, autor: str) -> None:
    from ..models import STATUSY_ZGLOSZENIA

    if status not in STATUSY_ZGLOSZENIA:
        raise BladHelpdesku(f"nieznany status: {status}")
    if status == zgloszenie.status:
        return
    poprzedni = STATUSY_ZGLOSZENIA[zgloszenie.status]
    zgloszenie.status = status
    zgloszenie.ostatnia_aktywnosc = utcnow()
    zgloszenie.zamkniete_o = utcnow() if status == STATUS_ZAMKNIETE else None
    zdarzenie(
        db, zgloszenie,
        f"status: {poprzedni} -> {STATUSY_ZGLOSZENIA[status]}",
        autor=autor,
    )


def opis_osoby(user: PortalUser) -> str:
    return user.full_name or user.email


def pierwszy_wpis(db: Session, zgloszenie_id: str) -> WpisZgloszenia | None:
    """Pierwsza wiadomosc klienta - ta, ktora zalozyla zgloszenie."""
    return db.execute(
        select(WpisZgloszenia)
        .where(
            WpisZgloszenia.zgloszenie_id == zgloszenie_id,
            WpisZgloszenia.rodzaj == WPIS_OD_KLIENTA,
        )
        .order_by(WpisZgloszenia.utworzono)
        .limit(1)
    ).scalar_one_or_none()


def ostatnia_od_klienta(db: Session, zgloszenie_id: str) -> WpisZgloszenia | None:
    """Ostatnia wiadomosc klienta w watku.

    Z niej biora sie naglowki odpowiedzi i lista DW: rozmowa ma wrocic do tych
    samych osob, ktore w niej sa, a nie do zestawu sprzed tygodnia.
    """
    return db.execute(
        select(WpisZgloszenia)
        .where(
            WpisZgloszenia.zgloszenie_id == zgloszenie_id,
            WpisZgloszenia.rodzaj == WPIS_OD_KLIENTA,
        )
        .order_by(WpisZgloszenia.utworzono.desc())
        .limit(1)
    ).scalar_one_or_none()


# --- czas pracy -------------------------------------------------------------

def dodaj_czas(
    db: Session, zgloszenie: Zgloszenie, technik: PortalUser, minuty: int,
    opis: str | None = None,
) -> CzasPracy:
    """Dopisuje czas jednego technika.

    Zgloszenie ma jednego wlasciciela, ale pracowac przy nim moze kilka osob -
    kazda dopisuje swoje minuty i kazda widnieje w raporcie osobno. Wpisu nie
    da sie pozniej zmienic ani skasowac, bo te minuty ida na fakture.
    """
    if minuty <= 0:
        raise BladHelpdesku("czas pracy musi byc dodatni")
    if not ma_dostep(db, technik, zgloszenie.tenant_id):
        raise BladHelpdesku(f"{technik.email} nie ma dostepu do firmy tego zgloszenia")

    wpis = CzasPracy(
        zgloszenie_id=zgloszenie.id,
        tenant_id=zgloszenie.tenant_id,
        technik_id=technik.id,
        minuty=int(minuty),
        opis=(opis or None),
    )
    db.add(wpis)
    zgloszenie.ostatnia_aktywnosc = utcnow()
    zdarzenie(
        db, zgloszenie,
        f"{opis_osoby(technik)} dopisał {formatuj_czas(minuty)}"
        + (f" - {opis}" if opis else ""),
        autor=opis_osoby(technik), autor_id=technik.id,
    )
    return wpis


@dataclass(frozen=True)
class UdzialTechnika:
    technik_id: str
    nazwa: str
    minuty: int


def czas_zgloszenia(db: Session, zgloszenie_id: str) -> tuple[int, list[UdzialTechnika]]:
    """Suma minut i rozbicie na techników - w kolejnosci od najwiekszego wkladu."""
    wiersze = db.execute(
        select(
            CzasPracy.technik_id,
            func.coalesce(PortalUser.full_name, PortalUser.email),
            func.sum(CzasPracy.minuty),
        )
        .join(PortalUser, PortalUser.id == CzasPracy.technik_id)
        .where(CzasPracy.zgloszenie_id == zgloszenie_id)
        .group_by(CzasPracy.technik_id, PortalUser.full_name, PortalUser.email)
        .order_by(func.sum(CzasPracy.minuty).desc())
    ).all()
    udzialy = [UdzialTechnika(t, nazwa, int(minuty)) for t, nazwa, minuty in wiersze]
    return sum(u.minuty for u in udzialy), udzialy


def formatuj_czas(minuty: int | None) -> str:
    """Minuty jako "1 h 35 min". Zero to kreska, a nie "0 min"."""
    if not minuty:
        return "-"
    godziny, reszta = divmod(int(minuty), 60)
    if not godziny:
        return f"{reszta} min"
    return f"{godziny} h {reszta:02d} min"


def czas_okresu(
    db: Session, *, tenant_id: str | None = None, technik_id: str | None = None,
    od: datetime | None = None, do: datetime | None = None,
):
    """Zapytanie o czas pracy zawezone do firmy, technika i okresu.

    Wspolna podstawa obu raportow - firma -> technik i technik -> firma to ta
    sama tabela ogladana z dwoch stron, wiec filtr powstaje w jednym miejscu.
    """
    stmt = select(CzasPracy)
    if tenant_id is not None:
        stmt = stmt.where(CzasPracy.tenant_id == tenant_id)
    if technik_id is not None:
        stmt = stmt.where(CzasPracy.technik_id == technik_id)
    if od is not None:
        stmt = stmt.where(CzasPracy.utworzono >= od)
    if do is not None:
        stmt = stmt.where(CzasPracy.utworzono < do)
    return stmt


# --- sprzet, ktorego dotyczy zgloszenie -------------------------------------

def osoba_o_adresie(db: Session, tenant_id: str, email: str) -> WpisSlownika | None:
    """Osoba z kartoteki firmy o tym adresie e-mail.

    Adres jest w slowniku osob polem wymaganym, wiec nadawca maila da sie
    zwykle rozpoznac dokladnie - bez zgadywania po imieniu i nazwisku, ktore
    przy dwoch Kowalskich konczy sie podpieciem cudzego laptopa.
    """
    czysty = (email or "").strip().lower()
    if not czysty:
        return None
    return db.execute(
        select(WpisSlownika).where(
            WpisSlownika.tenant_id == tenant_id,
            WpisSlownika.kategoria == "osoba",
            func.lower(WpisSlownika.atrybuty["email"].astext) == czysty,
        )
    ).scalars().first()


def sprzet_zglaszajacego(db: Session, tenant_id: str, email: str) -> list[Asset]:
    """Sprzet przypisany zglaszajacemu jako uzytkownikowi.

    Opiekun to nie to samo co uzytkownik: opiekunem laptopa prezesa jest ktos
    z IT, a zglasza awarie prezes. Szukamy wiec po tym, kto przy sprzecie
    siedzi.
    """
    osoba = osoba_o_adresie(db, tenant_id, email)
    if osoba is None:
        return []
    return list(db.execute(
        select(Asset)
        .where(Asset.tenant_id == tenant_id, Asset.uzytkownik_id == osoba.id)
        .order_by(Asset.hostname)
    ).scalars())


def podepnij_sprzet(
    db: Session, zgloszenie: Zgloszenie, asset: Asset, *,
    zrodlo: str = "reczne", dodal: str | None = None,
) -> ZgloszenieSprzet:
    """Wiaze zgloszenie ze sprzetem. Sprzet musi nalezec do firmy zgloszenia."""
    if asset.tenant_id != zgloszenie.tenant_id:
        raise BladHelpdesku("sprzet nalezy do innej firmy niz zgloszenie")

    istniejace = db.execute(
        select(ZgloszenieSprzet).where(
            ZgloszenieSprzet.zgloszenie_id == zgloszenie.id,
            ZgloszenieSprzet.asset_id == asset.id,
        )
    ).scalar_one_or_none()
    if istniejace is not None:
        return istniejace

    wiazanie = ZgloszenieSprzet(
        zgloszenie_id=zgloszenie.id, asset_id=asset.id, zrodlo=zrodlo, dodal=dodal
    )
    db.add(wiazanie)
    db.flush()
    if zrodlo == "reczne":
        zdarzenie(db, zgloszenie, f"podpięty sprzęt: {asset.hostname}", autor=dodal)
    return wiazanie


def odepnij_sprzet(db: Session, zgloszenie: Zgloszenie, asset_id: str, autor: str | None = None) -> bool:
    wiazanie = db.execute(
        select(ZgloszenieSprzet).where(
            ZgloszenieSprzet.zgloszenie_id == zgloszenie.id,
            ZgloszenieSprzet.asset_id == asset_id,
        )
    ).scalar_one_or_none()
    if wiazanie is None:
        return False
    asset = db.get(Asset, asset_id)
    db.delete(wiazanie)
    zdarzenie(
        db, zgloszenie,
        f"odpięty sprzęt: {asset.hostname if asset else asset_id}", autor=autor,
    )
    return True


def podepnij_sprzet_zglaszajacego(db: Session, zgloszenie: Zgloszenie) -> Asset | None:
    """Podpina sprzet nadawcy, gdy da sie go wskazac jednoznacznie.

    Jeden sprzet uzytkownika - podpinamy sam, bo tego dotyczy zgloszenie
    w zdecydowanej wiekszosci przypadkow. Kilka - zostawiamy technikowi:
    dopisanie monitora i telefonu do zgloszenia o VPN zasmiecilo by ich karty
    napraw, a to wlasnie te karty maja pozniej odpowiadac, ile razy dany
    sprzet sie psul.
    """
    kandydaci = sprzet_zglaszajacego(db, zgloszenie.tenant_id, zgloszenie.zglaszajacy_email)
    if len(kandydaci) != 1:
        return None
    podepnij_sprzet(db, zgloszenie, kandydaci[0], zrodlo="automat")
    return kandydaci[0]


def sprzet_zgloszenia(db: Session, zgloszenie_id: str) -> list[tuple[Asset, ZgloszenieSprzet]]:
    return [
        (asset, wiazanie)
        for asset, wiazanie in db.execute(
            select(Asset, ZgloszenieSprzet)
            .join(ZgloszenieSprzet, ZgloszenieSprzet.asset_id == Asset.id)
            .where(ZgloszenieSprzet.zgloszenie_id == zgloszenie_id)
            .order_by(ZgloszenieSprzet.utworzono)
        ).all()
    ]


def zgloszenia_sprzetu(db: Session, asset_id: str, limit: int | None = None) -> list[Zgloszenie]:
    """Karta napraw sprzetu: jego zgloszenia od najnowszego.

    Odpowiada na pytanie, ktorego dzis w CMDB nie da sie zadac - ile razy ta
    drukarka juz sie psula i czy nie taniej ja wymienic niz naprawiac.
    """
    stmt = (
        select(Zgloszenie)
        .join(ZgloszenieSprzet, ZgloszenieSprzet.zgloszenie_id == Zgloszenie.id)
        .where(ZgloszenieSprzet.asset_id == asset_id)
        .order_by(Zgloszenie.utworzono.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())


def czas_sprzetu(db: Session, asset_id: str) -> int:
    """Ile minut lacznie poszlo na zgloszenia tego sprzetu."""
    return int(db.execute(
        select(func.coalesce(func.sum(CzasPracy.minuty), 0))
        .join(ZgloszenieSprzet, ZgloszenieSprzet.zgloszenie_id == CzasPracy.zgloszenie_id)
        .where(ZgloszenieSprzet.asset_id == asset_id)
    ).scalar_one())
