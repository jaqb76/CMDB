"""Globalna wyszukiwarka panelu: jedno pole w gornym pasku, wszystko z firmy.

Szuka tam, gdzie w CMDB zyja adresy i nazwy:

* zasoby - nazwa, FQDN (DNS), numer seryjny, glowny IP, rola, opiekun,
  uzytkownik, lokalizacja, a takze kazdy adres IP i MAC interfejsow z raportu
  agenta oraz z odczytu wirtualizacji (IP i MAC kart VM, UUID, projekt),
* urzadzenia wykryte skanerem sieci - IP, MAC, nazwa,
* monitorowane uslugi - nazwa i adres,
* zgloszenia helpdesku - numer, temat, zglaszajacy; tylko dla kogos, kto ma
  dostep do helpdesku tej firmy (tak jak menu helpdesku).

Zawsze w obrebie jednej firmy (ctx). Wyniki ida z bazy, nie z przegladarki,
wiec nic, czego konto nie widzi, nie trafia do strony.
"""
from __future__ import annotations

import re

from sqlalchemy import String, case, cast, func, literal_column, or_, select
from sqlalchemy.orm import Session

from ..models import (
    LIFECYCLE_AKTYWNY,
    Asset,
    AssetCurrentReport,
    DiscoveryDevice,
    MonitorUslugi,
    NutanixObiekt,
    PortalUser,
    WpisSlownika,
    Zgloszenie,
)
from . import helpdesk
from .scoping import TenantContext

MIN_DLUGOSC = 2
_MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){2,5}$", re.I)


def warianty(q: str) -> list[str]:
    """Zapytanie i jego odmiany: MAC wpisany z myslnikami szukamy tez z dwukropkami
    (i odwrotnie), bo Windows podaje go inaczej niz Linux i hypervisory."""
    q = q.strip()
    wynik = [q]
    if _MAC.match(q):
        for inny in (q.replace("-", ":"), q.replace(":", "-")):
            if inny not in wynik:
                wynik.append(inny)
    return wynik


def _sciezka(wyrazenie: str):
    """Stala sciezka JSON z typem jsonpath - Postgres nie rzutuje jej sam z tekstu."""
    return literal_column(f"'{wyrazenie}'::jsonpath")


def _ktores(kolumna, wzorce: list[str]):
    return or_(*[kolumna.ilike(w) for w in wzorce])


def _dlaczego(a: Asset, q: str) -> str:
    """Ktore pole pasuje - zeby z listy bylo widac, czemu zasob sie pojawil."""
    m = q.lower().replace("-", ":")
    for etykieta, wartosc in (("nazwa", a.hostname), ("DNS", a.fqdn), ("IP", a.primary_ip),
                              ("numer seryjny", a.serial_number), ("rola", a.role_label),
                              ("opiekun", a.owner.wartosc if a.owner else None),
                              ("użytkownik", a.uzytkownik.wartosc if a.uzytkownik else None),
                              ("lokalizacja", a.lokalizacja.wartosc if a.lokalizacja else None)):
        if wartosc and (q.lower() in wartosc.lower() or m in wartosc.lower().replace("-", ":")):
            return etykieta
    return "adres lub dane z raportu / wirtualizacji"


def zasoby(db: Session, ctx: TenantContext, q: str, limit: int = 50) -> list[dict]:
    wzorce = [f"%{w}%" for w in warianty(q)]
    t = ctx.tenant_id
    # Z raportu tylko WLASNE adresy interfejsow (IP i MAC) - bez bram i serwerow
    # DNS, bo te sa wspolne: adres routera trafialby w kazdy komputer w sieci.
    adresy = func.concat(
        cast(func.jsonb_path_query_array(AssetCurrentReport.payload, _sciezka("$.network.interfaces[*].ip_addresses[*]")), String),
        " ",
        cast(func.jsonb_path_query_array(AssetCurrentReport.payload, _sciezka("$.network.interfaces[*].mac_address")), String))
    z_raportu = select(AssetCurrentReport.asset_id).where(
        AssetCurrentReport.tenant_id == t, _ktores(adresy, wzorce))
    z_wirtualizacji = select(NutanixObiekt.asset_id).where(
        NutanixObiekt.tenant_id == t, NutanixObiekt.zniknal_o.is_(None), NutanixObiekt.asset_id.is_not(None),
        or_(_ktores(cast(NutanixObiekt.dane, String), wzorce), _ktores(NutanixObiekt.ext_id, wzorce)))
    slownik = select(WpisSlownika.id).where(_ktores(WpisSlownika.wartosc, wzorce))
    q_lower = q.strip().lower()
    stmt = (select(Asset).where(
        Asset.tenant_id == t,
        or_(_ktores(Asset.hostname, wzorce), _ktores(Asset.fqdn, wzorce), _ktores(Asset.serial_number, wzorce),
            _ktores(Asset.primary_ip, wzorce), _ktores(Asset.role_label, wzorce),
            Asset.owner_id.in_(slownik), Asset.uzytkownik_id.in_(slownik), Asset.lokalizacja_id.in_(slownik),
            Asset.id.in_(z_raportu), Asset.id.in_(z_wirtualizacji)))
        # Dokladne trafienie nazwy/IP najpierw, potem aktywne przed wycofanymi.
        .order_by(case((func.lower(Asset.hostname) == q_lower, 0), (Asset.primary_ip == q.strip(), 0), else_=1),
                  case((Asset.lifecycle == LIFECYCLE_AKTYWNY, 0), else_=1), Asset.hostname)
        .limit(limit))
    return [{"asset": a, "dlaczego": _dlaczego(a, q)} for a in db.execute(stmt).scalars()]


def wykryte(db: Session, ctx: TenantContext, q: str, limit: int = 20) -> list[DiscoveryDevice]:
    wzorce = [f"%{w}%" for w in warianty(q)]
    return list(db.execute(select(DiscoveryDevice).where(
        DiscoveryDevice.tenant_id == ctx.tenant_id,
        or_(_ktores(DiscoveryDevice.ip, wzorce), _ktores(DiscoveryDevice.mac, wzorce),
            _ktores(DiscoveryDevice.hostname, wzorce)),
    ).order_by(DiscoveryDevice.last_seen.desc()).limit(limit)).scalars())


def uslugi(db: Session, ctx: TenantContext, q: str, limit: int = 20) -> list[MonitorUslugi]:
    wzorce = [f"%{w}%" for w in warianty(q)]
    return list(db.execute(select(MonitorUslugi).where(
        MonitorUslugi.tenant_id == ctx.tenant_id,
        or_(_ktores(MonitorUslugi.nazwa, wzorce), _ktores(MonitorUslugi.host, wzorce)),
    ).order_by(MonitorUslugi.nazwa).limit(limit)).scalars())


def zgloszenia(db: Session, ctx: TenantContext, user: PortalUser, q: str, limit: int = 20) -> list[Zgloszenie] | None:
    """None = konto nie ma dostepu do helpdesku tej firmy (sekcji nie pokazujemy)."""
    if not helpdesk.ma_dostep(db, user, ctx.tenant_id):
        return None
    wzorce = [f"%{w}%" for w in warianty(q)]
    return list(db.execute(select(Zgloszenie).where(
        Zgloszenie.tenant_id == ctx.tenant_id,
        or_(_ktores(Zgloszenie.numer_pelny, wzorce), _ktores(Zgloszenie.temat, wzorce),
            _ktores(Zgloszenie.zglaszajacy_email, wzorce), _ktores(Zgloszenie.zglaszajacy_nazwa, wzorce)),
    ).order_by(Zgloszenie.ostatnia_aktywnosc.desc()).limit(limit)).scalars())


def wszystko(db: Session, ctx: TenantContext, user: PortalUser, q: str, limit: int = 50) -> dict:
    q = (q or "").strip()
    if len(q) < MIN_DLUGOSC:
        return {"q": q, "za_krotkie": bool(q), "zasoby": [], "wykryte": [], "uslugi": [], "zgloszenia": None}
    return {"q": q, "za_krotkie": False, "zasoby": zasoby(db, ctx, q, limit),
            "wykryte": wykryte(db, ctx, q, min(limit, 20)), "uslugi": uslugi(db, ctx, q, min(limit, 20)),
            "zgloszenia": zgloszenia(db, ctx, user, q, min(limit, 20))}


def podpowiedzi(db: Session, ctx: TenantContext, user: PortalUser, q: str) -> list[dict]:
    """Krotka lista do rozwijanej podpowiedzi w gornym pasku."""
    w = wszystko(db, ctx, user, q, limit=8)
    wynik = []
    for z in w["zasoby"]:
        a = z["asset"]
        wynik.append({"rodzaj": "zasób", "nazwa": a.hostname, "url": f"/assets/{a.id}",
                      "opis": " · ".join(x for x in (a.primary_ip, a.fqdn if a.fqdn != a.hostname else None,
                                                     "wycofany" if a.lifecycle != LIFECYCLE_AKTYWNY else None,
                                                     f"pasuje: {z['dlaczego']}") if x)})
    for d in w["wykryte"][:4]:
        wynik.append({"rodzaj": "wykryte w sieci", "nazwa": d.hostname or d.ip, "url": f"/wykrywanie/{d.id}",
                      "opis": " · ".join(x for x in (d.ip, d.mac) if x)})
    for m in w["uslugi"][:4]:
        wynik.append({"rodzaj": "usługa", "nazwa": m.nazwa, "url": f"/monitoring/{m.id}",
                      "opis": f"{m.host}:{m.port}"})
    for z in (w["zgloszenia"] or [])[:4]:
        wynik.append({"rodzaj": "zgłoszenie", "nazwa": f"{z.numer_pelny} {z.temat}"[:120],
                      "url": f"/helpdesk/zgloszenie/{z.id}", "opis": z.status})
    return wynik[:12]
