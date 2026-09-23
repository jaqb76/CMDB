"""Nutanix Prism Central: konfiguracja dla agenta i wlaczanie odczytu do ewidencji.

Z Prism laczy sie agent (patrz agent/cmdb_agent/nutanix.py). Serwer tylko
wydaje mu konfiguracje, przyjmuje wynik i przeklada go na zasoby i relacje:

    klaster  <- host_cluster -  host  <- vm_host -  maszyna wirtualna

Maszyna wirtualna, w ktorej dziala agent CMDB, nie dostaje drugiego wpisu:
jej UUID z BIOS-u (raport agenta) rowna sie identyfikatorowi VM w Prism,
wiec odczyt dopisuje sie do karty maszyny agenta. Pozostale VM, hosty
i klastry dostaja wpisy o zrodle "nutanix".
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    ZRODLO_AGENT,
    ZRODLO_NUTANIX,
    Asset,
    AssetChange,
    AssetCurrentReport,
    AssetRelation,
    NutanixObiekt,
    NutanixUstawienia,
    utcnow,
)
from ..nutanix_schema import WynikNutanix
from . import sekrety

log = logging.getLogger(__name__)

KLASTER, HOST, VM = "klaster", "host", "vm"
WYCOFAL = "nutanix"  # retired_by - odroznia wycofanie z odczytu od decyzji czlowieka
INTERWALY_MINUT = (15, 30, 60, 120, 240, 720, 1440)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# --- konfiguracja -----------------------------------------------------------

def ustawienia(db: Session, asset: Asset) -> NutanixUstawienia | None:
    row = db.get(NutanixUstawienia, asset.id)
    if row is not None and row.tenant_id != asset.tenant_id:
        return None
    return row


def wersja(row: NutanixUstawienia | None) -> str:
    return row.revision if row else "unassigned"


def test_oczekuje(row: NutanixUstawienia | None) -> bool:
    if row is None or row.test_zlecony_o is None:
        return False
    return row.test_o is None or row.test_o < row.test_zlecony_o


def odczyt_oczekuje(row: NutanixUstawienia | None) -> bool:
    if row is None or not row.wlaczona or row.odczyt_zlecony_o is None:
        return False
    return row.odczyt_o is None or row.odczyt_o < row.odczyt_zlecony_o


def polityka_dla_agenta(db: Session, asset: Asset) -> dict:
    """Konfiguracja odczytu dla tej maszyny - albo "wylaczone".

    Haslo jedzie wylacznie tu, wylacznie do maszyny, ktorej je przypisano,
    i wylacznie wtedy, gdy funkcja jest wlaczona. Test polaczenia dostaje
    konfiguracje nawet przy wylaczonym odczycie - inaczej nie dalo by sie
    sprawdzic ustawien przed wlaczeniem.
    """
    row = ustawienia(db, asset)
    aktywna = bool(asset.is_active and not asset.enrollment_blocked)
    test = aktywna and test_oczekuje(row)
    if row is None or not aktywna or not (row.wlaczona or test):
        return {"enabled": False, "test": False}
    haslo = sekrety.odszyfruj(row.haslo)
    if not row.adres or not row.uzytkownik or haslo is None:
        return {"enabled": False, "test": False}
    return {
        "enabled": bool(row.wlaczona),
        "test": test,
        "odczyt_teraz": aktywna and odczyt_oczekuje(row),
        "adres": row.adres,
        "uzytkownik": row.uzytkownik,
        "haslo": haslo,
        "ca_pem": row.ca_pem or "",
        "interwal_sekund": row.interwal_minut * 60,
    }


def normalizuj_adres(adres: str) -> str:
    """https://host[:port] bez sciezki. Rzuca ValueError z opisem."""
    adres = (adres or "").strip().rstrip("/")
    if not adres:
        raise ValueError("podaj adres Prism Central")
    if "://" not in adres:
        adres = "https://" + adres
    if not adres.lower().startswith("https://"):
        raise ValueError("adres Prism Central musi zaczynac sie od https://")
    reszta = adres[len("https://"):]
    if not reszta or "/" in reszta or "@" in reszta or any(z.isspace() for z in reszta):
        raise ValueError("podaj sam adres, np. https://prism.firma.pl:9440, bez sciezki")
    if ":" not in reszta.rsplit("]", 1)[-1]:
        adres += ":9440"
    return adres


def sprawdz_ca(pem: str) -> str | None:
    pem = (pem or "").strip()
    if not pem:
        return None
    if "-----BEGIN CERTIFICATE-----" not in pem or len(pem) > 20_000:
        raise ValueError("certyfikat CA musi byc w formacie PEM (-----BEGIN CERTIFICATE-----)")
    return pem


# --- przyjecie wyniku -------------------------------------------------------

def przyjmij(db: Session, czytnik: Asset, wynik: WynikNutanix) -> str:
    """Zapisuje wynik testu albo odczytu. Zwraca "przyjeto" / "pominieto"."""
    row = ustawienia(db, czytnik)
    if row is None:
        return "pominieto"
    teraz = utcnow()
    if wynik.rodzaj == "test":
        row.test_o, row.test_ok = teraz, wynik.ok
        row.test_opis = (_opis_testu(wynik) if wynik.ok else (wynik.blad or "nieznany blad"))[:2000]
        return "przyjeto"

    # Odczyt wykonany na starej konfiguracji (np. innym adresie Prism) nie
    # moze niczego wycofac ani przepiac - opisuje nie to, co jest ustawione.
    if not row.wlaczona or wynik.revision != row.revision:
        return "pominieto"
    row.odczyt_o, row.odczyt_ok = teraz, wynik.ok
    if not wynik.ok:
        row.odczyt_blad = (wynik.blad or "nieznany blad")[:2000]
        return "przyjeto"
    row.odczyt_blad = None
    row.odczyt_liczby = synchronizuj(db, czytnik, wynik)
    return "przyjeto"


def _opis_testu(wynik: WynikNutanix) -> str:
    czesci = ["polaczenie OK"]
    if wynik.wersja_pc:
        czesci.append(f"Prism Central {wynik.wersja_pc}")
    czesci.append(f"klastry: {len(wynik.klastry)}")
    if wynik.czas_ms is not None:
        czesci.append(f"{wynik.czas_ms} ms")
    return " · ".join(czesci)


def warianty_uuid(wartosc: str | None) -> set[str]:
    """UUID w obu kolejnosciach bajtow.

    SMBIOS zapisuje trzy pierwsze pola UUID little-endian, a nie wszystkie
    narzedzia to odwracaja - ta sama maszyna bywa widziana jako 0c53f7eb-...
    i jako ebf7530c-... Porownujemy oba warianty, zeby nie zalozyc drugiego
    wpisu dla maszyny, ktora juz jest w ewidencji.
    """
    w = (wartosc or "").strip().lower()
    if not _UUID.match(w):
        return set()
    a, b, c, d, e = w.split("-")

    def odwroc(x: str) -> str:
        return "".join(reversed([x[i:i + 2] for i in range(0, len(x), 2)]))

    return {w, f"{odwroc(a)}-{odwroc(b)}-{odwroc(c)}-{d}-{e}"}


def _maszyny_agentow(db: Session, tenant_id: str, uuidy: set[str]) -> dict[str, Asset]:
    """UUID (oba warianty) -> maszyna z agentem, ktora go zglosila."""
    if not uuidy:
        return {}
    wynik: dict[str, Asset] = {}
    po_serialu = db.execute(
        select(Asset).where(
            Asset.tenant_id == tenant_id, Asset.zrodlo == ZRODLO_AGENT,
            func.lower(Asset.serial_number).in_(uuidy),
        )
    ).scalars().all()
    for a in po_serialu:
        wynik[(a.serial_number or "").lower()] = a
    uuid_raportu = func.lower(AssetCurrentReport.payload["hardware"]["system"]["uuid"].astext)
    po_raporcie = db.execute(
        select(Asset, uuid_raportu)
        .join(AssetCurrentReport, AssetCurrentReport.asset_id == Asset.id)
        .where(Asset.tenant_id == tenant_id, Asset.zrodlo == ZRODLO_AGENT, uuid_raportu.in_(uuidy))
    ).all()
    for a, u in po_raporcie:
        wynik[u] = a
    return wynik


def synchronizuj(db: Session, czytnik: Asset, wynik: WynikNutanix) -> dict:
    """Przeklada udany, pelny odczyt na obiekty, zasoby i relacje."""
    tenant_id = czytnik.tenant_id
    teraz = utcnow()
    istniejace = {
        (o.rodzaj, o.ext_id): o for o in db.execute(
            select(NutanixObiekt).where(NutanixObiekt.tenant_id == tenant_id)
        ).scalars()
    }
    widziane: set[tuple[str, str]] = set()
    liczby = {"klastry": 0, "hosty": 0, "vm": 0, "vm_z_agentem": 0, "nowe": 0, "wycofane": 0}

    def obiekt(rodzaj: str, ext_id: str, nazwa: str, dane: dict) -> NutanixObiekt:
        klucz = (rodzaj, ext_id.lower())
        widziane.add(klucz)
        o = istniejace.get(klucz)
        if o is None:
            o = NutanixObiekt(tenant_id=tenant_id, rodzaj=rodzaj, ext_id=klucz[1])
            db.add(o)
            istniejace[klucz] = o
        o.nazwa, o.dane, o.czytnik_id = nazwa[:255], dane, czytnik.id
        o.widziany_o, o.zniknal_o = teraz, None
        return o

    def wpis(o: NutanixObiekt, typ: str, **pola) -> Asset:
        """Zasob o zrodle "nutanix" dla obiektu - zalozony albo odswiezony."""
        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None or asset.tenant_id != tenant_id:
            machine_id = f"nutanix:{o.rodzaj}:{o.ext_id}"
            asset = db.execute(select(Asset).where(
                Asset.tenant_id == tenant_id, Asset.machine_id == machine_id)).scalar_one_or_none()
            if asset is None:
                asset = Asset(tenant_id=tenant_id, machine_id=machine_id, typ=typ,
                              zrodlo=ZRODLO_NUTANIX, hostname=o.nazwa or o.ext_id,
                              first_seen=teraz)
                db.add(asset)
                db.flush()
                liczby["nowe"] += 1
        if asset.zrodlo == ZRODLO_NUTANIX:
            asset.hostname = (o.nazwa or o.ext_id)[:255]
            for kolumna, wartosc in pola.items():
                setattr(asset, kolumna, wartosc)
            asset.last_seen = teraz
            _przywroc(asset)
        o.asset_id = asset.id
        return asset

    klastry: dict[str, Asset] = {}
    for k in wynik.klastry:
        o = obiekt(KLASTER, k.ext_id, k.nazwa, k.model_dump(exclude={"ext_id", "nazwa"}))
        klastry[o.ext_id] = wpis(o, "klaster", manufacturer="Nutanix",
                                 os_name=_pierwsze(k.hipernadzorca, "AOS"),
                                 os_version=k.wersja)
        liczby["klastry"] += 1

    hosty: dict[str, Asset] = {}
    for h in wynik.hosty:
        o = obiekt(HOST, h.ext_id, h.nazwa, h.model_dump(exclude={"ext_id", "nazwa"}))
        asset = wpis(o, "host", manufacturer="Nutanix", model=h.model,
                     serial_number=h.numer_seryjny, primary_ip=h.ip, os_name=h.hipernadzorca)
        hosty[o.ext_id] = asset
        _ustaw_relacje(db, tenant_id, asset, klastry.get((h.klaster_id or "").lower()), "host_cluster")
        liczby["hosty"] += 1

    uuidy: set[str] = set()
    for v in wynik.vm:
        uuidy |= warianty_uuid(v.ext_id) | warianty_uuid(v.bios_uuid)
    agenci = _maszyny_agentow(db, tenant_id, uuidy)

    for v in wynik.vm:
        dane = v.model_dump(exclude={"ext_id", "nazwa"})
        o = obiekt(VM, v.ext_id, v.nazwa, dane)
        agent = next((agenci[u] for u in warianty_uuid(v.ext_id) | warianty_uuid(v.bios_uuid)
                      if u in agenci), None)
        if agent is not None:
            _polacz_z_agentem(db, o, agent)
            asset = agent
            liczby["vm_z_agentem"] += 1
        else:
            ip = next((a for karta in v.karty for a in karta.ip), None)
            asset = wpis(o, "vm", manufacturer="Nutanix", model="AHV",
                         os_name=v.system, primary_ip=ip)
        host = hosty.get((v.host_id or "").lower())
        _ustaw_relacje(db, tenant_id, asset, host, "vm_host", zapisz_zmiane=True)
        liczby["vm"] += 1

    # Znikniecie oceniamy tylko dla obiektow widzianych dotad przez TEN
    # czytnik - i tylko po udanym, pelnym odczycie, bo tylko taki mowi,
    # czego w Prism nie ma.
    for klucz, o in istniejace.items():
        if klucz in widziane or o.czytnik_id != czytnik.id or o.zniknal_o is not None:
            continue
        o.zniknal_o = teraz
        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None:
            continue
        _usun_relacje_odczytu(db, tenant_id, asset)
        if asset.zrodlo == ZRODLO_NUTANIX and asset.lifecycle == LIFECYCLE_AKTYWNY:
            asset.lifecycle, asset.retired_at = LIFECYCLE_WYCOFANY, teraz
            asset.retired_by, asset.retired_reason = WYCOFAL, "zniknęła z Prism Central"
            liczby["wycofane"] += 1
    db.flush()
    return liczby


def _pierwsze(*wartosci):
    return next((w for w in wartosci if w), None)


def _przywroc(asset: Asset) -> None:
    """Obiekt wrocil do Prism - cofamy tylko wlasne wycofanie, nie ludzkie."""
    if asset.lifecycle == LIFECYCLE_WYCOFANY and asset.retired_by == WYCOFAL:
        asset.lifecycle, asset.retired_at = LIFECYCLE_AKTYWNY, None
        asset.retired_by = asset.retired_reason = None


def _polacz_z_agentem(db: Session, o: NutanixObiekt, agent: Asset) -> None:
    """VM dostala agenta: odczyt dopisuje sie do jego karty.

    Wpis zalozony wczesniej z samego odczytu (zanim zainstalowano agenta)
    zostaje wycofany z adnotacja - nie kasujemy go, bo mogl juz dostac
    opiekuna albo uwagi, ktore ktos zechce przepisac.
    """
    if o.asset_id and o.asset_id != agent.id:
        stary = db.get(Asset, o.asset_id)
        if stary is not None and stary.zrodlo == ZRODLO_NUTANIX:
            _usun_relacje_odczytu(db, stary.tenant_id, stary)
            if stary.lifecycle == LIFECYCLE_AKTYWNY:
                stary.lifecycle, stary.retired_at = LIFECYCLE_WYCOFANY, utcnow()
                stary.retired_by = WYCOFAL
                stary.retired_reason = f"połączona z maszyną z agentem: {agent.hostname}"
    o.asset_id = agent.id


def _ustaw_relacje(db: Session, tenant_id: str, zrodlo: Asset, cel: Asset | None, rodzaj: str,
                   zapisz_zmiane: bool = False) -> None:
    """Jedna relacja danego rodzaju od zrodla - przepinana, gdy cel sie zmienil."""
    obecne = db.execute(select(AssetRelation).where(
        AssetRelation.tenant_id == tenant_id, AssetRelation.source_id == zrodlo.id,
        AssetRelation.kind == rodzaj)).scalars().all()
    if cel is None:
        return
    if any(r.target_id == cel.id for r in obecne) and len(obecne) == 1:
        return
    stary_cel = next((r.target for r in obecne if r.target_id != cel.id), None)
    for r in obecne:
        db.delete(r)
    db.flush()
    db.add(AssetRelation(tenant_id=tenant_id, source_id=zrodlo.id, target_id=cel.id,
                         kind=rodzaj, created_by=WYCOFAL))
    if zapisz_zmiane and stary_cel is not None:
        db.add(AssetChange(
            tenant_id=tenant_id, asset_id=zrodlo.id, category="hardware", action="zmieniono",
            path="wirtualizacja.host", label="Host wirtualizacji (Nutanix)",
            old_value=stary_cel.hostname, new_value=cel.hostname,
        ))


def _usun_relacje_odczytu(db: Session, tenant_id: str, asset: Asset) -> None:
    for r in db.execute(select(AssetRelation).where(
            AssetRelation.tenant_id == tenant_id, AssetRelation.source_id == asset.id,
            AssetRelation.kind.in_(("vm_host", "host_cluster")),
            AssetRelation.created_by == WYCOFAL)).scalars():
        db.delete(r)


# --- odczyt dla panelu ------------------------------------------------------

def obiekt_zasobu(db: Session, asset: Asset) -> NutanixObiekt | None:
    return db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == asset.tenant_id, NutanixObiekt.asset_id == asset.id,
        NutanixObiekt.zniknal_o.is_(None),
    ).order_by(NutanixObiekt.widziany_o.desc()).limit(1)).scalar_one_or_none()


def drzewo(db: Session, tenant_id: str) -> list[dict]:
    """Klastry -> hosty -> VM dla widoku Wirtualizacja."""
    obiekty = db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.zniknal_o.is_(None),
    ).order_by(NutanixObiekt.nazwa)).scalars().all()
    zasoby = {a.id: a for a in db.execute(select(Asset).where(
        Asset.id.in_([o.asset_id for o in obiekty if o.asset_id]))).scalars()} if obiekty else {}
    klastry = {o.ext_id: {"obiekt": o, "asset": zasoby.get(o.asset_id), "hosty": {}, "bez_hosta": []}
               for o in obiekty if o.rodzaj == KLASTER}
    bez_klastra = {"obiekt": None, "asset": None, "hosty": {}, "bez_hosta": []}
    for o in obiekty:
        if o.rodzaj == HOST:
            k = klastry.get(((o.dane or {}).get("klaster_id") or "").lower(), bez_klastra)
            k["hosty"][o.ext_id] = {"obiekt": o, "asset": zasoby.get(o.asset_id), "vm": []}
    for o in obiekty:
        if o.rodzaj != VM:
            continue
        dane = o.dane or {}
        wiersz = {"obiekt": o, "asset": zasoby.get(o.asset_id),
                  "z_agentem": bool(zasoby.get(o.asset_id) and zasoby[o.asset_id].zrodlo == ZRODLO_AGENT)}
        k = klastry.get((dane.get("klaster_id") or "").lower(), bez_klastra)
        host = k["hosty"].get((dane.get("host_id") or "").lower())
        (host["vm"] if host else k["bez_hosta"]).append(wiersz)
    wynik = list(klastry.values())
    if bez_klastra["hosty"] or bez_klastra["bez_hosta"]:
        wynik.append(bez_klastra)
    return wynik


def czytnik_milczy(row: NutanixUstawienia | None, teraz=None) -> bool:
    """Wlaczony odczyt, a ostatni udany dawniej niz trzy odstepy."""
    if row is None or not row.wlaczona:
        return False
    teraz = teraz or utcnow()
    granica = timedelta(minutes=row.interwal_minut * 3)
    return row.odczyt_o is None or teraz - row.odczyt_o > granica
