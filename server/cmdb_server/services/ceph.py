"""Ceph (Ceph Dashboard): zapis odczytu do ewidencji i relacja z OpenStackiem.

Konfiguracja polaczenia, polityka dla agenta, test i "Odczytaj teraz" ida
ta sama droga co wirtualizacja (services/nutanix.py, dostawca "ceph").
Rozni sie tylko zapis udanego odczytu: klaster Ceph to jeden zasob rodzaju
"magazyn" (pamiec masowa) z danymi w obiekcie odczytu - wezly i pule sa jego
szczegolami, nie osobnymi zasobami.

Klaster identyfikuje fsid - ten sam w kazdym miejscu, ktore o nim mowi.
Agent OpenStacka podaje fsid klastrow pod backendami Cindera (RBD), wiec
region OpenStacka laczy sie z klastrem Ceph relacja cluster_storage.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, AssetRelation, NutanixObiekt, WirtualizacjaPolaczenie, utcnow
from . import nutanix, rodzaje

CEPH = nutanix.CEPH
RODZAJ = "ceph"           # NutanixObiekt.rodzaj - poza drzewem klaster/host/VM
TYP = "magazyn"           # Asset.typ
RELACJA = "cluster_storage"


def synchronizuj(db: Session, czytnik: Asset, wynik, dostawca: str = CEPH, przestrzen: str = "",
                 pol: WirtualizacjaPolaczenie | None = None) -> dict:
    """Przeklada udany odczyt Dashboardu na zasob klastra Ceph."""
    tenant_id = czytnik.tenant_id
    rodzaje.zapewnij_rodzaj(db, tenant_id, TYP)
    teraz = utcnow()
    liczby = {"klastry": 0, "hosty": 0, "pule": 0, "osd": 0, "nowe": 0, "wycofane": 0}
    istniejace = {o.ext_id: o for o in db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.dostawca == CEPH,
        NutanixObiekt.rodzaj == RODZAJ)).scalars()}
    widziane: set[str] = set()
    for k in wynik.klastry:
        fsid = k.ext_id.lower()
        widziane.add(fsid)
        # Nazwa w ewidencji: wlasna nazwa polaczenia (np. "Ceph DC1"), inaczej adres.
        nazwa = (nutanix.nazwa_polaczenia(pol) if pol is not None else "") or f"ceph-{fsid[:8]}"
        o = istniejace.get(fsid)
        if o is None:
            o = NutanixObiekt(tenant_id=tenant_id, rodzaj=RODZAJ, ext_id=fsid, dostawca=CEPH)
            db.add(o)
            istniejace[fsid] = o
        o.nazwa, o.dane, o.czytnik_id = nazwa[:255], k.model_dump(exclude={"ext_id"}, by_alias=True), czytnik.id
        if pol is not None:
            o.polaczenie_id = pol.id
        o.widziany_o, o.zniknal_o = teraz, None

        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None or asset.tenant_id != tenant_id:
            machine_id = f"{CEPH}:{RODZAJ}:{fsid}"
            asset = db.execute(select(Asset).where(
                Asset.tenant_id == tenant_id, Asset.machine_id == machine_id)).scalar_one_or_none()
            if asset is None:
                asset = Asset(tenant_id=tenant_id, machine_id=machine_id, typ=TYP, zrodlo=CEPH,
                              hostname=nazwa[:255], first_seen=teraz)
                db.add(asset)
                db.flush()
                liczby["nowe"] += 1
        if asset.zrodlo == CEPH:
            asset.hostname = nazwa[:255]
            asset.typ, asset.manufacturer, asset.model = TYP, "Ceph", "Ceph"
            asset.os_name, asset.os_version = "Ceph", k.wersja
            asset.serial_number = fsid
            asset.last_seen = teraz
            nutanix._przywroc(asset, CEPH)
        o.asset_id = asset.id
        liczby["klastry"] += 1
        liczby["hosty"] += k.liczba_hostow or len(k.hosty)
        liczby["pule"] += len(k.pule)
        liczby["osd"] += k.liczba_osd or 0

    # Znikniecie: tylko klastry widziane dotad przez TO polaczenie.
    powod = "zniknął z Ceph Dashboard"
    for fsid, o in istniejace.items():
        if fsid in widziane or o.zniknal_o is not None:
            continue
        if o.polaczenie_id is not None and (pol is None or o.polaczenie_id != pol.id):
            continue
        if o.polaczenie_id is None and o.czytnik_id != czytnik.id:
            continue
        nutanix._zniknij(db, tenant_id, o, CEPH, teraz, powod, liczby)
    db.flush()
    polacz_magazyny(db, tenant_id)
    return liczby


def polacz_magazyny(db: Session, tenant_id: str) -> None:
    """Relacje region OpenStacka -> klaster Ceph wg fsid z Cindera.

    Wolane po kazdym odczycie OpenStacka i Cepha (i po usunieciu polaczenia),
    bo dowolna strona moze pojawic sie pierwsza. Relacje wykryte (created_by
    "openstack") sa uzgadniane z odczytem; dodane recznie zostaja nietkniete.
    """
    obiekty = db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.zniknal_o.is_(None),
        NutanixObiekt.asset_id.is_not(None),
        ((NutanixObiekt.dostawca == CEPH) & (NutanixObiekt.rodzaj == RODZAJ))
        | ((NutanixObiekt.dostawca == nutanix.OPENSTACK) & (NutanixObiekt.rodzaj == nutanix.KLASTER)),
    )).scalars().all()
    ceph = {o.ext_id: o.asset_id for o in obiekty if o.dostawca == CEPH}
    chciane = set()
    for o in obiekty:
        if o.dostawca != nutanix.OPENSTACK:
            continue
        for m in (o.dane or {}).get("magazyny") or []:
            cel = ceph.get(str(m.get("fsid") or "").lower())
            if cel and cel != o.asset_id:
                chciane.add((o.asset_id, cel))
    obecne = db.execute(select(AssetRelation).where(
        AssetRelation.tenant_id == tenant_id, AssetRelation.kind == RELACJA)).scalars().all()
    wszystkie = {(r.source_id, r.target_id) for r in obecne}
    for r in obecne:
        if r.created_by == nutanix.OPENSTACK and (r.source_id, r.target_id) not in chciane:
            db.delete(r)
    for zrodlo, cel in chciane - wszystkie:
        db.add(AssetRelation(tenant_id=tenant_id, source_id=zrodlo, target_id=cel, kind=RELACJA,
                             created_by=nutanix.OPENSTACK))
    db.flush()


def klastry(db: Session, tenant_id: str, zrodlo: WirtualizacjaPolaczenie | None = None) -> list[dict]:
    """Klastry Ceph dla strony Wirtualizacja, z regionami OpenStacka, ktore z nich korzystaja."""
    warunki = [NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.zniknal_o.is_(None),
               NutanixObiekt.dostawca == CEPH, NutanixObiekt.rodzaj == RODZAJ]
    if zrodlo is not None:
        warunki.append(NutanixObiekt.polaczenie_id == zrodlo.id)
    obiekty = db.execute(select(NutanixObiekt).where(*warunki).order_by(NutanixObiekt.nazwa)).scalars().all()
    if not obiekty:
        return []
    ids = [o.asset_id for o in obiekty if o.asset_id]
    zasoby = {a.id: a for a in db.execute(select(Asset).where(Asset.id.in_(ids))).scalars()}
    uzywa: dict[str, list[Asset]] = {}
    for r in db.execute(select(AssetRelation).where(
            AssetRelation.tenant_id == tenant_id, AssetRelation.kind == RELACJA,
            AssetRelation.target_id.in_(ids))).scalars():
        uzywa.setdefault(r.target_id, []).append(r.source)
    return [{"obiekt": o, "asset": zasoby.get(o.asset_id),
             "uzywa": sorted(uzywa.get(o.asset_id, []), key=lambda a: a.hostname)} for o in obiekty]
