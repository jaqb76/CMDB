"""Panel glownego administratora: firmy, konta, tokeny i wersje agenta.

Odrebny router z jedna zaleznoscia wejsciowa (require_superadmin), zamiast
sprawdzania roli w kazdym widoku - zapomniana kontrola w jednym miejscu
otwieralaby caly panel globalny.

Widoki firmowe (api/ui.py) pokazuja zawsze jedna firme i przechodza przez
services/scoping. Tutaj patrzymy na wszystkie naraz, wiec zapytania swiadomie
tego filtra nie maja - kazde takie miejsce jest w tym pliku i tylko tutaj.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from .. import wersja
from ..config import get_settings
from ..db import get_db
from ..models import (
    LIFECYCLE_AKTYWNY,
    ZRODLO_AGENT,
    as_utc,
    AgentRelease,
    ReleaseImportStatus,
    Asset,
    AuditLog,
    CveFeed,
    EnrollmentToken,
    GlobalAgentTarget,
    WpisSlownika,
    PortalUser,
    Tenant,
    TenantAgentTarget,
    utcnow,
)
from ..security import (
    check_csrf_token,
    generate_token,
    hash_password,
    issue_csrf_token,
    verify_password,
)
from ..services.auth import client_ip, require_superadmin
from ..services import architektura, cve, mobilna, pakiet, ustawienia
from ..services import upgrades
from ..services.scoping import audit
from .ui import MIN_DLUGOSC_HASLA, motyw_z_ciasteczka, templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
WERSJA_RE = re.compile(r"[0-9][0-9a-zA-Z._+-]{0,31}")

# Agent budowany jest osobno dla kazdego systemu. Sygnatura pliku pozwala
# odrzucic pomylke juz przy wgrywaniu, a nie dopiero na maszynie klienta.
# Bajt 0x7F zapisujemy przez fromhex, bo doslownie w zrodle jest niewidoczny.
SYSTEMY = {
    "windows": {"etykieta": "Windows", "sygnatura": b"MZ", "opis": "program Windows (.exe)"},
    "linux": {"etykieta": "Linux", "sygnatura": bytes.fromhex("7f") + b"ELF", "opis": "program ELF"},
}

# Skrypt budujacy dopisuje metadane na koncu pliku agenta. PyInstaller pakuje
# kod w skompresowane archiwum, wiec numeru wersji nie da sie odczytac
# z gotowego pliku, a uruchomienie go na serwerze byloby wykonywaniem obcego
# kodu. Stopka rozwiazuje to bez jednego i bez drugiego.
ZNACZNIK_POCZATEK = b"<<<CMDB-AGENT-META>>>"
ZNACZNIK_KONIEC = b"<<<KONIEC>>>"
# Zostaw miejsce na doklejony certyfikat Authenticode za stopka buildu.
OGON_METADANYCH = 65536


def odczytaj_metadane(sciezka: Path) -> dict | None:
    """Wyciaga metadane dopisane na koncu pliku agenta.

    To wygoda, a nie zabezpieczenie: plik sam o sobie mowi, jaka ma wersje.
    Sygnatura formatu i skrot SHA-256 sprawdzane sa niezaleznie, a gdyby ktos
    podal zly numer, serwer w kolko proponowalby te sama aktualizacje - agent
    zglaszalby przeciez inna wersje niz oczekiwana.
    """
    try:
        rozmiar = sciezka.stat().st_size
        with sciezka.open("rb") as plik:
            plik.seek(max(0, rozmiar - OGON_METADANYCH))
            ogon = plik.read()
    except OSError:
        return None

    poczatek = ogon.rfind(ZNACZNIK_POCZATEK)
    if poczatek < 0:
        return None
    koniec = ogon.find(ZNACZNIK_KONIEC, poczatek)
    if koniec < 0:
        return None

    surowe = ogon[poczatek + len(ZNACZNIK_POCZATEK):koniec]
    try:
        dane = json.loads(surowe.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("plik agenta ma uszkodzone metadane wersji")
        return None
    return dane if isinstance(dane, dict) else None


def render_admin(request: Request, template: str, user: PortalUser, strona: str, **extra):
    payload = {
        "request": request,
        "user": user,
        "strona": strona,
        "csrf_token": issue_csrf_token(user.id),
        "motyw": motyw_z_ciasteczka(request),
        "wersja_portalu": wersja.opis(),
        **extra,
    }
    return templates.TemplateResponse(request, template, payload)


def sprawdz_csrf(user: PortalUser, token: str | None) -> None:
    if not token or not check_csrf_token(token, user.id):
        raise HTTPException(status_code=403, detail="nieprawidlowy token CSRF")


def _konto_do_zmiany(db: Session, user: PortalUser, user_id: str) -> PortalUser:
    """Konto, na ktorym wolno wykonac operacje - albo wyjatek.

    Jedyna regula brzmi: nie na sobie. Wystarcza ona takze za ochrone przed
    zamknieciem sobie drogi do panelu - skoro operacje wykonuje superadmin,
    a wlasnego konta ruszyc nie moze, to zawsze zostaje przynajmniej jeden
    czynny superadmin. Wczesniej regula brzmiala "superadmina nie ruszamy",
    ale wtedy pomylkowo zalozonego superadmina nie dalo sie ani wylaczyc,
    ani usunac - z panelu nie dalo sie zrobic nic poza zmiana jego hasla.
    """
    konto = db.get(PortalUser, user_id)
    if konto is None:
        raise HTTPException(status_code=404, detail="nie znaleziono konta")
    if konto.id == user.id:
        raise HTTPException(
            status_code=400,
            detail="wlasnego konta nie da sie wylaczyc ani usunac - popros innego administratora",
        )
    return konto


def znajdz_firme(db: Session, tenant_id: str) -> Tenant:
    firma = db.get(Tenant, tenant_id)
    if firma is None:
        raise HTTPException(status_code=404, detail="nie znaleziono firmy")
    return firma


def katalog_wersji() -> Path:
    katalog = Path(get_settings().release_dir)
    katalog.mkdir(parents=True, exist_ok=True)
    return katalog


def _klucz_wersji(wersja: str | None) -> tuple:
    """Klucz sortowania wersji liczbowo: "0.6.131+1" po "0.6.77+1".

    Sortowanie tekstowe stawialo 0.6.77 nad 0.6.131 ("7" > "1"), przez co
    nowe wydania ladowaly na samym dole listy i wygladaly na nieistniejace.
    """
    import re

    return tuple(int(czesc) for czesc in re.findall(r"\d+", wersja or ""))


def _wydania(db: Session):
    """Wszystkie wydania, mapa po identyfikatorze i podzial na systemy."""
    # System rosnaco, w obrebie systemu najnowsze wersje na gorze.
    wszystkie = sorted(
        db.execute(select(AgentRelease)).scalars().all(),
        key=lambda w: (w.os_family or "", tuple(-n for n in _klucz_wersji(w.version))),
    )
    wedlug_systemu: dict[str, list[AgentRelease]] = {}
    for wydanie in wszystkie:
        wedlug_systemu.setdefault(wydanie.os_family, []).append(wydanie)
    return wszystkie, {w.id: w for w in wszystkie}, wedlug_systemu


def _statystyki_agentow(db: Session):
    """Per firma: rozklad wersji, liczba aktywnych i nieaktywnych agentow.

    Prog braku kontaktu moze byc ustawiony osobno dla kazdej firmy, wiec
    zliczamy po stronie Pythona zamiast jednym zapytaniem agregujacym -
    zapytanie musialoby uwzglednic inny prog dla kazdej firmy naraz, a przy
    kilkunastu firmach i setkach maszyn roznica w czasie wykonania jest
    niemierzalna.
    """
    # Tylko maszyny z agentem. Sprzet wpisany recznie nie ma agenta, nigdy sie
    # nie odezwie i nie ma wersji - liczenie go jako "nieaktywny agent w wersji
    # nieznanej" zamienialo przeglad w stala falszywa alarmowke.
    wiersze = db.execute(
        select(Asset.tenant_id, Asset.os_family, Asset.agent_version, Asset.last_seen)
        .where(Asset.lifecycle == LIFECYCLE_AKTYWNY, Asset.zrodlo == ZRODLO_AGENT)
    ).all()

    granice = {
        firma.id: ustawienia.granica_aktywnosci(firma)
        for firma in db.execute(select(Tenant)).scalars()
    }

    zliczone: dict[str, dict[tuple, dict]] = {}
    aktywne: dict[str, int] = {}
    nieaktywne: dict[str, int] = {}

    for tenant_id, system, wersja, ostatni in wiersze:
        klucz = (system or "nieznany", wersja or "nieznana")
        pozycja = zliczone.setdefault(tenant_id, {}).setdefault(
            klucz,
            {"os_family": klucz[0], "wersja": klucz[1], "ile": 0, "aktywne": 0, "nieaktywne": 0},
        )
        granica = granice.get(tenant_id)
        czy_aktywny = ostatni is not None and as_utc(ostatni) >= granica if granica else False

        pozycja["ile"] += 1
        if czy_aktywny:
            pozycja["aktywne"] += 1
            aktywne[tenant_id] = aktywne.get(tenant_id, 0) + 1
        else:
            pozycja["nieaktywne"] += 1
            nieaktywne[tenant_id] = nieaktywne.get(tenant_id, 0) + 1

    rozklad = {
        tenant_id: sorted(pozycje.values(), key=lambda p: (p["os_family"], p["wersja"]))
        for tenant_id, pozycje in zliczone.items()
    }
    return rozklad, aktywne, nieaktywne


def _oficjalne(db: Session, wersje: dict) -> dict:
    return {
        cel.os_family: wersje.get(cel.release_id)
        for cel in db.execute(select(GlobalAgentTarget)).scalars()
    }


# --- przeglad ---------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def przeglad(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    rozklad, aktywne, nieaktywne = _statystyki_agentow(db)
    _, wersje, _ = _wydania(db)

    cele: dict[str, dict[str, AgentRelease]] = {}
    for cel in db.execute(select(TenantAgentTarget)).scalars():
        wydanie = wersje.get(cel.release_id)
        if wydanie is not None:
            cele.setdefault(cel.tenant_id, {})[cel.os_family] = wydanie

    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    wiersze = [
        {
            "firma": firma,
            "aktywne": aktywne.get(firma.id, 0),
            "nieaktywne": nieaktywne.get(firma.id, 0),
            "agenci": rozklad.get(firma.id, []),
            "cele": cele.get(firma.id, {}),
        }
        for firma in firmy
    ]

    return render_admin(
        request, "admin_przeglad.html", user, "przeglad",
        wiersze=wiersze,
        podsumowanie={
            "firmy": len(firmy),
            "firmy_aktywne": sum(1 for f in firmy if f.is_active),
            "aktywne": sum(aktywne.values()),
            "nieaktywne": sum(nieaktywne.values()),
        },
        oficjalne=_oficjalne(db, wersje),
        systemy=SYSTEMY,
        stale_after_hours=get_settings().stale_after_hours,
    )


# --- firmy i konta ----------------------------------------------------------

@router.get("/firmy", response_class=HTMLResponse)
def widok_firm(
    request: Request,
    wydany_token: str = "",
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    opiekunowie = dict(
        db.execute(select(WpisSlownika.tenant_id, func.count(WpisSlownika.id))
                  .where(WpisSlownika.kategoria == "osoba")
                  .group_by(WpisSlownika.tenant_id)).all()
    )
    tokeny = dict(
        db.execute(
            select(EnrollmentToken.tenant_id, func.count(EnrollmentToken.id))
            .where(EnrollmentToken.revoked_at.is_(None))
            .group_by(EnrollmentToken.tenant_id)
        ).all()
    )
    maszyny = dict(
        db.execute(
            select(Asset.tenant_id, func.count(Asset.id))
            .where(Asset.lifecycle == LIFECYCLE_AKTYWNY)
            .group_by(Asset.tenant_id)
        ).all()
    )
    konta_firm: dict[str, list[PortalUser]] = {}
    for konto in db.execute(
        select(PortalUser).where(PortalUser.tenant_id.is_not(None)).order_by(PortalUser.email)
    ).scalars():
        konta_firm.setdefault(konto.tenant_id, []).append(konto)

    # Konta bez firmy: superadmini i audytorzy globalni. Widac je w jednym
    # miejscu, bo to jedyne konta, ktore siegaja poza granice jednej firmy.
    konta_globalne = db.execute(
        select(PortalUser).where(PortalUser.tenant_id.is_(None)).order_by(PortalUser.email)
    ).scalars().all()

    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
    return render_admin(
        request, "admin_firmy.html", user, "firmy",
        firmy=firmy,
        konta_firm=konta_firm,
        konta_globalne=konta_globalne,
        min_dlugosc_hasla=MIN_DLUGOSC_HASLA,
        opiekunowie=opiekunowie,
        tokeny=tokeny,
        maszyny=maszyny,
        wydany_token=wydany_token,
    )


@router.post("/tenants")
def utworz_firme(
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    identyfikator = slug.strip().lower()
    if not SLUG_RE.match(identyfikator):
        raise HTTPException(
            status_code=400,
            detail="identyfikator moze zawierac male litery, cyfry i myslnik (2-63 znaki)",
        )
    if db.execute(select(Tenant).where(Tenant.slug == identyfikator)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="firma o tym identyfikatorze juz istnieje")

    firma = Tenant(name=name.strip(), slug=identyfikator)
    db.add(firma)
    db.flush()
    audit(db, None, action="tenant.created", target=identyfikator,
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s utworzyl firme %s", user.email, identyfikator)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/active")
def przelacz_firme(
    tenant_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)
    firma.is_active = not firma.is_active
    audit(db, None, action="tenant.active_changed", target=firma.slug,
          detail={"is_active": firma.is_active}, ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/users")
def utworz_konto(
    tenant_id: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("admin"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)
    adres = email.strip().lower()

    if len(password) < MIN_DLUGOSC_HASLA:
        raise HTTPException(
            status_code=400,
            detail=f"haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow",
        )
    if role not in {"admin", "viewer"}:
        raise HTTPException(status_code=400, detail="nieznana rola")
    if db.execute(select(PortalUser).where(PortalUser.email == adres)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="konto o tym adresie juz istnieje")

    db.add(
        PortalUser(
            tenant_id=firma.id,
            email=adres,
            full_name=full_name.strip() or None,
            password_hash=hash_password(password),
            role=role,
        )
    )
    audit(db, None, action="user.created", target=adres,
          detail={"tenant": firma.slug, "role": role}, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s utworzyl konto %s w firmie %s", user.email, adres, firma.slug)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


def _powrot(wskazany: str) -> str:
    """Dokad wracamy po akcji na koncie.

    Te same przyciski stoja na dwoch ekranach - przy firmie i na liscie kont -
    i maja wracac tam, skad je klikniejeto. Lista dozwolonych adresow jest
    krotka i zamknieta, bo pole formularza nie moze przekierowywac gdziekolwiek.
    """
    return wskazany if wskazany in ("/admin/konta", "/admin/firmy") else "/admin/firmy"


@router.post("/users/{user_id}/active")
def przelacz_konto(
    user_id: str,
    request: Request,
    csrf_token: str = Form(""),
    powrot: str = Form("/admin/firmy"),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    konto = _konto_do_zmiany(db, user, user_id)
    konto.is_active = not konto.is_active
    # Wylaczone konto ma stracic dostep natychmiast, a nie z wygasnieciem
    # ciasteczka: sesja sprawdza wersje przy kazdym zapytaniu.
    konto.session_version = PortalUser.session_version + 1
    audit(db, None, action="user.active_changed", target=konto.email,
          detail={"is_active": konto.is_active}, ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse(_powrot(powrot), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/usun")
def usun_konto(
    user_id: str,
    request: Request,
    csrf_token: str = Form(""),
    powrot: str = Form("/admin/firmy"),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Kasuje konto panelu. Dane firmy zostaja - konto to tylko dostep do nich.

    Wylaczenie konta zostawia je w bazie i da sie cofnac; usuniecie jest dla
    kont, ktore nie powinny istniec (pomylka przy zakladaniu, osoba, ktora
    odeszla). Wpisy w dzienniku audytu zostaja - zapisany jest w nich adres,
    a nie odwolanie do konta, wiec historia nie znika razem z nim.
    """
    sprawdz_csrf(user, csrf_token)
    konto = _konto_do_zmiany(db, user, user_id)
    # Adres i firme odczytujemy PRZED skasowaniem - po nim obiekt jest juz
    # tylko wpisem do usuniecia i siegniecie po powiazana firme moze sie nie udac.
    adres = konto.email
    firma = konto.tenant.slug if konto.tenant else None

    # Czas pracy technika jest podstawa faktury i nie wolno mu zniknac razem
    # z kontem - baza tego pilnuje, a my zamieniamy jej blad na zdanie, ktore
    # mowi, co zrobic zamiast usuwania.
    from ..models import CzasPracy

    minuty = db.execute(
        select(func.count(CzasPracy.id)).where(CzasPracy.technik_id == konto.id)
    ).scalar_one()
    if minuty:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{adres} ma zapisany czas pracy w {minuty} wpisach i usunięcie konta "
                "zabrałoby go z raportów. Wyłącz konto — przestanie się logować, "
                "a historia zostanie."
            ),
        )

    db.delete(konto)
    audit(db, None, action="user.deleted", target=adres,
          detail={"tenant": firma}, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s usunal konto %s", user.email, adres)
    return RedirectResponse(_powrot(powrot), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/globalne")
def utworz_konto_globalne(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Audytor globalny: widzi wszystkie firmy, nie zmienia w nich niczego.

    Powstaje osobno, a nie jako konto firmowe z rola "viewer", bo to inne
    uprawnienie: rola opisuje, co wolno w JEDNEJ firmie, a to jest zgoda na
    ogladanie wszystkich. Prawo zapisu odbiera services/auth.tenant_context_for
    - jedyne miejsce, w ktorym prawo zapisu w ogole powstaje.

    Do panelu administracyjnego taki audytor nie wchodzi: zarzadzanie firmami,
    kontami i wersjami agenta wymaga require_superadmin.
    """
    sprawdz_csrf(user, csrf_token)
    adres = email.strip().lower()

    if len(password) < MIN_DLUGOSC_HASLA:
        raise HTTPException(
            status_code=400,
            detail=f"haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow",
        )
    if db.execute(select(PortalUser).where(PortalUser.email == adres)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="konto o tym adresie juz istnieje")

    db.add(
        PortalUser(
            tenant_id=None,
            email=adres,
            full_name=full_name.strip() or None,
            password_hash=hash_password(password),
            role="viewer",
            is_superadmin=False,
            is_global_viewer=True,
        )
    )
    audit(db, None, action="user.created", target=adres,
          detail={"zakres": "wszystkie firmy", "tryb": "tylko odczyt"},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s utworzyl audytora globalnego %s", user.email, adres)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/haslo")
def ustaw_haslo_konta(
    user_id: str,
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Ustawia haslo dowolnemu kontu panelu - takze administratorowi firmy.

    Bez tego jedyna reakcja na zapomniane haslo administratora firmy bylo
    zalozenie mu drugiego konta, a stare zostawalo aktywne. Nowego hasla nie
    wyswietlamy ani nie zapisujemy - wpisuje je administrator i przekazuje
    wlascicielowi konta osobno; w bazie zostaje wylacznie skrot Argon2id.

    W dzienniku audytu zostaje sam fakt zmiany. Kto zmienil komu haslo, jest
    informacja, ktora musi byc widoczna - sama zmiana nie.
    """
    sprawdz_csrf(user, csrf_token)
    konto = db.get(PortalUser, user_id)
    if konto is None:
        raise HTTPException(status_code=404, detail="nie znaleziono konta")
    if len(password) < MIN_DLUGOSC_HASLA:
        raise HTTPException(
            status_code=400,
            detail=f"haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow",
        )

    konto.session_version = PortalUser.session_version + 1
    konto.password_hash = hash_password(password)
    audit(db, None, action="haslo.ustawione_przez_admina", target=konto.email,
          detail={"tenant": konto.tenant.slug if konto.tenant else None},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s ustawil nowe haslo konta %s", user.email, konto.email)
    return RedirectResponse("/admin/firmy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tenants/{tenant_id}/tokens")
def wydaj_token(
    tenant_id: str,
    request: Request,
    name: str = Form("token rejestracyjny"),
    expires_days: int = Form(0),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)

    token = generate_token("ent")
    db.add(
        EnrollmentToken(
            tenant_id=firma.id,
            name=name.strip() or "token rejestracyjny",
            prefix=token.prefix,
            token_hash=token.token_hash,
            expires_at=utcnow() + timedelta(days=expires_days) if expires_days > 0 else None,
            created_by=user.email,
        )
    )
    audit(db, None, action="token.created", target=firma.slug,
          detail={"prefix": token.prefix}, ip=client_ip(request), actor=user.email)
    db.commit()
    # Wartosc jawna pokazujemy raz - w bazie zostaje wylacznie skrot.
    return RedirectResponse(
        f"/admin/firmy?wydany_token={token.plaintext}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/tenants/{tenant_id}/ustawienia", response_class=HTMLResponse)
def widok_ustawien(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    firma = znajdz_firme(db, tenant_id)
    globalne = get_settings()
    return render_admin(
        request, "admin_ustawienia.html", user, "firmy",
        firma=firma,
        domyslne={
            "report_interval_seconds": globalne.report_interval_seconds,
            "stale_after_hours": globalne.stale_after_hours,
            "snapshot_retention": globalne.snapshot_retention,
        },
        maszyny=db.execute(
            select(func.count(Asset.id)).where(Asset.tenant_id == firma.id)
        ).scalar_one(),
    )


@router.post("/tenants/{tenant_id}/ustawienia")
def zapisz_ustawienia(
    tenant_id: str,
    request: Request,
    name: str = Form(...),
    slug: str = Form(...),
    report_interval_hours: str = Form(""),
    stale_after_hours: str = Form(""),
    snapshot_retention: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Zmiana danych i ustawien firmy.

    Puste pole liczbowe znaczy "jak w konfiguracji serwera" - nie zapisujemy
    wtedy wartosci domyslnej, bo zmiana globalna przestalaby dzialac dla firm,
    ktore nigdy swiadomie niczego nie ustawily.
    """
    sprawdz_csrf(user, csrf_token)
    firma = znajdz_firme(db, tenant_id)

    nowy_slug = slug.strip().lower()
    if not SLUG_RE.match(nowy_slug):
        raise HTTPException(
            status_code=400,
            detail="identyfikator moze zawierac male litery, cyfry i myslnik (2-63 znaki)",
        )
    if nowy_slug != firma.slug:
        zajety = db.execute(
            select(Tenant).where(Tenant.slug == nowy_slug, Tenant.id != firma.id)
        ).scalar_one_or_none()
        if zajety is not None:
            raise HTTPException(status_code=400, detail="ten identyfikator jest juz zajety")

    def liczba(wartosc: str, nazwa: str, minimum: int, maksimum: int) -> int | None:
        tekst = wartosc.strip()
        if not tekst:
            return None
        try:
            liczbowa = int(tekst)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{nazwa}: podaj liczbe") from None
        if not minimum <= liczbowa <= maksimum:
            raise HTTPException(
                status_code=400,
                detail=f"{nazwa}: wartosc musi miescic sie w zakresie {minimum}-{maksimum}",
            )
        return liczbowa

    godziny = liczba(report_interval_hours, "czestotliwosc raportowania", 1, 168)
    prog = liczba(stale_after_hours, "prog braku kontaktu", 1, 8760)
    retencja = liczba(snapshot_retention, "retencja raportow", 0, 10000)

    poprzednie = {"slug": firma.slug, "name": firma.name}
    firma.name = name.strip() or firma.name
    firma.slug = nowy_slug
    firma.report_interval_seconds = godziny * 3600 if godziny else None
    firma.stale_after_hours = prog
    firma.snapshot_retention = retencja
    firma.notes = notes.strip() or None

    audit(db, None, action="tenant.settings_changed", target=firma.slug,
          detail={"z": poprzednie, "interwal_h": godziny, "prog_h": prog, "retencja": retencja},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s zmienil ustawienia firmy %s", user.email, firma.slug)
    return RedirectResponse(
        f"/admin/tenants/{firma.id}/ustawienia", status_code=status.HTTP_303_SEE_OTHER
    )


# --- wersje agenta ----------------------------------------------------------

@router.get("/wersje", response_class=HTMLResponse)
def widok_wersji(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    wszystkie, wersje, wedlug_systemu = _wydania(db)

    from ..services.release_trust import admission_problem
    release_problems = {release.id: admission_problem(release) for release in wszystkie}
    release_admitted = {release_id: problem is None for release_id, problem in release_problems.items()}

    cele: dict[str, dict[str, AgentRelease]] = {}
    for cel in db.execute(select(TenantAgentTarget)).scalars():
        wydanie = wersje.get(cel.release_id)
        if wydanie is not None:
            cele.setdefault(cel.tenant_id, {})[cel.os_family] = wydanie

    rozklad, _, _ = _statystyki_agentow(db)
    firmy = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()

    # Systemy faktycznie obecne u kazdej firmy - nie ma sensu proponowac celu
    # dla Linuksa firmie, ktora ma same maszyny z Windows.
    systemy_firmy = {
        firma.id: sorted({p["os_family"] for p in rozklad.get(firma.id, [])})
        for firma in firmy
    }

    return render_admin(
        request, "admin_wersje.html", user, "wersje", release_admitted=release_admitted,
        release_problems=release_problems,
        import_enabled=get_settings().release_import_enabled,
        import_status=db.get(ReleaseImportStatus, 1),
        import_repository=get_settings().release_repository,
        etykiety_arch=architektura.ETYKIETY,
        wydania=wszystkie,
        wydania_wg_systemu=wedlug_systemu,
        oficjalne=_oficjalne(db, wersje),
        cele=cele,
        firmy=firmy,
        systemy_firmy=systemy_firmy,
        systemy=SYSTEMY,
        # Agent dla Linuksa jest wydawany jako zrodla, a nie plik
        # wykonywalny, wiec nie ma go w magazynie wydan. Bez pokazania go
        # tutaj strona sugerowalaby, ze zadnego agenta dla Linuksa nie ma.
        paczka_zrodel=pakiet.opis(pakiet.katalog_paczki()),
        aplikacja=mobilna.opis(),
    )


@router.get("/podatnosci", response_class=HTMLResponse)
def strona_podatnosci(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Stan kanalow danych o podatnosciach i wydania w uzyciu we flocie."""
    kanaly = db.execute(select(CveFeed).order_by(CveFeed.source, CveFeed.release)).scalars().all()
    return render_admin(
        request, "admin_podatnosci.html", user, "podatnosci",
        kanaly=kanaly,
        wydania=cve.wydania_we_flocie(db),
        przestarzaly_po=cve.PRZESTARZALY_PO_GODZINACH,
        co_ile_godzin=get_settings().cve_refresh_hours,
    )


@router.post("/podatnosci/odswiez")
def odswiez_podatnosci(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Pobiera kanaly dla wydan faktycznie uzywanych we flocie.

    Pobieramy tylko to, co jest w uzyciu - kanal Debiana ma 86 MB, a dane dla
    wydania, ktorego nikt nie uzywa, sa czystym obciazeniem.
    """
    sprawdz_csrf(user, csrf_token)
    podsumowanie = cve.odswiez(db, cve.wydania_we_flocie(db))
    audit(db, None, action="cve.refreshed", target=", ".join(sorted(podsumowanie)),
          detail=podsumowanie, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s odswiezyl kanaly podatnosci: %s", user.email, podsumowanie)
    return RedirectResponse("/admin/podatnosci", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/podatnosci/oceny")
def pobierz_oceny_cvss(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Uzupelnia oceny CVSS dla podatnosci znalezionych na maszynach.

    Pobieramy je z NVD, bo dane dystrybucji ich nie zawieraja - Debian ma
    wlasna skale, Ubuntu nie podaje zadnej. Tylko dla podatnosci faktycznie
    dopasowanych: sciaganie ocen dla calego kanalu to dziesiatki tysiecy
    zapytan po to, by opisac luki, ktorych u nikogo nie ma.
    """
    sprawdz_csrf(user, csrf_token)
    podsumowanie = cve.pobierz_oceny(
        db, cve.cve_we_flocie(db), klucz_api=get_settings().nvd_api_key
    )
    ubuntu = cve.cve_we_flocie(db, source="ubuntu")
    if ubuntu:
        podsumowanie["ubuntu"] = cve.pobierz_oceny_ubuntu(db, ubuntu)
    audit(db, None, action="cve.scores", target=str(podsumowanie.get("pobrane")),
          detail=podsumowanie, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s pobral oceny CVSS: %s", user.email, podsumowanie)
    return RedirectResponse("/admin/podatnosci", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/releases")
async def wgraj_wersje(
    request: Request,
    version: str = Form(""),
    os_family: str = Form("windows"),
    notes: str = Form(""),
    plik: UploadFile = File(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Wgrywa plik agenta dla wskazanego systemu i liczy jego skrot.

    Skrot jest sercem calego mechanizmu: agent porownuje go po pobraniu
    i odmawia podmiany, gdy sie nie zgadza. Liczymy go strumieniowo, zeby
    trzydziestomegabajtowy plik nie ladowal w calosci do pamieci.
    """
    sprawdz_csrf(user, csrf_token)
    settings = get_settings()

    system = os_family.strip().lower()
    if settings.release_import_enabled:
        raise HTTPException(403, "Wlaczony automatyczny import: reczny upload wydan jest zablokowany")
    if system not in SYSTEMY:
        raise HTTPException(status_code=400, detail="nieznany system operacyjny")

    podany = version.strip()
    katalog = katalog_wersji()
    tymczasowy = katalog / f".wgrywanie-{utcnow().timestamp()}"
    skrot = hashlib.sha256()
    rozmiar = 0

    try:
        with tymczasowy.open("wb") as wyjscie:
            while fragment := await plik.read(1024 * 1024):
                rozmiar += len(fragment)
                if rozmiar > settings.max_release_bytes:
                    raise HTTPException(status_code=413, detail="plik przekracza dozwolony rozmiar")
                skrot.update(fragment)
                wyjscie.write(fragment)

        if rozmiar == 0:
            raise HTTPException(status_code=400, detail="pusty plik")

        # Agent dla Linuksa moze byc paczka zrodel zamiast pliku wykonywalnego.
        # Jedna taka paczka obsluguje kazda architekture, bo agent stoi na samej
        # bibliotece standardowej - stad osobna "architektura" o nazwie zrodla.
        if system == "linux" and pakiet.czy_paczka_zrodel(tymczasowy):
            try:
                wykryta = pakiet.sprawdz_paczke(tymczasowy)
            except pakiet.BrakZrodel as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            arch = pakiet.ARCH_ZRODLA
            numer = wykryta or podany
            wykryty_system = "linux"
        else:
            # Sygnatura odrzuca pomylke juz tutaj, a nie dopiero na maszynie
            # klienta, gdzie plik po prostu nie chcialby sie uruchomic.
            oczekiwana = SYSTEMY[system]["sygnatura"]
            with tymczasowy.open("rb") as sprawdzany:
                if sprawdzany.read(len(oczekiwana)) != oczekiwana:
                    raise HTTPException(
                        status_code=400,
                        detail=f"plik nie jest programem dla {SYSTEMY[system]['etykieta']} "
                               f"({SYSTEMY[system]['opis']})",
                    )

            metadane = odczytaj_metadane(tymczasowy) or {}
            wykryta = str(metadane.get("version") or "").strip()
            numer = wykryta or podany
            wykryty_system = str(metadane.get("os_family") or "").strip().lower()

            # Architekture czytamy z naglowka pliku - to fakt, a nie deklaracja.
            # Bez tego wersja zbudowana na serwerze x86 trafialaby na kazde
            # Raspberry Pi jako plik nie do uruchomienia.
            arch = architektura.wykryj_z_pliku(tymczasowy)
            if arch is None:
                raise HTTPException(
                    status_code=400,
                    detail="nie rozpoznaje architektury pliku - czy to na pewno program?",
                )

            # A footer is not proof of worker support. Only independently
            # approved, tested bytes may be classified as a windowed worker.
            from ..services.release_trust import trusted_build
            if system == "windows" and architektura.czy_okienkowy(tymczasowy):
                if not trusted_build(skrot.hexdigest(), numer, arch):
                    if metadane.get("entry_mode") == "unified":
                        raise HTTPException(400, "Niezweryfikowany worker Windows: stopka unified nie wystarcza. "
                            "Wymagany SHA-256 sprawdzonego buildu w konfiguracji serwera.")
                    arch = architektura.ARCH_TRAY

        if not numer:
            raise HTTPException(
                status_code=400,
                detail="plik nie zawiera numeru wersji - zbuduj go aktualnym "
                       "build-agent.ps1 albo podaj numer recznie",
            )
        if not WERSJA_RE.fullmatch(numer):
            raise HTTPException(status_code=400, detail="niepoprawny numer wersji")
        # Numer podany recznie nie moze byc sprzeczny z tym z pliku - taka
        # rozbieznosc konczylaby sie aktualizacja proponowana bez konca.
        if wykryta and podany and wykryta != podany:
            raise HTTPException(
                status_code=400,
                detail=f"plik zglasza wersje {wykryta}, a podano {podany}",
            )
        if wykryty_system and wykryty_system != system:
            raise HTTPException(
                status_code=400,
                detail=f"plik zbudowano dla {wykryty_system}, a wybrano "
                       f"{SYSTEMY[system]['etykieta']}",
            )
        if db.execute(
            select(AgentRelease).where(
                AgentRelease.version == numer,
                AgentRelease.os_family == system,
                AgentRelease.arch == arch,
            )
        ).scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=400,
                detail=f"wersja {numer} dla {SYSTEMY[system]['etykieta']} "
                       f"({architektura.etykieta(arch)}) juz istnieje",
            )

        odcisk = skrot.hexdigest()
        rozszerzenie = "tar.gz" if arch == pakiet.ARCH_ZRODLA else "bin"
        nazwa_w_magazynie = f"{system}-{arch}-{odcisk}.{rozszerzenie}"
        tymczasowy.replace(katalog / nazwa_w_magazynie)
    except Exception:
        tymczasowy.unlink(missing_ok=True)
        raise

    db.add(
        AgentRelease(
            version=numer,
            os_family=system,
            arch=arch,
            filename=plik.filename or f"cmdb-agent-{numer}",
            storage_name=nazwa_w_magazynie,
            sha256=odcisk,
            size_bytes=rozmiar,
            notes=notes.strip() or None,
            created_by=user.email,
        )
    )
    audit(db, None, action="release.uploaded", target=f"{numer} ({system}/{arch})",
          detail={"sha256": odcisk, "size": rozmiar, "os_family": system, "arch": arch},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s wgral agenta %s dla %s/%s (%s, wersja %s)",
             user.email, numer, system, arch, odcisk[:16],
             "odczytana z pliku" if wykryta else "podana recznie")
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/releases/{release_id}/oficjalna")
def ustaw_oficjalna(
    release_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Oznacza wersje jako aktywna dla jej systemu.

    Firmy bez wlasnego ustawienia korzystaja wlasnie z niej. Tabela ma
    UNIQUE(os_family), wiec dwie wersje tego samego systemu nie moga byc
    oficjalne naraz - podmieniamy wskazanie, zamiast dokladac drugie.
    """
    sprawdz_csrf(user, csrf_token)
    wydanie = db.get(AgentRelease, release_id)
    if wydanie is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wersji")

    from ..services.release_trust import admission_problem
    problem = admission_problem(wydanie)
    if problem:
        raise HTTPException(400, f"Nie można aktywować wydania: {problem}")

    cel = db.execute(
        select(GlobalAgentTarget).where(GlobalAgentTarget.os_family == wydanie.os_family)
    ).scalar_one_or_none()
    if cel is None:
        db.add(
            GlobalAgentTarget(
                os_family=wydanie.os_family, release_id=wydanie.id, updated_by=user.email
            )
        )
    else:
        cel.release_id = wydanie.id
        cel.updated_at = utcnow()
        cel.updated_by = user.email

    audit(db, None, action="release.official", target=f"{wydanie.version} ({wydanie.os_family})",
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s uczynil %s oficjalna dla %s",
             user.email, wydanie.version, wydanie.os_family)
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


# --- aplikacja Android ------------------------------------------------------

@router.post("/mobilna")
async def wgraj_aplikacje(
    request: Request,
    version: str = Form(""),
    plik: UploadFile = File(...),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Wgrywa APK, ktore portal wydaje technikom kodem QR.

    Czytamy strumieniowo i liczymy skrot po drodze - dwudziestomegabajtowy plik
    nie ma powodu ladowac w calosci do pamieci. Numer wersji bierzemy z pola
    formularza, a gdy jest puste, z nazwy pliku ("CMDB-Mobile-0.5.0-debug.apk"):
    w samym APK numer siedzi w skompilowanym manifescie i jego rozbieranie
    byloby osobna biblioteka.
    """
    sprawdz_csrf(user, csrf_token)
    settings = get_settings()

    wersja_apk = version.strip() or mobilna.wersja_z_nazwy(plik.filename)
    if not wersja_apk:
        raise HTTPException(status_code=400, detail="podaj numer wersji - nie ma go w nazwie pliku")
    if len(wersja_apk) > 32:
        raise HTTPException(status_code=400, detail="numer wersji jest za dlugi")

    katalog = mobilna.katalog()
    tymczasowy = katalog / f".wgrywanie-{utcnow().timestamp()}"
    skrot = hashlib.sha256()
    rozmiar = 0
    try:
        with tymczasowy.open("wb") as wyjscie:
            while fragment := await plik.read(1024 * 1024):
                rozmiar += len(fragment)
                if rozmiar > settings.max_release_bytes:
                    raise HTTPException(status_code=413, detail="plik przekracza dozwolony rozmiar")
                skrot.update(fragment)
                wyjscie.write(fragment)
        if rozmiar == 0:
            raise HTTPException(status_code=400, detail="pusty plik")

        try:
            aplikacja = mobilna.przyjmij(
                tymczasowy, wersja=wersja_apk, sha256=skrot.hexdigest(), wgral=user.email,
            )
        except mobilna.BladAplikacji as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad
    finally:
        tymczasowy.unlink(missing_ok=True)

    audit(db, None, action="aplikacja.wgrana", target=aplikacja.wersja,
          detail={"sha256": aplikacja.sha256, "rozmiar": aplikacja.rozmiar},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s wgral aplikacje Android %s (%d B)",
             user.email, aplikacja.wersja, aplikacja.rozmiar)
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/mobilna/usun")
def usun_aplikacje(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Zdejmuje APK z portalu - kod QR znika razem z plikiem."""
    sprawdz_csrf(user, csrf_token)
    poprzednia = mobilna.opis()
    if not mobilna.usun():
        raise HTTPException(status_code=404, detail="na tym serwerze nie ma pliku aplikacji")
    audit(db, None, action="aplikacja.usunieta",
          target=poprzednia.wersja if poprzednia else "?",
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/releases/{release_id}/delete")
def usun_wersje(
    release_id: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    sprawdz_csrf(user, csrf_token)
    wydanie = db.get(AgentRelease, release_id)
    if wydanie is None:
        raise HTTPException(status_code=404, detail="nie znaleziono wersji")

    uzywana = (
        db.execute(
            select(func.count(GlobalAgentTarget.id)).where(
                GlobalAgentTarget.release_id == release_id
            )
        ).scalar_one()
        + db.execute(
            select(func.count(TenantAgentTarget.id)).where(
                TenantAgentTarget.release_id == release_id
            )
        ).scalar_one()
        + db.execute(
            select(func.count(Asset.id)).where(Asset.target_release_id == release_id)
        ).scalar_one()
    )
    if uzywana:
        raise HTTPException(
            status_code=400,
            detail="wersja jest gdzies ustawiona jako docelowa - najpierw zmien cel",
        )

    (katalog_wersji() / wydanie.storage_name).unlink(missing_ok=True)
    if wydanie.provenance and wydanie.provenance.setup_storage_name:
        (katalog_wersji() / wydanie.provenance.setup_storage_name).unlink(missing_ok=True)
    db.delete(wydanie)
    audit(db, None, action="release.deleted", target=f"{wydanie.version} ({wydanie.os_family})",
          ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse("/admin/wersje", status_code=status.HTTP_303_SEE_OTHER)


# --- zlecanie aktualizacji --------------------------------------------------

@router.get("/releases/{release_id}/setup")
def pobierz_setup(release_id: str, user: PortalUser = Depends(require_superadmin), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    from ..services.release_trust import verified_artifact, distributable
    from ..services.release_import import check_bytes, ImportFailure
    wydanie = db.get(AgentRelease, release_id)
    if wydanie is None or not distributable(wydanie):
        raise HTTPException(404, "Brak zatwierdzonego wydania")
    artifact = verified_artifact(wydanie, "setup")
    evidence = wydanie.provenance
    if not artifact or not evidence or not evidence.setup_storage_name:
        raise HTTPException(404, "Brak instalatora")
    root = katalog_wersji().resolve()
    path = root / evidence.setup_storage_name
    if path.resolve().parent != root:
        raise HTTPException(404, "Brak instalatora")
    try:
        check_bytes(path, artifact)
    except (OSError, ImportFailure):
        raise HTTPException(404, "Brak poprawnego instalatora")
    return FileResponse(path, filename=artifact["name"], media_type="application/octet-stream")

@router.get("/tenants/{tenant_id}/upgrade", response_class=HTMLResponse)
def widok_aktualizacji(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    firma = znajdz_firme(db, tenant_id)
    # Wpisow recznych tu nie ma: nie da sie zlecic aktualizacji agenta
    # maszynie, na ktorej agenta nie ma. Na liscie wygladalyby jak sprzet
    # zalegajacy z aktualizacja.
    maszyny = db.execute(
        select(Asset)
        .where(Asset.tenant_id == firma.id, Asset.lifecycle == LIFECYCLE_AKTYWNY,
               Asset.zrodlo == ZRODLO_AGENT)
        .order_by(Asset.os_family, Asset.hostname)
    ).scalars().all()

    wszystkie, wersje, wedlug_systemu = _wydania(db)
    cele = {
        cel.os_family: wersje.get(cel.release_id)
        for cel in db.execute(
            select(TenantAgentTarget).where(TenantAgentTarget.tenant_id == firma.id)
        ).scalars()
    }

    obecne: list[str] = []
    for maszyna in maszyny:
        system = (maszyna.os_family or "nieznany").lower()
        if system not in obecne:
            obecne.append(system)

    return render_admin(
        request, "admin_upgrade.html", user, "wersje",
        firma=firma,
        maszyny=maszyny,
        wersje=wersje,
        wydania=wszystkie,
        wydania_wg_systemu=wedlug_systemu,
        cele=cele,
        oficjalne=_oficjalne(db, wersje),
        obecne_systemy=obecne,
        systemy=SYSTEMY,
    )


@router.post("/tenants/{tenant_id}/upgrade")
async def zlec_aktualizacje(
    tenant_id: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Ustawia wersje docelowa dla systemu w calej firmie albo dla wybranych maszyn.

    Nie wysylamy niczego na maszyny - to agent przy swoim cyklicznym przebiegu
    dowiaduje sie, ze oczekiwana jest inna wersja, i sam ja pobiera. Serwer
    nigdy nie inicjuje polaczenia do agenta.
    """
    formularz = await request.form()
    sprawdz_csrf(user, formularz.get("csrf_token"))

    firma = znajdz_firme(db, tenant_id)
    zakres = formularz.get("zakres", "wybrane")
    release_id = (formularz.get("release_id") or "").strip()
    powrot = formularz.get("powrot") or f"/admin/tenants/{firma.id}/upgrade"

    try:
        wydanie = upgrades.wydanie_do_rozeslania(db, release_id)
        if zakres == "firma":
            objete = upgrades.ustaw_cel_firmy(
                db, firma.id, formularz.get("os_family") or "", wydanie, user.email
            )
        else:
            objete = upgrades.ustaw_cel_maszyn(
                db, firma.id, formularz.getlist("asset_id"), wydanie
            )
    except upgrades.BladCelu as blad:
        raise HTTPException(status_code=400, detail=str(blad)) from blad

    audit(db, None, action="agent.upgrade_requested", target=firma.slug,
          detail={"wersja": wydanie.version if wydanie else None, "zakres": objete},
          ip=client_ip(request), actor=user.email)
    db.commit()
    log.info(
        "superadmin %s zlecil wersje %s dla %s w firmie %s",
        user.email, wydanie.version if wydanie else "(brak)", objete, firma.slug,
    )
    return RedirectResponse(powrot, status_code=status.HTTP_303_SEE_OTHER)


# --- konta w jednym miejscu -------------------------------------------------

# Rodzaje kont opisane tak, jak wyglada ich uprawnienie, a nie jak sa zapisane
# w kolumnach. Konto "technik" to konto bez wlasnej firmy z wpisami w
# helpdesk_dostepy - i dopoki nie widac tego w jednym miejscu, takie konto po
# nadaniu mu firm znikalo z listy kont firmowych i nie dalo sie go usunac.
ZAKRES_FIRMA = "firma"
ZAKRES_TECHNIK = "technik"
ZAKRES_AUDYTOR = "audytor"
ZAKRES_SUPERADMIN = "superadmin"
ZAKRESY = {
    ZAKRES_FIRMA: "konto firmy",
    ZAKRES_TECHNIK: "technik helpdesku",
    ZAKRES_AUDYTOR: "audytor globalny",
    ZAKRES_SUPERADMIN: "superadmin",
}

# Ktore pola formularza w ogole dotycza danego rodzaju konta. Superadmin nie ma
# firmy, audytor nie ma roli (nigdzie nie zapisuje), technik dostaje firmy
# w osobnej kolumnie - pokazywanie tych pol zawsze sugerowalo, ze cos znacza.
POLA_ZAKRESU = {
    ZAKRES_FIRMA: "firma rola",
    ZAKRES_TECHNIK: "",
    ZAKRES_AUDYTOR: "",
    ZAKRES_SUPERADMIN: "",
}

OPISY_ZAKRESU = {
    ZAKRES_FIRMA:
        "Pracuje w jednej firmie. Rola decyduje, czy w niej zapisuje, czy tylko ogląda.",
    ZAKRES_TECHNIK:
        "Nie należy do żadnej firmy. Obsługuje te, które dostanie w kolumnie "
        "„Firmy helpdesku” — w każdej z nich pracuje z prawami jej operatora, "
        "także w CMDB.",
    ZAKRES_AUDYTOR:
        "Ogląda wszystkie firmy i nie zmienia w nich niczego. Roli się nie ustawia, "
        "bo prawo zapisu tego konta nie powstaje nigdzie.",
    ZAKRES_SUPERADMIN:
        "Prowadzi cały system: firmy, konta, wydania agenta i skrzynkę helpdesku. "
        "Widzi wszystkie firmy bez przydzielania.",
}


def _zakres_konta(konto: PortalUser, firmy_helpdesku: set[str]) -> str:
    if konto.is_superadmin:
        return ZAKRES_SUPERADMIN
    if konto.is_global_viewer:
        return ZAKRES_AUDYTOR
    if firmy_helpdesku:
        return ZAKRES_TECHNIK
    return ZAKRES_FIRMA


@router.get("/konta", response_class=HTMLResponse)
def widok_kont(
    request: Request,
    komunikat: str = "",
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Wszystkie konta panelu na jednej liscie.

    Konta byly dotad pokazywane pod firmami, a technik helpdesku do zadnej
    firmy nie nalezy - po nadaniu mu firm znikal z widoku i nie dalo sie go
    ani wylaczyc, ani usunac. Ta lista pokazuje kazde konto niezaleznie od
    tego, skad bierze sie jego uprawnienie.
    """
    from ..models import CzasPracy, HelpdeskDostep

    dostepy: dict[str, list[str]] = {}
    for user_id, tenant_id in db.execute(
        select(HelpdeskDostep.user_id, HelpdeskDostep.tenant_id)
    ).all():
        dostepy.setdefault(user_id, []).append(tenant_id)

    nazwy_firm = dict(db.execute(select(Tenant.id, Tenant.name)).all())
    czasy = dict(db.execute(
        select(CzasPracy.technik_id, func.count(CzasPracy.id)).group_by(CzasPracy.technik_id)
    ).all())

    konta = []
    for konto in db.execute(
        select(PortalUser).order_by(PortalUser.email)
    ).scalars():
        firmy_konta = dostepy.get(konto.id, [])
        konta.append({
            "konto": konto,
            "zakres": _zakres_konta(konto, set(firmy_konta)),
            "firma": nazwy_firm.get(konto.tenant_id),
            "firmy_helpdesku": sorted(
                (nazwy_firm.get(t, t), t) for t in firmy_konta
            ),
            # Konto z zapisanym czasem pracy da sie wylaczyc, ale nie usunac -
            # pokazujemy to od razu, zeby nie proponowac czegos, co odmowi.
            "wpisow_czasu": czasy.get(konto.id, 0),
            # Audytor globalny nigdzie nie zapisuje, wiec firmy helpdesku nic mu
            # nie daja. Taki stan da sie odziedziczyc po starszych danych i lepiej
            # go nazwac, niz pozwolic komus liczyc, ze technik dziala.
            "niespojne": bool(firmy_konta) and konto.is_global_viewer,
        })

    return render_admin(
        request, "admin_konta.html", user, "konta",
        konta=konta,
        firmy=db.execute(select(Tenant).order_by(Tenant.name)).scalars().all(),
        zakresy=ZAKRESY,
        pola_zakresu=POLA_ZAKRESU,
        opisy_zakresu=OPISY_ZAKRESU,
        min_dlugosc_hasla=MIN_DLUGOSC_HASLA,
        komunikat=komunikat,
    )


@router.post("/konta")
def utworz_konto_dowolne(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    zakres: str = Form(ZAKRES_FIRMA),
    tenant_id: str = Form(""),
    role: str = Form("admin"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Zaklada konto dowolnego rodzaju - takze technika helpdesku od razu z firmami.

    Technika nie dalo sie dotad zalozyc wcale: panel umial tworzyc konto firmy
    albo audytora globalnego, a technik jest kontem bez firmy, ktore dostaje
    ich kilka.
    """
    sprawdz_csrf(user, csrf_token)
    adres = email.strip().lower()
    if len(password) < MIN_DLUGOSC_HASLA:
        raise HTTPException(
            status_code=400,
            detail=f"haslo musi miec co najmniej {MIN_DLUGOSC_HASLA} znakow",
        )
    if db.execute(select(PortalUser).where(PortalUser.email == adres)).scalar_one_or_none():
        raise HTTPException(status_code=400, detail="konto o tym adresie juz istnieje")
    if zakres not in ZAKRESY:
        raise HTTPException(status_code=400, detail="nieznany rodzaj konta")
    if role not in {"admin", "viewer"}:
        raise HTTPException(status_code=400, detail="nieznana rola")

    konto = PortalUser(
        email=adres,
        full_name=full_name.strip() or None,
        password_hash=hash_password(password),
        role=role,
        tenant_id=None,
        is_superadmin=(zakres == ZAKRES_SUPERADMIN),
        is_global_viewer=(zakres == ZAKRES_AUDYTOR),
    )
    if zakres == ZAKRES_FIRMA:
        konto.tenant_id = znajdz_firme(db, tenant_id).id
    if zakres == ZAKRES_AUDYTOR:
        # Audytor niczego nie zmienia w zadnej firmie - rola opisuje, co wolno
        # W firmie, a to jest zgoda na ogladanie wszystkich.
        konto.role = "viewer"

    db.add(konto)
    db.flush()
    audit(db, None, action="user.created", target=adres,
          detail={"zakres": zakres, "role": konto.role}, ip=client_ip(request), actor=user.email)
    db.commit()
    log.info("superadmin %s zalozyl konto %s (%s)", user.email, adres, zakres)
    komunikat = f"Konto {adres} założone jako {ZAKRESY[zakres]}."
    if zakres == ZAKRES_TECHNIK:
        komunikat += " Przydziel mu firmy — bez nich nie widzi żadnych zgłoszeń."
    return RedirectResponse(
        f"/admin/konta?komunikat={quote(komunikat)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/users/{user_id}/zakres")
def zmien_zakres_konta(
    user_id: str,
    request: Request,
    zakres: str = Form(ZAKRES_FIRMA),
    tenant_id: str = Form(""),
    role: str = Form("admin"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Zmienia rodzaj konta i jego role.

    Zmiana uprawnien uniewaznia zalogowane sesje tego konta: inaczej odebrane
    prawo dzialaloby az do wygasniecia ciasteczka, a nadane - dopiero po
    ponownym zalogowaniu.
    """
    sprawdz_csrf(user, csrf_token)
    konto = _konto_do_zmiany(db, user, user_id)
    if zakres not in ZAKRESY:
        raise HTTPException(status_code=400, detail="nieznany rodzaj konta")
    if role not in {"admin", "viewer"}:
        raise HTTPException(status_code=400, detail="nieznana rola")

    konto.is_superadmin = zakres == ZAKRES_SUPERADMIN
    konto.is_global_viewer = zakres == ZAKRES_AUDYTOR
    konto.role = "viewer" if zakres == ZAKRES_AUDYTOR else role
    konto.tenant_id = znajdz_firme(db, tenant_id).id if zakres == ZAKRES_FIRMA else None
    konto.session_version = PortalUser.session_version + 1

    audit(db, None, action="user.role_changed", target=konto.email,
          detail={"zakres": zakres, "role": konto.role}, ip=client_ip(request), actor=user.email)
    db.commit()
    komunikat = f"{konto.email}: {ZAKRESY[zakres]}. Konto musi zalogować się ponownie."
    return RedirectResponse(
        f"/admin/konta?komunikat={quote(komunikat)}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/users/{user_id}/helpdesk")
def zmien_firmy_technika(
    user_id: str,
    request: Request,
    tenant_id: str = Form(""),
    akcja: str = Form("nadaj"),
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Nadaje albo odbiera kontu firme helpdesku - z listy kont.

    To samo, co robi ekran helpdesku, ale tutaj obok reszty uprawnien konta:
    firmy technika sa jego uprawnieniem, wiec maja stac tam, gdzie sie je
    oglada i odbiera.
    """
    from ..services import helpdesk

    sprawdz_csrf(user, csrf_token)
    konto = db.get(PortalUser, user_id)
    firma = db.get(Tenant, tenant_id) if tenant_id else None
    if konto is None or firma is None:
        raise HTTPException(status_code=400, detail="wskaz konto i firme")

    if akcja == "odbierz":
        helpdesk.odbierz_dostep(db, konto.id, firma.id)
        komunikat = f"{konto.email} nie obsługuje już firmy {firma.name}. Historia zostaje."
    else:
        try:
            helpdesk.nadaj_dostep(db, konto.id, firma.id, nadal=user.email)
        except helpdesk.BladHelpdesku as blad:
            raise HTTPException(status_code=400, detail=str(blad)) from blad
        komunikat = f"{konto.email} obsługuje firmę {firma.name} — zgłoszenia i CMDB."

    audit(db, None, action="helpdesk.dostep", target=konto.email,
          detail={"firma": firma.slug, "akcja": akcja}, ip=client_ip(request), actor=user.email)
    db.commit()
    return RedirectResponse(
        f"/admin/konta?komunikat={quote(komunikat)}", status_code=status.HTTP_303_SEE_OTHER
    )


# --- audyt globalny ---------------------------------------------------------

# Podkreslnik nie moze zaczynac sluga firmy (SLUG_RE), wiec wartosc nie koliduje.
AUDYT_SYSTEMOWE = "_systemowe"


@router.get("/audyt", response_class=HTMLResponse)
def widok_audytu(
    request: Request,
    # Pusta wartosc - wszystkie firmy, SYSTEMOWE - zdarzenia bez firmy
    # (logowania superadmina, operacje globalne), inaczej slug firmy.
    # Portal firmowy kieruje tu z filtrem ustawionym na biezaca firme.
    firma: str = Query("", max_length=100),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    firmy = list(db.execute(select(Tenant).order_by(Tenant.name)).scalars())
    zapytanie = select(AuditLog)
    wybrana = None
    if firma == AUDYT_SYSTEMOWE:
        zapytanie = zapytanie.where(AuditLog.tenant_id.is_(None))
    elif firma:
        wybrana = next((f for f in firmy if f.slug == firma), None)
        if wybrana is None:
            # Nieznana firma to pusty wynik, nie "wszystkie" - inaczej literowka
            # w adresie po cichu pokazalaby zdarzenia wszystkich firm.
            zapytanie = zapytanie.where(false())
        else:
            zapytanie = zapytanie.where(AuditLog.tenant_id == wybrana.id)
    wpisy = db.execute(
        zapytanie.order_by(AuditLog.created_at.desc()).limit(300)
    ).scalars().all()
    return render_admin(
        request, "admin_audyt.html", user, "audyt", wpisy=wpisy,
        nazwy_firm={f.id: f.name for f in firmy}, firmy=firmy, firma=firma,
        wybrana=wybrana, systemowe=AUDYT_SYSTEMOWE,
    )


# --- kopie zapasowe ---------------------------------------------------------
#
# Caly ten rozdzial nalezy do superadmina i tylko do niego: archiwum zawiera
# dane WSZYSTKICH firm naraz, a przywracanie zastepuje baze.
#
# Panel nie odtwarza bazy sam. Uvicorn chodzi w kilku workerach, kazdy trzyma
# polaczenia, a w tle kreca sie petla IMAP, harmonogram i import wydan -
# pg_restore --clean musialby usunac tabele, ktore te sesje trzymaja otwarte.
# Dlatego panel UZBRAJA przywrocenie, a wykonuje je wejscie kontenera przy
# najblizszym starcie, zanim wstanie uvicorn.

@router.get("/kopie", response_class=HTMLResponse)
def widok_kopii(
    request: Request,
    komunikat: str = Query("", max_length=500),
    blad: str = Query("", max_length=500),
    user: PortalUser = Depends(require_superadmin),
) -> Response:
    from ..services import kopie

    wolne, ostatnia = kopie.miejsce_na_dysku()
    return render_admin(
        request, "admin_kopie.html", user, "kopie",
        kopie=kopie.lista(),
        uzbrojone=kopie.uzbrojone(),
        wolne_bajty=wolne,
        ostatnia_bajty=ostatnia,
        limit_wgrania=get_settings().max_kopia_bytes,
        komunikat=komunikat,
        blad=blad,
    )


def _wroc_do_kopii(komunikat: str = "", blad: str = "") -> RedirectResponse:
    pytanie = f"komunikat={quote(komunikat)}" if komunikat else f"blad={quote(blad)}"
    return RedirectResponse(
        f"/admin/kopie?{pytanie}" if (komunikat or blad) else "/admin/kopie",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/kopie")
def zrob_kopie(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Kopia na zadanie - ta sama funkcja, ktora wola nocne zadanie."""
    from ..services import kopie

    sprawdz_csrf(user, csrf_token)
    try:
        kopia = kopie.utworz("reczna", autor=user.email)
    except kopie.BladKopii as bledne:
        return _wroc_do_kopii(blad=f"Kopia się nie udała: {bledne}")

    audit(db, None, action="kopia.utworzona", target=kopia.nazwa,
          detail={"rozmiar": kopia.rozmiar}, ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc_do_kopii(
        f"Kopia {kopia.nazwa} gotowa ({kopia.rozmiar // 1048576} MB)."
    )


@router.get("/kopie/{nazwa}/pobierz")
def pobierz_kopie(
    nazwa: str,
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Archiwum do przegladarki. Strumieniem - plik bywa wiekszy od pamieci."""
    from ..services import kopie

    kopia = kopie.znajdz(nazwa)
    if kopia is None:
        raise HTTPException(status_code=404, detail="nie ma takiej kopii")

    # Pobranie kopii to wyniesienie calej bazy poza serwer - to sie audytuje
    # tak samo jak jej przywrocenie.
    audit(db, None, action="kopia.pobrana", target=kopia.nazwa,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return FileResponse(
        kopia.sciezka, filename=kopia.nazwa, media_type="application/gzip",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/kopie/{nazwa}/sprawdz")
def sprawdz_kopie(
    nazwa: str,
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Odtworzenie do bazy pomocniczej. Dzialajacej instalacji nie rusza.

    Kopia, ktorej nikt nigdy nie odtworzyl, to nie kopia, tylko plik. Tu
    sprawdzamy to bez ryzyka: pg_restore idzie do osobnej bazy, ktora zaraz
    potem znika.
    """
    from sqlalchemy import text

    from ..db import engine
    from ..services import kopie

    sprawdz_csrf(user, csrf_token)
    kopia = kopie.znajdz(nazwa)
    if kopia is None:
        raise HTTPException(status_code=404, detail="nie ma takiej kopii")

    baza = f"cmdb_sprawdzenie_{secrets.token_hex(4)}"
    # CREATE DATABASE nie dziala w transakcji, stad polaczenie w trybie
    # autocommit. Nazwa bazy pochodzi z secrets, wiec nie ma tu cudzego tekstu.
    polaczenie = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        polaczenie.execute(text(f'CREATE DATABASE "{baza}"'))
    except Exception as bledne:  # noqa: BLE001
        polaczenie.close()
        return _wroc_do_kopii(blad=f"Nie mogę założyć bazy pomocniczej: {bledne}")

    try:
        kopie.odtworz(kopia.sciezka, do_bazy=baza)
        wynik = _policz_w_bazie(baza)
        komunikat = (
            f"Kopia {kopia.nazwa} jest dobra — odtworzyła się, "
            f"{wynik['firmy']} firm, {wynik['maszyny']} maszyn, "
            f"{wynik['zgloszenia']} zgłoszeń."
        )
        odpowiedz = _wroc_do_kopii(komunikat)
    except kopie.BladKopii as bledne:
        odpowiedz = _wroc_do_kopii(blad=f"Kopia {kopia.nazwa} NIE nadaje się: {bledne}")
    finally:
        try:
            polaczenie.execute(text(f'DROP DATABASE IF EXISTS "{baza}" WITH (FORCE)'))
        except Exception as bledne:  # noqa: BLE001
            log.warning("nie usunalem bazy pomocniczej %s: %s", baza, bledne)
        polaczenie.close()

    audit(db, None, action="kopia.sprawdzona", target=kopia.nazwa,
          ip=client_ip(request), actor=user.email)
    db.commit()
    return odpowiedz


def _policz_w_bazie(nazwa_bazy: str) -> dict[str, int]:
    """Kilka liczb z odtworzonej bazy - dowod, ze w srodku sa dane."""
    from sqlalchemy import create_engine, text

    adres = get_settings().database_url.rsplit("/", 1)[0] + "/" + nazwa_bazy
    silnik = create_engine(adres, pool_pre_ping=False)
    liczby = {"firmy": 0, "maszyny": 0, "zgloszenia": 0}
    try:
        with silnik.connect() as polaczenie:
            for klucz, tabela in (("firmy", "tenants"), ("maszyny", "assets"),
                                  ("zgloszenia", "helpdesk_zgloszenia")):
                try:
                    liczby[klucz] = polaczenie.execute(
                        text(f"SELECT count(*) FROM {tabela}")  # noqa: S608 - stale nazwy
                    ).scalar_one()
                except Exception:  # noqa: BLE001 - brak tabeli to tez odpowiedz
                    liczby[klucz] = -1
    finally:
        silnik.dispose()
    return liczby


@router.post("/kopie/przywroc")
async def przywroc_kopie(
    request: Request,
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    """Uzbraja przywrocenie z listy albo z wgranego pliku.

    Haslo pytamy ponownie, mimo ze konto jest juz zalogowane. Zrzut Postgresa
    nie jest biernym plikiem z danymi, tylko skryptem wykonujacym SQL jako
    wlasciciel bazy - przejeta sesja superadmina nie moze wystarczyc.
    """
    from ..services import kopie

    formularz = await request.form()
    sprawdz_csrf(user, formularz.get("csrf_token"))

    haslo = (formularz.get("haslo") or "").strip()
    if not haslo or not verify_password(haslo, user.password_hash):
        return _wroc_do_kopii(blad="Hasło się nie zgadza — nic nie zostało zmienione.")

    wgrany = formularz.get("plik")
    nazwa = (formularz.get("nazwa") or "").strip()

    with tempfile.TemporaryDirectory() as roboczy:
        if wgrany is not None and getattr(wgrany, "filename", ""):
            sciezka = Path(roboczy) / "wgrana.tar.gz"
            limit = get_settings().max_kopia_bytes
            zapisane = 0
            with sciezka.open("wb") as plik:
                while kawalek := await wgrany.read(1024 * 1024):
                    zapisane += len(kawalek)
                    if zapisane > limit:
                        return _wroc_do_kopii(
                            blad=f"Plik przekracza {limit // 1048576} MB — "
                                 "przy tej wielkości użyj scp i polecenia "
                                 "'python -m cmdb_server.cli przywroc'."
                        )
                    plik.write(kawalek)
            zrodlo = wgrany.filename
        elif nazwa:
            kopia = kopie.znajdz(nazwa)
            if kopia is None:
                raise HTTPException(status_code=404, detail="nie ma takiej kopii")
            sciezka = kopia.sciezka
            zrodlo = kopia.nazwa
        else:
            return _wroc_do_kopii(blad="Wskaż kopię z listy albo wgraj plik.")

        try:
            znacznik = kopie.uzbroj(sciezka, autor=user.email)
        except kopie.BladKopii as bledne:
            return _wroc_do_kopii(blad=f"Tego archiwum nie przyjmuję: {bledne}")

    audit(db, None, action="kopia.uzbrojona", target=zrodlo,
          detail={"manifest": znacznik.get("manifest", {})},
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc_do_kopii(
        f"Przywrócenie z {zrodlo} przygotowane. Uruchom na serwerze "
        "'docker compose restart server', żeby je wgrać."
    )


@router.post("/kopie/odwolaj")
def odwolaj_przywrocenie(
    request: Request,
    csrf_token: str = Form(""),
    user: PortalUser = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> Response:
    from ..services import kopie

    sprawdz_csrf(user, csrf_token)
    if not kopie.rozbroj():
        return _wroc_do_kopii(blad="Nie było czego odwoływać.")

    audit(db, None, action="kopia.odwolana", target="-",
          ip=client_ip(request), actor=user.email)
    db.commit()
    return _wroc_do_kopii("Przywrócenie odwołane — restart niczego nie zmieni.")
