"""Dell OpenManage Enterprise: serwery z OME dopisane do ewidencji.

Konfiguracja polaczenia, polityka dla agenta, test i "Odczytaj teraz" ida
ta sama droga co wirtualizacja (services/nutanix.py, dostawca "ome").

OME opisuje fizyczny sprzet, ktory zwykle juz jest w CMDB - jako maszyna
z agentem, host ESXi z vCenter albo wpis reczny. Kluczem jest Service Tag,
czyli numer seryjny: serwer, ktorego numer jest juz w ewidencji, dostaje
dane OME na swojej karcie (sekcja "Sprzet (OME)"), a nie drugi wpis.
Wlasna karte (zrodlo "ome") zakladamy tylko serwerom, ktorych w ewidencji
nie ma. Gdy taki serwer pojawi sie pozniej inna droga (np. instalacja
agenta), kolejny odczyt przepina dane OME na tamta karte, a wpis z OME
wycofuje z adnotacja.
"""
from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import (
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    ZRODLO_AGENT,
    Asset,
    NutanixObiekt,
    WirtualizacjaPolaczenie,
    utcnow,
)
from . import nutanix

OME = nutanix.OME
RODZAJ = "serwer"   # NutanixObiekt.rodzaj


def _karty_po_numerze(db: Session, tenant_id: str, tagi: set[str]) -> dict[str, Asset]:
    """Service Tag -> karta spoza OME z tym numerem seryjnym (aktywna).

    Przy kilku kartach z tym samym numerem wygrywa maszyna z agentem, potem
    host wirtualizacji, potem reszta - agent wie o maszynie najwiecej.
    """
    if not tagi:
        return {}
    kolejnosc = case((Asset.zrodlo == ZRODLO_AGENT, 0), (Asset.typ == "host", 1), else_=2)
    wynik: dict[str, Asset] = {}
    for a in db.execute(select(Asset).where(
            Asset.tenant_id == tenant_id, Asset.zrodlo != OME, Asset.lifecycle == LIFECYCLE_AKTYWNY,
            func.lower(Asset.serial_number).in_(tagi)).order_by(kolejnosc, Asset.first_seen)).scalars():
        wynik.setdefault((a.serial_number or "").lower(), a)
    return wynik


def synchronizuj(db: Session, czytnik: Asset, wynik, dostawca: str = OME, przestrzen: str = "",
                 pol: WirtualizacjaPolaczenie | None = None) -> dict:
    """Przeklada udany odczyt OME na dane przy kartach serwerow."""
    tenant_id = czytnik.tenant_id
    teraz = utcnow()
    liczby = {"serwery": 0, "dopasowane": 0, "nowe": 0, "wycofane": 0}
    istniejace = {o.ext_id: o for o in db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.dostawca == OME,
        NutanixObiekt.rodzaj == RODZAJ)).scalars()}
    karty = _karty_po_numerze(db, tenant_id, {s.ext_id.lower() for s in wynik.urzadzenia})
    widziane: set[str] = set()

    for s in wynik.urzadzenia:
        tag = s.ext_id.upper()
        widziane.add(tag)
        o = istniejace.get(tag)
        if o is None:
            o = NutanixObiekt(tenant_id=tenant_id, rodzaj=RODZAJ, ext_id=tag, dostawca=OME)
            db.add(o)
            istniejace[tag] = o
        o.nazwa, o.dane, o.czytnik_id = (s.hostname_os or s.nazwa or tag)[:255], s.model_dump(), czytnik.id
        if pol is not None:
            o.polaczenie_id = pol.id
        o.widziany_o, o.zniknal_o = teraz, None
        liczby["serwery"] += 1

        karta = karty.get(tag.lower())
        if karta is not None:
            _przepnij(db, o, karta)
            liczby["dopasowane"] += 1
            continue
        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None or asset.tenant_id != tenant_id or asset.zrodlo != OME:
            machine_id = f"{OME}:{RODZAJ}:{tag}"
            asset = db.execute(select(Asset).where(
                Asset.tenant_id == tenant_id, Asset.machine_id == machine_id)).scalar_one_or_none()
            if asset is None:
                asset = Asset(tenant_id=tenant_id, machine_id=machine_id, typ="komputer", zrodlo=OME,
                              hostname=o.nazwa, first_seen=teraz)
                db.add(asset)
                db.flush()
                liczby["nowe"] += 1
        asset.hostname = o.nazwa
        asset.serial_number, asset.manufacturer, asset.model = tag, "Dell", s.model
        asset.os_name = s.system
        asset.last_seen = teraz
        nutanix._przywroc(asset, OME)
        o.asset_id = asset.id

    # Znikniecie: tylko serwery widziane dotad przez TO polaczenie.
    for tag, o in istniejace.items():
        if tag in widziane or o.zniknal_o is not None:
            continue
        if o.polaczenie_id is not None and (pol is None or o.polaczenie_id != pol.id):
            continue
        if o.polaczenie_id is None and o.czytnik_id != czytnik.id:
            continue
        nutanix._zniknij(db, tenant_id, o, OME, teraz, "zniknął z OME", liczby)
    db.flush()
    return liczby


def _przepnij(db: Session, o: NutanixObiekt, karta: Asset) -> None:
    """Dane OME na istniejacej karcie. Wpis zalozony wczesniej z samego OME
    zostaje wycofany z adnotacja (nie kasujemy - mogl dostac opiekuna, uwagi)."""
    if o.asset_id and o.asset_id != karta.id:
        stary = db.get(Asset, o.asset_id)
        if stary is not None and stary.zrodlo == OME and stary.lifecycle == LIFECYCLE_AKTYWNY:
            stary.lifecycle, stary.retired_at = LIFECYCLE_WYCOFANY, utcnow()
            stary.retired_by = OME
            stary.retired_reason = f"ten sam Service Tag co karta: {karta.hostname}"
    o.asset_id = karta.id


def obiekt_zasobu(db: Session, asset: Asset) -> NutanixObiekt | None:
    """Dane OME karty (sekcja "Sprzet (OME)")."""
    return db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == asset.tenant_id, NutanixObiekt.asset_id == asset.id,
        NutanixObiekt.dostawca == OME, NutanixObiekt.zniknal_o.is_(None),
    ).order_by(NutanixObiekt.widziany_o.desc()).limit(1)).scalar_one_or_none()
