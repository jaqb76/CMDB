"""Kto moze sie zalogowac i z jakimi uprawnieniami.

Jedno wejscie dla panelu WWW i aplikacji mobilnej: ``uwierzytelnij``. Po
domenie loginu wybiera katalog firmy (AD) albo konto lokalne CMDB -
uzytkownik niczego nie wybiera.

Uprawnienia kont z katalogu wynikaja z grup i sa liczone od nowa przy kazdym
logowaniu oraz przy synchronizacji co kwadrans. Zasada granicy firmy:
  * katalog firmy X daje wylacznie role admin/viewer w firmie X,
  * superadmina, audytora i technika helpdesku daje wylacznie katalog
    operatora - administrator AD klienta moze dopisac sie do dowolnej grupy
    u siebie i nie moze przez to wyjsc poza swoja firme.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    HASLO_NIEUZYWANE,
    ROLE_KATALOGU_FIRMY,
    ROLE_KATALOGU_OPERATORA,
    ZRODLO_AD,
    ZRODLO_LOKALNE,
    HelpdeskDostep,
    KatalogTozsamosci,
    PortalUser,
    Tenant,
    utcnow,
)
from . import katalog as ldap
from .katalog import BladKatalogu, WpisKatalogu, normalizuj_domene, normalizuj_identyfikator

log = logging.getLogger(__name__)

SPOSOB_AD = "ad"
SPOSOB_LOKALNE = "lokalne"


@dataclass
class Wynik:
    """Wynik logowania. ``user`` None znaczy odmowe; ``powod`` mowi jaka."""

    user: PortalUser | None
    powod: str = ""  # "haslo" | "brak_dostepu" | "katalog" | "lokalne_wylaczone"
    sposob: str = SPOSOB_LOKALNE


@dataclass
class Uprawnienia:
    rola: str = "viewer"
    tenant_id: str | None = None
    superadmin: bool = False
    audytor: bool = False
    firmy_helpdesku: set[str] = field(default_factory=set)
    zrodla: list[str] = field(default_factory=list)

    def klucz(self) -> tuple:
        return (self.rola, self.tenant_id, self.superadmin, self.audytor,
                tuple(sorted(self.firmy_helpdesku)))


# --- rozpoznanie katalogu ---------------------------------------------------

def katalog_dla_loginu(db: Session, login: str) -> KatalogTozsamosci | None:
    domena, _ = ldap.rozbierz_login(login)
    if not domena:
        return None
    for kandydat in db.execute(
        select(KatalogTozsamosci).where(KatalogTozsamosci.aktywny.is_(True))
    ).scalars():
        if domena in {normalizuj_domene(d) for d in kandydat.domeny or []}:
            if kandydat.tenant is not None and not kandydat.tenant.is_active:
                return None
            return kandydat
    return None


def domeny_zajete(db: Session, pomijajac: str | None = None) -> dict[str, KatalogTozsamosci]:
    wynik = {}
    for kandydat in db.execute(select(KatalogTozsamosci)).scalars():
        if kandydat.id == pomijajac:
            continue
        for d in kandydat.domeny or []:
            wynik[normalizuj_domene(d)] = kandydat
    return wynik


def katalog_dla_adresu(db: Session, email: str) -> KatalogTozsamosci | None:
    """Katalog, do ktorego nalezy domena adresu - takze nieaktywny.

    Konto lokalne z adresem w domenie katalogu byloby niejednoznaczne:
    po wpisaniu takiego loginu nie wiadomo, czy pytac AD, czy CMDB.
    """
    domena, _ = ldap.rozbierz_login(email)
    return domeny_zajete(db).get(domena) if domena else None


def sposob_logowania(db: Session, login: str) -> dict:
    """Dla podpowiedzi pod polem loginu. Nie zdradza, czy konto istnieje."""
    kat = katalog_dla_loginu(db, login)
    if kat is None:
        return {"sposob": SPOSOB_LOKALNE}
    return {"sposob": SPOSOB_AD, "firma": kat.tenant.name if kat.tenant else kat.nazwa}


# --- uprawnienia z grup -----------------------------------------------------

def ustal_uprawnienia(kat: KatalogTozsamosci, wpis: WpisKatalogu) -> Uprawnienia | None:
    """Uprawnienia osoby z katalogu albo None, gdy nie ma dostepu."""
    if not wpis.aktywne:
        return None
    grupy = wpis.identyfikatory_grup()
    trafione = [m for m in kat.mapowania if normalizuj_identyfikator(m.identyfikator) in grupy]

    if not kat.operatora:
        role = {m.rola for m in trafione if m.rola in ROLE_KATALOGU_FIRMY}
        zrodla = sorted({m.nazwa for m in trafione if m.rola in ROLE_KATALOGU_FIRMY})
        if "admin" in role:
            return Uprawnienia(rola="admin", tenant_id=kat.tenant_id,
                               zrodla=sorted(m.nazwa for m in trafione if m.rola == "admin"))
        if "viewer" in role:
            return Uprawnienia(rola="viewer", tenant_id=kat.tenant_id, zrodla=zrodla)
        if kat.bez_grupy == "viewer":
            return Uprawnienia(rola="viewer", tenant_id=kat.tenant_id,
                               zrodla=["każdy z katalogu"])
        return None

    operatorskie = [m for m in trafione if m.rola in ROLE_KATALOGU_OPERATORA]
    if any(m.rola == "superadmin" for m in operatorskie):
        return Uprawnienia(rola="admin", superadmin=True,
                           zrodla=sorted(m.nazwa for m in operatorskie if m.rola == "superadmin"))
    if any(m.rola == "audytor" for m in operatorskie):
        return Uprawnienia(rola="viewer", audytor=True,
                           zrodla=sorted(m.nazwa for m in operatorskie if m.rola == "audytor"))
    firmy = {m.tenant_id for m in operatorskie if m.rola == "helpdesk" and m.tenant_id}
    if firmy:
        return Uprawnienia(rola="admin", firmy_helpdesku=firmy,
                           zrodla=sorted({m.nazwa for m in operatorskie if m.rola == "helpdesk"}))
    return None


# --- zakladanie i aktualizacja kont -----------------------------------------

def _obecne(db: Session, user: PortalUser) -> Uprawnienia:
    firmy = set(db.execute(
        select(HelpdeskDostep.tenant_id).where(
            HelpdeskDostep.user_id == user.id, HelpdeskDostep.zrodlo == ZRODLO_AD)
    ).scalars())
    return Uprawnienia(rola=user.role, tenant_id=user.tenant_id, superadmin=user.is_superadmin,
                       audytor=user.is_global_viewer, firmy_helpdesku=firmy)


def _znajdz_konto(db: Session, kat: KatalogTozsamosci, wpis: WpisKatalogu) -> PortalUser | None:
    konto = db.execute(select(PortalUser).where(
        PortalUser.katalog_id == kat.id, PortalUser.zewnetrzny_id == wpis.guid
    )).scalar_one_or_none()
    if konto is not None or not wpis.email:
        return konto
    # Jednorazowe przejecie istniejacego konta lokalnego tej samej firmy -
    # tylko gdy adres jest w domenie tego katalogu. Po nim konto jest juz
    # wiazane po GUID, a adres nie ma znaczenia.
    domena, _ = ldap.rozbierz_login(wpis.email)
    if domena not in {normalizuj_domene(d) for d in kat.domeny or []}:
        return None
    kandydat = db.execute(
        select(PortalUser).where(PortalUser.email == wpis.email)
    ).scalar_one_or_none()
    if (kandydat is not None and kandydat.zrodlo == ZRODLO_LOKALNE
            and kandydat.tenant_id == kat.tenant_id):
        log.info("konto lokalne %s przechodzi na katalog %s", kandydat.email, kat.nazwa)
        return kandydat
    return None


def _email_konta(db: Session, kat: KatalogTozsamosci, wpis: WpisKatalogu,
                 konto: PortalUser | None) -> str | None:
    email = (wpis.email or "").strip().lower()
    if not email:
        domena = next((normalizuj_domene(d) for d in kat.domeny or []
                       if not normalizuj_domene(d).endswith("\\")), "")
        email = f"{wpis.login.lower()}@{domena}" if domena and wpis.login else ""
    if not email:
        return None
    zajete = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()
    if zajete is not None and (konto is None or zajete.id != konto.id):
        return None
    return email


def zastosuj(db: Session, kat: KatalogTozsamosci, wpis: WpisKatalogu,
             upr: Uprawnienia | None, konto: PortalUser | None = None,
             aktor: str = "system") -> PortalUser | None:
    """Zaklada albo aktualizuje konto wedlug katalogu. None = brak dostepu."""
    from .scoping import audit

    if konto is None:
        konto = _znajdz_konto(db, kat, wpis)

    if konto is not None and konto.wylaczone_recznie:
        # Superadmin wylaczyl konto w CMDB - katalog tego nie cofa.
        konto.ostatnia_synchronizacja = utcnow()
        return None

    if upr is None:
        if konto is not None and konto.is_active:
            konto.is_active = False
            konto.session_version = PortalUser.session_version + 1
            konto.uprawnienia_z = []
            _ustaw_firmy_helpdesku(db, konto, set())
            audit(db, None, action="katalog.dostep_odebrany", target=konto.email,
                  detail={"katalog": kat.nazwa, "aktywne_w_ad": wpis.aktywne}, actor=aktor)
        if konto is not None:
            konto.ostatnia_synchronizacja = utcnow()
        return None

    email = _email_konta(db, kat, wpis, konto)
    if email is None:
        log.warning("katalog %s: %s nie ma adresu albo adres jest zajety przez inne konto",
                    kat.nazwa, wpis.login)
        return None

    nowe = konto is None
    if nowe:
        konto = PortalUser(email=email, password_hash=HASLO_NIEUZYWANE, session_version=1)
        db.add(konto)
        poprzednie = None
    else:
        poprzednie = _obecne(db, konto)
        byl_aktywny = konto.is_active

    konto.email = email
    konto.full_name = wpis.nazwa or konto.full_name
    konto.zrodlo = ZRODLO_AD
    konto.katalog_id = kat.id
    konto.zewnetrzny_id = wpis.guid
    konto.password_hash = HASLO_NIEUZYWANE
    konto.role = upr.rola
    konto.tenant_id = upr.tenant_id
    konto.is_superadmin = upr.superadmin
    konto.is_global_viewer = upr.audytor
    konto.uprawnienia_z = upr.zrodla
    konto.ostatnia_synchronizacja = utcnow()
    konto.is_active = True
    db.flush()
    _ustaw_firmy_helpdesku(db, konto, upr.firmy_helpdesku)

    if nowe:
        audit(db, None, action="katalog.konto_utworzone", target=email,
              detail={"katalog": kat.nazwa, "rola": upr.rola, "grupy": upr.zrodla}, actor=aktor)
    elif poprzednie.klucz() != upr.klucz() or not byl_aktywny:
        # Zmiana uprawnien ma dzialac od razu, a nie po wygasnieciu sesji.
        konto.session_version = PortalUser.session_version + 1
        audit(db, None, action="katalog.uprawnienia_zmienione", target=email,
              detail={"katalog": kat.nazwa, "rola": upr.rola, "grupy": upr.zrodla,
                      "superadmin": upr.superadmin, "audytor": upr.audytor}, actor=aktor)
    return konto


def _ustaw_firmy_helpdesku(db: Session, konto: PortalUser, firmy: set[str]) -> None:
    obecne = {d.tenant_id: d for d in db.execute(
        select(HelpdeskDostep).where(HelpdeskDostep.user_id == konto.id)
    ).scalars()}
    for tenant_id, dostep in obecne.items():
        if dostep.zrodlo == ZRODLO_AD and tenant_id not in firmy:
            db.delete(dostep)
    for tenant_id in firmy - set(obecne):
        db.add(HelpdeskDostep(user_id=konto.id, tenant_id=tenant_id, nadal="katalog",
                              zrodlo=ZRODLO_AD))


# --- logowanie --------------------------------------------------------------

def uwierzytelnij(db: Session, login: str, haslo: str) -> Wynik:
    """Sprawdza login i haslo - w katalogu albo lokalnie."""
    from .auth import authenticate_user

    kat = katalog_dla_loginu(db, login)
    if kat is not None:
        if not haslo:
            return Wynik(None, "haslo", SPOSOB_AD)
        try:
            wpis = ldap.klient(kat).sprawdz_haslo(login, haslo)
        except BladKatalogu as blad:
            log.warning("katalog %s niedostepny przy logowaniu: %s", kat.nazwa, blad)
            return Wynik(None, "katalog", SPOSOB_AD)
        if wpis is None:
            return Wynik(None, "haslo", SPOSOB_AD)
        konto = zastosuj(db, kat, wpis, ustal_uprawnienia(kat, wpis), aktor=wpis.email or login)
        if konto is None:
            return Wynik(None, "brak_dostepu", SPOSOB_AD)
        return Wynik(konto, "", SPOSOB_AD)

    user = authenticate_user(db, login, haslo)
    if user is None:
        return Wynik(None, "haslo")
    if user.zrodlo != ZRODLO_LOKALNE:
        return Wynik(None, "haslo")
    if user.tenant_id:
        firma = db.get(Tenant, user.tenant_id)
        if firma is not None and not firma.logowanie_lokalne:
            return Wynik(None, "lokalne_wylaczone")
    return Wynik(user)


KOMUNIKATY = {
    "haslo": "Nieprawidłowy login lub hasło.",
    "brak_dostepu": ("Twoje konto nie ma dostępu do CMDB. Poproś administratora IT "
                     "o dodanie do odpowiedniej grupy."),
    "katalog": ("Nie udało się połączyć z serwerem domeny. Spróbuj za chwilę "
                "lub zgłoś to do działu IT."),
    "lokalne_wylaczone": ("Twoja firma loguje się wyłącznie kontem domenowym. "
                          "Konto lokalne jest wyłączone."),
}


# --- synchronizacja ---------------------------------------------------------

def synchronizuj(db: Session, kat: KatalogTozsamosci) -> dict:
    """Przelicza uprawnienia wszystkich kont z katalogu.

    Konto usuniete albo wylaczone w AD traci dostep. Gdy katalog nie odpowiada,
    NIE ruszamy zadnego konta - awaria sieci nie moze wylogowac calej firmy.
    """
    wynik = {"sprawdzone": 0, "odebrane": 0, "zmienione": 0, "blad": None}
    try:
        klient = ldap.klient(kat)
        konta = db.execute(select(PortalUser).where(
            PortalUser.katalog_id == kat.id, PortalUser.zrodlo == ZRODLO_AD
        )).scalars().all()
        for konto in konta:
            wersja = konto.session_version
            aktywne = konto.is_active
            wpis = klient.znajdz_po_guid(konto.zewnetrzny_id) if konto.zewnetrzny_id else None
            if wpis is None:
                wpis = WpisKatalogu(guid=konto.zewnetrzny_id or "", dn="", login="",
                                    email=konto.email, nazwa=None, aktywne=False)
            zastosuj(db, kat, wpis, ustal_uprawnienia(kat, wpis), konto=konto,
                     aktor="synchronizacja")
            wynik["sprawdzone"] += 1
            if aktywne and not konto.is_active:
                wynik["odebrane"] += 1
            elif konto.session_version != wersja:
                wynik["zmienione"] += 1
        kat.ostatni_blad = None
    except BladKatalogu as blad:
        db.rollback()
        wynik["blad"] = str(blad)
        kat.ostatni_blad = str(blad)[:1000]
        log.warning("synchronizacja katalogu %s nieudana: %s", kat.nazwa, blad)
    kat.ostatnia_synchronizacja = utcnow()
    db.commit()
    return wynik


def synchronizuj_wszystkie(db: Session) -> dict:
    wyniki = {}
    for kat in db.execute(
        select(KatalogTozsamosci).where(KatalogTozsamosci.aktywny.is_(True))
    ).scalars().all():
        try:
            wyniki[kat.nazwa] = synchronizuj(db, kat)
        except Exception as blad:  # jeden katalog nie moze zatrzymac reszty
            db.rollback()
            log.error("synchronizacja katalogu %s: %s", kat.nazwa, blad)
            wyniki[kat.nazwa] = {"blad": str(blad)}
    return wyniki
