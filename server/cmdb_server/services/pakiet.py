"""Paczka zrodel agenta wydawana przez serwer po HTTPS.

Na Linuksie agent instaluje sie wprost ze zrodel - nie ma pliku wykonywalnego
do pobrania, a PyInstaller nie kompiluje na inna architekture, wiec jeden build
i tak nie obsluzylby zarazem serwerow x86 i Raspberry Pi. Zeby nie zmuszac
nikogo do klonowania repozytorium (czesto prywatnego) na kazdej maszynie,
serwer wydaje te zrodla jako paczke.

Paczka jest artefaktem w magazynie wydan, a nie czyms budowanym w locie z
katalogu obok: obraz produkcyjny zawiera wylacznie katalog server/, wiec
zrodel agenta zwyczajnie tam nie ma. Buduje sie ja przy wdrozeniu.

Archiwum jest deterministyczne - te same zrodla daja bajt w bajt ten sam plik,
a wiec i ten sam skrot. Bez tego kazde przebudowanie zmienialoby SHA-256
i nie dalo by sie stwierdzic, czy paczka faktycznie sie zmienila.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

NAZWA_ARCHIWUM = "cmdb-agent-zrodla.tar.gz"
NAZWA_OPISU = "cmdb-agent-zrodla.json"
KATALOG_W_ARCHIWUM = "cmdb-agent"

# Znacznik czasu w archiwum jest staly, bo inaczej ten sam kod dawalby za
# kazdym razem inny skrot. Data budowania jest w pliku opisu.
STALY_CZAS = 0

# Co trafia do paczki: pakiet agenta i skrypt instalacyjny. Reszta repozytorium
# (testy, build dla Windows, dokumentacja) nie jest do niczego potrzebna na
# maszynie docelowej.
ZAWARTOSC = (
    ("cmdb_agent", "cmdb_agent"),
    ("packaging/install-agent.sh", "packaging/install-agent.sh"),
)

POMIJANE_KATALOGI = {"__pycache__", ".pytest_cache", ".mypy_cache"}
POMIJANE_ROZSZERZENIA = {".pyc", ".pyo"}


class BrakZrodel(RuntimeError):
    """Katalog zrodel agenta nie wyglada na katalog zrodel agenta."""


def katalog_paczki() -> Path:
    """Katalog, w ktorym lezy paczka zrodel agenta."""
    from ..config import get_settings

    ustawienia = get_settings()
    return Path(ustawienia.agent_bundle_dir or ustawienia.release_dir)


def sciezka_archiwum(katalog_wydan: Path) -> Path:
    return katalog_wydan / NAZWA_ARCHIWUM


def sciezka_opisu(katalog_wydan: Path) -> Path:
    return katalog_wydan / NAZWA_OPISU


def opis(katalog_wydan: Path) -> dict | None:
    """Wersja, skrot i data zbudowania paczki - albo None, gdy jej nie ma."""
    plik = sciezka_opisu(katalog_wydan)
    if not plik.is_file() or not sciezka_archiwum(katalog_wydan).is_file():
        return None
    try:
        return json.loads(plik.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("nie moge odczytac opisu paczki zrodel: %s", exc)
        return None


def odczytaj_wersje(katalog_zrodel: Path) -> str:
    """Wersja agenta wprost ze zrodel - zeby nie trzeba bylo jej podawac."""
    plik = katalog_zrodel / "cmdb_agent" / "__init__.py"
    try:
        tresc = plik.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrakZrodel(f"nie moge odczytac {plik}: {exc}") from exc
    dopasowanie = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', tresc, re.MULTILINE)
    if dopasowanie is None:
        raise BrakZrodel(f"nie znajduje __version__ w {plik}")
    return dopasowanie.group(1)


def _pliki(katalog_zrodel: Path) -> list[tuple[Path, str]]:
    """Pary (sciezka na dysku, nazwa w archiwum), posortowane dla powtarzalnosci."""
    zebrane: list[tuple[Path, str]] = []
    for zrodlo, cel in ZAWARTOSC:
        sciezka = katalog_zrodel / zrodlo
        if sciezka.is_file():
            zebrane.append((sciezka, f"{KATALOG_W_ARCHIWUM}/{cel}"))
            continue
        if not sciezka.is_dir():
            raise BrakZrodel(f"brakuje {sciezka} - to nie jest katalog zrodel agenta")
        for plik in sciezka.rglob("*"):
            if not plik.is_file():
                continue
            if set(plik.parts) & POMIJANE_KATALOGI or plik.suffix in POMIJANE_ROZSZERZENIA:
                continue
            wzgledna = plik.relative_to(sciezka).as_posix()
            zebrane.append((plik, f"{KATALOG_W_ARCHIWUM}/{cel}/{wzgledna}"))
    return sorted(zebrane, key=lambda para: para[1])


def _zawartosc(sciezka: Path, nazwa: str) -> bytes:
    """Zawartosc pliku gotowa do spakowania.

    Skryptom powloki narzucamy zakonczenia LF. Na Windows git domyslnie
    wystawia w kopii roboczej CRLF, a wtedy pierwsza linia rozpakowanego
    skryptu konczy sie znakiem powrotu karetki - i system szuka
    interpretera o nazwie 'bash' z tym znakiem na koncu. Paczka ma byc
    poprawna niezaleznie od tego, jak wyglada czyjas kopia robocza.
    """
    dane = sciezka.read_bytes()
    if nazwa.endswith(".sh"):
        # CRLF -> LF. Zapis przez chr(), bo dosowny odwrotny ukosnik
        # bywa zjadany po drodze przy edycji tego pliku.
        dane = dane.replace((chr(13) + chr(10)).encode(), chr(10).encode())
    return dane


def zbuduj(katalog_zrodel: Path, katalog_wydan: Path) -> dict:
    """Buduje paczke zrodel i zapisuje ja w magazynie wydan."""
    katalog_zrodel = Path(katalog_zrodel)
    wersja = odczytaj_wersje(katalog_zrodel)
    pliki = _pliki(katalog_zrodel)
    katalog_wydan.mkdir(parents=True, exist_ok=True)

    archiwum = sciezka_archiwum(katalog_wydan)
    tymczasowy = archiwum.with_suffix(".tmp")

    # Strumien gzip tworzymy sami: tarfile.open("w:gz") wpisuje w naglowek
    # biezacy czas, wiec te same zrodla dawalyby za kazdym razem inny skrot.
    # Pusta nazwa pliku z tego samego powodu - inaczej trafia tam nazwa
    # pliku tymczasowego.
    with tymczasowy.open("wb") as wyjscie:
        with gzip.GzipFile(filename="", mode="wb", fileobj=wyjscie, mtime=0,
                           compresslevel=9) as spakowane:
            with tarfile.open(fileobj=spakowane, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for sciezka, nazwa in pliki:
                    info = tar.gettarinfo(str(sciezka), arcname=nazwa)
                    info.mtime = STALY_CZAS
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    # Skrypt instalacyjny musi zostac wykonywalny po rozpakowaniu.
                    info.mode = 0o755 if nazwa.endswith(".sh") else 0o644
                    dane = _zawartosc(sciezka, nazwa)
                    info.size = len(dane)
                    tar.addfile(info, io.BytesIO(dane))

    dane = tymczasowy.read_bytes()
    skrot = hashlib.sha256(dane).hexdigest()
    tymczasowy.replace(archiwum)

    metadane = {
        "version": wersja,
        "sha256": skrot,
        "size_bytes": len(dane),
        "files": len(pliki),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    sciezka_opisu(katalog_wydan).write_text(
        json.dumps(metadane, indent=2) + "\n", encoding="utf-8"
    )
    log.info("zbudowano paczke zrodel agenta %s (%d plikow, %s)", wersja, len(pliki), skrot[:16])
    return metadane


def zbuduj_jesli_trzeba(katalog_zrodel: Path, katalog_wydan: Path) -> dict | None:
    """Przebudowuje paczke, gdy zrodla sa nowsze niz ona.

    Wygoda przy pracy z repozytorium: po zmianie w agencie serwer wyda nowa
    paczke bez pamietania o osobnym poleceniu. W obrazie produkcyjnym zrodel
    nie ma i funkcja po prostu nic nie robi.
    """
    katalog_zrodel = Path(katalog_zrodel)
    if not (katalog_zrodel / "cmdb_agent").is_dir():
        return opis(katalog_wydan)

    archiwum = sciezka_archiwum(katalog_wydan)
    if archiwum.is_file():
        try:
            najnowsze = max(sciezka.stat().st_mtime for sciezka, _ in _pliki(katalog_zrodel))
        except (BrakZrodel, ValueError, OSError):
            return opis(katalog_wydan)
        if najnowsze <= archiwum.stat().st_mtime:
            return opis(katalog_wydan)

    try:
        return zbuduj(katalog_zrodel, katalog_wydan)
    except (BrakZrodel, OSError) as exc:
        log.warning("nie udalo sie zbudowac paczki zrodel agenta: %s", exc)
        return opis(katalog_wydan)


# --- paczka jako wydanie ----------------------------------------------------

ARCH_ZRODLA = "zrodla"
SYGNATURA_GZIP = bytes([0x1F, 0x8B])

# Bez limitu rozpakowany tar moze byc dowolnie wiekszy od archiwum. Paczka
# zrodel ma kilkadziesiat kB, wiec 32 MB to zapas z ogromnym marginesem,
# a jednoczesnie zapora przed archiwum-bomba.
LIMIT_ROZPAKOWANY = 32 * 1024 * 1024


def czy_paczka_zrodel(sciezka: Path) -> bool:
    """Czy plik wyglada na archiwum gzip."""
    try:
        with sciezka.open("rb") as plik:
            return plik.read(2) == SYGNATURA_GZIP
    except OSError:
        return False


def sprawdz_paczke(sciezka: Path) -> str:
    """Sprawdza, czy archiwum jest paczka zrodel agenta, i zwraca jej wersje.

    Sprawdzamy zawartosc, a nie nazwe pliku: wgrywajacy moze sie pomylic,
    a maszyna docelowa dostanie dokladnie to, co tu wpuscimy. Numer wersji
    czytamy ze zrodel w archiwum - tak samo jak przy pliku wykonywalnym
    czytamy go z dopisanych metadanych.
    """
    wymagane = {
        f"{KATALOG_W_ARCHIWUM}/cmdb_agent/__init__.py",
        f"{KATALOG_W_ARCHIWUM}/packaging/install-agent.sh",
    }
    try:
        with tarfile.open(sciezka, "r:gz") as archiwum:
            nazwy = set()
            laczny = 0
            for wpis in archiwum:
                # Sciezki wychodzace poza katalog paczki to klasyczna droga
                # do nadpisania czegokolwiek na maszynie docelowej.
                if wpis.name.startswith("/") or ".." in Path(wpis.name).parts:
                    raise BrakZrodel(f"archiwum zawiera podejrzana sciezke: {wpis.name}")
                if not (wpis.isfile() or wpis.isdir()):
                    raise BrakZrodel(f"archiwum zawiera wpis, ktory nie jest plikiem: {wpis.name}")
                laczny += wpis.size
                if laczny > LIMIT_ROZPAKOWANY:
                    raise BrakZrodel("rozpakowane archiwum jest za duze")
                nazwy.add(wpis.name)

            brakujace = wymagane - nazwy
            if brakujace:
                raise BrakZrodel(
                    "to nie jest paczka zrodel agenta - brakuje: "
                    + ", ".join(sorted(brakujace))
                )

            wpis = archiwum.extractfile(f"{KATALOG_W_ARCHIWUM}/cmdb_agent/__init__.py")
            tresc = wpis.read().decode("utf-8", errors="replace") if wpis else ""
    except tarfile.TarError as exc:
        raise BrakZrodel(f"nie moge odczytac archiwum: {exc}") from exc
    except OSError as exc:
        raise BrakZrodel(f"nie moge otworzyc archiwum: {exc}") from exc

    dopasowanie = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', tresc, re.MULTILINE)
    if dopasowanie is None:
        raise BrakZrodel("w paczce nie ma numeru wersji agenta")
    return dopasowanie.group(1)


def zarejestruj(db, metadane: dict, katalog_paczki: Path, katalog_wydan: Path):
    """Wpisuje zbudowana paczke do magazynu wydan, jesli jeszcze jej tam nie ma.

    Dzieki temu paczka podlega tym samym regulom co plik dla Windows: mozna ja
    ustawic jako aktywna, przypisac jednej firmie wersje probna, a agenty
    aktualizuja sie do wskazanej wersji, a nie do tej, ktora akurat lezy
    na dysku serwera.

    Szukamy po WERSJI, nie po skrocie. Ograniczenie unikalnosci obejmuje
    wersje, system i architekture, wiec paczka przebudowana pod tym samym
    numerem ma inny skrot, ale nadal koliduje - wyszukiwanie po skrocie jej
    nie znajdowalo i proba wstawienia konczyla sie bledem przy starcie serwera.

    Funkcje wykonuje kazdy proces roboczy przy starcie, wiec sprawdzenie
    i wstawienie moga sie przepleść miedzy procesami. Kolizje przechwytujemy
    zamiast jej zapobiegac: jest rzadka, a blokada na czas zapisu pliku
    byla by kosztowniejsza niz ponowne odczytanie wiersza.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from ..models import AgentRelease

    wersja = metadane["version"]
    odcisk = metadane["sha256"]

    def znajdz():
        return db.execute(
            select(AgentRelease).where(
                AgentRelease.os_family == "linux",
                AgentRelease.arch == ARCH_ZRODLA,
                AgentRelease.version == wersja,
            )
        ).scalar_one_or_none()

    nazwa = f"linux-{ARCH_ZRODLA}-{odcisk}.tar.gz"
    katalog_wydan.mkdir(parents=True, exist_ok=True)
    docelowy = katalog_wydan / nazwa
    if not docelowy.exists():
        docelowy.write_bytes(sciezka_archiwum(katalog_paczki).read_bytes())

    istniejace = znajdz()
    if istniejace is not None:
        if istniejace.sha256 != odcisk:
            # Ta sama wersja, inna zawartosc - paczke przebudowano bez zmiany
            # numeru. Wydajemy to, co faktycznie lezy na dysku, wiec zapis
            # musi za tym nadazyc, inaczej agent dostalby skrot nie do pary.
            log.info("paczka zrodel %s zmieniona - aktualizuje wpis wydania", wersja)
            istniejace.storage_name = nazwa
            istniejace.sha256 = odcisk
            istniejace.size_bytes = metadane["size_bytes"]
            db.commit()
        return istniejace

    db.add(
        AgentRelease(
            version=wersja,
            os_family="linux",
            arch=ARCH_ZRODLA,
            filename=f"cmdb-agent-{wersja}.tar.gz",
            storage_name=nazwa,
            sha256=odcisk,
            size_bytes=metadane["size_bytes"],
            notes="paczka zrodel zbudowana na serwerze",
            created_by="system",
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Inny proces roboczy zdazyl pierwszy - to nie jest blad, tylko wyscig
        # o zapis tej samej wartosci.
        db.rollback()
        log.debug("paczke zrodel %s zarejestrowal inny proces", wersja)
        return znajdz()

    log.info("zarejestrowano paczke zrodel agenta %s jako wydanie", wersja)
    return znajdz()
