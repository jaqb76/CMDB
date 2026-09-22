"""Kopie zapasowe calej instalacji: baza, pliki i opis archiwum.

Baza to nie wszystko. Instalacja to cztery rzeczy, z ktorych trzy nie leza
w Postgresie: wgrane wersje agenta, zalaczniki zgloszen oraz ``.env``
z ``CMDB_SECRET_KEY``. Archiwum obejmuje dwie pierwsze - trzecia NIE i to
jest decyzja, a nie przeoczenie: kluczem odszyfrowuje sie hasla skrzynki
zapisane w bazie, wiec klucz w tym samym pliku co zrzut bazy zamienilby kopie
zapasowa w komplet do odczytania wszystkiego. Klucz jedzie osobnym kanalem.

Kolejnosc w srodku archiwum nie jest dowolna: NAJPIERW baza, POTEM pliki.
Plik wgrany pomiedzy jednym a drugim ma wtedy postac sieroty bez wiersza
w bazie - nikomu to nie przeszkadza. W odwrotnej kolejnosci mielibysmy wiersz
bez pliku, czyli zepsuty link w watku zgloszenia.

Odtwarzanie nie dzieje sie tutaj i nie dzieje sie w dzialajacej aplikacji:
``pg_restore --clean`` musi usunac tabele, ktore trzymaja otwarte wlasne
workery. Panel wiec tylko UZBRAJA przywrocenie (``uzbroj``), a wykonuje je
``cmdb_server.wejscie`` przy starcie kontenera, zanim wstanie uvicorn.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import get_settings
from .. import wersja

log = logging.getLogger(__name__)

# Nazwa pliku z opisem archiwum. Lezy w srodku, obok zrzutu i plikow.
MANIFEST = "manifest.json"
ZRZUT = "baza.dump"
KATALOGI = {"releases": "release_dir", "helpdesk": "helpdesk_dir"}

# Znacznik "przy najblizszym starcie odtworz to archiwum". Plik, a nie wiersz
# w bazie - baza za chwile zostanie zastapiona i wiersz zniknalby razem z nia.
ZNACZNIK = "do-odtworzenia.json"

# Ile czasu dajemy pg_dump/pg_restore. Kopia duzej bazy potrafi trwac, ale
# proces, ktory stoi godzine, na pewno nie pracuje.
LIMIT_SEKUND = 1800


class BladKopii(RuntimeError):
    """Kopii nie udalo sie zrobic albo odtworzyc."""


@dataclass(frozen=True)
class Kopia:
    """Jedno archiwum na dysku."""

    nazwa: str
    sciezka: Path
    rozmiar: int
    utworzono: datetime
    rodzaj: str          # "dzienna" | "tygodniowa" | "reczna"
    opis: dict           # manifest, pusty gdy nie da sie go odczytac

    @property
    def wersja_portalu(self) -> str:
        return self.opis.get("wersja", "nieznana")

    @property
    def instancja(self) -> str:
        return self.opis.get("instancja", "")


# --- polaczenie z baza ------------------------------------------------------

def _parametry_bazy() -> tuple[list[str], dict[str, str], str]:
    """Argumenty i srodowisko dla pg_dump/pg_restore z CMDB_DATABASE_URL.

    Haslo idzie przez PGPASSWORD, a nie przez argument: lista argumentow
    procesu jest widoczna w ``ps`` dla kazdego na maszynie.

    Adres czyta ten sam parser, ktorym laczy sie aplikacja (SQLAlchemy),
    a nie ``urllib``. Haslo z ``openssl rand -base64`` potrafi zawierac ``/``,
    a reczne - ``#`` albo ``?``; ``urlparse`` bierze je za poczatek sciezki,
    fragmentu lub zapytania i gubi nazwe bazy, choc sama aplikacja laczy sie
    bez problemu. Kopia musi rozumiec adres dokladnie tak jak polaczenie.
    """
    try:
        adres = make_url(get_settings().database_url)
    except Exception as blad:  # noqa: BLE001 - kazdy blad parsera to zly adres
        raise BladKopii("CMDB_DATABASE_URL nie jest poprawnym adresem bazy") from blad
    baza = adres.database or ""
    if not baza:
        raise BladKopii("CMDB_DATABASE_URL nie wskazuje nazwy bazy")

    argumenty = ["--host", adres.host or "localhost",
                 "--port", str(adres.port or 5432)]
    if adres.username:
        argumenty += ["--username", adres.username]

    srodowisko = dict(os.environ)
    if adres.password:
        srodowisko["PGPASSWORD"] = str(adres.password)
    return argumenty, srodowisko, baza


def _uruchom(polecenie: list[str], srodowisko: dict[str, str], co: str) -> None:
    try:
        wynik = subprocess.run(
            polecenie, env=srodowisko, capture_output=True, text=True,
            timeout=LIMIT_SEKUND, check=False,
        )
    except FileNotFoundError as blad:
        # Najczestsza przyczyna: obraz bez klienta PostgreSQL. Mowimy wprost,
        # czego brakuje, zamiast zostawiac "No such file or directory".
        raise BladKopii(f"brak programu {polecenie[0]} w obrazie serwera") from blad
    except subprocess.TimeoutExpired as blad:
        raise BladKopii(f"{co} trwalo dluzej niz {LIMIT_SEKUND} s") from blad

    if wynik.returncode != 0:
        # Ostatnie linie bledu sa najkonkretniejsze; calosc idzie do dziennika.
        log.error("%s nie powiodlo sie: %s", co, wynik.stderr.strip())
        ogon = " | ".join(wynik.stderr.strip().splitlines()[-3:])
        raise BladKopii(f"{co} nie powiodlo sie: {ogon or 'bez komunikatu'}")


# --- katalog kopii ----------------------------------------------------------

def katalog() -> Path:
    sciezka = Path(get_settings().kopie_dir)
    sciezka.mkdir(parents=True, exist_ok=True)
    return sciezka


def _rodzaj_z_nazwy(nazwa: str) -> str:
    for rodzaj in ("dzienna", "tygodniowa", "reczna"):
        if f"-{rodzaj}." in nazwa:
            return rodzaj
    return "reczna"


def _opis_archiwum(sciezka: Path) -> dict:
    """Manifest ze srodka archiwum. Pusty slownik, gdy go tam nie ma."""
    try:
        with tarfile.open(sciezka, "r:gz") as paczka:
            plik = paczka.extractfile(MANIFEST)
            if plik is None:
                return {}
            return json.loads(plik.read().decode("utf-8"))
    except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError):
        return {}


def lista() -> list[Kopia]:
    """Kopie na dysku, od najnowszej."""
    zebrane: list[Kopia] = []
    for sciezka in katalog().glob("cmdb-*.tar.gz"):
        if not sciezka.is_file():
            continue
        stan = sciezka.stat()
        zebrane.append(Kopia(
            nazwa=sciezka.name,
            sciezka=sciezka,
            rozmiar=stan.st_size,
            utworzono=datetime.fromtimestamp(stan.st_mtime, tz=timezone.utc),
            rodzaj=_rodzaj_z_nazwy(sciezka.name),
            opis=_opis_archiwum(sciezka),
        ))
    return sorted(zebrane, key=lambda k: k.utworzono, reverse=True)


def znajdz(nazwa: str) -> Kopia | None:
    """Kopia o tej nazwie. Nazwa jest tylko nazwa pliku - nigdy sciezka.

    Pole z formularza nie moze wyprowadzic poza katalog kopii, wiec zamiast
    sklejac sciezke, szukamy po liscie tego, co faktycznie tam lezy.
    """
    czysta = Path(nazwa).name
    return next((k for k in lista() if k.nazwa == czysta), None)


def miejsce_na_dysku() -> tuple[int, int]:
    """(wolne bajty, rozmiar ostatniej kopii). Do decyzji, czy zaczynac."""
    wolne = shutil.disk_usage(katalog()).free
    kopie = lista()
    return wolne, (kopie[0].rozmiar if kopie else 0)


# --- tworzenie --------------------------------------------------------------

def _nazwa_pliku(rodzaj: str, teraz: datetime) -> Path:
    """Wolna nazwa pliku dla kopii.

    Sama data nie wystarcza, niezaleznie od rozdzielczosci: dwie kopie moga
    powstac w tej samej sekundzie (skrypt w petli, dwa klikniecia, mala baza),
    a wtedy druga po cichu nadpisywalaby pierwsza. Dlatego przy zajetej nazwie
    doklejamy licznik, zamiast ufac zegarowi.
    """
    podstawa = f"cmdb-{teraz.strftime('%Y%m%d-%H%M%S')}"
    cel = katalog() / f"{podstawa}-{rodzaj}.tar.gz"
    numer = 2
    while cel.exists():
        cel = katalog() / f"{podstawa}-{numer}-{rodzaj}.tar.gz"
        numer += 1
    return cel


def utworz(rodzaj: str = "reczna", autor: str = "system") -> Kopia:
    """Zrzut bazy plus pliki w jednym archiwum.

    Archiwum powstaje w pliku tymczasowym i dopiero gotowe jest przenoszone
    pod docelowa nazwe. Przerwana kopia nie zostaje wtedy na liscie jako
    kusząco wygladajacy, niekompletny plik.
    """
    if rodzaj not in ("dzienna", "tygodniowa", "reczna"):
        raise BladKopii(f"nieznany rodzaj kopii: {rodzaj}")

    ustawienia = get_settings()
    wolne, ostatnia = miejsce_na_dysku()
    if ostatnia and wolne < 2 * ostatnia:
        raise BladKopii(
            f"za malo miejsca na dysku: wolne {wolne // 1024 // 1024} MB, "
            f"ostatnia kopia {ostatnia // 1024 // 1024} MB"
        )

    argumenty, srodowisko, baza = _parametry_bazy()
    teraz = datetime.now(timezone.utc)
    cel = _nazwa_pliku(rodzaj, teraz)

    with tempfile.TemporaryDirectory(dir=str(katalog())) as roboczy:
        praca = Path(roboczy)

        # 1. Baza. Format wlasny: skompresowany i pozwalajacy pg_restore
        #    wyjac pojedyncza tabele, gdyby kiedys zaszla taka potrzeba.
        zrzut = praca / ZRZUT
        _uruchom(
            ["pg_dump", *argumenty, "--format=custom", "--file", str(zrzut), baza],
            srodowisko, "pg_dump",
        )

        # 2. Pliki - PO bazie, swiadomie. Zalacznik wgrany pomiedzy jednym
        #    a drugim krokiem zostanie sierota bez wiersza; w odwrotnej
        #    kolejnosci bylby wiersz bez pliku, czyli zepsuty link w watku.
        obecne: dict[str, str] = {}
        for nazwa, pole in KATALOGI.items():
            zrodlo = Path(getattr(ustawienia, pole))
            if zrodlo.is_dir():
                shutil.copytree(zrodlo, praca / nazwa)
                obecne[nazwa] = str(zrodlo)

        manifest = {
            "format": 1,
            "utworzono": teraz.isoformat(),
            "rodzaj": rodzaj,
            "autor": autor,
            "wersja": wersja.opis(),
            "instancja": ustawienia.instancja or "produkcja",
            "baza": baza,
            "katalogi": obecne,
        }
        (praca / MANIFEST).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        tymczasowy = cel.with_suffix(".tworzone")
        with tarfile.open(tymczasowy, "w:gz") as paczka:
            for element in sorted(praca.iterdir()):
                paczka.add(element, arcname=element.name)
        tymczasowy.replace(cel)

    log.info("kopia %s gotowa (%d B), zrobil %s", cel.name, cel.stat().st_size, autor)
    return znajdz(cel.name)  # type: ignore[return-value]


def rotuj() -> list[str]:
    """Kasuje nadmiarowe kopie. Zwraca nazwy usunietych.

    Rotacja liczy sie osobno dla dziennych i tygodniowych, a kopii recznych
    nie rusza wcale: skoro ktos zrobil ja swiadomie przed ryzykowna zmiana,
    to nocne zadanie nie ma prawa jej sprzatnac.
    """
    ustawienia = get_settings()
    limity = {
        "dzienna": ustawienia.kopie_dziennych,
        "tygodniowa": ustawienia.kopie_tygodniowych,
    }
    usuniete: list[str] = []
    for rodzaj, limit in limity.items():
        pasujace = [k for k in lista() if k.rodzaj == rodzaj]
        for kopia in pasujace[limit:]:
            try:
                kopia.sciezka.unlink()
                usuniete.append(kopia.nazwa)
            except OSError as blad:
                log.warning("nie usunalem kopii %s: %s", kopia.nazwa, blad)
    if usuniete:
        log.info("rotacja usunela %d kopii: %s", len(usuniete), ", ".join(usuniete))
    return usuniete


def kopia_nocna(autor: str = "cron") -> Kopia:
    """Zadanie z crona: w niedziele tygodniowa, w pozostale dni dzienna."""
    teraz = datetime.now(timezone.utc)
    kopia = utworz("tygodniowa" if teraz.weekday() == 6 else "dzienna", autor=autor)
    rotuj()
    return kopia


# --- odtwarzanie ------------------------------------------------------------

def sprawdz_archiwum(sciezka: Path) -> dict:
    """Manifest archiwum albo blad z powodem.

    To nie jest zabezpieczenie przed zlym zamiarem - kto moze przywracac,
    ten i tak wykonuje SQL jako wlasciciel bazy. To zabezpieczenie przed
    pomylka: plikiem sprzed roku, archiwum z innej instalacji, uciętym
    transferem.
    """
    if not sciezka.is_file():
        raise BladKopii("nie ma takiego pliku")
    opis = _opis_archiwum(sciezka)
    if not opis:
        raise BladKopii("to nie wyglada na kopie CMDB - brak manifestu w archiwum")
    if opis.get("format") != 1:
        raise BladKopii(f"nieobslugiwany format kopii: {opis.get('format')}")

    try:
        with tarfile.open(sciezka, "r:gz") as paczka:
            nazwy = paczka.getnames()
    except (OSError, tarfile.TarError) as blad:
        raise BladKopii(f"archiwum jest uszkodzone: {blad}") from blad
    if ZRZUT not in nazwy:
        raise BladKopii("w archiwum nie ma zrzutu bazy")
    return opis


def uzbroj(sciezka: Path, autor: str) -> dict:
    """Odklada archiwum do odtworzenia przy najblizszym starcie.

    Panel nie odtwarza bazy sam: uvicorn chodzi w kilku workerach, kazdy
    trzyma polaczenia, a w tle kreca sie petla IMAP, harmonogram i import
    wydan. pg_restore --clean musialby usunac tabele, ktore te sesje trzymaja
    otwarte. Dlatego panel zapisuje znacznik, a odtwarza wejscie kontenera -
    tam zadnego polaczenia aplikacji jeszcze nie ma.
    """
    opis = sprawdz_archiwum(sciezka)
    docelowy = katalog() / "do-odtworzenia.tar.gz"
    if sciezka.resolve() != docelowy.resolve():
        shutil.copy2(sciezka, docelowy)

    znacznik = {
        "plik": docelowy.name,
        "zrodlo": sciezka.name,
        "uzbroil": autor,
        "uzbrojono": datetime.now(timezone.utc).isoformat(),
        "manifest": opis,
    }
    (katalog() / ZNACZNIK).write_text(
        json.dumps(znacznik, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Wpis takze do dziennika kontenera: audyt w bazie za chwile zostanie
    # zastapiony odtwarzana zawartoscia i slad po tej decyzji by zniknal.
    log.warning(
        "PRZYWRACANIE UZBROJONE przez %s z archiwum %s (kopia z %s, wersja %s)",
        autor, sciezka.name, opis.get("utworzono"), opis.get("wersja"),
    )
    return znacznik


def uzbrojone() -> dict | None:
    """Znacznik czekajacy na restart albo None."""
    plik = katalog() / ZNACZNIK
    if not plik.is_file():
        return None
    try:
        return json.loads(plik.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def rozbroj() -> bool:
    """Odwolanie uzbrojonego przywrocenia. True, gdy bylo co odwolywac."""
    plik = katalog() / ZNACZNIK
    if not plik.is_file():
        return False
    plik.unlink()
    log.warning("przywracanie odwolane przed restartem")
    return True


def odtworz(sciezka: Path, *, do_bazy: str | None = None) -> dict:
    """Odtwarza archiwum. Wolane przy starcie kontenera albo do sprawdzenia.

    ``do_bazy`` wskazuje baze pomocnicza - wtedy niczego nie ruszamy
    w dzialajacej instalacji i sluzy to wylacznie sprawdzeniu, czy kopia
    w ogole daje sie odtworzyc. Bez tego argumentu odtwarzamy w miejsce
    i wolno to robic TYLKO wtedy, gdy aplikacja jeszcze nie wstala.
    """
    opis = sprawdz_archiwum(sciezka)
    argumenty, srodowisko, baza = _parametry_bazy()
    cel = do_bazy or baza

    with tempfile.TemporaryDirectory() as roboczy:
        praca = Path(roboczy)
        with tarfile.open(sciezka, "r:gz") as paczka:
            # filter="data" odrzuca dowiazania i sciezki wychodzace poza
            # katalog - archiwum moglo przyjsc z zewnatrz.
            paczka.extractall(praca, filter="data")

        _uruchom(
            ["pg_restore", *argumenty, "--dbname", cel,
             "--clean", "--if-exists", "--no-owner", "--no-privileges",
             str(praca / ZRZUT)],
            srodowisko, "pg_restore",
        )

        if do_bazy is None:
            # Pliki odtwarzamy tylko przy przywracaniu w miejsce. Sprawdzenie
            # kopii dotyczy bazy i nie ma prawa podmienic zalacznikow.
            ustawienia = get_settings()
            for nazwa, pole in KATALOGI.items():
                zrodlo = praca / nazwa
                if not zrodlo.is_dir():
                    continue
                docelowy = Path(getattr(ustawienia, pole))
                docelowy.mkdir(parents=True, exist_ok=True)
                for element in zrodlo.rglob("*"):
                    wzgledna = element.relative_to(zrodlo)
                    if element.is_dir():
                        (docelowy / wzgledna).mkdir(parents=True, exist_ok=True)
                    else:
                        shutil.copy2(element, docelowy / wzgledna)

    log.info("odtworzono kopie %s do bazy %s", sciezka.name, cel)
    return opis
