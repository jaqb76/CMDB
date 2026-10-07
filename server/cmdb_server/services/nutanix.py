"""Wirtualizacja (Nutanix Prism Central, VMware vCenter, OpenStack): konfiguracja
dla agenta i wlaczanie odczytu do ewidencji.

Wszyscy dostawcy przechodza te sama droge - roznia sie tylko tym, co agent pyta
(agent/cmdb_agent/nutanix.py, vmware.py, openstack.py). Agent sprowadza
odpowiedz do tej samej plaskiej postaci, wiec reszta jest wspolna.

Z platformy laczy sie agent. Serwer tylko
wydaje mu konfiguracje, przyjmuje wynik i przeklada go na zasoby i relacje:

    klaster  <- host_cluster -  host  <- vm_host -  maszyna wirtualna

Maszyna wirtualna, w ktorej dziala agent CMDB, nie dostaje drugiego wpisu:
jej UUID z BIOS-u (raport agenta) rowna sie identyfikatorowi VM w Prism
albo bios_uuid z vCenter (na KVM w OpenStacku: UUID instancji), wiec odczyt
dopisuje sie do karty maszyny agenta. Pozostale VM, hosty i klastry dostaja
wpisy o zrodle dostawcy ("nutanix", "vmware", "openstack").
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    LIFECYCLE_AKTYWNY,
    LIFECYCLE_WYCOFANY,
    ZRODLO_AGENT,
    ZRODLO_CEPH,
    ZRODLO_NUTANIX,
    ZRODLO_OME,
    ZRODLO_OPENSTACK,
    ZRODLO_VMWARE,
    Asset,
    AssetChange,
    AssetCurrentReport,
    AssetRelation,
    NutanixObiekt,
    WirtualizacjaPolaczenie,
    utcnow,
)
from ..nutanix_schema import WynikNutanix
from . import sekrety

log = logging.getLogger(__name__)

KLASTER, HOST, VM = "klaster", "host", "vm"
NUTANIX, VMWARE, OPENSTACK, CEPH, OME = ZRODLO_NUTANIX, ZRODLO_VMWARE, ZRODLO_OPENSTACK, ZRODLO_CEPH, ZRODLO_OME
INTERWALY_MINUT = (15, 30, 60, 120, 240, 720, 1440)

# Wszystko, czym dostawcy sie roznia. retired_by/created_by = klucz dostawcy:
# odroznia wycofanie i relacje z odczytu od decyzji czlowieka.
#   sciezka   - adres moze miec sciezke (Keystone bywa pod /identity),
#   projekt   - formularz ma pola domeny i projektu (logowanie do OpenStacka)
#               i reczne adresy uslug,
#   http      - mozna zezwolic na polaczenie bez TLS (OpenStack, Ceph Dashboard),
#   magazyn   - pamiec masowa (Ceph): wlasny zapis do ewidencji, poza drzewem
#               klaster -> host -> VM,
#   narzedzia - nazwa narzedzi goscia, ktore podaja system VM (brak = nie podaje).
DOSTAWCY = {
    NUTANIX: {"nazwa": "Nutanix Prism Central", "platforma": "Prism Central",
              "port": 9440, "producent": "Nutanix", "model_vm": "AHV", "system_klastra": "AOS",
              "przyklad": "https://prism.firma.pl:9440", "przyklad_konta": "cmdb-ro",
              "konto": "z rolą <b>Viewer</b>", "identyfikator": "Prism",
              "narzedzia": "Nutanix Guest Tools", "sciezka": False, "projekt": False, "http": False},
    VMWARE: {"nazwa": "VMware vCenter", "platforma": "vCenter",
             "port": 443, "producent": "VMware", "model_vm": "vSphere", "system_klastra": "vSphere",
             "przyklad": "https://vcenter.firma.pl", "przyklad_konta": "cmdb-ro@vsphere.local",
             "konto": "z rolą <b>Read-only</b> nadaną na poziomie vCenter (z propagacją)",
             "identyfikator": "vCenter", "narzedzia": "VMware Tools", "sciezka": False, "projekt": False,
             "http": False},
    # Port OpenStacka nie jest dopisywany: klasyczny Keystone slucha na 5000,
    # ale za routerem OpenShift (RHOSO) albo load balancerem - na zwyklym 443.
    # Adres bez portu = domyslny port schematu; inny trzeba wpisac jawnie.
    OPENSTACK: {"nazwa": "OpenStack", "platforma": "OpenStack",
                "port": None, "producent": "OpenStack", "model_vm": "Nova", "system_klastra": "OpenStack",
                "przyklad": "https://keystone.firma.pl:5000", "przyklad_konta": "ID application credential",
                "konto": "z rolą <b>admin</b> (lista hypervisorów i maszyn wszystkich projektów); "
                         "zalecane <b>application credential</b> zamiast hasła użytkownika",
                "identyfikator": "OpenStack", "narzedzia": None, "sciezka": True, "projekt": True, "http": True,
                # Compute to zwykly Linux - czesto z agentem CMDB, wiec host
                # dopisuje sie do jego karty (po nazwie), zamiast zakladac druga.
                "host_z_agentem": True},
    CEPH: {"nazwa": "Ceph", "platforma": "Ceph Dashboard",
           "port": 8443, "producent": "Ceph", "model_vm": "", "system_klastra": "Ceph",
           "przyklad": "https://ceph-mgr.firma.pl:8443", "przyklad_konta": "cmdb-ro",
           "konto": "z rolą <b>read-only</b> w Ceph Dashboard",
           "identyfikator": "Cephie (fsid)", "narzedzia": None, "sciezka": False, "projekt": False,
           "http": True, "magazyn": True},
    OME: {"nazwa": "Dell OpenManage Enterprise", "platforma": "OME",
          "port": 443, "producent": "Dell", "model_vm": "", "system_klastra": "",
          "przyklad": "https://ome.firma.pl", "przyklad_konta": "cmdb-ro",
          "konto": "z rolą <b>VIEWER</b>", "identyfikator": "OME (Service Tag)", "narzedzia": None,
          "sciezka": False, "projekt": False, "http": False, "sprzet": True},
}

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# --- konfiguracja -----------------------------------------------------------

def polaczenia(db: Session, asset: Asset, dostawca: str) -> list[WirtualizacjaPolaczenie]:
    """Polaczenia agenta z platformami danego rodzaju, w kolejnosci dodania."""
    return list(db.execute(select(WirtualizacjaPolaczenie).where(
        WirtualizacjaPolaczenie.tenant_id == asset.tenant_id,
        WirtualizacjaPolaczenie.asset_id == asset.id,
        WirtualizacjaPolaczenie.dostawca == dostawca,
    ).order_by(WirtualizacjaPolaczenie.utworzono, WirtualizacjaPolaczenie.id)).scalars())


def polaczenie(db: Session, asset: Asset, dostawca: str, polaczenie_id: str):
    row = db.get(WirtualizacjaPolaczenie, polaczenie_id) if polaczenie_id else None
    if row is None or row.tenant_id != asset.tenant_id or row.asset_id != asset.id or row.dostawca != dostawca:
        return None
    return row


def wersja_zbiorcza(lista: list[WirtualizacjaPolaczenie]) -> str:
    """Jedna wersja wszystkich polaczen - do "czeka na odebranie"."""
    if not lista:
        return "unassigned"
    if len(lista) == 1:
        return lista[0].revision
    tresc = ",".join(sorted(f"{r.id}:{r.revision}" for r in lista))
    return hashlib.sha256(tresc.encode()).hexdigest()[:32]


def nazwa_polaczenia(row: WirtualizacjaPolaczenie) -> str:
    return row.nazwa or urlsplit(row.adres or "").hostname or "nowe połączenie"


def wersja(row: WirtualizacjaPolaczenie | None) -> str:
    return row.revision if row else "unassigned"


def test_oczekuje(row: WirtualizacjaPolaczenie | None) -> bool:
    if row is None or row.test_zlecony_o is None:
        return False
    return row.test_o is None or row.test_o < row.test_zlecony_o


def odczyt_oczekuje(row: WirtualizacjaPolaczenie | None) -> bool:
    if row is None or not row.wlaczona or row.odczyt_zlecony_o is None:
        return False
    return row.odczyt_o is None or row.odczyt_o < row.odczyt_zlecony_o


def polityka_polaczenia(row: WirtualizacjaPolaczenie, aktywna: bool) -> dict:
    """Konfiguracja jednego polaczenia dla agenta - albo "wylaczone".

    Haslo jedzie wylacznie tu, wylacznie do maszyny, ktorej je przypisano,
    i wylacznie wtedy, gdy odczyt jest wlaczony albo zlecono test. Test
    dostaje konfiguracje nawet przy wylaczonym odczycie - inaczej nie da sie
    sprawdzic ustawien przed wlaczeniem.
    """
    test = aktywna and test_oczekuje(row)
    if not aktywna or not (row.wlaczona or test):
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
        **({"domena": row.domena or "", "projekt": row.projekt or "",
            "adres_compute": row.adres_compute or "", "adres_volumes": row.adres_volumes or ""}
           if DOSTAWCY[row.dostawca]["projekt"] else {}),
        **({"bez_tls": bool(row.bez_tls)} if DOSTAWCY[row.dostawca]["http"] else {}),
    }


def polityka_dla_agenta(db: Session, asset: Asset, dostawca: str = NUTANIX) -> dict:
    """Wszystkie polaczenia agenta z platformami danego rodzaju.

    "polaczenia" czyta agent od wersji z wieloma polaczeniami. "policy"
    i "revision" to pierwsze aktywne polaczenie w dawnym ksztalcie - dla
    agenta, ktory jeszcze sie nie zaktualizowal.
    """
    lista = polaczenia(db, asset, dostawca)
    aktywna = bool(asset.is_active and not asset.enrollment_blocked)
    wpisy = [{"id": r.id, "revision": r.revision, "nazwa": nazwa_polaczenia(r),
              **polityka_polaczenia(r, aktywna)} for r in lista]
    pierwsza = next((w for w in wpisy if w["enabled"] or w["test"]), None)
    if pierwsza is None:
        polityka, revision = {"enabled": False, "test": False}, wersja_zbiorcza(lista)
    else:
        polityka = {k: v for k, v in pierwsza.items() if k not in ("id", "revision", "nazwa")}
        revision = pierwsza["revision"]
    return {"policy": polityka, "revision": revision, "polaczenia": wpisy,
            "wersja": wersja_zbiorcza(lista)}


def normalizuj_adres(adres: str, port: int | None = 9440, sciezka: bool = False, http: bool = False) -> str:
    """https://host[:port] bez sciezki. Rzuca ValueError z opisem.

    sciezka=True (Keystone) dopuszcza sciezke, np. https://chmura.firma.pl/identity;
    wtedy domyslny port dopisujemy tylko adresowi bez sciezki. http=True
    (jawna zgoda w konfiguracji OpenStacka) przepuszcza tez http://.
    port=None - nie dopisujemy portu (OpenStack: 5000 albo 443, zaleznie od wdrozenia).
    """
    adres = (adres or "").strip().rstrip("/")
    if not adres:
        raise ValueError("podaj adres")
    if "://" not in adres:
        adres = "https://" + adres
    schemat = adres.split("://", 1)[0].lower()
    if schemat != "https" and not (http and schemat == "http"):
        raise ValueError("adres musi zaczynac sie od https://")
    adres = schemat + adres[len(schemat):]
    reszta = adres[len(schemat) + 3:]
    host, _, sciezka_adresu = reszta.partition("/")
    if (not host or "@" in reszta or any(z.isspace() for z in reszta) or "?" in reszta or "#" in reszta
            or (sciezka_adresu and not sciezka)):
        raise ValueError("podaj sam adres, np. https://nazwa.firma.pl, bez sciezki")
    if port and ":" not in host.rsplit("]", 1)[-1] and not sciezka_adresu:
        adres += f":{port}"
    return adres


def sprawdz_ca(pem: str) -> str | None:
    pem = (pem or "").strip()
    if not pem:
        return None
    if "-----BEGIN CERTIFICATE-----" not in pem or len(pem) > 20_000:
        raise ValueError("certyfikat CA musi byc w formacie PEM (-----BEGIN CERTIFICATE-----)")
    return pem


# --- przyjecie wyniku -------------------------------------------------------

def przyjmij(db: Session, czytnik: Asset, wynik: WynikNutanix, dostawca: str = NUTANIX,
             synchronizacja=None) -> str:
    """Zapisuje wynik testu albo odczytu. Zwraca "przyjeto" / "pominieto".

    synchronizacja - wlasny zapis udanego odczytu do ewidencji (Ceph); domyslnie
    drzewo klaster -> host -> VM.

    Polaczenie wskazuje polaczenie_id; agent sprzed wielu polaczen go nie
    wysyla, wiec wtedy szukamy po rewizji, ktora i tak jest unikalna.
    """
    if wynik.polaczenie_id:
        row = polaczenie(db, czytnik, dostawca, wynik.polaczenie_id)
    else:
        row = next((r for r in polaczenia(db, czytnik, dostawca) if r.revision == wynik.revision), None)
    if row is None:
        return "pominieto"
    teraz = utcnow()
    if wynik.rodzaj == "test":
        row.test_o, row.test_ok = teraz, wynik.ok
        row.test_opis = (_opis_testu(wynik, dostawca) if wynik.ok else (wynik.blad or "nieznany blad"))[:2000]
        return "przyjeto"

    # Odczyt wykonany na starej konfiguracji (np. innym adresie) nie moze
    # niczego wycofac ani przepiac - opisuje nie to, co jest ustawione.
    if not row.wlaczona or wynik.revision != row.revision:
        return "pominieto"
    row.odczyt_o, row.odczyt_ok = teraz, wynik.ok
    if not wynik.ok:
        row.odczyt_blad = (wynik.blad or "nieznany blad")[:2000]
        return "przyjeto"
    row.odczyt_blad = None
    row.odczyt_liczby = (synchronizacja or synchronizuj)(db, czytnik, wynik, dostawca, _przestrzen(row, dostawca), row)
    return "przyjeto"


def usun_polaczenie(db: Session, czytnik: Asset, row: WirtualizacjaPolaczenie) -> int:
    """Usuwa polaczenie; jego obiekty znikaja tak, jakby zniknely z platformy.

    Bez tego wpisy z usunietego vCenter wisialyby w ewidencji jako aktywne,
    choc nikt ich juz nie odswieza. Wycofanie jest odwracalne: po ponownym
    dodaniu polaczenia pierwszy odczyt je cofnie.
    """
    teraz = utcnow()
    liczby = {"wycofane": 0}
    powod = f"usunięto połączenie z {DOSTAWCY[row.dostawca]['platforma']} w CMDB"
    for o in db.execute(select(NutanixObiekt).where(
            NutanixObiekt.tenant_id == czytnik.tenant_id, NutanixObiekt.polaczenie_id == row.id,
            NutanixObiekt.zniknal_o.is_(None))).scalars().all():
        _zniknij(db, czytnik.tenant_id, o, row.dostawca, teraz, powod, liczby)
    db.delete(row)
    db.flush()
    if row.dostawca in (OPENSTACK, CEPH):
        from . import ceph
        ceph.polacz_magazyny(db, czytnik.tenant_id)
    return liczby["wycofane"]


def _przestrzen(row, dostawca: str) -> str:
    """Przedrostek identyfikatorow: w VMware "vm-123" powtarza sie w kazdym vCenter,
    a w OpenStacku region "RegionOne" - w kazdej chmurze, wiec skrot adresu
    platformy odroznia obiekty dwoch instalacji."""
    if dostawca not in (VMWARE, OPENSTACK):
        return ""
    host = (urlsplit(row.adres or "").hostname or ("vcenter" if dostawca == VMWARE else dostawca)).lower()
    return hashlib.sha1(host.encode()).hexdigest()[:8] + "/"


def _opis_testu(wynik: WynikNutanix, dostawca: str = NUTANIX) -> str:
    czesci = ["polaczenie OK"]
    if wynik.wersja_pc:
        czesci.append(f"{DOSTAWCY[dostawca]['platforma']} {wynik.wersja_pc}")
    if DOSTAWCY[dostawca].get("sprzet"):
        czesci.append("lista serwerów dostępna" if wynik.klastry else "OME nie zwrócił żadnego serwera")
    else:
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


def _nazwy_hosta(nazwa: str | None) -> list[str]:
    """Pelna nazwa i jej pierwszy czlon: cmp01.cloud.firma.pl i cmp01."""
    nazwa = (nazwa or "").strip().lower().rstrip(".")
    if not nazwa:
        return []
    krotka = nazwa.split(".", 1)[0]
    return [nazwa] if krotka == nazwa else [nazwa, krotka]


def _hosty_agentow(db: Session, tenant_id: str, nazwy: list[str]) -> dict[str, Asset]:
    """Nazwa (pelna albo krotka) -> aktywna maszyna z agentem o tej nazwie.

    Nazwa wskazujaca kilka maszyn nie wskazuje zadnej - lepiej zalozyc wpis
    z odczytu niz dopisac hypervisor do przypadkowej karty.
    """
    szukane = {n for nazwa in nazwy for n in _nazwy_hosta(nazwa)}
    if not szukane:
        return {}
    trafienia: dict[str, set[str]] = {}
    zasoby: dict[str, Asset] = {}
    for a in db.execute(select(Asset).where(
            Asset.tenant_id == tenant_id, Asset.zrodlo == ZRODLO_AGENT,
            Asset.lifecycle == LIFECYCLE_AKTYWNY,
            (func.lower(Asset.hostname).in_(szukane)) | (func.lower(Asset.fqdn).in_(szukane)))).scalars():
        zasoby[a.id] = a
        for n in {(a.hostname or "").lower(), (a.fqdn or "").lower().rstrip(".")} & szukane:
            trafienia.setdefault(n, set()).add(a.id)
    return {n: zasoby[next(iter(ids))] for n, ids in trafienia.items() if len(ids) == 1}


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


def synchronizuj(db: Session, czytnik: Asset, wynik: WynikNutanix,
                 dostawca: str = NUTANIX, przestrzen: str = "",
                 pol: WirtualizacjaPolaczenie | None = None) -> dict:
    """Przeklada udany, pelny odczyt na obiekty, zasoby i relacje."""
    tenant_id = czytnik.tenant_id
    teraz = utcnow()
    opis = DOSTAWCY[dostawca]
    istniejace = {
        (o.rodzaj, o.ext_id): o for o in db.execute(
            select(NutanixObiekt).where(NutanixObiekt.tenant_id == tenant_id)
        ).scalars() if (o.dostawca or NUTANIX) == dostawca
    }

    def ident(wartosc: str | None) -> str:
        if not wartosc:
            return ""
        wartosc = wartosc.lower()
        if len(przestrzen) + len(wartosc) > 64:  # kolumna ext_id ma 64 znaki
            wartosc = hashlib.sha1(wartosc.encode()).hexdigest()
        return przestrzen + wartosc

    widziane: set[tuple[str, str]] = set()
    liczby = {"klastry": 0, "hosty": 0, "vm": 0, "vm_z_agentem": 0, "nowe": 0, "wycofane": 0}

    def obiekt(rodzaj: str, ext_id: str, nazwa: str, dane: dict) -> NutanixObiekt:
        klucz = (rodzaj, ident(ext_id))
        widziane.add(klucz)
        o = istniejace.get(klucz)
        if o is None:
            o = NutanixObiekt(tenant_id=tenant_id, rodzaj=rodzaj, ext_id=klucz[1], dostawca=dostawca)
            db.add(o)
            istniejace[klucz] = o
        o.nazwa, o.dane, o.czytnik_id = nazwa[:255], dane, czytnik.id
        if pol is not None:
            o.polaczenie_id = pol.id
        o.widziany_o, o.zniknal_o = teraz, None
        return o

    def wpis(o: NutanixObiekt, typ: str, **pola) -> Asset:
        """Zasob o zrodle dostawcy dla obiektu - zalozony albo odswiezony."""
        asset = db.get(Asset, o.asset_id) if o.asset_id else None
        if asset is None or asset.tenant_id != tenant_id:
            machine_id = f"{dostawca}:{o.rodzaj}:{o.ext_id}"
            asset = db.execute(select(Asset).where(
                Asset.tenant_id == tenant_id, Asset.machine_id == machine_id)).scalar_one_or_none()
            if asset is None:
                asset = Asset(tenant_id=tenant_id, machine_id=machine_id, typ=typ,
                              zrodlo=dostawca, hostname=o.nazwa or o.ext_id,
                              first_seen=teraz)
                db.add(asset)
                db.flush()
                liczby["nowe"] += 1
        if asset.zrodlo == dostawca:
            asset.hostname = (o.nazwa or o.ext_id)[:255]
            for kolumna, wartosc in pola.items():
                setattr(asset, kolumna, wartosc)
            asset.last_seen = teraz
            _przywroc(asset, dostawca)
        o.asset_id = asset.id
        return asset

    klastry: dict[str, Asset] = {}
    for k in wynik.klastry:
        o = obiekt(KLASTER, k.ext_id, _nazwa_klastra(k, dostawca, pol, len(wynik.klastry)),
                   k.model_dump(exclude={"ext_id", "nazwa"}))
        klastry[o.ext_id] = wpis(o, "klaster", manufacturer=opis["producent"],
                                 os_name=_pierwsze(k.hipernadzorca, opis["system_klastra"]),
                                 os_version=k.wersja)
        liczby["klastry"] += 1

    hosty: dict[str, Asset] = {}
    agenci_hostow = _hosty_agentow(db, tenant_id, [h.nazwa for h in wynik.hosty]) \
        if opis.get("host_z_agentem") else {}
    for h in wynik.hosty:
        dane = h.model_dump(exclude={"ext_id", "nazwa"})
        dane["klaster_id"] = ident(h.klaster_id) or None
        o = obiekt(HOST, h.ext_id, h.nazwa, dane)
        agent = next((agenci_hostow[n] for n in _nazwy_hosta(h.nazwa) if n in agenci_hostow), None)
        if agent is not None:
            _polacz_z_agentem(db, o, agent, dostawca)
            asset = agent
        else:
            asset = wpis(o, "host", manufacturer=_pierwsze(h.producent, opis["producent"]), model=h.model,
                         serial_number=h.numer_seryjny, primary_ip=h.ip, os_name=h.hipernadzorca)
        hosty[o.ext_id] = asset
        _ustaw_relacje(db, tenant_id, asset, klastry.get(ident(h.klaster_id)), "host_cluster", dostawca)
        liczby["hosty"] += 1

    uuidy: set[str] = set()
    for v in wynik.vm:
        uuidy |= warianty_uuid(v.ext_id) | warianty_uuid(v.bios_uuid)
    agenci = _maszyny_agentow(db, tenant_id, uuidy)

    for v in wynik.vm:
        dane = v.model_dump(exclude={"ext_id", "nazwa"})
        dane["klaster_id"], dane["host_id"] = ident(v.klaster_id) or None, ident(v.host_id) or None
        o = obiekt(VM, v.ext_id, v.nazwa, dane)
        agent = next((agenci[u] for u in warianty_uuid(v.ext_id) | warianty_uuid(v.bios_uuid)
                      if u in agenci), None)
        if agent is not None:
            _polacz_z_agentem(db, o, agent, dostawca)
            asset = agent
            liczby["vm_z_agentem"] += 1
        else:
            ip = next((a for karta in v.karty for a in karta.ip), None)
            asset = wpis(o, "vm", manufacturer=opis["producent"], model=opis["model_vm"],
                         os_name=v.system, primary_ip=ip)
        host = hosty.get(ident(v.host_id))
        _ustaw_relacje(db, tenant_id, asset, host, "vm_host", dostawca, zapisz_zmiane=True)
        liczby["vm"] += 1

    # Znikniecie oceniamy tylko dla obiektow widzianych dotad przez TO
    # polaczenie (obiekty sprzed wielu polaczen: przez tego agenta) - i tylko
    # po udanym, pelnym odczycie, bo tylko taki mowi, czego na platformie nie ma.
    powod = f"zniknęła z {opis['platforma']}"
    for klucz, o in istniejace.items():
        if klucz in widziane or o.zniknal_o is not None:
            continue
        if o.polaczenie_id is not None:
            if pol is None or o.polaczenie_id != pol.id:
                continue
        elif o.czytnik_id != czytnik.id:
            continue
        _zniknij(db, tenant_id, o, dostawca, teraz, powod, liczby)
    db.flush()
    if dostawca == OPENSTACK:
        from . import ceph
        ceph.polacz_magazyny(db, tenant_id)
    return liczby


def _zniknij(db: Session, tenant_id: str, o: NutanixObiekt, dostawca: str, teraz, powod: str,
             liczby: dict) -> None:
    o.zniknal_o = teraz
    asset = db.get(Asset, o.asset_id) if o.asset_id else None
    if asset is None:
        return
    _usun_relacje_odczytu(db, tenant_id, asset, dostawca)
    if asset.zrodlo == dostawca and asset.lifecycle == LIFECYCLE_AKTYWNY:
        asset.lifecycle, asset.retired_at = LIFECYCLE_WYCOFANY, teraz
        asset.retired_by, asset.retired_reason = dostawca, powod
        liczby["wycofane"] += 1


def _nazwa_klastra(k, dostawca: str, pol: WirtualizacjaPolaczenie | None, ile_klastrow: int = 1) -> str:
    """Nazwa klastra w ewidencji.

    Region OpenStacka prawie zawsze nazywa sie "regionOne" - kilka chmur
    wygladaloby identycznie. Klastrem jest wiec nazwa polaczenia ("Openstack
    G1 DC1"), a przy kilku regionach w jednej chmurze - z regionem w nawiasie.
    Prism i vCenter maja wlasne, rozne nazwy klastrow.
    """
    if dostawca == OPENSTACK and pol is not None:
        nazwa = nazwa_polaczenia(pol)
        return nazwa if ile_klastrow <= 1 else f"{nazwa} ({k.nazwa or k.ext_id})"
    return k.nazwa


def _pierwsze(*wartosci):
    return next((w for w in wartosci if w), None)


def _przywroc(asset: Asset, dostawca: str = NUTANIX) -> None:
    """Obiekt wrocil na platforme - cofamy tylko wlasne wycofanie, nie ludzkie."""
    if asset.lifecycle == LIFECYCLE_WYCOFANY and asset.retired_by == dostawca:
        asset.lifecycle, asset.retired_at = LIFECYCLE_AKTYWNY, None
        asset.retired_by = asset.retired_reason = None


def _polacz_z_agentem(db: Session, o: NutanixObiekt, agent: Asset, dostawca: str = NUTANIX) -> None:
    """VM (albo host OpenStacka) ma agenta: odczyt dopisuje sie do jego karty.

    Wpis zalozony wczesniej z samego odczytu (zanim zainstalowano agenta)
    zostaje wycofany z adnotacja - nie kasujemy go, bo mogl juz dostac
    opiekuna albo uwagi, ktore ktos zechce przepisac.
    """
    if o.asset_id and o.asset_id != agent.id:
        stary = db.get(Asset, o.asset_id)
        if stary is not None and stary.zrodlo == dostawca:
            _usun_relacje_odczytu(db, stary.tenant_id, stary, dostawca)
            if stary.lifecycle == LIFECYCLE_AKTYWNY:
                stary.lifecycle, stary.retired_at = LIFECYCLE_WYCOFANY, utcnow()
                stary.retired_by = dostawca
                stary.retired_reason = f"połączona z maszyną z agentem: {agent.hostname}"
    o.asset_id = agent.id


def _ustaw_relacje(db: Session, tenant_id: str, zrodlo: Asset, cel: Asset | None, rodzaj: str,
                   dostawca: str = NUTANIX, zapisz_zmiane: bool = False) -> None:
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
                         kind=rodzaj, created_by=dostawca))
    if zapisz_zmiane and stary_cel is not None:
        db.add(AssetChange(
            tenant_id=tenant_id, asset_id=zrodlo.id, category="hardware", action="zmieniono",
            path="wirtualizacja.host", label=f"Host wirtualizacji ({DOSTAWCY[dostawca]['producent']})",
            old_value=stary_cel.hostname, new_value=cel.hostname,
        ))


def _usun_relacje_odczytu(db: Session, tenant_id: str, asset: Asset, dostawca: str = NUTANIX) -> None:
    for r in db.execute(select(AssetRelation).where(
            AssetRelation.tenant_id == tenant_id, AssetRelation.source_id == asset.id,
            AssetRelation.kind.in_(("vm_host", "host_cluster")),
            AssetRelation.created_by == dostawca)).scalars():
        db.delete(r)


# --- odczyt dla panelu ------------------------------------------------------

def obiekt_zasobu(db: Session, asset: Asset) -> NutanixObiekt | None:
    """Obiekt wirtualizacji/pamieci masowej karty - bez OME (ma wlasna sekcje, patrz ome.obiekt_zasobu)."""
    return db.execute(select(NutanixObiekt).where(
        NutanixObiekt.tenant_id == asset.tenant_id, NutanixObiekt.asset_id == asset.id,
        NutanixObiekt.zniknal_o.is_(None), NutanixObiekt.dostawca != OME,
    ).order_by(NutanixObiekt.widziany_o.desc()).limit(1)).scalar_one_or_none()


def drzewo(db: Session, tenant_id: str, zrodlo: WirtualizacjaPolaczenie | None = None) -> list[dict]:
    """Klastry -> hosty -> VM dla widoku Wirtualizacja.

    zrodlo zaweza drzewo do obiektow jednego polaczenia. Obiekty sprzed
    wielu polaczen nie maja polaczenia - naleza do niego, gdy czytal je ten
    sam agent z ta sama platforma.
    """
    warunki = [NutanixObiekt.tenant_id == tenant_id, NutanixObiekt.zniknal_o.is_(None)]
    if zrodlo is not None:
        warunki.append((NutanixObiekt.polaczenie_id == zrodlo.id) | (
            NutanixObiekt.polaczenie_id.is_(None) & (NutanixObiekt.czytnik_id == zrodlo.asset_id)
            & (NutanixObiekt.dostawca == zrodlo.dostawca)))
    obiekty = db.execute(select(NutanixObiekt).where(*warunki).order_by(NutanixObiekt.nazwa)).scalars().all()
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


def czytnik_milczy(row: WirtualizacjaPolaczenie | None, teraz=None) -> bool:
    """Wlaczony odczyt, a ostatni udany dawniej niz trzy odstepy."""
    if row is None or not row.wlaczona:
        return False
    teraz = teraz or utcnow()
    granica = timedelta(minutes=row.interwal_minut * 3)
    return row.odczyt_o is None or teraz - row.odczyt_o > granica
