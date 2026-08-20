"""Wykrywanie zasobow, ktore wygladaja na te sama maszyne.

Najczestsza przyczyna to klonowanie maszyn wirtualnych: kopia dziedziczy
UUID z SMBIOS, wiec zglasza sie pod tym samym identyfikatorem. Agent odrzuca
znane wartosci smieciowe wpisywane seryjnie przez producentow, ale kopii
zrobionej z dzialajacego systemu nie da sie odroznic po samym UUID.

Sygnalem sa wiec cechy, ktore w normalnej sytuacji sa niepowtarzalne:
numer seryjny obudowy i adresy sprzetowe kart sieciowych. Nie kasujemy
niczego automatycznie - to raport dla czlowieka, a rozwiazaniem jest
wycofanie jednego z zasobow.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LIFECYCLE_AKTYWNY, Asset


def znajdz_duplikaty(db: Session, tenant_id: str) -> list[dict]:
    """Grupy zasobow tej samej firmy o wspolnej cesze niepowtarzalnej."""
    maszyny = db.execute(
        select(Asset).where(
            Asset.tenant_id == tenant_id, Asset.lifecycle == LIFECYCLE_AKTYWNY
        )
    ).scalars().all()

    wedlug_cechy: dict[tuple[str, str], list[Asset]] = defaultdict(list)
    for maszyna in maszyny:
        numer = (maszyna.serial_number or "").strip()
        if numer:
            wedlug_cechy[("numer seryjny", numer)].append(maszyna)
        for mak in (maszyna.facts or {}).get("mac_addresses") or []:
            if mak:
                wedlug_cechy[("adres MAC", str(mak).upper())].append(maszyna)

    grupy: list[dict] = []
    juz_zgloszone: set[frozenset] = set()
    for (rodzaj, wartosc), zasoby in wedlug_cechy.items():
        if len(zasoby) < 2:
            continue
        # Ta sama para maszyn moze miec wspolny i numer seryjny, i adres MAC -
        # zglaszamy ja raz, z pierwsza znaleziona przyczyna.
        klucz = frozenset(z.id for z in zasoby)
        if klucz in juz_zgloszone:
            continue
        juz_zgloszone.add(klucz)
        grupy.append(
            {
                "rodzaj": rodzaj,
                "wartosc": wartosc,
                "maszyny": sorted(zasoby, key=lambda z: z.hostname),
            }
        )

    grupy.sort(key=lambda g: (g["rodzaj"], g["wartosc"]))
    return grupy
