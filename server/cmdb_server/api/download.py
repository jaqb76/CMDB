"""Wydawanie instalatorow agenta po HTTPS.

Zeby postawic agenta na nowej maszynie, nie powinno byc potrzebne klonowanie
repozytorium - czesto prywatnego, a poswiadczen do niego nie chcemy rozdawac
po serwerowni. Serwer wydaje wiec sam to, co jest do instalacji potrzebne.

Podzial na to, co publiczne, a co za tokenem:

- skrypt startowy jest publiczny; nie ma w nim nic tajnego, a wymaganie tokenu
  do jego pobrania oznaczaloby podawanie go dwa razy w jednym poleceniu,
- wlasciwe pliki agenta wymagaja tokenu firmowego, tego samego, ktorym maszyna
  sie rejestruje. Kto go nie ma, nie zainstaluje agenta - i nie zobaczy, jakie
  wersje sa w tej instalacji uzywane.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from sqlalchemy import select

from ..models import AgentRelease, EnrollmentToken, Tenant
from ..services import architektura, pakiet, upgrades
from ..services.auth import require_enrollment_token_do_pobrania

log = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["download"])

KATALOG_BOOTSTRAP = Path(__file__).resolve().parent.parent / "bootstrap"
ZNACZNIK_ADRESU = "@@ADRES_SERWERA@@"


def adres_publiczny(request: Request) -> str:
    """Adres, pod ktorym klienci widza ten serwer."""
    ustawiony = get_settings().public_url.strip().rstrip("/")
    if ustawiony:
        return ustawiony
    return str(request.base_url).rstrip("/")


def _skrypt(nazwa: str) -> str:
    """Tresc skryptu startowego.

    W obrazie produkcyjnym skrypty leza obok kodu serwera. W repozytorium
    czesc z nich zyje w katalogu agenta - stamtad je bierzemy, zeby nie
    trzymac dwoch kopii tego samego pliku.
    """
    kandydaci = [
        KATALOG_BOOTSTRAP / nazwa,
        Path(__file__).resolve().parents[3] / "agent" / "packaging" / nazwa,
    ]
    zrodlo = get_settings().agent_source_dir
    if zrodlo:
        kandydaci.insert(1, Path(zrodlo) / "packaging" / nazwa)

    for sciezka in kandydaci:
        try:
            return sciezka.read_text(encoding="utf-8")
        except OSError:
            continue
    log.error("brak skryptu startowego %s (szukano w: %s)", nazwa, kandydaci)
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"skrypt {nazwa} jest niedostepny na tym serwerze",
    )


@router.get("/install.sh", response_class=PlainTextResponse)
def skrypt_instalacyjny(request: Request) -> PlainTextResponse:
    """Skrypt startowy dla Linuksa, z wpisanym adresem tego serwera."""
    return PlainTextResponse(
        content=_skrypt("install.sh").replace(ZNACZNIK_ADRESU, adres_publiczny(request)),
        media_type="text/x-shellscript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/install.ps1", response_class=PlainTextResponse)
def skrypt_instalacyjny_windows(request: Request) -> PlainTextResponse:
    """Skrypt startowy dla Windows, z wpisanym adresem tego serwera.

    Windows nie mial odpowiednika install.sh: pod /download/agent-windows.exe
    lezy SAM AGENT, bo to jego podmienia samoaktualizacja. Uruchomiony wprost
    wypisywal tylko skladnie i nie instalowal niczego.
    """
    return PlainTextResponse(
        content=_skrypt("install.ps1").replace(ZNACZNIK_ADRESU, adres_publiczny(request)),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/install-agent.ps1", response_class=PlainTextResponse)
def wlasciwy_instalator_windows() -> PlainTextResponse:
    """Wlasciwy instalator dla Windows, pobierany przez skrypt startowy."""
    return PlainTextResponse(
        content=_skrypt("install-agent.ps1"),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/agent-linux.tar.gz")
def paczka_linux(
    db: Session = Depends(get_db),
    auth: tuple[EnrollmentToken, Tenant] = Depends(require_enrollment_token_do_pobrania),
) -> FileResponse:
    """Zrodla agenta dla Linuksa.

    Zrodla, a nie plik wykonywalny: PyInstaller nie kompiluje na inna
    architekture, wiec jeden build nie obsluzylby zarazem serwerow x86
    i Raspberry Pi. Agent stoi na samej bibliotece standardowej, wiec
    dziala wszedzie tam, gdzie jest Python.
    """
    _, tenant = auth
    # Wersja obowiazujaca te firme - tak samo jak przy pliku dla Windows.
    wydanie = upgrades.wersja_dla_firmy(db, tenant.id, "linux", pakiet.ARCH_ZRODLA)
    if wydanie is None:
        # Nikt nie ustawil jeszcze polityki. Instalacje nowej maszyny lepiej
        # przeprowadzic na najnowszej dostepnej wersji, niz odmowic - o tym,
        # do czego maszyna sie PO instalacji aktualizuje, decyduje juz
        # ustawienie firmy albo wersja aktywna.
        wydanie = db.execute(
            select(AgentRelease)
            .where(AgentRelease.os_family == "linux",
                   AgentRelease.arch == pakiet.ARCH_ZRODLA)
            .order_by(AgentRelease.created_at.desc())
        ).scalars().first()
    if wydanie is None:
        log.error("brak paczki zrodel agenta w magazynie wydan")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="paczka agenta dla Linuksa nie zostala przygotowana na tym serwerze",
        )

    sciezka = upgrades.sciezka_pliku(wydanie)
    if not sciezka.is_file():
        log.error("brak pliku paczki %s w magazynie (%s)", wydanie.version, sciezka)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plik paczki jest niedostepny na serwerze",
        )

    log.info("firma %s pobiera zrodla agenta %s", tenant.slug, wydanie.version)
    return FileResponse(
        path=sciezka,
        media_type="application/gzip",
        filename=wydanie.filename,
        headers={
            "X-CMDB-SHA256": wydanie.sha256,
            "X-CMDB-Version": wydanie.version,
            "Cache-Control": "no-store",
        },
    )


@router.get("/agent-windows-tray.exe")
def ikona_windows(
    db: Session = Depends(get_db),
    auth: tuple[EnrollmentToken, Tenant] = Depends(require_enrollment_token_do_pobrania),
) -> FileResponse:
    """Wariant z ikona w zasobniku - dodatek do agenta, nie zamiennik.

    Instalator kopiuje go, jesli lezy obok agenta. Przy instalacji z serwera
    agent jest pobierany do katalogu tymczasowego, wiec bez tego endpointu
    ikona nie mialaby skad sie tam wziasc i nigdy nie byla instalowana.

    Osobny endpoint, a nie drugi plik przy tym samym wydaniu, bo magazyn
    trzyma jeden plik na wpis - ikona jest wiec zapisana jako wpis o wlasnej
    "architekturze" i nigdy nie trafia do samoaktualizacji.
    """
    _, tenant = auth
    wydanie = upgrades.wersja_ikony(db, tenant.id)
    if wydanie is None:
        # Brak ikony nie jest bledem instalacji - agent dziala bez niej.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dla tej firmy nie wgrano wariantu z ikona w zasobniku",
        )

    sciezka = upgrades.sciezka_pliku(wydanie)
    if not sciezka.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plik jest niedostepny na serwerze",
        )

    log.info("firma %s pobiera ikone agenta %s", tenant.slug, wydanie.version)
    return FileResponse(
        path=sciezka,
        media_type="application/octet-stream",
        filename=wydanie.filename,
        headers={
            "X-CMDB-SHA256": wydanie.sha256,
            "X-CMDB-Version": wydanie.version,
            "Cache-Control": "no-store",
        },
    )


@router.get("/agent-windows.exe")
def instalator_windows(
    db: Session = Depends(get_db),
    auth: tuple[EnrollmentToken, Tenant] = Depends(require_enrollment_token_do_pobrania),
) -> FileResponse:
    """Plik agenta dla Windows w wersji obowiazujacej te firme.

    Nie ma tu parametru wskazujacego wydanie - serwer wydaje dokladnie to,
    co sam uznaje za wersje docelowa dla tej firmy. Nie da sie wiec tym
    endpointem wyciagnac dowolnego pliku z magazynu.
    """
    _, tenant = auth
    wydanie = upgrades.wersja_dla_firmy(db, tenant.id, "windows")
    if wydanie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dla tej firmy nie ustawiono wersji agenta dla Windows",
        )

    sciezka = upgrades.sciezka_pliku(wydanie)
    if not sciezka.is_file():
        log.error("brak pliku wersji %s w magazynie (%s)", wydanie.version, sciezka)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plik wersji jest niedostepny na serwerze",
        )

    log.info("firma %s pobiera agenta dla Windows %s", tenant.slug, wydanie.version)
    return FileResponse(
        path=sciezka,
        media_type="application/octet-stream",
        filename=wydanie.filename,
        headers={
            "X-CMDB-SHA256": wydanie.sha256,
            "X-CMDB-Version": wydanie.version,
            "Cache-Control": "no-store",
        },
    )
