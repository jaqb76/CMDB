"""Laczenie wpisu recznego z maszyna, ktora zglosila sie agentem.

Typowa droga: ktos wpisuje sprzet zanim ten trafi do sieci - zna dostawce,
umowe, lokalizacje i date zakupu. Pozniej na tej samej maszynie staje agent
i rejestruje sie WLASNYM identyfikatorem odczytanym ze sprzetu, ktory z
"reczne:<uuid>" nie ma nic wspolnego. Powstaja dwa wpisy o tym samym sprzecie.

Scalenie jest bezpieczne, bo oba opisy sa ROZLACZNE. Raport agenta nadpisuje
wylacznie hostname, domene, system, architekture, producenta, model, numer
seryjny, adres i wersje agenta. Nie dotyka niczego, co wpisuje czlowiek:
dostawcy, zakupu, gwarancji, lokalizacji, opiekuna ani uwag. Przeniesienie
tych pol nie jest wiec rozstrzyganiem konfliktu, tylko dolozeniem tego,
o co agent sie nie upomina.

Zostaje rekord agenta, a nie reczny: to on bedzie zbieral raporty, migawki,
zmiany i poswiadczenia. Wpis reczny zostaje WYCOFANY, a nie skasowany -
historia zostaje, a pomylke da sie zobaczyc.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    ZRODLO_AGENT,
    ZRODLO_RECZNE,
    Asset,
    AssetRelation,
    DiscoveryDevice,
    utcnow,
)
from .scoping import TenantContext, audit

log = logging.getLogger(__name__)

# Pola opisujace sprzet od strony czlowieka. Agent zadnego z nich nie zapisuje,
# wiec przenosimy je bez pytania o pierwszenstwo.
POLA_RECZNE = (
    "lokalizacja_id", "miejsce", "dostawca_id", "owner_id", "uzytkownik_id",
    "role_label", "notes", "purchase_date", "warranty_until", "purchase_price",
    "purchase_currency", "invoice_number", "support_contract", "purchase_notes",
)

# Numery seryjne, ktore producent zostawil niewypelnione. Wiazanie po nich
# scalilo by ze soba maszyny nie majace ze soba nic wspolnego - a to jest
# dokladnie ten rodzaj bledu, ktorego nikt nie zauwaza.
SMIECIOWE_SERIALE = {
    "", "0", "00000000", "0123456789", "123456789", "1234567890",
    "default string", "none", "n/a", "na", "not specified", "not applicable",
    "to be filled by o.e.m.", "to be filled by oem", "system serial number",
    "chassis serial number", "serial number", "invalid", "unknown",
    "oem", "xxxxxxx", "000000000000",
}

# Tylko te rodzaje moga byc ta sama rzecza co maszyna z agentem. Drukarka
# i monitor z przypadkowo tym samym numerem seryjnym nie sa kandydatami -
# agent stoi na komputerze, nie na monitorze.
RODZAJE_Z_AGENTEM = {"komputer", "vm", "host"}

MIN_DLUGOSC_SERIALA = 4


def sensowny_serial(numer: str | None) -> bool:
    """Czy po tym numerze wolno cokolwiek wiazac."""
    czysty = (numer or "").strip()
    if len(czysty) < MIN_DLUGOSC_SERIALA:
        return False
    if czysty.casefold() in SMIECIOWE_SERIALE:
        return False
    # Same zera albo same iksy to tez wypelniacz, tyle ze wlasnej produkcji.
    return len(set(czysty.casefold()) - {"0", "x", "-", " "}) > 0


def kandydat(db: Session, ctx: TenantContext, maszyna: Asset) -> Asset | None:
    """Wpis reczny opisujacy te sama maszyne, o ile jest dokladnie jeden.

    Warunek "dokladnie jeden" jest tu istotniejszy niz samo dopasowanie:
    przy dwoch kandydatach automat nie ma z czego wybrac, a zle scalenie
    jest nieodwracalne.
    """
    if maszyna.zrodlo != ZRODLO_AGENT or not sensowny_serial(maszyna.serial_number):
        return None
    numer = maszyna.serial_number.strip()
    pasujace = db.execute(
        select(Asset).where(
            Asset.tenant_id == ctx.tenant_id,
            Asset.id != maszyna.id,
            Asset.zrodlo == ZRODLO_RECZNE,
            Asset.lifecycle == LIFECYCLE_AKTYWNY,
            Asset.serial_number.is_not(None),
        )
    ).scalars().all()
    zgodne = [w for w in pasujace
              if (w.serial_number or "").strip().casefold() == numer.casefold()
              and w.typ in RODZAJE_Z_AGENTEM]
    return zgodne[0] if len(zgodne) == 1 else None


def scal(db: Session, ctx: TenantContext, docelowy: Asset, zrodlowy: Asset,
         sposob: str, ip: str | None = None) -> list[str]:
    """Przenosi dane reczne do maszyny z agentem i wycofuje wpis reczny.

    Zwraca nazwy przeniesionych pol - wywolujacy pokazuje je czlowiekowi,
    zeby scalenie nie bylo zdarzeniem bez sladu.
    """
    if docelowy.id == zrodlowy.id:
        raise ValueError("nie mozna scalic wpisu z samym soba")
    if docelowy.tenant_id != ctx.tenant_id or zrodlowy.tenant_id != ctx.tenant_id:
        raise ValueError("scalanie dziala wylacznie w obrebie jednej firmy")

    przeniesione: list[str] = []
    for pole in POLA_RECZNE:
        nowa = getattr(zrodlowy, pole, None)
        if nowa in (None, ""):
            continue
        if getattr(docelowy, pole, None) not in (None, ""):
            # Wartosc w rekordzie docelowym jest nowsza i ktos ja tam wpisal
            # swiadomie - scalenie nie moze jej cicho zastapic.
            continue
        setattr(docelowy, pole, nowa)
        przeniesione.append(pole)

    # Odwolania do wycofywanego wpisu przepinamy, inaczej relacje i wykryte
    # urzadzenia wskazywalyby rekord, ktory zniknal z ewidencji.
    for kolumna in (AssetRelation.source_id, AssetRelation.target_id):
        db.execute(update(AssetRelation)
                   .where(kolumna == zrodlowy.id, AssetRelation.tenant_id == ctx.tenant_id)
                   .values({kolumna.key: docelowy.id}))
    db.execute(update(DiscoveryDevice)
               .where(DiscoveryDevice.asset_id == zrodlowy.id,
                      DiscoveryDevice.tenant_id == ctx.tenant_id)
               .values(asset_id=docelowy.id))

    zrodlowy.lifecycle = LIFECYCLE_WYCOFANY
    zrodlowy.is_active = False
    zrodlowy.last_change_at = utcnow()
    docelowy.last_change_at = utcnow()

    audit(db, ctx, action="asset.scalony", target=docelowy.hostname,
          detail={"z_wpisu": zrodlowy.id, "nazwa_wpisu": zrodlowy.hostname,
                  "do_maszyny": docelowy.id, "sposob": sposob,
                  "przeniesione": przeniesione},
          ip=ip, actor=ctx.actor if sposob == "recznie" else "scalanie-automatyczne")
    log.info("scalono wpis reczny %s z maszyna %s (%s): %d pol",
             zrodlowy.hostname, docelowy.hostname, sposob, len(przeniesione))
    return przeniesione


def scal_automatycznie(db: Session, ctx: TenantContext, maszyna: Asset) -> Asset | None:
    """Laczy sama, gdy dowod jest jednoznaczny. Zwraca scalony wpis albo None."""
    zrodlowy = kandydat(db, ctx, maszyna)
    if zrodlowy is None:
        return None
    scal(db, ctx, maszyna, zrodlowy, sposob="automatycznie")
    return zrodlowy
