"""Samodzielna aktualizacja agenta.

Komunikacja jest jednostronna: serwer nigdy nie laczy sie z maszyna. Agent
przed kazda zaplanowana synchronizacja pyta, jaka wersja jest oczekiwana,
i jesli rozni sie od jego wlasnej - pobiera plik po HTTPS tym samym
polaczeniem, ktorym raportuje.

Kanal aktualizacji jest z natury zdalnym uruchamianiem kodu na kazdej
maszynie, dlatego obwarowany jest czterema warunkami. Zaden nie jest
opcjonalny:

  1. adres pobierania sklada agent z WLASNEJ konfiguracji - serwer podaje
     wylacznie numer wersji i skrot, nigdy URL-a,
  2. pobrany plik musi zgadzac sie ze skrotem SHA-256 podanym przez serwer,
  3. plik musi byc programem Windows (sygnatura MZ),
  4. nowy plik musi dac sie uruchomic - sprawdzamy to przed zatwierdzeniem
     podmiany, a gdy zawiedzie, wracamy do poprzedniej wersji.

Bez punktu 4 jedna wadliwa wersja unieruchomilaby cala flote bez mozliwosci
zdalnej naprawy - agent nie odezwalby sie juz nigdy.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import subprocess
import sys
from pathlib import Path

from . import __version__
from .transport import ApiError, CmdbClient, TransportError

log = logging.getLogger(__name__)

SCIEZKA_WERSJI = "/api/v1/agent/version"
SCIEZKA_PLIKU = "/api/v1/agent/release"
SCIEZKA_WYNIKU = "/api/v1/agent/upgrade-result"

# Gorny limit pobieranego pliku - agent z interfejsem ma okolo 30 MB.
MAX_ROZMIAR = 128 * 1024 * 1024

ROZSZERZENIE_NOWEJ = ".nowa"
ROZSZERZENIE_STAREJ = ".stara"


class UpgradeError(Exception):
    """Aktualizacja nie doszla do skutku - agent pracuje dalej na starej wersji."""


def wlasny_plik() -> Path | None:
    """Sciezka do dzialajacego pliku agenta, o ile dziala jako .exe."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def posprzataj_poprzednia(plik: Path) -> None:
    """Usuwa plik poprzedniej wersji.

    Nie da sie tego zrobic zaraz po podmianie, bo wtedy proces dziala wlasnie
    z tego pliku. Robimy to przy nastepnym uruchomieniu, gdy blokady juz nie ma.
    """
    stara = plik.with_suffix(plik.suffix + ROZSZERZENIE_STAREJ)
    if stara.exists():
        try:
            stara.unlink()
            log.debug("usunieto plik poprzedniej wersji: %s", stara.name)
        except OSError as exc:
            log.debug("nie moge usunac %s: %s", stara.name, exc)


def sprawdz_oferte(client: CmdbClient, token: str) -> dict | None:
    """Pyta serwer o oczekiwana wersje. None, gdy nie ma czego robic."""
    try:
        oferta = client.get(SCIEZKA_WERSJI, token)
    except (TransportError, ApiError) as exc:
        # Brak odpowiedzi na pytanie o wersje nie moze wstrzymac raportowania.
        log.warning("nie udalo sie sprawdzic oczekiwanej wersji agenta: %s", exc)
        return None

    if not oferta.get("available"):
        return None
    wersja = (oferta.get("version") or "").strip()
    if not wersja or wersja == __version__:
        return None
    return oferta


def _zglos(client: CmdbClient, token: str, wersja: str, status: str, detail: str = "") -> None:
    try:
        client.post(
            SCIEZKA_WYNIKU,
            token=token,
            payload={"version": wersja, "status": status, "detail": detail[:1000] or None},
        )
    except (TransportError, ApiError) as exc:
        log.warning("nie udalo sie zglosic wyniku aktualizacji: %s", exc)


def _pobierz_i_sprawdz(client: CmdbClient, token: str, oferta: dict, cel: Path) -> None:
    oczekiwany = (oferta.get("sha256") or "").strip().lower()
    if len(oczekiwany) != 64:
        raise UpgradeError("serwer nie podal poprawnego skrotu SHA-256 - odmawiam podmiany")

    cel.unlink(missing_ok=True)
    faktyczny = client.download(SCIEZKA_PLIKU, token, cel, MAX_ROZMIAR)

    # Porownanie w czasie stalym - skrot pochodzi ze zdalnego zrodla.
    if not hmac.compare_digest(faktyczny, oczekiwany):
        cel.unlink(missing_ok=True)
        raise UpgradeError(
            f"skrot pobranego pliku ({faktyczny[:16]}...) nie zgadza sie "
            f"z podanym przez serwer ({oczekiwany[:16]}...)"
        )

    with cel.open("rb") as plik:
        if plik.read(2) != b"MZ":
            cel.unlink(missing_ok=True)
            raise UpgradeError("pobrany plik nie jest programem Windows")


def _czy_dziala(plik: Path) -> tuple[bool, str]:
    """Uruchamia nowy plik z --version. To ostatnia bariera przed podmiana."""
    try:
        wynik = subprocess.run(
            [str(plik), "--version"],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    opis = (wynik.stdout or wynik.stderr or b"").decode("utf-8", errors="replace").strip()
    return wynik.returncode == 0, opis


def _podmien(biezacy: Path, nowy: Path) -> Path:
    """Podmienia plik agenta i zwraca sciezke kopii poprzedniej wersji.

    Windows nie pozwala nadpisac dzialajacego programu, ale pozwala go
    PRZEMIANOWAC - i z tego korzystamy. Proces dziala dalej z pliku pod nowa
    nazwa, a w jego miejsce wchodzi nowa wersja.
    """
    stara = biezacy.with_suffix(biezacy.suffix + ROZSZERZENIE_STAREJ)
    stara.unlink(missing_ok=True)
    biezacy.rename(stara)
    try:
        nowy.rename(biezacy)
    except OSError:
        stara.rename(biezacy)  # nie udalo sie wstawic nowej - wracamy
        raise
    return stara


def zastosuj(config, state, client: CmdbClient) -> str | None:
    """Sprawdza i ewentualnie instaluje nowa wersje.

    Zwraca numer zainstalowanej wersji albo None, gdy nic nie zrobiono.
    Zaden blad aktualizacji nie moze przerwac raportowania - to funkcja
    poboczna wzgledem glownego zadania agenta.
    """
    biezacy = wlasny_plik()
    if biezacy is None:
        log.debug("agent dziala ze zrodel - aktualizacja pomijana")
        return None

    posprzataj_poprzednia(biezacy)

    oferta = sprawdz_oferte(client, state.agent_token)
    if oferta is None:
        return None

    wersja = oferta["version"]
    log.info("serwer oczekuje wersji agenta %s (mam %s)", wersja, __version__)
    nowy = biezacy.with_suffix(biezacy.suffix + ROZSZERZENIE_NOWEJ)

    try:
        _pobierz_i_sprawdz(client, state.agent_token, oferta, nowy)
    except (UpgradeError, TransportError, ApiError, OSError) as exc:
        log.error("pobieranie wersji %s nie powiodlo sie: %s", wersja, exc)
        _zglos(client, state.agent_token, wersja, "blad", str(exc))
        nowy.unlink(missing_ok=True)
        return None

    try:
        stara = _podmien(biezacy, nowy)
    except OSError as exc:
        log.error("nie udalo sie podmienic pliku agenta: %s", exc)
        _zglos(client, state.agent_token, wersja, "blad", f"podmiana pliku: {exc}")
        nowy.unlink(missing_ok=True)
        return None

    dziala, opis = _czy_dziala(biezacy)
    if not dziala:
        # Wycofujemy sie, zanim maszyna zostanie z niedzialajacym agentem.
        log.error("nowa wersja %s nie uruchamia sie (%s) - przywracam poprzednia", wersja, opis)
        try:
            biezacy.unlink(missing_ok=True)
            stara.rename(biezacy)
        except OSError as exc:
            log.critical("PRZYWROCENIE POPRZEDNIEJ WERSJI NIE POWIODLO SIE: %s", exc)
        _zglos(client, state.agent_token, wersja, "blad", f"nowa wersja nie uruchamia sie: {opis}")
        return None

    log.info("zainstalowano wersje agenta %s (%s)", wersja, opis)
    _zglos(client, state.agent_token, wersja, "ok", opis)
    return wersja


def uruchom_ponownie(argumenty: list[str]) -> int:
    """Uruchamia swiezo zainstalowana wersje, zeby to ona wykonala ten cykl.

    Znacznik --po-aktualizacji chroni przed petla: nowy proces nie sprawdza
    juz wersji ponownie.
    """
    plik = wlasny_plik()
    if plik is None:
        return 0
    polecenie = [str(plik), *argumenty, "--po-aktualizacji"]
    log.info("uruchamiam nowa wersje agenta dla tego cyklu")
    try:
        wynik = subprocess.run(
            polecenie,
            timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return wynik.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("nie udalo sie uruchomic nowej wersji: %s", exc)
        return 1
