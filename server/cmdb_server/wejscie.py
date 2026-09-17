"""Wejscie kontenera: odtworzenie kopii, potem to, co kazano uruchomic.

Ten modul stoi PRZED uvicornem i tylko dlatego przywracanie bazy w ogole jest
mozliwe. W dzialajacej aplikacji nie jest: uvicorn chodzi w kilku workerach,
kazdy trzyma polaczenia do bazy, a w tle kreca sie petla IMAP, harmonogram
i import wydan. ``pg_restore --clean`` musialby usunac tabele, ktore te sesje
trzymaja otwarte, i albo by sie zablokowal, albo urwal w polowie. Tutaj zadnej
z tych rzeczy jeszcze nie ma.

Panel nie odtwarza wiec niczego sam - zapisuje znacznik (``kopie.uzbroj``)
i mowi, ze trzeba zrestartowac kontener. Ta droga jest dluzsza o jedno
polecenie i o cala klase problemow krotsza.

Blad odtwarzania NIE zatrzymuje startu serwera. Instalacja, ktora nie wstaje,
bo kopia byla uszkodzona, jest gorsza od instalacji dzialajacej na starych
danych z czytelnym bledem w dzienniku: w tej drugiej mozna sie zalogowac
i sprobowac jeszcze raz.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("cmdb.wejscie")


def _neutralizuj_dev() -> None:
    """Na instancji testowej wylacza skrzynke odtworzona z produkcji.

    To nie moze byc wybor przy przywracaniu, bo wybor da sie przeoczyc raz -
    a jeden raz wystarczy. Dev z kopia produkcji ma te same haslo do skrzynki
    i te sama petle IMAP: pobralby maila klienta, oznaczyl jako przeczytany,
    zalozyl zgloszenie u siebie, a produkcja tego maila JUZ BY NIE ZOBACZYLA.
    Do tego rozsylalby potwierdzenia i odpowiedzi pod prawdziwe adresy.
    """
    from .db import SessionLocal
    from .models import HelpdeskUstawienia

    with SessionLocal() as db:
        wpis = db.get(HelpdeskUstawienia, "helpdesk")
        if wpis is None:
            return
        wpis.aktywne = False
        wpis.imap_haslo_szyfr = None
        wpis.smtp_haslo_szyfr = None
        db.commit()
    log.warning(
        "instancja dev: skrzynka helpdesku wylaczona, hasla wyczyszczone - "
        "odtworzona kopia nie bedzie czytac ani wysylac poczty"
    )


def _sprawdz_klucz() -> None:
    """Czy tym kluczem da sie odczytac hasla zapisane w odtworzonej bazie.

    Hasla skrzynki sa szyfrowane kluczem wyprowadzonym z CMDB_SECRET_KEY.
    Po odtworzeniu na serwerze z innym kluczem baza wstaje bez jednego bledu,
    a skrzynka po prostu milczy - i nikt nie wie dlaczego. Lepiej powiedziec
    to wprost przy starcie.
    """
    from .db import SessionLocal
    from .models import HelpdeskUstawienia
    from .services import sekrety

    with SessionLocal() as db:
        wpis = db.get(HelpdeskUstawienia, "helpdesk")
        zaszyfrowane = wpis.smtp_haslo_szyfr if wpis else None
    if not zaszyfrowane:
        return
    if sekrety.odszyfruj(zaszyfrowane) is None:
        log.warning(
            "UWAGA: klucz tej instancji (CMDB_SECRET_KEY) jest inny niz ten, "
            "ktorym zaszyfrowano hasla w odtworzonej bazie - hasla skrzynki "
            "trzeba wpisac ponownie w panelu"
        )


def odtworz_jesli_uzbrojone() -> None:
    """Wykonuje przywrocenie odlozone przez panel, o ile jakies czeka."""
    from .services import kopie

    znacznik = kopie.uzbrojone()
    if znacznik is None:
        return

    archiwum = kopie.katalog() / znacznik.get("plik", "")
    log.warning(
        "przywracam kopie %s uzbrojona przez %s",
        znacznik.get("zrodlo") or archiwum.name, znacznik.get("uzbroil", "?"),
    )
    try:
        kopie.odtworz(archiwum)
    except kopie.BladKopii as blad:
        # Znacznik ZOSTAJE: nieudane przywrocenie ma sie powtorzyc przy
        # kolejnym starcie albo zostac odwolane swiadomie z panelu. Ciche
        # skasowanie go wygladaloby jak sukces.
        log.error("przywracanie nie powiodlo sie: %s", blad)
        return

    kopie.rozbroj()
    log.warning("przywracanie zakonczone")

    from .config import get_settings

    try:
        if get_settings().env == "dev":
            _neutralizuj_dev()
        _sprawdz_klucz()
    except Exception as blad:  # noqa: BLE001 - start serwera jest wazniejszy
        log.error("krok po przywracaniu nie powiodl sie: %s", blad)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("CMDB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    argumenty = list(argv if argv is not None else sys.argv[1:])
    if not argumenty:
        raise SystemExit("wejscie: podaj polecenie do uruchomienia")

    try:
        odtworz_jesli_uzbrojone()
    except Exception as blad:  # noqa: BLE001
        # Serwer ma wstac takze wtedy, gdy przywracanie sie nie powiodlo.
        log.error("przywracanie przerwane bledem: %s", blad)

    # exec zamiast podprocesu: uvicorn przejmuje PID 1 i sygnaly z dockera
    # (docker stop) docieraja do niego wprost, a nie do posrednika.
    os.execvp(argumenty[0], argumenty)


if __name__ == "__main__":
    main()
