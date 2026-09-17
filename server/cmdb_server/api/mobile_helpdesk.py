"""Helpdesk w aplikacji Android.

Ta czesc /api/v1/mobile rozni sie od reszty jedna rzecza: nie pracuje w JEDNEJ
firmie. Technik obsluguje kilka i ma je widziec na jednej liscie - dokladnie
tak samo jak w panelu WWW. Zakres danych wyznacza wiec lista firm konta
(``helpdesk.firmy_technika``), a naglowek ``X-CMDB-Tenant``, ktory rzadzi
pozostalymi trasami mobilnymi, jest tu bez znaczenia. Aplikacja moze miec
przelaczona firme A i odpowiadac na zgloszenie firmy B, bo tak wyglada praca
technika.

Konsekwencja jest ta sama co w panelu: KAZDA droga do zgloszenia przechodzi
przez ``_zgloszenie``, ktore sprawdza przynaleznosc do tych firm. Nie ma tu
sciezki czytajacej zgloszenie po samym identyfikatorze.

Nazwy pol w JSON sa angielskie jak w reszcie API mobilnego; nazwy w kodzie
zostaja polskie jak w reszcie serwera.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    STATUS_NOWE,
    STATUS_OCZEKUJE,
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    STATUSY_OTWARTE,
    STATUSY_ZGLOSZENIA,
    TYPY_ZGLOSZENIA,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    ZRODLA_RECZNE,
    Asset,
    CzasPracy,
    HelpdeskDostep,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    ZalacznikWpisu,
    Zgloszenie,
    ZgloszenieSprzet,
)
from ..services import helpdesk, helpdesk_wysylka, slowniki
from ..services import helpdesk_poczta as poczta
from ..services.auth import client_ip, tenant_context_for
from ..services.scoping import audit
from .mobile import mobile_user

router = APIRouter(prefix="/api/v1/mobile/helpdesk", tags=["mobile", "helpdesk"])

# Ile zgloszen oddajemy na jedno zapytanie listy. Telefon dociaga kolejne
# strony, wiec wieksza porcja nie przyspiesza pierwszego ekranu, a kosztuje
# transfer na komorce.
STRONA = 30

# Ile plikow wolno doczepic do jednej wiadomosci. Granica jest po to, zeby
# pomylkowe zaznaczenie calej galerii nie zapchalo dysku serwera.
MAX_PLIKOW = 10


# --- zakres i wyszukiwanie zgloszen -----------------------------------------

def _firmy(db: Session, user: PortalUser) -> list[str]:
    """Firmy, ktorych zgloszenia widzi to konto. Pusto znaczy: nie ten panel."""
    firmy = helpdesk.firmy_technika(db, user)
    if not firmy:
        raise HTTPException(403, "to konto nie obsluguje zgloszen - dostep do firm nadaje superadmin")
    return firmy


def _zgloszenie(db: Session, user: PortalUser, zgloszenie_id: str) -> Zgloszenie:
    """Zgloszenie z firm, ktore to konto obsluguje. Jedyna droga do zgloszenia."""
    zgloszenie = db.execute(
        select(Zgloszenie).where(
            Zgloszenie.id == zgloszenie_id,
            Zgloszenie.tenant_id.in_(_firmy(db, user)),
        )
    ).scalar_one_or_none()
    if zgloszenie is None:
        # Cudze zgloszenie i nieistniejace odpowiadaja tak samo: inaczej sama
        # odpowiedz mowilaby, ze taki numer istnieje.
        raise HTTPException(404, "nie znaleziono zgloszenia")
    return zgloszenie


def _iso(wartosc: datetime | None) -> str | None:
    return wartosc.isoformat() if wartosc else None


def _nazwy_firm(db: Session, firmy: list[str]) -> dict[str, str]:
    return dict(db.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(firmy))).all())


def _technicy(db: Session, firmy: list[str]) -> list[PortalUser]:
    """Technicy majacy dostep do ktorejkolwiek z tych firm - do list wyboru."""
    return list(db.execute(
        select(PortalUser)
        .join(HelpdeskDostep, HelpdeskDostep.user_id == PortalUser.id)
        .where(HelpdeskDostep.tenant_id.in_(firmy), PortalUser.is_active.is_(True))
        .distinct()
        .order_by(PortalUser.full_name, PortalUser.email)
    ).scalars())


def _wybor_technikow(
    db: Session, firmy: list[str], user: PortalUser, dodatkowy: str | None = None
) -> list[dict]:
    """Lista do wyboru odpowiedzialnego za zgloszenie.

    Poza technikami z nadanym dostepem dopisujemy dwa konta, ktorych tam nie
    ma, a ktore do zgloszenia nalezec moga: siebie (superadmin obsluguje
    wszystkie firmy, ale nie ma wpisu dostepu) i obecnego wlasciciela sprawy.
    Bez tego aplikacja pokazywalaby "nieprzypisane" przy przypisanym
    zgloszeniu i nie dalaby wziac sprawy na siebie. Samo wypisanie konta
    niczego nie otwiera - przypisanie sprawdza dostep osobno.
    """
    technicy = _technicy(db, firmy)
    znane = {technik.id for technik in technicy}
    for dodatek in (user.id, dodatkowy):
        if not dodatek or dodatek in znane:
            continue
        konto = db.get(PortalUser, dodatek)
        if konto is None:
            continue
        technicy.append(konto)
        znane.add(konto.id)
    return [
        {"id": technik.id, "name": helpdesk.opis_osoby(technik), "email": technik.email}
        for technik in technicy
    ]


def _osoby(db: Session, identyfikatory: set[str] | None = None) -> dict[str, str]:
    """Opisy kont panelu - autorzy wpisow i przypisani technicy.

    Pytamy o same kolumny i tylko o wskazane konta: lista zgloszen potrzebuje
    kilku nazwisk, a nie calej tabeli uzytkownikow instalacji.
    """
    czyste = {klucz for klucz in (identyfikatory or set()) if klucz}
    if identyfikatory is not None and not czyste:
        return {}
    zapytanie = select(PortalUser.id, PortalUser.full_name, PortalUser.email)
    if identyfikatory is not None:
        zapytanie = zapytanie.where(PortalUser.id.in_(czyste))
    return {
        klucz: (nazwa or email)
        for klucz, nazwa, email in db.execute(zapytanie).all()
    }


# --- postac JSON ------------------------------------------------------------

def _sprzet_zgloszen(db: Session, identyfikatory: list[str]) -> dict[str, list[dict]]:
    """Sprzet wszystkich wypisanych zgloszen jednym zapytaniem.

    Lista pokazuje sprzet pod tematem, wiec bez tego kazdy wiersz pytalby bazy
    osobno - przy trzydziestu zgloszeniach widac to na telefonie.
    """
    if not identyfikatory:
        return {}
    wynik: dict[str, list[dict]] = {}
    wiersze = db.execute(
        select(ZgloszenieSprzet.zgloszenie_id, Asset)
        .join(Asset, Asset.id == ZgloszenieSprzet.asset_id)
        .where(ZgloszenieSprzet.zgloszenie_id.in_(identyfikatory))
        .order_by(ZgloszenieSprzet.utworzono)
    ).all()
    for zgloszenie_id, asset in wiersze:
        wynik.setdefault(zgloszenie_id, []).append(
            {"id": asset.id, "hostname": asset.hostname, "type": asset.typ}
        )
    return wynik


def _minuty_zgloszen(db: Session, identyfikatory: list[str]) -> dict[str, int]:
    if not identyfikatory:
        return {}
    return {
        zgloszenie_id: int(minuty or 0)
        for zgloszenie_id, minuty in db.execute(
            select(CzasPracy.zgloszenie_id, func.sum(CzasPracy.minuty))
            .where(CzasPracy.zgloszenie_id.in_(identyfikatory))
            .group_by(CzasPracy.zgloszenie_id)
        ).all()
    }


def _czeka_od(db: Session, zgloszenie_id: str) -> str | None:
    """Kiedy przyszla wiadomosc, na ktora nie odpisalismy."""
    wpis = helpdesk.ostatnia_od_klienta(db, zgloszenie_id)
    return _iso(wpis.utworzono) if wpis else None


def _zgloszenie_json(
    zgloszenie: Zgloszenie, *, firmy: dict[str, str], osoby: dict[str, str],
    sprzet: list[dict], minuty: int, czeka: bool, czeka_od: str | None = None,
) -> dict:
    return {
        "id": zgloszenie.id,
        "number": zgloszenie.numer_pelny,
        "subject": zgloszenie.temat,
        "status": zgloszenie.status,
        "status_label": STATUSY_ZGLOSZENIA.get(zgloszenie.status, zgloszenie.status),
        "type": zgloszenie.typ,
        "type_label": TYPY_ZGLOSZENIA.get(zgloszenie.typ or "", ""),
        "tenant_id": zgloszenie.tenant_id,
        "tenant": firmy.get(zgloszenie.tenant_id, ""),
        "requester_email": zgloszenie.zglaszajacy_email,
        "requester_name": zgloszenie.zglaszajacy_nazwa,
        "technician_id": zgloszenie.technik_id,
        "technician": osoby.get(zgloszenie.technik_id or "", None),
        "created_at": _iso(zgloszenie.utworzono),
        "last_activity": _iso(zgloszenie.ostatnia_aktywnosc),
        "closed_at": _iso(zgloszenie.zamkniete_o),
        # "Czeka" znaczy: ostatnie slowo nalezy do klienta. To jest jedyna
        # pilnosc, jaka ten system zna - priorytetu ani terminu SLA nie ma
        # w modelu danych i nie udajemy, ze sa.
        "waiting": czeka,
        "waiting_since": czeka_od,
        "assets": sprzet,
        "minutes": minuty,
    }


def _wpis_json(
    wpis: WpisZgloszenia, *, osoby: dict[str, str], zalaczniki: list[ZalacznikWpisu],
    user: PortalUser,
) -> dict:
    return {
        "id": wpis.id,
        "kind": wpis.rodzaj,
        "author": (
            osoby.get(wpis.autor_id or "")
            or wpis.autor_nazwa
            or wpis.autor_email
            or "System"
        ),
        "author_email": wpis.autor_email,
        # Po tej fladze telefon ustawia dymek po prawej stronie. Liczy sie
        # autor, a nie rodzaj wpisu: odpowiedz kolegi ma byc po lewej.
        "mine": wpis.autor_id == user.id,
        "content": wpis.tresc,
        "created_at": _iso(wpis.utworzono),
        "sent_at": _iso(wpis.wyslano_o),
        "error": wpis.blad_wysylki,
        "attachments": [
            {
                "id": plik.id,
                "name": plik.nazwa,
                "mime": plik.typ_mime,
                "size": plik.rozmiar,
                "image": (plik.typ_mime or "").split(";")[0].strip().lower() in poczta.TYPY_PODGLADU,
            }
            for plik in zalaczniki
        ],
    }


# --- katalog ----------------------------------------------------------------

@router.get("/catalog")
def katalog(user: PortalUser = Depends(mobile_user), db: Session = Depends(get_db)) -> dict:
    """Slowniki ekranow helpdesku i informacja, czy konto w ogole je widzi.

    Odpowiada 200 takze kontu bez dostepu - z ``available: false``. Aplikacja
    ma na tej podstawie ukryc zakladke, a nie pokazac blad: brak dostepu do
    helpdesku jest zwyklym stanem konta CMDB, a nie awaria.
    """
    firmy = helpdesk.firmy_technika(db, user)
    if not firmy:
        return {"available": False, "tenants": [], "technicians": [], "waiting": 0,
                "statuses": [], "types": [], "sources": [], "mailbox": False,
                "closing_email": False, "attachment_mb": 0}

    nazwy = _nazwy_firm(db, firmy)
    skrzynka = helpdesk_wysylka.ustawienia(db)
    return {
        "available": True,
        "tenants": [{"id": klucz, "name": nazwa} for klucz, nazwa in sorted(nazwy.items(), key=lambda para: para[1])],
        "technicians": _wybor_technikow(db, firmy, user),
        "statuses": [{"key": klucz, "label": etykieta} for klucz, etykieta in STATUSY_ZGLOSZENIA.items()],
        "types": [{"key": klucz, "label": etykieta} for klucz, etykieta in TYPY_ZGLOSZENIA.items()],
        "sources": [{"key": klucz, "label": etykieta} for klucz, etykieta in ZRODLA_RECZNE.items()],
        "mailbox": skrzynka is not None and bool(skrzynka.smtp_host and skrzynka.nadawca),
        "closing_email": helpdesk_wysylka.zamkniecie_nalezne(db),
        "attachment_mb": get_settings().helpdesk_zalacznik_mb,
        "waiting": helpdesk.ile_czeka(db, firmy),
    }


# --- lista zgloszen ---------------------------------------------------------

@router.get("/tickets")
def lista(
    q: str = Query("", max_length=200),
    scope: str = Query("open", max_length=16),
    status: str = Query("", max_length=20),
    tenant: str = Query("", max_length=36),
    page: int = Query(1, ge=1),
    page_size: int = Query(STRONA, ge=1, le=100),
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Zgloszenia wszystkich obslugiwanych firm z licznikami do naglowka.

    Liczniki sa liczone dla CALEGO zakresu, a nie dla wyswietlonej strony -
    kafelek "Otwarte 12" ma znaczyc dwanascie otwartych spraw, a nie dwanascie
    spraw, ktore akurat przeszly przez filtr.
    """
    firmy = _firmy(db, user)
    czekaja = helpdesk.czekaja_na_odpowiedz(db, firmy)

    zapytanie = select(Zgloszenie).where(Zgloszenie.tenant_id.in_(firmy))
    if tenant:
        if tenant not in firmy:
            raise HTTPException(403, "to konto nie obsluguje tej firmy")
        zapytanie = zapytanie.where(Zgloszenie.tenant_id == tenant)
    if q.strip():
        wzorzec = f"%{q.strip().lower()}%"
        zapytanie = zapytanie.where(or_(
            func.lower(Zgloszenie.numer_pelny).like(wzorzec),
            func.lower(Zgloszenie.temat).like(wzorzec),
            func.lower(Zgloszenie.zglaszajacy_email).like(wzorzec),
        ))
    if status:
        if status not in STATUSY_ZGLOSZENIA:
            raise HTTPException(400, "nieznany status zgloszenia")
        zapytanie = zapytanie.where(Zgloszenie.status == status)

    if scope == "mine":
        zapytanie = zapytanie.where(
            Zgloszenie.technik_id == user.id, Zgloszenie.status != STATUS_ZAMKNIETE
        )
    elif scope == "unassigned":
        zapytanie = zapytanie.where(
            Zgloszenie.technik_id.is_(None), Zgloszenie.status != STATUS_ZAMKNIETE
        )
    elif scope == "waiting":
        # Pusty zbior musi dac pusta liste, a nie cala tablice - stad podstawa
        # "-", ktora nie jest niczyim identyfikatorem.
        zapytanie = zapytanie.where(Zgloszenie.id.in_(list(czekaja) or ["-"]))
    elif scope == "closed":
        zapytanie = zapytanie.where(Zgloszenie.status == STATUS_ZAMKNIETE)
    elif scope == "open":
        zapytanie = zapytanie.where(Zgloszenie.status.in_(STATUSY_OTWARTE))
    elif scope != "all":
        raise HTTPException(400, "nieznany zakres listy")

    razem = db.scalar(select(func.count()).select_from(zapytanie.subquery())) or 0
    zgloszenia = list(db.execute(
        zapytanie.order_by(Zgloszenie.ostatnia_aktywnosc.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars())

    identyfikatory = [zgloszenie.id for zgloszenie in zgloszenia]
    sprzet = _sprzet_zgloszen(db, identyfikatory)
    minuty = _minuty_zgloszen(db, identyfikatory)
    nazwy = _nazwy_firm(db, firmy)
    osoby = _osoby(db, {zgloszenie.technik_id for zgloszenie in zgloszenia})

    def policz(*warunki) -> int:
        return db.scalar(
            select(func.count(Zgloszenie.id)).where(Zgloszenie.tenant_id.in_(firmy), *warunki)
        ) or 0

    return {
        "items": [
            _zgloszenie_json(
                zgloszenie, firmy=nazwy, osoby=osoby,
                sprzet=sprzet.get(zgloszenie.id, []), minuty=minuty.get(zgloszenie.id, 0),
                czeka=zgloszenie.id in czekaja,
            )
            for zgloszenie in zgloszenia
        ],
        "page": page,
        "page_size": page_size,
        "total": razem,
        "counters": {
            "open": policz(Zgloszenie.status.in_(STATUSY_OTWARTE)),
            "mine": policz(Zgloszenie.technik_id == user.id, Zgloszenie.status != STATUS_ZAMKNIETE),
            "unassigned": policz(Zgloszenie.technik_id.is_(None), Zgloszenie.status != STATUS_ZAMKNIETE),
            "closed": policz(Zgloszenie.status == STATUS_ZAMKNIETE),
            "waiting": len(czekaja),
        },
    }


# --- karta zgloszenia -------------------------------------------------------

@router.get("/tickets/{zgloszenie_id}")
def karta(
    zgloszenie_id: str,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Zgloszenie z calym watkiem, sprzetem, kartoteka zglaszajacego i czasem."""
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    firmy = _firmy(db, user)

    wpisy = list(db.execute(
        select(WpisZgloszenia)
        .where(WpisZgloszenia.zgloszenie_id == zgloszenie.id)
        .order_by(WpisZgloszenia.utworzono)
    ).scalars())
    zalaczniki: dict[str, list[ZalacznikWpisu]] = {}
    for plik in db.execute(
        select(ZalacznikWpisu)
        .where(ZalacznikWpisu.zgloszenie_id == zgloszenie.id)
        .order_by(ZalacznikWpisu.utworzono)
    ).scalars():
        zalaczniki.setdefault(plik.wpis_id, []).append(plik)

    osoby = _osoby(
        db, {wpis.autor_id for wpis in wpisy} | {zgloszenie.technik_id}
    )
    suma, udzialy = helpdesk.czas_zgloszenia(db, zgloszenie.id)
    czekaja = helpdesk.czekaja_na_odpowiedz(db, [zgloszenie.tenant_id])

    # Kim jest zglaszajacy: kartoteka firmy zna jego telefon i lokalizacje.
    # Kontekst budujemy dla firmy ZGLOSZENIA, bo aplikacja moze miec
    # przelaczona inna - w helpdesku to normalne.
    firma = db.get(Tenant, zgloszenie.tenant_id)
    osoba = helpdesk.osoba_o_adresie(db, zgloszenie.tenant_id, zgloszenie.zglaszajacy_email)
    pola_osoby = slowniki.szczegoly(db, tenant_context_for(user, firma, helpdesk=True), osoba)

    return {
        "ticket": _zgloszenie_json(
            zgloszenie, firmy=_nazwy_firm(db, firmy), osoby=osoby,
            sprzet=_sprzet_zgloszen(db, [zgloszenie.id]).get(zgloszenie.id, []),
            minuty=suma, czeka=zgloszenie.id in czekaja,
            czeka_od=_czeka_od(db, zgloszenie.id) if zgloszenie.id in czekaja else None,
        ),
        "entries": [
            _wpis_json(wpis, osoby=osoby, zalaczniki=zalaczniki.get(wpis.id, []), user=user)
            for wpis in wpisy
        ],
        "requester": {
            "email": zgloszenie.zglaszajacy_email,
            "name": zgloszenie.zglaszajacy_nazwa,
            "known": osoba is not None,
            "fields": [{"label": pole["etykieta"], "value": pole["wartosc"]} for pole in pola_osoby],
        },
        "candidates": [
            {"id": asset.id, "hostname": asset.hostname, "type": asset.typ}
            for asset in helpdesk.sprzet_zglaszajacego(
                db, zgloszenie.tenant_id, zgloszenie.zglaszajacy_email
            )
        ],
        "technicians": _wybor_technikow(
            db, [zgloszenie.tenant_id], user, zgloszenie.technik_id
        ),
        "time": {
            "total": suma,
            "total_label": helpdesk.formatuj_czas(suma),
            "shares": [
                {"technician": udzial.nazwa, "minutes": udzial.minuty,
                 "label": helpdesk.formatuj_czas(udzial.minuty)}
                for udzial in udzialy
            ],
        },
        "closing_email": helpdesk_wysylka.zamkniecie_nalezne(db),
        "mailbox": helpdesk_wysylka.ustawienia(db) is not None,
    }


# --- sprzet do wyboru -------------------------------------------------------

@router.get("/assets")
def sprzet_firmy(
    tenant: str = Query("", max_length=36),
    q: str = Query("", max_length=200),
    limit: int = Query(30, ge=1, le=100),
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Sprzet firmy do podpiecia pod zgloszenie - z wyszukiwaniem po nazwie.

    Telefon nie sciaga calej kartoteki: technik wpisuje kawalek nazwy albo
    adresu i wybiera z kilkunastu wynikow.
    """
    firmy = _firmy(db, user)
    if tenant and tenant not in firmy:
        raise HTTPException(403, "to konto nie obsluguje tej firmy")

    zapytanie = select(Asset).where(Asset.tenant_id.in_([tenant] if tenant else firmy))
    if q.strip():
        wzorzec = f"%{q.strip()}%"
        zapytanie = zapytanie.where(or_(
            Asset.hostname.ilike(wzorzec), Asset.fqdn.ilike(wzorzec),
            Asset.primary_ip.ilike(wzorzec), Asset.serial_number.ilike(wzorzec),
        ))
    return [
        {"id": asset.id, "hostname": asset.hostname, "type": asset.typ,
         "primary_ip": asset.primary_ip, "tenant_id": asset.tenant_id}
        for asset in db.execute(zapytanie.order_by(Asset.hostname).limit(limit)).scalars()
    ]


# --- zakladanie zgloszenia --------------------------------------------------

class NoweZgloszenie(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=36)
    requester_email: str = Field(min_length=3, max_length=320)
    requester_name: str = Field(default="", max_length=200)
    subject: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=20000)
    type: str = Field(default="", max_length=20)
    source: str = Field(default="telefon", max_length=20)
    asset_id: str = Field(default="", max_length=36)
    assign_to_me: bool = True
    notify_customer: bool = True


class NowaWiadomosc(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    kind: str = Field(default=WPIS_WEWNETRZNY, max_length=20)
    # Odpowiedz przestawia zgloszenie na "Oczekuje", bo pilka jest po stronie
    # klienta. Wiadomosc czysto informacyjna moze to wylaczyc.
    keep_in_progress: bool = False


class ZmianaStatusu(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    summary: str = Field(default="", max_length=20000)


class ZmianaTechnika(BaseModel):
    technician_id: str = Field(default="", max_length=36)


class DopisanyCzas(BaseModel):
    minutes: int
    description: str = Field(default="", max_length=500)


class ZmianaSprzetu(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    action: str = Field(default="attach", max_length=16)


def _z_formularza(model: type[BaseModel], dane: str):
    """Czesc ``dane`` z multipartu jako model pydantic.

    Wiadomosc ma pola i pliki, wiec idzie multipartem; opisywanie kazdego pola
    osobnym kawalkiem formularza rozjechaloby sie z modelem przy pierwszej
    zmianie, dlatego pola jada jednym kawalkiem JSON.
    """
    try:
        return model.model_validate_json(dane)
    except ValidationError as blad:
        raise HTTPException(400, detail=blad.errors(include_url=False)[:5]) from blad


def _zalaczniki_z_plikow(pliki: list[UploadFile]) -> tuple[poczta.Zalacznik, ...]:
    """Pliki z telefonu w tej samej postaci, w ktorej przychodza z poczty.

    Dzieki temu zapisuje je ten sam kod (``poczta.zapisz_zalaczniki``), ktory
    zapisuje zalaczniki maili - z tym samym nadawaniem nazw na dysku. Nazwa
    z telefonu jest wylacznie opisem, tak samo jak nazwa z maila.
    """
    prawdziwe = [plik for plik in pliki if plik is not None and plik.filename]
    if len(prawdziwe) > MAX_PLIKOW:
        raise HTTPException(400, f"na raz mozna dolaczyc najwyzej {MAX_PLIKOW} plikow")

    limit = get_settings().helpdesk_zalacznik_mb * 1024 * 1024
    wynik: list[poczta.Zalacznik] = []
    for plik in prawdziwe:
        dane = plik.file.read()
        if not dane:
            continue
        if limit and len(dane) > limit:
            raise HTTPException(
                413,
                f"plik {plik.filename} ma wiecej niz {get_settings().helpdesk_zalacznik_mb} MB",
            )
        wynik.append(poczta.Zalacznik(
            nazwa=plik.filename or "zalacznik",
            typ_mime=(plik.content_type or "application/octet-stream"),
            dane=dane,
        ))
    return tuple(wynik)


@router.post("/tickets", status_code=201)
def zaloz(
    request: Request,
    dane: str = Form(...),
    pliki: list[UploadFile] = File(default=[]),
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Zgloszenie z telefonu: technik zapisuje je w imieniu klienta.

    Te same sprawdzenia co w panelu - firme wybiera tu czlowiek, a nie domena
    nadawcy, wiec tylko tutaj trzeba pilnowac, zeby adres nie nalezal do innej
    firmy niz wybrana.
    """
    body = _z_formularza(NoweZgloszenie, dane)
    firmy = _firmy(db, user)
    if body.tenant_id not in firmy:
        raise HTTPException(403, "to konto nie obsluguje tej firmy")

    adres = body.requester_email.strip().lower()
    if "@" not in adres or "." not in helpdesk.domena_adresu(adres):
        raise HTTPException(400, "adres zglaszajacego jest potrzebny - bez niego nie ma jak odpisac")
    obca = helpdesk.obca_firma_adresu(db, adres, body.tenant_id)
    if obca is not None:
        raise HTTPException(
            400,
            f"domena adresu {adres} nalezy do firmy {obca.name} - wybierz te firme albo popraw adres",
        )

    asset = None
    if body.asset_id:
        asset = db.get(Asset, body.asset_id)
        if asset is None or asset.tenant_id != body.tenant_id:
            raise HTTPException(400, "sprzet nie nalezy do firmy zgloszenia")

    # Pliki czytamy przed zapisem: odrzucony zalacznik nie ma zostawiac
    # zgloszenia zalozonego w polowie.
    zalaczniki = _zalaczniki_z_plikow(pliki)

    autor = helpdesk.opis_osoby(user)
    zgloszenie = helpdesk.utworz_zgloszenie(
        db, tenant_id=body.tenant_id, temat=body.subject.strip(), tresc=body.content.strip(),
        zglaszajacy_email=adres, zglaszajacy_nazwa=body.requester_name.strip() or None,
        typ=body.type if body.type in TYPY_ZGLOSZENIA else None,
        technik_id=user.id if body.assign_to_me else None,
        zrodlo=ZRODLA_RECZNE.get(body.source, ZRODLA_RECZNE["inne"]).lower(),
        autor=autor,
    )
    if zalaczniki:
        pierwszy = helpdesk.pierwszy_wpis(db, zgloszenie.id)
        if pierwszy is not None:
            poczta.zapisz_zalaczniki(db, pierwszy, zalaczniki)
    if asset is not None:
        helpdesk.podepnij_sprzet(db, zgloszenie, asset, dodal=autor)
    if body.assign_to_me:
        # Technik wlasnie rozmawia z klientem, wiec sprawa jest w toku, a nie
        # czeka w pierwszej kolumnie na kogos, kto ja przeczyta.
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor=autor)

    komunikat = f"Zgłoszenie {zgloszenie.numer_pelny} założone."
    if body.notify_customer:
        wpis = helpdesk_wysylka.wyslij_potwierdzenie(db, zgloszenie)
        if wpis is None:
            komunikat += " Skrzynka jest wyłączona, więc klient nie dostał numeru."
        elif wpis.blad_wysylki:
            komunikat += f" Potwierdzenie NIE wyszło: {wpis.blad_wysylki}"
        else:
            komunikat += f" Klient dostał numer na {adres}."

    audit(db, None, action="helpdesk.zgloszenie.mobilne", target=zgloszenie.numer_pelny,
          detail={"firma": body.tenant_id, "zrodlo": body.source, "plikow": len(zalaczniki)},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return {"id": zgloszenie.id, "number": zgloszenie.numer_pelny, "detail": komunikat}


# --- watek ------------------------------------------------------------------

@router.post("/tickets/{zgloszenie_id}/messages", status_code=201)
def dopisz(
    zgloszenie_id: str,
    request: Request,
    dane: str = Form(...),
    pliki: list[UploadFile] = File(default=[]),
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Odpowiedz do klienta albo notatka wewnetrzna - jedno pole, dwa skutki.

    Rodzaj wpisu decyduje, czy tresc wyjdzie na zewnatrz, wiec nieznana wartosc
    jest bledem. Zalaczniki odpowiedzi ida mailem razem z nia; zalaczniki
    notatki zostaja w systemie, bo notatka z definicji nie opuszcza zgloszenia.
    """
    body = _z_formularza(NowaWiadomosc, dane)
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    zalaczniki = _zalaczniki_z_plikow(pliki)
    autor = helpdesk.opis_osoby(user)
    tresc = body.content.strip()

    if body.kind == WPIS_DO_KLIENTA:
        wpis = helpdesk.dopisz_wiadomosc(
            db, zgloszenie, rodzaj=WPIS_DO_KLIENTA, tresc=tresc, autor=user
        )
        poczta.zapisz_zalaczniki(db, wpis, zalaczniki)
        try:
            helpdesk_wysylka.wyslij_wpis(db, zgloszenie, wpis)
        except helpdesk_wysylka.BladWysylki as blad:
            # Tresc zostaje w watku - zapis byl przed wysylka. Statusu nie
            # ruszamy: nie czekamy na klienta, ktory niczego nie dostal.
            db.commit()
            return {"detail": f"Wiadomość zapisana, ale NIE wyszła: {blad}", "sent": False}
        komunikat = f"Odpowiedź poszła do {zgloszenie.zglaszajacy_email}."
        if not body.keep_in_progress:
            helpdesk.zmien_status(db, zgloszenie, STATUS_OCZEKUJE, autor=autor)
            komunikat += " Zgłoszenie czeka na klienta."
            db.commit()
            return {"detail": komunikat, "sent": True}
    elif body.kind == WPIS_WEWNETRZNY:
        wpis = helpdesk.dopisz_wiadomosc(
            db, zgloszenie, rodzaj=WPIS_WEWNETRZNY, tresc=tresc, autor=user
        )
        poczta.zapisz_zalaczniki(db, wpis, zalaczniki)
        komunikat = "Notatka widoczna tylko dla techników."
    else:
        raise HTTPException(400, "nieznany rodzaj wpisu")

    if zgloszenie.status == STATUS_NOWE:
        # Ktos odpisal, wiec sprawa nie jest juz "nowa".
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor=autor)
    db.commit()
    return {"detail": komunikat, "sent": body.kind == WPIS_DO_KLIENTA}


@router.post("/tickets/{zgloszenie_id}/status")
def status(
    zgloszenie_id: str,
    body: ZmianaStatusu,
    request: Request,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Zmiana statusu, a przy zamknieciu - wiadomosc do klienta.

    Mail idzie tylko przy przejsciu do zamknietego: powtorne wybranie tego
    samego statusu niczego nie wysyla.
    """
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    zamykamy = body.status == STATUS_ZAMKNIETE and zgloszenie.status != STATUS_ZAMKNIETE
    try:
        helpdesk.zmien_status(db, zgloszenie, body.status, autor=helpdesk.opis_osoby(user))
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(400, str(blad)) from blad

    komunikat = f"Status: {STATUSY_ZGLOSZENIA[body.status]}."
    if zamykamy and helpdesk_wysylka.zamkniecie_nalezne(db):
        wpis = helpdesk_wysylka.wyslij_zamkniecie(db, zgloszenie, body.summary)
        if wpis is not None and wpis.blad_wysylki:
            komunikat = f"Zgłoszenie zamknięte, ale mail NIE wyszedł: {wpis.blad_wysylki}"
        else:
            komunikat = f"Zgłoszenie zamknięte, klient dostał wiadomość na {zgloszenie.zglaszajacy_email}."

    audit(db, None, action="helpdesk.status.mobilny", target=zgloszenie.numer_pelny,
          detail={"status": body.status}, ip=client_ip(request), actor=user.email)
    db.commit()
    return {"detail": komunikat}


@router.post("/tickets/{zgloszenie_id}/technician")
def technik(
    zgloszenie_id: str,
    body: ZmianaTechnika,
    request: Request,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Przypisanie zgloszenia. Pusty identyfikator zdejmuje przypisanie."""
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    wybrany = db.get(PortalUser, body.technician_id) if body.technician_id else None
    if body.technician_id and wybrany is None:
        raise HTTPException(400, "nie znaleziono technika")
    try:
        helpdesk.przypisz(db, zgloszenie, wybrany, autor=helpdesk.opis_osoby(user))
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(400, str(blad)) from blad

    audit(db, None, action="helpdesk.technik.mobilny", target=zgloszenie.numer_pelny,
          detail={"technik": body.technician_id or None}, ip=client_ip(request), actor=user.email)
    db.commit()
    return {"detail": "Zgłoszenie przypisane." if wybrany else "Zgłoszenie bez technika."}


@router.post("/tickets/{zgloszenie_id}/time")
def czas(
    zgloszenie_id: str,
    body: DopisanyCzas,
    request: Request,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Dopisuje czas pracy. Zawsze SWOJ - czasu kolegi nikt za niego nie wpisze."""
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    try:
        helpdesk.dodaj_czas(
            db, zgloszenie, user, body.minutes, body.description.strip() or None
        )
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(400, str(blad)) from blad

    audit(db, None, action="helpdesk.czas.mobilny", target=zgloszenie.numer_pelny,
          detail={"minuty": body.minutes}, ip=client_ip(request), actor=user.email)
    db.commit()
    czynnosc = "Dopisano" if body.minutes > 0 else "Odjęto"
    return {"detail": f"{czynnosc} {helpdesk.formatuj_czas(abs(body.minutes))}. "
                      "Wpisu nie da się już zmienić."}


@router.post("/tickets/{zgloszenie_id}/assets")
def sprzet(
    zgloszenie_id: str,
    body: ZmianaSprzetu,
    request: Request,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
) -> dict:
    """Podpiecie i odpiecie sprzetu, ktorego dotyczy zgloszenie."""
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    autor = helpdesk.opis_osoby(user)
    if body.action == "detach":
        if not helpdesk.odepnij_sprzet(db, zgloszenie, body.asset_id, autor=autor):
            raise HTTPException(404, "ten sprzet nie jest podpiety")
        komunikat = "Sprzęt odpięty."
    else:
        asset = db.get(Asset, body.asset_id)
        if asset is None:
            raise HTTPException(400, "nie znaleziono sprzetu")
        try:
            helpdesk.podepnij_sprzet(db, zgloszenie, asset, dodal=autor)
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(400, str(blad)) from blad
        komunikat = f"Podpięty sprzęt: {asset.hostname}."

    audit(db, None, action="helpdesk.sprzet.mobilny", target=zgloszenie.numer_pelny,
          detail={"sprzet": body.asset_id, "akcja": body.action},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return {"detail": komunikat}


# --- zalaczniki -------------------------------------------------------------

def _plik_zalacznika(zalacznik: ZalacznikWpisu) -> Path:
    """Sciezka na dysku, pilnujac, zeby nie wyszla poza katalog zalacznikow."""
    sciezka = (poczta.katalog_zalacznikow() / zalacznik.sciezka).resolve()
    korzen = poczta.katalog_zalacznikow().resolve()
    if korzen not in sciezka.parents or not sciezka.is_file():
        raise HTTPException(404, "pliku nie ma juz na dysku")
    return sciezka


def _zalacznik(db: Session, user: PortalUser, zalacznik_id: str) -> ZalacznikWpisu:
    zalacznik = db.get(ZalacznikWpisu, zalacznik_id)
    if zalacznik is None:
        raise HTTPException(404, "nie znaleziono zalacznika")
    # Sprawdzamy firme zgloszenia, a nie sam identyfikator zalacznika.
    _zgloszenie(db, user, zalacznik.zgloszenie_id)
    return zalacznik


@router.get("/attachments/{zalacznik_id}")
def zalacznik(
    zalacznik_id: str,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
):
    """Plik z wiadomosci - zawsze jako dane do zapisania, nigdy do wykonania."""
    opis = _zalacznik(db, user, zalacznik_id)
    return FileResponse(
        _plik_zalacznika(opis), filename=opis.nazwa, media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/attachments/{zalacznik_id}/preview")
def podglad(
    zalacznik_id: str,
    user: PortalUser = Depends(mobile_user),
    db: Session = Depends(get_db),
):
    """Obrazek do pokazania wprost w watku.

    Podglad dostaja tylko formaty, ktore niczego nie wykonuja, i tylko wtedy,
    gdy poczatek pliku zgadza sie z deklarowanym typem - dokladnie tak samo jak
    w panelu WWW. Reszta zostaje przy pobieraniu.
    """
    opis = _zalacznik(db, user, zalacznik_id)
    sciezka = _plik_zalacznika(opis)
    with sciezka.open("rb") as plik:
        typ = poczta.do_podgladu(opis.typ_mime, plik.read(16))
    if typ is None:
        raise HTTPException(404, "ten plik nie ma podgladu")
    return FileResponse(
        sciezka, media_type=typ,
        headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"},
    )
