"""Budowanie paczki zrodel agenta wydawanej przez serwer.

    python -m cmdb_server.pakiet ../agent

Uruchamiane przy wdrozeniu (i przez obraz Dockera), bo katalog server/ nie
zawiera zrodel agenta - trzeba je wskazac jawnie.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .services import pakiet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="buduje paczke zrodel agenta")
    parser.add_argument("zrodla", type=Path, help="katalog agent/ z repozytorium")
    parser.add_argument("--do", type=Path, default=None,
                        help="katalog wydan (domyslnie z ustawien serwera)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    katalog_wydan = args.do or pakiet.katalog_paczki()

    try:
        metadane = pakiet.zbuduj(args.zrodla, katalog_wydan)
    except pakiet.BrakZrodel as exc:
        print(f"BLAD: {exc}", file=sys.stderr)
        return 2

    print(f"wersja   : {metadane['version']}")
    print(f"plikow   : {metadane['files']}")
    print(f"rozmiar  : {metadane['size_bytes']} B")
    print(f"sha256   : {metadane['sha256']}")
    print(f"zapisano : {pakiet.sciezka_archiwum(katalog_wydan)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
