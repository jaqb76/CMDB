"""NetApp ONTAP: zapis odczytu klastra do ewidencji.

Konfiguracja polaczenia, polityka dla agenta, test i "Odczytaj teraz" ida
ta sama droga co wirtualizacja (services/nutanix.py, dostawca "netapp").
Jak przy Cephie (services/ceph.py): klaster to jeden zasob rodzaju "magazyn"
(pamiec masowa), a wezly, agregaty, SVM, wolumeny, LUN-y, dyski, polki,
interfejsy i SnapMirror sa jego szczegolami w obiekcie odczytu.

Klaster identyfikuje UUID ONTAP. Nazwa w ewidencji to nazwa klastra z ONTAP
(np. "pod01-netapp01"), a gdy jej brak - nazwa polaczenia.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, AssetRelation, NutanixObiekt, WirtualizacjaPolaczenie, utcnow
from . import nutanix, rodzaje

NETAPP = nutanix.NETAPP
RODZAJ = "netapp"   # NutanixObiekt.rodzaj - poza drzewem klaster/host/VM
TYP = "magazyn"     # Asset.typ


def synchronizuj(db: Session, czytnik: Asset, wynik, dostawca: str = NETAPP, przestrzen: str = "",
                 pol: WirtualizacjaPolaczenie | None = None) -> dict:
    """Przeklada udany odczyt ONTAP na zasob klastra."""
    tenant_id = czytnik.tenant_id
    rodzaje.zapewnij_rodzaj(db, tenant_id, TYP)
    teraz = utcnow()
    liczby = {"klastry": 0, "wezly": 0, "wolumeny": 0, "dyski": 0, "nowe": 0, "wycofane": 0}
    istniejace = {o.ext_id: o for o in db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.dostawca == NETAPP,
        NutanixObiekt.rodzaj == RODZAJ)).scalars()}
    widziane: set[str] = set()
    for k in wynik.klastry:
        uuid = k.ext_id.lower()
        widziane.add(uuid)
        nazwa = k.nazwa or (nutanix.nazwa_polaczenia(pol) if pol is not None else "") or f"netapp-{uuid[:8]}"
        o = istniejace.get(uuid)
        if o is None:
            o = NutanixObiekt(tenant_id=tenant_id, rodzaj=RODZAJ, ext_id=uuid, dostawca=NETAPP)
            db.add(o)
            istniejace[uuid] = o
        o.nazwa, o.dane, o.czytnik_id = nazwa[:255], k.model_dump(exclude={"ext_id"}), czytnik.id
        if pol is not None:
            o.polaczenie_id = pol.id
        o.widziany_o, o.zniknal_o = teraz, None

        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None or asset.tenant_id != tenant_id:
            machine_id = f"{NETAPP}:{RODZAJ}:{uuid}"
            asset = db.execute(select(Asset).where(
                Asset.tenant_id == tenant_id, Asset.machine_id == machine_id)).scalar_one_or_none()
            if asset is None:
                asset = Asset(tenant_id=tenant_id, machine_id=machine_id, typ=TYP, zrodlo=NETAPP,
                              hostname=nazwa[:255], first_seen=teraz)
                db.add(asset)
                db.flush()
                liczby["nowe"] += 1
        if asset.zrodlo == NETAPP:
            modele = sorted({w.model for w in k.wezly if w.model})
            asset.hostname = nazwa[:255]
            asset.typ, asset.manufacturer, asset.model = TYP, "NetApp", (", ".join(modele) or "ONTAP")[:255]
            asset.os_name, asset.os_version = "ONTAP", k.wersja
            asset.serial_number = k.numer_seryjny or uuid
            asset.primary_ip = k.ip
            asset.last_seen = teraz
            nutanix._przywroc(asset, NETAPP)
        o.asset_id = asset.id
        liczby["klastry"] += 1
        liczby["wezly"] += len(k.wezly)
        liczby["wolumeny"] += len(k.wolumeny)
        liczby["dyski"] += len(k.dyski)

    powod = "zniknął z ONTAP"
    for uuid, o in istniejace.items():
        if uuid in widziane or o.zniknal_o is not None:
            continue
        if o.polaczenie_id is not None and (pol is None or o.polaczenie_id != pol.id):
            continue
        if o.polaczenie_id is None and o.czytnik_id != czytnik.id:
            continue
        nutanix._zniknij(db, tenant_id, o, NETAPP, teraz, powod, liczby)
    db.flush()
    return liczby


def klastry(db: Session, tenant_id: str, zrodlo: WirtualizacjaPolaczenie | None = None) -> list[dict]:
    """Klastry NetApp dla strony Wirtualizacja (sekcja Pamiec masowa)."""
    warunki = [NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.zniknal_o.is_(None),
               NutanixObiekt.dostawca == NETAPP, NutanixObiekt.rodzaj == RODZAJ]
    if zrodlo is not None:
        warunki.append(NutanixObiekt.polaczenie_id == zrodlo.id)
    obiekty = db.execute(select(NutanixObiekt).where(*warunki).order_by(NutanixObiekt.nazwa)).scalars().all()
    if not obiekty:
        return []
    ids = [o.asset_id for o in obiekty if o.asset_id]
    zasoby = {a.id: a for a in db.execute(select(Asset).where(Asset.id.in_(ids))).scalars()}
    uzywa: dict[str, list[Asset]] = {}
    for r in db.execute(select(AssetRelation).where(
            AssetRelation.tenant_id == tenant_id, AssetRelation.kind == "cluster_storage",
            AssetRelation.target_id.in_(ids))).scalars():
        uzywa.setdefault(r.target_id, []).append(r.source)
    return [{"obiekt": o, "asset": zasoby.get(o.asset_id),
             "uzywa": sorted(uzywa.get(o.asset_id, []), key=lambda a: a.hostname)} for o in obiekty]
