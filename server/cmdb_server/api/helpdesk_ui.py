"""Panel helpdesku: zgloszenia, karta zgloszenia, poczta i raporty czasu.

Ten panel rozni sie od reszty CMDB jedna rzecza: nie pracuje w JEDNEJ firmie.
Technik obsluguje kilka i ma widziec ich zgloszenia na jednej tablicy - inaczej
poranek zaczynalby sie od przeklikania czterech firm po kolei. Dlatego zakres
danych wyznacza tu lista firm konta (``helpdesk.firmy_technika``), a nie
kontekst pojedynczej firmy; kontekst sluzy tylko naglowkowi strony.

Konsekwencja jest taka, ze KAZDE zapytanie o zgloszenie przechodzi przez
``_zgloszenie``, ktore sprawdza przynaleznosc do tych firm. Nie ma tu sciezki
czytajacej zgloszenie po samym identyfikatorze.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    NIEROZPOZNANA_CZEKA,
    NIEROZPOZNANA_PRZYPISANA,
    NIEROZPOZNANA_ZIGNOROWANA,
    STATUS_NOWE,
    STATUS_OCZEKUJE,
    STATUS_W_TRAKCIE,
    STATUS_ZAMKNIETE,
    STATUSY_OTWARTE,
    STATUSY_ZGLOSZENIA,
    TYPY_ZGLOSZENIA,
    WPIS_DO_KLIENTA,
    WPIS_WEWNETRZNY,
    Asset,
    CzasPracy,
    HelpdeskDostep,
    HelpdeskFirma,
    HelpdeskUstawienia,
    IgnorowanaDomena,
    NierozpoznanaWiadomosc,
    PortalUser,
    Tenant,
    WpisZgloszenia,
    ZalacznikWpisu,
    Zgloszenie,
    utcnow,
)
from ..services import helpdesk, helpdesk_imap, helpdesk_raporty, helpdesk_wysylka, sekrety
from ..services import helpdesk_poczta as poczta
from ..services.auth import client_ip, require_user, verify_csrf
from ..services.scoping import TenantContext, audit
from .ui import render, resolve_tenant

log = logging.getLogger(__name__)
router = APIRouter(tags=["helpdesk"])

# Ile zgloszen pokazujemy w kolumnie tablicy. Wiecej i tak nikt nie przeczyta,
# a zamkniete ciagna sie miesiacami.
NA_KOLUMNE = 25


# --- zakres i uprawnienia ---------------------------------------------------

def _firmy(db: Session, user: PortalUser) -> list[str]:
    """Firmy, ktorych zgloszenia widzi to konto. Pusto znaczy: nie ten panel."""
    firmy = helpdesk.firmy_technika(db, user)
    if not firmy:
        raise HTTPException(
            status_code=403,
            detail="to konto nie obsluguje zgloszen - dostep do firm nadaje superadmin",
        )
    return firmy


def _superadmin(user: PortalUser) -> None:
    if not helpdesk.prowadzi_helpdesk(user):
        raise HTTPException(
            status_code=403, detail="ta czesc helpdesku nalezy do superadmina"
        )


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
        raise HTTPException(status_code=404, detail="nie znaleziono zgloszenia")
    return zgloszenie


def _wroc(sciezka: str, komunikat: str = "") -> RedirectResponse:
    adres = f"{sciezka}?komunikat={quote(komunikat)}" if komunikat else sciezka
    return RedirectResponse(adres, status_code=status.HTTP_303_SEE_OTHER)


def _nazwy_firm(db: Session, firmy: list[str]) -> dict[str, str]:
    return {
        identyfikator: nazwa for identyfikator, nazwa in db.execute(
            select(Tenant.id, Tenant.name).where(Tenant.id.in_(firmy))
        ).all()
    }


def _technicy(db: Session, firmy: list[str]) -> list[PortalUser]:
    """Technicy majacy dostep do ktorejkolwiek z tych firm - do list wyboru."""
    return list(db.execute(
        select(PortalUser)
        .join(HelpdeskDostep, HelpdeskDostep.user_id == PortalUser.id)
        .where(HelpdeskDostep.tenant_id.in_(firmy), PortalUser.is_active.is_(True))
        .distinct()
        .order_by(PortalUser.full_name, PortalUser.email)
    ).scalars())


# --- lista zgloszen ---------------------------------------------------------

@router.get("/helpdesk", response_class=HTMLResponse)
def lista_zgloszen(
    request: Request,
    widok: str = Query("tablica", max_length=16),
    szukaj: str = Query("", max_length=200),
    firma: str = Query("", max_length=36),
    technik: str = Query("", max_length=36),
    stan: str = Query("", max_length=20),
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Tablica albo lista - ten sam zbior zgloszen, dwa sposoby patrzenia."""
    firmy = _firmy(db, user)
    if firma and firma in firmy:
        firmy_widoku = [firma]
    else:
        firma, firmy_widoku = "", firmy

    zapytanie = select(Zgloszenie).where(Zgloszenie.tenant_id.in_(firmy_widoku))
    if szukaj.strip():
        wzorzec = f"%{szukaj.strip().lower()}%"
        zapytanie = zapytanie.where(or_(
            func.lower(Zgloszenie.numer_pelny).like(wzorzec),
            func.lower(Zgloszenie.temat).like(wzorzec),
            func.lower(Zgloszenie.zglaszajacy_email).like(wzorzec),
        ))
    if technik == "brak":
        zapytanie = zapytanie.where(Zgloszenie.technik_id.is_(None))
    elif technik:
        zapytanie = zapytanie.where(Zgloszenie.technik_id == technik)
    if stan in STATUSY_ZGLOSZENIA:
        zapytanie = zapytanie.where(Zgloszenie.status == stan)

    zgloszenia = list(db.execute(
        zapytanie.order_by(Zgloszenie.ostatnia_aktywnosc.desc()).limit(400)
    ).scalars())

    # Czas pracy dla calej listy jednym zapytaniem - inaczej kazdy wiersz
    # tablicy pytalby bazy osobno i przy stu zgloszeniach widac by to bylo.
    czasy = dict(db.execute(
        select(CzasPracy.zgloszenie_id, func.sum(CzasPracy.minuty))
        .where(CzasPracy.zgloszenie_id.in_([z.id for z in zgloszenia] or ["-"]))
        .group_by(CzasPracy.zgloszenie_id)
    ).all())

    kolumny = {status_: [] for status_ in STATUSY_ZGLOSZENIA}
    for zgloszenie in zgloszenia:
        kolumny[zgloszenie.status].append(zgloszenie)

    return render(
        request, "helpdesk_lista.html", user, ctx, db,
        zgloszenia=zgloszenia,
        kolumny={klucz: wartosc[:NA_KOLUMNE] for klucz, wartosc in kolumny.items()},
        ukryte={klucz: max(0, len(wartosc) - NA_KOLUMNE) for klucz, wartosc in kolumny.items()},
        czasy=czasy, statusy=STATUSY_ZGLOSZENIA,
        nazwy_firm=_nazwy_firm(db, firmy), firmy=firmy,
        technicy=_technicy(db, firmy),
        widok=("lista" if widok == "lista" else "tablica"),
        szukaj=szukaj, firma=firma, technik=technik, stan=stan,
        komunikat=komunikat,
        otwarte=sum(1 for z in zgloszenia if z.status in STATUSY_OTWARTE),
        czekajace=db.execute(
            select(func.count(NierozpoznanaWiadomosc.id))
            .where(NierozpoznanaWiadomosc.stan == NIEROZPOZNANA_CZEKA)
        ).scalar_one() if helpdesk.prowadzi_helpdesk(user) else 0,
    )


# --- karta zgloszenia -------------------------------------------------------

@router.get("/helpdesk/zgloszenie/{zgloszenie_id}", response_class=HTMLResponse)
def karta_zgloszenia(
    zgloszenie_id: str,
    request: Request,
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)

    wpisy = list(db.execute(
        select(WpisZgloszenia)
        .where(WpisZgloszenia.zgloszenie_id == zgloszenie.id)
        .order_by(WpisZgloszenia.utworzono)
    ).scalars())
    zalaczniki: dict[str, list[ZalacznikWpisu]] = {}
    for zalacznik in db.execute(
        select(ZalacznikWpisu).where(ZalacznikWpisu.zgloszenie_id == zgloszenie.id)
    ).scalars():
        zalaczniki.setdefault(zalacznik.wpis_id, []).append(zalacznik)

    suma, udzialy = helpdesk.czas_zgloszenia(db, zgloszenie.id)
    wpisy_czasu = list(db.execute(
        select(CzasPracy)
        .where(CzasPracy.zgloszenie_id == zgloszenie.id)
        .order_by(CzasPracy.utworzono)
    ).scalars())

    return render(
        request, "helpdesk_zgloszenie.html", user, ctx, db,
        zgloszenie=zgloszenie,
        firma=db.get(Tenant, zgloszenie.tenant_id),
        wpisy=wpisy, zalaczniki=zalaczniki,
        suma_czasu=suma, udzialy=udzialy, wpisy_czasu=wpisy_czasu,
        osoby={u.id: helpdesk.opis_osoby(u) for u in db.execute(select(PortalUser)).scalars()},
        technicy=_technicy(db, [zgloszenie.tenant_id]),
        statusy=STATUSY_ZGLOSZENIA, typy=TYPY_ZGLOSZENIA,
        sprzet=helpdesk.sprzet_zgloszenia(db, zgloszenie.id),
        kandydaci=helpdesk.sprzet_zglaszajacego(
            db, zgloszenie.tenant_id, zgloszenie.zglaszajacy_email
        ),
        sprzet_firmy=list(db.execute(
            select(Asset).where(Asset.tenant_id == zgloszenie.tenant_id)
            .order_by(Asset.hostname).limit(500)
        ).scalars()),
        niewyslane=helpdesk_wysylka.niewyslane(db, zgloszenie.id),
        skrzynka=helpdesk_wysylka.ustawienia(db),
        zamkniecie_mailem=helpdesk_wysylka.zamkniecie_nalezne(db),
        komunikat=komunikat,
    )


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/wiadomosc")
def dopisz_wiadomosc(
    zgloszenie_id: str,
    request: Request,
    tresc: str = Form("", max_length=20000),
    rodzaj: str = Form(WPIS_WEWNETRZNY),
    zostaw_w_trakcie: bool = Form(False),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Odpowiedz do klienta albo komentarz wewnetrzny - jedno pole, dwa skutki.

    Rodzaj wpisu decyduje, czy tresc wyjdzie na zewnatrz, wiec nieznana wartosc
    jest bledem. Domyslnie komentarz wewnetrzny: pomylka w te strone zostaje
    w zgloszeniu, pomylka w druga idzie do klienta i nie da sie jej cofnac.
    """
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    verify_csrf(request, user, csrf_token)
    if not tresc.strip():
        raise HTTPException(status_code=400, detail="pusta wiadomosc")

    powrot = f"/helpdesk/zgloszenie/{zgloszenie.id}"
    autor = helpdesk.opis_osoby(user)
    if rodzaj == WPIS_DO_KLIENTA:
        try:
            helpdesk_wysylka.odpowiedz_klientowi(db, zgloszenie, tresc.strip(), user)
            komunikat = f"Odpowiedź poszła do {zgloszenie.zglaszajacy_email}."
        except helpdesk_wysylka.BladWysylki as blad:
            # Tresc zostaje w watku - zapis byl przed wysylka. Statusu nie
            # ruszamy: nie czekamy na klienta, ktory niczego nie dostal.
            db.commit()
            return _wroc(powrot, f"Wiadomość zapisana, ale NIE wyszła: {blad}")

        if not zostaw_w_trakcie:
            # Napisalismy do klienta, wiec pilka jest po jego stronie - i tak
            # ma to wygladac na tablicy. Technik moze to wylaczyc dla
            # wiadomosci czysto informacyjnej, po ktorej na nic nie czeka.
            helpdesk.zmien_status(db, zgloszenie, STATUS_OCZEKUJE, autor=autor)
            db.commit()
            return _wroc(powrot, komunikat + " Zgłoszenie czeka na klienta.")
    elif rodzaj == WPIS_WEWNETRZNY:
        helpdesk.dopisz_wiadomosc(
            db, zgloszenie, rodzaj=WPIS_WEWNETRZNY, tresc=tresc.strip(), autor=user
        )
        komunikat = "Komentarz widoczny tylko dla techników."
    else:
        raise HTTPException(status_code=400, detail="nieznany rodzaj wpisu")

    if zgloszenie.status == STATUS_NOWE:
        # Ktos odpisal, wiec sprawa nie jest juz "nowa" - inaczej pierwsza
        # kolumna tablicy zbiera zgloszenia, ktorymi ktos sie zajmuje.
        helpdesk.zmien_status(db, zgloszenie, STATUS_W_TRAKCIE, autor=autor)
    db.commit()
    return _wroc(powrot, komunikat)


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/status")
def zmien_status(
    zgloszenie_id: str,
    request: Request,
    stan: str = Form(""),
    podsumowanie: str = Form("", max_length=20000),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Zmiana statusu, a przy zamknieciu - wiadomosc do klienta.

    Mail idzie tylko przy przejsciu do zamknietego: powtorne wybranie tego
    samego statusu niczego nie wysyla, bo klient dostalby drugie zawiadomienie
    o zakonczeniu sprawy, ktora juz raz zakonczylismy.
    """
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    verify_csrf(request, user, csrf_token)

    zamykamy = stan == STATUS_ZAMKNIETE and zgloszenie.status != STATUS_ZAMKNIETE
    try:
        helpdesk.zmien_status(db, zgloszenie, stan, autor=helpdesk.opis_osoby(user))
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(status_code=400, detail=str(blad)) from blad

    komunikat = ""
    if zamykamy and helpdesk_wysylka.zamkniecie_nalezne(db):
        # Wysylka nie rzuca wyzej - zamkniecie ma sie udac takze wtedy, gdy
        # serwer poczty akurat nie odpowiada. Blad widac przy wpisie w watku.
        wpis = helpdesk_wysylka.wyslij_zamkniecie(db, zgloszenie, podsumowanie)
        if wpis is not None and wpis.blad_wysylki:
            komunikat = f"Zgłoszenie zamknięte, ale mail NIE wyszedł: {wpis.blad_wysylki}"
        else:
            komunikat = f"Zgłoszenie zamknięte, klient dostał wiadomość na {zgloszenie.zglaszajacy_email}."

    db.commit()
    return _wroc(f"/helpdesk/zgloszenie/{zgloszenie.id}", komunikat)


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/technik")
def zmien_technika(
    zgloszenie_id: str,
    request: Request,
    technik_id: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    verify_csrf(request, user, csrf_token)

    technik = db.get(PortalUser, technik_id) if technik_id else None
    if technik_id and technik is None:
        raise HTTPException(status_code=400, detail="nie znaleziono technika")
    try:
        helpdesk.przypisz(db, zgloszenie, technik, autor=helpdesk.opis_osoby(user))
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(status_code=400, detail=str(blad)) from blad
    db.commit()
    return _wroc(f"/helpdesk/zgloszenie/{zgloszenie.id}")


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/czas")
def dodaj_czas(
    zgloszenie_id: str,
    request: Request,
    minuty: str = Form(""),
    opis: str = Form("", max_length=500),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Dopisuje czas pracy. Zawsze SWOJ - czasu kolegi nikt za niego nie wpisze."""
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    verify_csrf(request, user, csrf_token)

    try:
        ile = int("".join(znak for znak in minuty if znak.isdigit()) or 0)
        helpdesk.dodaj_czas(db, zgloszenie, user, ile, opis.strip() or None)
    except (ValueError, helpdesk.BladHelpdesku) as blad:
        raise HTTPException(status_code=400, detail=str(blad) or "podaj liczbe minut") from blad
    db.commit()
    return _wroc(
        f"/helpdesk/zgloszenie/{zgloszenie.id}",
        f"Dopisano {helpdesk.formatuj_czas(ile)}. Wpisu nie da sie juz zmienic.",
    )


@router.post("/helpdesk/zgloszenie/{zgloszenie_id}/sprzet")
def zmien_sprzet(
    zgloszenie_id: str,
    request: Request,
    asset_id: str = Form(""),
    akcja: str = Form("podepnij"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    zgloszenie = _zgloszenie(db, user, zgloszenie_id)
    verify_csrf(request, user, csrf_token)

    if akcja == "odepnij":
        helpdesk.odepnij_sprzet(db, zgloszenie, asset_id, autor=helpdesk.opis_osoby(user))
    else:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(status_code=400, detail="nie znaleziono sprzetu")
        try:
            helpdesk.podepnij_sprzet(
                db, zgloszenie, asset, dodal=helpdesk.opis_osoby(user)
            )
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad
    db.commit()
    return _wroc(f"/helpdesk/zgloszenie/{zgloszenie.id}")


@router.get("/helpdesk/zalacznik/{zalacznik_id}")
def pobierz_zalacznik(
    zalacznik_id: str,
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Plik z wiadomosci. Sprawdzamy firme, nie sam identyfikator zalacznika."""
    zalacznik = db.get(ZalacznikWpisu, zalacznik_id)
    if zalacznik is None:
        raise HTTPException(status_code=404, detail="nie znaleziono zalacznika")
    _zgloszenie(db, user, zalacznik.zgloszenie_id)

    sciezka = (poczta.katalog_zalacznikow() / zalacznik.sciezka).resolve()
    korzen = poczta.katalog_zalacznikow().resolve()
    if korzen not in sciezka.parents or not sciezka.is_file():
        raise HTTPException(status_code=404, detail="pliku nie ma juz na dysku")
    # Zawsze jako plik do pobrania: tresc przyszla od klienta, wiec nie ma
    # prawa wykonac sie w przegladarce technika.
    return FileResponse(
        sciezka, filename=zalacznik.nazwa, media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


# --- nierozpoznana poczta (superadmin) --------------------------------------

@router.get("/helpdesk/nierozpoznane", response_class=HTMLResponse)
def nierozpoznane(
    request: Request,
    pokaz: str = Query("czeka", max_length=20),
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Poczta bez rozpoznanej firmy. Widok superadmina: dopoki nie znamy firmy,
    nie ma czym mierzyc dostepu technika."""
    _superadmin(user)
    stan = pokaz if pokaz in (
        NIEROZPOZNANA_CZEKA, NIEROZPOZNANA_ZIGNOROWANA, NIEROZPOZNANA_PRZYPISANA
    ) else NIEROZPOZNANA_CZEKA

    wiadomosci = list(db.execute(
        select(NierozpoznanaWiadomosc)
        .where(NierozpoznanaWiadomosc.stan == stan)
        .order_by(NierozpoznanaWiadomosc.otrzymano.desc())
        .limit(200)
    ).scalars())

    return render(
        request, "helpdesk_nierozpoznane.html", user, ctx, db,
        wiadomosci=wiadomosci, stan=stan,
        firmy=list(db.execute(
            select(Tenant).join(HelpdeskFirma, HelpdeskFirma.tenant_id == Tenant.id)
            .order_by(Tenant.name)
        ).scalars()),
        reguly=list(db.execute(
            select(IgnorowanaDomena).order_by(IgnorowanaDomena.przechwycone.desc())
        ).scalars()),
        liczby={
            klucz: db.execute(
                select(func.count(NierozpoznanaWiadomosc.id))
                .where(NierozpoznanaWiadomosc.stan == klucz)
            ).scalar_one()
            for klucz in (NIEROZPOZNANA_CZEKA, NIEROZPOZNANA_ZIGNOROWANA,
                          NIEROZPOZNANA_PRZYPISANA)
        },
        komunikat=komunikat,
    )


@router.post("/helpdesk/nierozpoznane/{wiadomosc_id}")
def decyzja_o_wiadomosci(
    wiadomosc_id: str,
    request: Request,
    akcja: str = Form(""),
    tenant_id: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Utworz zgloszenie, zignoruj wiadomosc albo cala jej domene."""
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    wiadomosc = db.get(NierozpoznanaWiadomosc, wiadomosc_id)
    if wiadomosc is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wiadomosci")

    powrot = "/helpdesk/nierozpoznane"
    if akcja == "utworz":
        firma = db.get(Tenant, tenant_id) if tenant_id else None
        if firma is None:
            raise HTTPException(status_code=400, detail="wskaz firme")
        try:
            zgloszenie = helpdesk.utworz_zgloszenie(
                db, tenant_id=firma.id,
                temat=wiadomosc.temat or "(bez tematu)", tresc=wiadomosc.tresc,
                zglaszajacy_email=wiadomosc.nadawca_email,
                zglaszajacy_nazwa=wiadomosc.nadawca_nazwa,
                message_id=wiadomosc.message_id,
                zrodlo="decyzja operatora", autor=helpdesk.opis_osoby(user),
            )
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad

        wiadomosc.stan = NIEROZPOZNANA_PRZYPISANA
        wiadomosc.zgloszenie_id = zgloszenie.id
        komunikat = (
            f"Założono {zgloszenie.numer_pelny} dla firmy {firma.name}. "
            f"Domena {wiadomosc.domena} NIE została dodana do firmy — "
            "zrób to w Firmach, jeśli kolejne maile mają zakładać zgłoszenia same."
        )
    elif akcja == "zignoruj":
        wiadomosc.stan = NIEROZPOZNANA_ZIGNOROWANA
        komunikat = "Wiadomość zignorowana. Nie została skasowana."
    elif akcja == "zignoruj_domene":
        try:
            poczta.ignoruj_domene(db, wiadomosc.domena, zalozyl=user.email)
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad
        wiadomosc.stan = NIEROZPOZNANA_ZIGNOROWANA
        komunikat = (
            f"Kolejne wiadomości z {wiadomosc.domena} nie trafią już na tę listę. "
            "Regułę widać niżej razem z licznikiem przechwyconej poczty."
        )
    else:
        raise HTTPException(status_code=400, detail="nieznana decyzja")

    wiadomosc.decyzje_podjal = user.email
    wiadomosc.decyzja_o = utcnow()
    audit(db, None, action="helpdesk.nierozpoznana", target=wiadomosc.nadawca_email,
          detail={"akcja": akcja}, ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc(powrot, komunikat)


@router.post("/helpdesk/domeny/{regula_id}/przywroc")
def przywroc_domene(
    regula_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    regula = db.get(IgnorowanaDomena, regula_id)
    if regula is None:
        raise HTTPException(status_code=404, detail="nie znaleziono reguly")
    domena = regula.domena
    poczta.przywroc_domene(db, domena)
    db.commit()
    return _wroc(
        "/helpdesk/nierozpoznane",
        f"Poczta z {domena} znów trafia na listę do przejrzenia. "
        "Wiadomości już przechwycone zostają w zignorowanych.",
    )


# --- firmy, domeny i dostepy (superadmin) -----------------------------------

@router.get("/helpdesk/firmy", response_class=HTMLResponse)
def firmy_helpdesku(
    request: Request,
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    wszystkie = list(db.execute(select(Tenant).order_by(Tenant.name)).scalars())
    wpisy = {w.tenant_id: w for w in db.execute(select(HelpdeskFirma)).scalars()}

    return render(
        request, "helpdesk_firmy.html", user, ctx, db,
        firmy=[{
            "tenant": tenant,
            "wpis": wpisy.get(tenant.id),
            "domeny": helpdesk.domeny_firmy(db, tenant.id),
            "propozycja": helpdesk.proponuj_skrot(tenant.name),
        } for tenant in wszystkie],
        konta=list(db.execute(
            select(PortalUser).where(PortalUser.is_active.is_(True))
            .order_by(PortalUser.email)
        ).scalars()),
        dostepy=list(db.execute(select(HelpdeskDostep)).scalars()),
        komunikat=komunikat,
    )


@router.post("/helpdesk/firmy/{tenant_id}/wlacz")
def wlacz_firme(
    tenant_id: str,
    request: Request,
    skrot: str = Form("", max_length=8),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="nie znaleziono firmy")
    try:
        wpis = helpdesk.zapewnij_firme(db, tenant, skrot.strip() or None)
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(status_code=400, detail=str(blad)) from blad
    db.commit()
    return _wroc(
        "/helpdesk/firmy",
        f"{tenant.name} obsluguje helpdesk. Numery zgloszen: {wpis.skrot}-1, {wpis.skrot}-2...",
    )


@router.post("/helpdesk/firmy/{tenant_id}/domena")
def dodaj_domene(
    tenant_id: str,
    request: Request,
    domena: str = Form("", max_length=255),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    try:
        wpis = helpdesk.dodaj_domene(db, tenant_id, domena, dodal=user.email)
    except helpdesk.BladHelpdesku as blad:
        raise HTTPException(status_code=400, detail=str(blad)) from blad
    db.commit()
    return _wroc("/helpdesk/firmy", f"Poczta z {wpis.domena} zakłada teraz zgłoszenia.")


@router.post("/helpdesk/firmy/domena/{domena_id}/usun")
def usun_domene(
    domena_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Usuniecie domeny nie rusza starych zgloszen - zamyka droge nowym."""
    from ..models import HelpdeskDomena

    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    wpis = db.get(HelpdeskDomena, domena_id)
    if wpis is None:
        raise HTTPException(status_code=404, detail="nie znaleziono domeny")
    domena = wpis.domena
    db.delete(wpis)
    db.commit()
    return _wroc(
        "/helpdesk/firmy",
        f"Poczta z {domena} trafi od teraz do nierozpoznanych. Stare zgloszenia zostaja.",
    )


@router.post("/helpdesk/dostepy")
def zmien_dostep(
    request: Request,
    user_id: str = Form(""),
    tenant_id: str = Form(""),
    akcja: str = Form("nadaj"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Nadaje albo odbiera technikowi firme. Wylacznie superadmin.

    Nadanie firmy otwiera technikowi takze jej kartoteke w CMDB z prawami
    operatora - wiec robi to konto, ktore samo ma prawo przekraczac granice
    firm, i tylko ono.
    """
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    konto = db.get(PortalUser, user_id)
    tenant = db.get(Tenant, tenant_id)
    if konto is None or tenant is None:
        raise HTTPException(status_code=400, detail="wskaz konto i firme")

    if akcja == "odbierz":
        helpdesk.odbierz_dostep(db, konto.id, tenant.id)
        komunikat = (
            f"{konto.email} nie widzi już firmy {tenant.name}. "
            "Jego zgłoszenia i czas pracy zostają."
        )
    else:
        try:
            helpdesk.nadaj_dostep(db, konto.id, tenant.id, nadal=user.email)
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad
        komunikat = f"{konto.email} obsługuje firmę {tenant.name} — ze zgłoszeniami i CMDB."

    audit(db, None, action="helpdesk.dostep", target=konto.email,
          detail={"firma": tenant.slug, "akcja": akcja},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc("/helpdesk/firmy", komunikat)


# --- skrzynka (superadmin) --------------------------------------------------

@router.get("/helpdesk/skrzynka", response_class=HTMLResponse)
def skrzynka(
    request: Request,
    komunikat: str = Query("", max_length=500),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    return render(
        request, "helpdesk_skrzynka.html", user, ctx, db,
        ustawienia=db.get(HelpdeskUstawienia, "helpdesk"),
        domyslne_potwierdzenie=helpdesk_wysylka.DOMYSLNE_POTWIERDZENIE,
        domyslne_zamkniecie=helpdesk_wysylka.DOMYSLNE_ZAMKNIECIE,
        komunikat=komunikat,
    )


@router.post("/helpdesk/skrzynka")
async def zapisz_skrzynke(
    request: Request,
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    _superadmin(user)
    formularz = await request.form()
    verify_csrf(request, user, formularz.get("csrf_token"))

    ustawienia = db.get(HelpdeskUstawienia, "helpdesk")
    if ustawienia is None:
        ustawienia = HelpdeskUstawienia(klucz="helpdesk")
        db.add(ustawienia)

    # Pola, ktore moga zostac puste - puste znaczy "nie ustawiono".
    for pole in ("imap_host", "imap_uzytkownik", "imap_folder_docelowy", "smtp_host",
                 "smtp_uzytkownik", "nadawca", "nazwa_nadawcy", "stopka",
                 "potwierdzenie_tresc", "zamkniecie_tresc"):
        if pole in formularz:
            setattr(ustawienia, pole, (formularz.get(pole) or "").strip() or None)

    # Pola wymagane przez baze. Pusta wartosc z formularza nie moze ich
    # wyzerowac - skonczyloby sie bledem zapisu zamiast zapisanymi ustawieniami.
    for pole, domyslna in (("imap_folder", "INBOX"), ("imap_szyfrowanie", "ssl"),
                           ("imap_po_pobraniu", "przeczytana"),
                           ("smtp_szyfrowanie", "starttls")):
        if pole in formularz:
            setattr(ustawienia, pole, (formularz.get(pole) or "").strip() or domyslna)

    for pole, domyslna in (("imap_port", 993), ("smtp_port", 587),
                           ("imap_interwal_sekund", 120)):
        wartosc = (formularz.get(pole) or "").strip()
        setattr(ustawienia, pole, int(wartosc) if wartosc.isdigit() else domyslna)

    for pole in ("usun_zignorowane_po_dniach", "usun_spam_po_dniach"):
        wartosc = (formularz.get(pole) or "").strip()
        # Puste znaczy "nie usuwaj nigdy" i taka jest wartosc domyslna.
        setattr(ustawienia, pole, int(wartosc) if wartosc.isdigit() and int(wartosc) else None)

    # Haslo puste znaczy "zostaw dotychczasowe" - inaczej kazdy zapis
    # ustawien kasowalby poswiadczenia, bo pola hasel wracaja puste.
    for pole, kolumna in (("imap_haslo", "imap_haslo_szyfr"), ("smtp_haslo", "smtp_haslo_szyfr")):
        nowe = (formularz.get(pole) or "").strip()
        if nowe:
            setattr(ustawienia, kolumna, sekrety.zaszyfruj(nowe))

    ustawienia.potwierdzenie_wlaczone = bool(formularz.get("potwierdzenie_wlaczone"))
    ustawienia.zamkniecie_wlaczone = bool(formularz.get("zamkniecie_wlaczone"))
    ustawienia.aktywne = bool(formularz.get("aktywne"))
    audit(db, None, action="helpdesk.skrzynka", target=ustawienia.nadawca or "-",
          ip=client_ip(request), actor=user.email)
    db.commit()

    if formularz.get("sprawdz"):
        try:
            helpdesk_wysylka.sprawdz(db, user.email)
            return _wroc("/helpdesk/skrzynka", f"Wiadomość próbna poszła na {user.email}.")
        except helpdesk_wysylka.BladWysylki as blad:
            return _wroc("/helpdesk/skrzynka", f"Wysyłka nie działa: {blad}")
    return _wroc("/helpdesk/skrzynka", "Ustawienia zapisane.")


@router.post("/helpdesk/skrzynka/pobierz")
def pobierz_teraz(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Pobranie poczty na zadanie - bez czekania na kolejny obieg."""
    _superadmin(user)
    verify_csrf(request, user, csrf_token)

    wynik = helpdesk_imap.pobierz(db)
    if "blad" in wynik:
        return _wroc("/helpdesk/skrzynka", f"Odbiór nie powiódł się: {wynik['blad']}")
    if "pominiete" in wynik:
        return _wroc("/helpdesk/skrzynka", wynik["pominiete"])
    return _wroc(
        "/helpdesk/skrzynka",
        f"Pobrano {wynik['pobrane']} wiadomości: {wynik['nowe']} nowych zgłoszeń, "
        f"{wynik['dopisane']} dopisanych, {wynik['nierozpoznane']} nierozpoznanych.",
    )


# --- raporty ----------------------------------------------------------------

@router.get("/helpdesk/raporty/firma", response_class=HTMLResponse)
def raport_firmy(
    request: Request,
    firma: str = Query("", max_length=36),
    okres: str = Query("", max_length=7),
    eksport: str = Query("", max_length=8),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Firma -> technik -> zgloszenie, z eksportem."""
    firmy = _firmy(db, user)
    wybrana = firma if firma in firmy else firmy[0]

    raport = helpdesk_raporty.raport_firmy(db, wybrana, okres)
    if eksport:
        return _plik(raport, eksport)

    return render(
        request, "helpdesk_raport.html", user, ctx, db,
        raport=raport, rodzaj="firma",
        wybor=[(t.id, t.name) for t in db.execute(
            select(Tenant).where(Tenant.id.in_(firmy)).order_by(Tenant.name)
        ).scalars()],
        wybrany=wybrana, okres=okres or helpdesk_raporty.ostatnie_miesiace(1)[0][0],
        miesiace=helpdesk_raporty.ostatnie_miesiace(),
        naglowek_grupy="Technik",
    )


@router.get("/helpdesk/raporty/technik", response_class=HTMLResponse)
def raport_technika(
    request: Request,
    technik: str = Query("", max_length=36),
    okres: str = Query("", max_length=7),
    eksport: str = Query("", max_length=8),
    user: PortalUser = Depends(require_user),
    ctx: TenantContext = Depends(resolve_tenant),
    db: Session = Depends(get_db),
) -> Response:
    """Technik -> firma -> zgloszenie.

    Technik oglada wlasny czas zawsze; czas kolegow tylko w firmach, ktore sam
    obsluguje - i tylko wtedy, gdy jest tam z nim. Superadmin widzi wszystkich.
    """
    firmy = _firmy(db, user)
    dostepni = _technicy(db, firmy)
    if not any(t.id == technik for t in dostepni) and technik != user.id:
        technik = user.id
    # Superadmin nie ma przydzielonych firm, wiec nie ma go na liscie technikow.
    # Bez tego lista pokazywalaby kogos innego niz raport pod nia.
    if not any(t.id == technik for t in dostepni):
        dostepni = [user, *dostepni]

    raport = helpdesk_raporty.raport_technika(
        db, technik, okres, firmy=None if helpdesk.prowadzi_helpdesk(user) else firmy
    )
    if eksport:
        return _plik(raport, eksport)

    return render(
        request, "helpdesk_raport.html", user, ctx, db,
        raport=raport, rodzaj="technik",
        wybor=[(t.id, helpdesk.opis_osoby(t)) for t in dostepni],
        wybrany=technik, okres=okres or helpdesk_raporty.ostatnie_miesiace(1)[0][0],
        miesiace=helpdesk_raporty.ostatnie_miesiace(),
        naglowek_grupy="Firma",
    )


def _plik(raport: helpdesk_raporty.Raport, format_: str) -> Response:
    if format_ == "xlsx":
        tresc = helpdesk_raporty.do_xlsx(raport)
        typ = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        nazwa = helpdesk_raporty.nazwa_pliku(raport, "xlsx")
    elif format_ == "csv":
        tresc = helpdesk_raporty.do_csv(raport)
        typ = "text/csv; charset=utf-8"
        nazwa = helpdesk_raporty.nazwa_pliku(raport, "csv")
    else:
        raise HTTPException(status_code=400, detail="nieznany format eksportu")
    return Response(
        content=tresc, media_type=typ,
        headers={"Content-Disposition": f'attachment; filename="{nazwa}"'},
    )
