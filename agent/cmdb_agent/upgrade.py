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
  3. plik musi byc programem wykonywalnym dla systemu tej maszyny
     (sygnatura MZ na Windows, ELF na Linuksie),
  4. nowy plik musi dac sie uruchomic - sprawdzamy to przed zatwierdzeniem
     podmiany, a gdy zawiedzie, wracamy do poprzedniej wersji.

Bez punktu 4 jedna wadliwa wersja unieruchomilaby cala flote bez mozliwosci
zdalnej naprawy - agent nie odezwalby sie juz nigdy.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from . import __version__
from .proces import flagi_bez_okna, srodowisko_dla_potomka
from .transport import ApiError, CmdbClient, TransportError

log = logging.getLogger(__name__)

SCIEZKA_WERSJI = "/api/v1/agent/version"
SCIEZKA_PLIKU = "/api/v1/agent/release"
SCIEZKA_WYNIKU = "/api/v1/agent/upgrade-result"

# Gorny limit pobieranego pliku - agent z interfejsem ma okolo 30 MB.
MAX_ROZMIAR = 128 * 1024 * 1024

ROZSZERZENIE_NOWEJ = ".nowa"
ROZSZERZENIE_STAREJ = ".stara"


# Agent jest budowany osobno dla kazdego systemu. Serwer pilnuje, zeby wydac
# plik zgodny z systemem maszyny, ale sprawdzamy to takze po stronie agenta -
# to on ma najwiecej do stracenia, gdyby podmienil plik na niewykonywalny.
SYGNATURY = {
    "win32": b"MZ",
    "linux": bytes.fromhex("7f") + b"ELF",
    "darwin": None,  # Mach-O ma kilka wariantow - nie zgadujemy
}


def sygnatura_systemu() -> bytes | None:
    """Oczekiwany poczatek pliku wykonywalnego dla biezacego systemu."""
    for przedrostek, sygnatura in SYGNATURY.items():
        if sys.platform.startswith(przedrostek):
            return sygnatura
    return None


RODZAJ_ZRODLA = "zrodla"
SYGNATURA_GZIP = bytes([0x1F, 0x8B])
KATALOG_W_ARCHIWUM = "cmdb-agent"

# Plik-znacznik zakladany przez instalator. Bez niego agent uruchomiony
# z kopii repozytorium podmienialby katalog ze zrodlami programisty -
# aktualizacja ma dotyczyc instalacji, a nie czyjegos katalogu roboczego.
ZNACZNIK_INSTALACJI = ".cmdb-instalacja"


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

    if oferta.get("kind") == RODZAJ_ZRODLA:
        # Paczka zrodel nie jest programem - sprawdzamy sygnature gzip,
        # a jej zawartosc weryfikuje rozpakowanie.
        with cel.open("rb") as plik:
            poczatek = plik.read(2)
        if poczatek != SYGNATURA_GZIP:
            cel.unlink(missing_ok=True)
            raise UpgradeError("pobrany plik nie jest archiwum - odmawiam podmiany")
        return

    oczekiwana = sygnatura_systemu()
    if oczekiwana:
        # Plik zamykamy przed skasowaniem: na Windows nie da sie usunac
        # pliku, ktory jest jeszcze otwarty, wiec zamiast czytelnego bledu
        # o niewlasciwym pliku wychodzil PermissionError.
        with cel.open("rb") as plik:
            poczatek = plik.read(len(oczekiwana))
        if poczatek != oczekiwana:
            cel.unlink(missing_ok=True)
            raise UpgradeError(
                "pobrany plik nie jest programem dla tego systemu - odmawiam podmiany"
            )


def _czy_dziala(plik: Path) -> tuple[bool, str]:
    """Uruchamia nowy plik z --version. To ostatnia bariera przed podmiana."""
    try:
        wynik = subprocess.run(
            [str(plik), "--version"],
            capture_output=True,
            timeout=60,
            creationflags=flagi_bez_okna(),
            # Bez wyczyszczenia zmiennych _PYI_* nowy plik odmawia startu,
            # bo jego bootloader widzi archiwum procesu nadrzednego.
            env=srodowisko_dla_potomka(),
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
    korzen = katalog_instalacji() if biezacy is None else None
    if biezacy is None and korzen is None:
        log.debug("agent nie dziala z instalacji - aktualizacja pomijana")
        return None

    if biezacy is not None:
        posprzataj_poprzednia(biezacy)

    oferta = sprawdz_oferte(client, state.agent_token)
    if oferta is None:
        return None

    # Rodzaj wydania decyduje o sposobie instalacji. Podmiana katalogu plikiem
    # wykonywalnym - albo odwrotnie - zostawilaby maszyne bez dzialajacego
    # agenta, wiec niezgodnosc odrzucamy, zanim cokolwiek pobierzemy.
    zrodla = oferta.get("kind") == RODZAJ_ZRODLA
    if zrodla and korzen is None:
        log.warning("serwer wskazuje paczke zrodel, a agent dziala z pliku - pomijam")
        return None
    if not zrodla and korzen is not None:
        log.warning("serwer wskazuje plik wykonywalny, a agent dziala ze zrodel - pomijam")
        return None

    if korzen is not None:
        return zastosuj_zrodla(config, state, client, korzen, oferta)

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

    Lista argumentow zawiera juz znacznik --po-aktualizacji, ktory chroni
    przed petla: nowy proces nie sprawdza wersji ponownie.
    """
    plik = wlasny_plik()
    if plik is not None:
        polecenie = [str(plik), *argumenty]
    else:
        korzen = katalog_instalacji()
        if korzen is None:
            return 0
        # Instalacja ze zrodel nie ma wlasnego pliku wykonywalnego - nowa
        # wersje uruchamiamy tym samym interpreterem, wskazujac jej katalog.
        polecenie = [sys.executable, "-m", "cmdb_agent.main", *argumenty]
    log.info("uruchamiam nowa wersje agenta dla tego cyklu")
    try:
        srodowisko = srodowisko_dla_potomka()
        if plik is None:
            srodowisko["PYTHONPATH"] = str(katalog_instalacji())
        wynik = subprocess.run(
            polecenie,
            timeout=1800,
            creationflags=flagi_bez_okna(),
            env=srodowisko,
        )
        return wynik.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("nie udalo sie uruchomic nowej wersji: %s", exc)
        return 1


# --- instalacja ze zrodel ---------------------------------------------------

def katalog_instalacji() -> Path | None:
    """Katalog, w ktorym agent jest zainstalowany ze zrodel.

    Zwraca sciezke tylko wtedy, gdy to faktycznie instalacja zalozona przez
    instalator - poznajemy ja po pliku-znaczniku. Bez tego warunku agent
    uruchomiony z kopii repozytorium podmienialby katalog roboczy
    programisty, a to nie jest aktualizacja, tylko utrata pracy.
    """
    if getattr(sys, "frozen", False):
        return None
    korzen = Path(__file__).resolve().parent.parent
    if not (korzen / ZNACZNIK_INSTALACJI).is_file():
        log.debug("brak znacznika instalacji w %s - aktualizacja ze zrodel pominieta", korzen)
        return None
    if not os.access(korzen, os.W_OK):
        log.debug("katalog %s nie jest zapisywalny - aktualizacja pominieta", korzen)
        return None
    return korzen


def _rozpakuj(archiwum: Path, cel: Path) -> Path:
    """Rozpakowuje paczke i zwraca katalog z pakietem agenta."""
    cel.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archiwum, "r:gz") as tar:
        for wpis in tar:
            # Sciezka wychodzaca poza katalog docelowy pozwolilaby nadpisac
            # dowolny plik na maszynie.
            if wpis.name.startswith("/") or ".." in Path(wpis.name).parts:
                raise UpgradeError(f"archiwum zawiera podejrzana sciezke: {wpis.name}")
            if not (wpis.isfile() or wpis.isdir()):
                raise UpgradeError(f"archiwum zawiera wpis, ktory nie jest plikiem: {wpis.name}")
        # Filtr "data" odrzuca dowiazania, pliki urzadzen i sciezki wychodzace
        # poza katalog. Sprawdzamy to takze sami wyzej, ale niech zabezpieczenie
        # biblioteki tez dziala. Starsze wydania Pythona 3.9 go nie znaja.
        try:
            tar.extractall(cel, filter="data")
        except TypeError:
            tar.extractall(cel)

    rozpakowany = cel / KATALOG_W_ARCHIWUM
    if not (rozpakowany / "cmdb_agent" / "__init__.py").is_file():
        raise UpgradeError("paczka nie zawiera pakietu agenta")
    return rozpakowany


def _czy_zrodla_dzialaja(korzen: Path, oczekiwana: str) -> tuple[bool, str]:
    """Uruchamia agenta z nowego katalogu i sprawdza, ze zglasza nowa wersje.

    To ostatnia bariera przed podmiana. Sam fakt, ze pliki sie rozpakowaly,
    nie znaczy jeszcze, ze agent wstanie - brakujacy modul albo blad skladni
    wyszlyby dopiero przy nastepnym raporcie, gdy nie byloby juz do czego
    wracac.
    """
    srodowisko = srodowisko_dla_potomka()
    srodowisko["PYTHONPATH"] = str(korzen)
    try:
        wynik = subprocess.run(
            [sys.executable, "-m", "cmdb_agent.main", "--version"],
            capture_output=True,
            timeout=60,
            cwd=str(korzen),
            env=srodowisko,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    opis = (wynik.stdout or wynik.stderr).decode("utf-8", errors="replace").strip()
    if wynik.returncode != 0:
        return False, opis or f"kod wyjscia {wynik.returncode}"
    if oczekiwana not in opis:
        return False, f"zglasza '{opis}', a oczekiwano wersji {oczekiwana}"
    return True, opis


def _podmien_katalog(korzen: Path, nowy: Path) -> Path:
    """Podmienia pakiet agenta, zostawiajac poprzedni obok do wycofania."""
    biezacy = korzen / "cmdb_agent"
    zapasowy = korzen / f"cmdb_agent{ROZSZERZENIE_STAREJ}"

    shutil.rmtree(zapasowy, ignore_errors=True)
    biezacy.rename(zapasowy)
    try:
        shutil.move(str(nowy / "cmdb_agent"), str(biezacy))
    except OSError:
        shutil.rmtree(biezacy, ignore_errors=True)
        zapasowy.rename(biezacy)
        raise

    # Instalator z paczki tez sie przydaje - pozwala odtworzyc usluge
    # bez ponownego pobierania czegokolwiek.
    zrodlowy_skrypt = nowy / "packaging" / "install-agent.sh"
    if zrodlowy_skrypt.is_file():
        docelowy = korzen / "packaging"
        docelowy.mkdir(exist_ok=True)
        shutil.copy2(zrodlowy_skrypt, docelowy / "install-agent.sh")
    return zapasowy


def zastosuj_zrodla(config, state, client: CmdbClient, korzen: Path, oferta: dict) -> str | None:
    """Instaluje nowa wersje agenta zainstalowanego ze zrodel."""
    wersja = oferta["version"]
    log.info("serwer oczekuje wersji agenta %s (mam %s)", wersja, __version__)

    roboczy = Path(tempfile.mkdtemp(prefix="cmdb-agent-", dir=str(korzen)))
    archiwum = roboczy / "paczka.tar.gz"
    try:
        _pobierz_i_sprawdz(client, state.agent_token, oferta, archiwum)
        rozpakowany = _rozpakuj(archiwum, roboczy / "nowa")

        dziala, opis = _czy_zrodla_dzialaja(rozpakowany, wersja)
        if not dziala:
            raise UpgradeError(f"nowa wersja nie uruchamia sie: {opis}")

        zapasowy = _podmien_katalog(korzen, rozpakowany)
    except (UpgradeError, TransportError, ApiError, OSError) as exc:
        log.error("aktualizacja ze zrodel do wersji %s nie powiodla sie: %s", wersja, exc)
        _zglos(client, state.agent_token, wersja, "blad", str(exc))
        shutil.rmtree(roboczy, ignore_errors=True)
        return None

    shutil.rmtree(roboczy, ignore_errors=True)

    # Sprawdzamy jeszcze raz, juz na docelowym miejscu: podmiana mogla sie
    # udac technicznie, a mimo to zostawic katalog, z ktorego agent nie wstaje.
    dziala, opis = _czy_zrodla_dzialaja(korzen, wersja)
    if not dziala:
        log.error("wersja %s nie dziala po podmianie (%s) - przywracam poprzednia", wersja, opis)
        try:
            shutil.rmtree(korzen / "cmdb_agent", ignore_errors=True)
            zapasowy.rename(korzen / "cmdb_agent")
        except OSError as exc:
            log.critical("PRZYWROCENIE POPRZEDNIEJ WERSJI NIE POWIODLO SIE: %s", exc)
        _zglos(client, state.agent_token, wersja, "blad", f"po podmianie: {opis}")
        return None

    shutil.rmtree(zapasowy, ignore_errors=True)
    log.info("zainstalowano wersje agenta %s ze zrodel (%s)", wersja, opis)
    _zglos(client, state.agent_token, wersja, "ok", opis)
    return wersja
