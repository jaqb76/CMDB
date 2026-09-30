"""Przeniesienie calego zgloszenia pomiedzy firmami w jednej transakcji.

Numery publiczne, wpisy i pliki zachowuja tozsamosc. Zmiana firmy musi tez
zmienic zakres raportow czasu, relacje sprzetowe i odbiorcow przyszlych maili.
Funkcje nie wykonuja commit ani wysylki; transakcja nalezy do wywolujacego.
"""
from __future__ import annotations

import hashlib
import json

from itsdangerous import BadSignature, URLSafeTimedSerializer

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import (
    CzasPracy, HelpdeskDostep, HelpdeskFirma, PortalUser, PrzeniesienieZgloszenia,
    Tenant, WpisSlownika, WpisZgloszenia, Zgloszenie, ZgloszenieSprzet,
    WPIS_DO_KLIENTA, WPIS_OD_KLIENTA, utcnow,
)
from ..config import get_settings
from . import helpdesk
from .scoping import audit


class BrakDostepu(helpdesk.BladHelpdesku):
    pass


class Konflikt(helpdesk.BladHelpdesku):
    pass


def uprawniona_rola(user: PortalUser) -> bool:
    return bool(user.is_active and (user.is_superadmin or (
        not user.is_global_viewer and not user.tenant_id and user.role == "admin"
    )))


def firmy_docelowe(db: Session, user: PortalUser, zgloszenie: Zgloszenie) -> list[Tenant]:
    if not uprawniona_rola(user) or not helpdesk.ma_dostep(db, user, zgloszenie.tenant_id):
        return []
    return list(db.execute(
        select(Tenant).join(HelpdeskFirma, HelpdeskFirma.tenant_id == Tenant.id)
        .where(Tenant.id.in_(helpdesk.firmy_technika(db, user)),
               Tenant.id != zgloszenie.tenant_id, Tenant.is_active.is_(True),
               HelpdeskFirma.aktywna.is_(True))
        .order_by(Tenant.name)
    ).scalars())


def ostatnie(db: Session, zgloszenie_id: str) -> PrzeniesienieZgloszenia | None:
    return db.execute(
        select(PrzeniesienieZgloszenia)
        .where(PrzeniesienieZgloszenia.zgloszenie_id == zgloszenie_id)
        .order_by(PrzeniesienieZgloszenia.utworzono.desc()).limit(1)
    ).scalar_one_or_none()


def wersja(db: Session, zgloszenie: Zgloszenie) -> str:
    """Odcisk stanu pokazanego przed zatwierdzeniem, takze przy powrocie A-B-A."""
    przeniesienie = ostatnie(db, zgloszenie.id)
    dane = [zgloszenie.tenant_id, zgloszenie.ostatnia_aktywnosc.isoformat(),
            zgloszenie.status, zgloszenie.technik_id, zgloszenie.zglaszajacy_email,
            przeniesienie.id if przeniesienie else None]
    return hashlib.sha256(json.dumps(dane).encode()).hexdigest()


def _sprawdz_firme(db: Session, user: PortalUser, tenant_id: str, blokuj: bool = False) -> Tenant:
    zapytanie = select(Tenant).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))
    if blokuj:
        zapytanie = zapytanie.with_for_update(read=True)
    firma = db.execute(zapytanie).scalar_one_or_none()
    if firma is None or not uprawniona_rola(user):
        raise BrakDostepu("Brak uprawnień do obsługi zgłoszeń w obu firmach.")
    if not user.is_superadmin:
        dostep = select(HelpdeskDostep.id).where(
            HelpdeskDostep.user_id == user.id, HelpdeskDostep.tenant_id == tenant_id)
        if blokuj:
            dostep = dostep.with_for_update(read=True)
        if db.execute(dostep).scalar_one_or_none() is None:
            raise BrakDostepu("Brak uprawnień do obsługi zgłoszeń w obu firmach.")
    return firma


def sprawdz_wybor(
    db: Session, user: PortalUser, zgloszenie: Zgloszenie, *, tenant_id: str,
    kontakt_id: str = "", technik_id: str = "", powod: str = "", blokuj: bool = False,
) -> tuple[Tenant, WpisSlownika | None, PortalUser | None]:
    _sprawdz_firme(db, user, zgloszenie.tenant_id, blokuj)
    cel = _sprawdz_firme(db, user, tenant_id, blokuj)
    if tenant_id == zgloszenie.tenant_id:
        raise helpdesk.BladHelpdesku("Wybierz inną firmę niż obecna.")
    config_query = select(HelpdeskFirma).where(
        HelpdeskFirma.tenant_id == tenant_id, HelpdeskFirma.aktywna.is_(True))
    if blokuj:
        config_query = config_query.with_for_update()
    if db.execute(config_query).scalar_one_or_none() is None:
        raise helpdesk.BladHelpdesku("Firma docelowa ma wyłączony helpdesk.")
    if not powod.strip() or len(powod.strip()) > 1000:
        raise helpdesk.BladHelpdesku("Podaj powód przeniesienia (maksymalnie 1000 znaków).")
    kontakt = None
    if kontakt_id:
        query = select(WpisSlownika).where(
            WpisSlownika.id == kontakt_id, WpisSlownika.tenant_id == tenant_id,
            WpisSlownika.kategoria == "osoba")
        if blokuj:
            query = query.with_for_update(read=True).execution_options(populate_existing=True)
        kontakt = db.execute(query).scalar_one_or_none()
        adres = ((kontakt.atrybuty or {}).get("email") or "").strip().lower() if kontakt else ""
        if not kontakt or not adres or "@" not in adres or "." not in helpdesk.domena_adresu(adres):
            raise helpdesk.BladHelpdesku("Wybierz kontakt z adresem e-mail w firmie docelowej.")
        if helpdesk.obca_firma_adresu(db, adres, tenant_id) is not None:
            raise helpdesk.BladHelpdesku("Adres kontaktu należy do innej firmy.")
    technik = None
    if technik_id:
        query = select(PortalUser).where(PortalUser.id == technik_id)
        if blokuj:
            query = query.with_for_update(read=True).execution_options(populate_existing=True)
        technik = db.execute(query).scalar_one_or_none()
        if technik is None:
            raise helpdesk.BladHelpdesku("Wybierz technika obsługującego firmę docelową.")
        _sprawdz_firme(db, technik, tenant_id, blokuj)
    return cel, kontakt, technik


def opcje(db: Session, user: PortalUser, zgloszenie: Zgloszenie, tenant_id: str) -> dict:
    if tenant_id not in {f.id for f in firmy_docelowe(db, user, zgloszenie)}:
        raise BrakDostepu("Wybierz firmę, w której obsługujesz zgłoszenia.")
    kontakty = list(db.execute(select(WpisSlownika).where(
        WpisSlownika.tenant_id == tenant_id, WpisSlownika.kategoria == "osoba")
        .order_by(WpisSlownika.wartosc)).scalars())
    konta = list(db.execute(select(PortalUser).where(
        PortalUser.is_active.is_(True), PortalUser.tenant_id.is_(None),
        PortalUser.is_global_viewer.is_(False))).scalars())
    return {
        "contacts": [{"id": k.id, "name": k.wartosc, "email": (k.atrybuty or {}).get("email", "")}
                     for k in kontakty if (k.atrybuty or {}).get("email")],
        "technicians": [{"id": k.id, "name": helpdesk.opis_osoby(k)} for k in konta
                        if uprawniona_rola(k) and helpdesk.ma_dostep(db, k, tenant_id)],
    }


def _podpis() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="helpdesk-transfer-v1")


def przygotuj(db: Session, user: PortalUser, zgloszenie: Zgloszenie, **dane) -> dict:
    cel, kontakt, technik = sprawdz_wybor(db, user, zgloszenie, **dane)
    email = ((kontakt.atrybuty or {}).get("email") or "").strip().lower() if kontakt else ""
    zapis = {**dane, "user_id": user.id, "session_version": user.session_version,
             "ticket_id": zgloszenie.id, "oczekiwana_wersja": wersja(db, zgloszenie),
             "oczekiwany_kontakt": email}
    return {"token": _podpis().dumps(zapis), "firma": cel.name,
            "kontakt": f"{kontakt.wartosc} · {email}" if kontakt else "Bez kontaktu — wysyłka e-mail wstrzymana",
            "technik": helpdesk.opis_osoby(technik) if technik else "Nieprzypisany",
            "powod": dane["powod"].strip()}


def zatwierdz(db: Session, user: PortalUser, zgloszenie_id: str, token: str, ip: str | None = None):
    try:
        dane = _podpis().loads(token, max_age=15 * 60)
    except BadSignature as exc:
        raise Konflikt("Potwierdzenie wygasło lub jest nieprawidłowe. Przygotuj podgląd ponownie.") from exc
    if (dane.pop("user_id", None) != user.id or dane.pop("ticket_id", None) != zgloszenie_id
            or dane.pop("session_version", None) != user.session_version):
        raise BrakDostepu("Potwierdzenie nie należy do tego konta i zgłoszenia.")
    return przenies(db, user, zgloszenie_id, **dane, ip=ip)


def przenies(
    db: Session, user: PortalUser, zgloszenie_id: str, *, oczekiwana_wersja: str,
    tenant_id: str, kontakt_id: str = "", technik_id: str = "", powod: str,
    ip: str | None = None, oczekiwany_kontakt: str | None = None,
) -> Zgloszenie:
    # Blokade tej samej sprawy biora tez zapisy WWW, mobilne i odbior poczty.
    zgloszenie = db.execute(select(Zgloszenie).where(Zgloszenie.id == zgloszenie_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if zgloszenie is None:
        raise BrakDostepu("Nie znaleziono dostępnego zgłoszenia.")
    user = db.execute(select(PortalUser).where(PortalUser.id == user.id)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one()
    _sprawdz_firme(db, user, zgloszenie.tenant_id, True)
    if wersja(db, zgloszenie) != oczekiwana_wersja:
        raise Konflikt("Zgłoszenie zmieniło się od podglądu. Sprawdź je ponownie przed przeniesieniem.")
    cel, kontakt, technik = sprawdz_wybor(
        db, user, zgloszenie, tenant_id=tenant_id, kontakt_id=kontakt_id,
        technik_id=technik_id, powod=powod, blokuj=True)
    email = ((kontakt.atrybuty or {}).get("email") or "").strip().lower() if kontakt else ""
    if oczekiwany_kontakt is not None and email != oczekiwany_kontakt:
        raise Konflikt("Kontakt zmienił się od podglądu. Przygotuj podgląd ponownie.")
    firma_z = zgloszenie.tenant_id
    powiazania = list(db.execute(select(ZgloszenieSprzet).where(
        ZgloszenieSprzet.zgloszenie_id == zgloszenie.id)).scalars())
    szczegoly = {
        "firma_z_nazwa": db.get(Tenant, firma_z).name, "firma_do_nazwa": cel.name,
        "kontakt_z": {"email": zgloszenie.zglaszajacy_email, "nazwa": zgloszenie.zglaszajacy_nazwa},
        "kontakt_do": kontakt_id or None, "technik_z": zgloszenie.technik_id,
        "technik_do": technik_id or None,
        "sprzet_odpiety": [p.asset_id for p in powiazania],
        "numer_pelny": zgloszenie.numer_pelny, "numer_wewnetrzny_z": zgloszenie.numer,
    }
    numer, _ = helpdesk.nadaj_numer(db, tenant_id)
    szczegoly["numer_wewnetrzny_do"] = numer
    db.execute(delete(ZgloszenieSprzet).where(ZgloszenieSprzet.zgloszenie_id == zgloszenie.id))
    czasy = db.execute(update(CzasPracy).where(CzasPracy.zgloszenie_id == zgloszenie.id)
                      .values(tenant_id=tenant_id))
    szczegoly["wpisy_czasu"] = czasy.rowcount
    teraz = utcnow()
    zgloszenie.tenant_id = tenant_id
    zgloszenie.numer = numer
    zgloszenie.zglaszajacy_email = ((kontakt.atrybuty or {}).get("email") or "").strip().lower() if kontakt else ""
    zgloszenie.zglaszajacy_nazwa = kontakt.wartosc if kontakt else None
    zgloszenie.technik_id = technik.id if technik else None
    zgloszenie.ostatnia_aktywnosc = teraz
    db.add(PrzeniesienieZgloszenia(
        zgloszenie_id=zgloszenie.id, firma_z=firma_z, firma_do=tenant_id,
        autor=user.email, powod=powod.strip(), szczegoly=szczegoly, utworzono=teraz))
    db.execute(update(WpisZgloszenia).where(
        WpisZgloszenia.zgloszenie_id == zgloszenie.id,
        WpisZgloszenia.rodzaj == WPIS_DO_KLIENTA, WpisZgloszenia.wyslano_o.is_(None))
        .values(blad_wysylki="Wysyłka wstrzymana po zmianie firmy. Utwórz nową odpowiedź do nowego kontaktu."))
    helpdesk.zdarzenie(db, zgloszenie,
        f"przeniesiono zgłoszenie: {szczegoly['firma_z_nazwa']} → {cel.name}; "
        f"powód: {powod.strip()}; wykonawca: {helpdesk.opis_osoby(user)}",
        autor=helpdesk.opis_osoby(user), autor_id=user.id)
    audit(db, None, action="helpdesk.zgloszenie.przeniesienie", target=zgloszenie.id,
          detail={**szczegoly, "firma_z": firma_z, "firma_do": tenant_id, "powod": powod.strip()},
          ip=ip, actor=user.email)
    db.flush()
    return zgloszenie


def dozwolony_nadawca(db: Session, zgloszenie: Zgloszenie, adres: str) -> bool:
    """Po transferze stare Message-ID i numer nie uprawniaja starej firmy."""
    adres = adres.strip().lower()
    firma = helpdesk.firma_dla_adresu(db, adres)
    if firma is not None:
        return firma.id == zgloszenie.tenant_id and firma.is_active
    return bool(adres and adres == zgloszenie.zglaszajacy_email.lower())


def ostatnia_do_odpowiedzi(db: Session, zgloszenie: Zgloszenie) -> WpisZgloszenia | None:
    transfer = ostatnie(db, zgloszenie.id)
    if transfer is None:
        return helpdesk.ostatnia_od_klienta(db, zgloszenie.id)
    return db.execute(select(WpisZgloszenia).where(
        WpisZgloszenia.zgloszenie_id == zgloszenie.id,
        WpisZgloszenia.rodzaj == WPIS_OD_KLIENTA,
        WpisZgloszenia.utworzono > transfer.utworzono)
        .order_by(WpisZgloszenia.utworzono.desc()).limit(1)).scalar_one_or_none()
