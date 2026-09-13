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
    CzasPracy,
    HelpdeskDomena,
    HelpdeskDostep,
    HelpdeskFirma,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    Zgloszenie,
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

def operator(user: PortalUser) -> bool:
    """Operator widzi wszystkie firmy i konfiguracje helpdesku.

    To ta sama osoba, ktora zarzadza CMDB globalnie - nie mnozymy rol, bo
    druga lista uprawnien rozjezdza sie z pierwsza.
    """
    return bool(user.is_superadmin)


def firmy_technika(db: Session, user: PortalUser) -> list[str]:
    """Identyfikatory firm, ktore technik obsluguje.

    Operator dostaje wszystkie firmy, bo prowadzi caly helpdesk. Zwykle konto
    portalu bez zadnego dostepu helpdeskowego dostaje pusta liste - a nie
    swoja wlasna firme: dostep do panelu firmy nie jest tym samym co prawo do
    czytania jej zgloszen.
    """
    if operator(user):
        return list(db.execute(select(Tenant.id).where(Tenant.is_active.is_(True))).scalars())
    return list(db.execute(
        select(HelpdeskDostep.tenant_id).where(HelpdeskDostep.user_id == user.id)
    ).scalars())


def ma_dostep(db: Session, user: PortalUser, tenant_id: str) -> bool:
    if operator(user):
        return True
    return db.execute(
        select(HelpdeskDostep.id).where(
            HelpdeskDostep.user_id == user.id,
            HelpdeskDostep.tenant_id == tenant_id,
        )
    ).scalar_one_or_none() is not None


def nadaj_dostep(db: Session, user_id: str, tenant_id: str, nadal: str | None = None) -> HelpdeskDostep:
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
        utworzono=teraz,
    ))
    zdarzenie(db, zgloszenie, f"zgloszenie utworzone ({zrodlo}) jako {pelny}", autor=autor)
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
        utworzono=utcnow(),
    )
    db.add(wpis)
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
        zdarzenie(db, zgloszenie, "zgloszenie bez przypisanego technika", autor=autor)
    elif poprzedni is None:
        zdarzenie(db, zgloszenie, f"zgloszenie przypisane: {opis_osoby(technik)}", autor=autor)
    else:
        zdarzenie(db, zgloszenie, f"zgloszenie przekazane: {opis_osoby(technik)}", autor=autor)


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
        f"{opis_osoby(technik)} dopisal {formatuj_czas(minuty)}"
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
