"""Odczyt macierzy NetApp ONTAP (REST API, ONTAP 9.6+) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto ONTAP z rola "readonly"; haslo tylko w pamieci; certyfikat
weryfikowany zawsze (domyslny self-signed klastra wklejony w konfiguracji
zostaje przypiety, patrz nutanix.kontekst_tls).

ONTAP uwierzytelnia kazde zapytanie naglowkiem Basic - nie ma sesji, wiec
po stronie macierzy nie powstaje zaden zapis. Wszystkie zapytania to GET.

Kazda lista prosi o konkretne pola (?fields=...). Gdy wersja ONTAP ktoregos
nie zna (HTTP 400), agent ponawia z fields=*, zamiast przerywac odczyt.
Listy poza klastrem i wezlami sa opcjonalne - brak uprawnien albo funkcji
(np. SnapMirror) daje pusta liste, nie blad.
"""
from __future__ import annotations

import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .nutanix import BladPrism, CzytnikNutanix, _liczba, _tekst, _z, kontekst_tls

log = logging.getLogger(__name__)

LIMIT_CZASU = 60
STRONA = 1000
MAKS_OBIEKTOW = 20000


class BladNetapp(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladNetapp(f"ONTAP przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


class _NieznanePole(Exception):
    """HTTP 400 przy liscie pol - ta wersja ONTAP ktoregos nie zna."""


def _opis_bledu(exc: urllib.error.HTTPError) -> str:
    try:
        dane = json.loads(exc.read(4096).decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    komunikat = _z(dane, "error.message") if isinstance(dane, dict) else None
    return f" ({str(komunikat)[:300]})" if komunikat else ""


class Ontap:
    """Minimalny klient ONTAP REST: GET z Basic i stronicowaniem (_links.next)."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        czesci = urllib.parse.urlsplit(adres.rstrip("/"))
        # Z adresu System Managera ("https://x/sysmgr/v4/") zostaje sam host - API jest pod /api.
        self.adres = f"{czesci.scheme}://{czesci.netloc}"
        self._autoryzacja = "Basic " + base64.b64encode(f"{uzytkownik}:{haslo}".encode("utf-8")).decode("ascii")
        # Bez przekierowan: przekierowanie mogloby zaniesc naglowek z haslem na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst_tls(self.adres, ca_pem)), _BezPrzekierowan())
        self.limit_czasu = limit_czasu

    def get(self, sciezka: str, opcjonalne: bool = False):
        adres = self.adres + sciezka
        if not adres.startswith("https://"):
            raise BladNetapp("adres ONTAP musi byc po https")
        zadanie = urllib.request.Request(adres, method="GET", headers={
            "Authorization": self._autoryzacja, "Accept": "application/json",
            "User-Agent": f"cmdb-agent/{__version__}"})
        sciezka_bez = sciezka.split("?")[0]
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                return json.loads(odpowiedz.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            opis = _opis_bledu(exc)
            if exc.code == 400 and "fields=" in sciezka:
                raise _NieznanePole(opis) from None
            if opcjonalne and exc.code in (403, 404):
                return None
            if exc.code == 401:
                raise BladNetapp(f"ONTAP odrzucil dane logowania (HTTP 401) - sprawdz uzytkownika i haslo"
                                 f" oraz czy konto ma aplikacje http{opis}") from None
            if exc.code == 403:
                raise BladNetapp(f"konto nie ma uprawnien do {sciezka_bez} (HTTP 403){opis}"
                                 " - nadaj mu role readonly") from None
            if exc.code == 404:
                raise BladNetapp(f"ONTAP nie zna {sciezka_bez} (HTTP 404) - czy to ONTAP 9.6+"
                                 " i adres klastra (management LIF)?") from None
            raise BladNetapp(f"ONTAP odpowiedzial bledem HTTP {exc.code} dla {sciezka_bez}{opis}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladNetapp(f"certyfikat ONTAP nie jest zaufany na tej maszynie: {powod.verify_message}"
                                 " - wklej w konfiguracji CA albo sam certyfikat klastra (zostanie przypiety)") from None
            raise BladNetapp(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladNetapp(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladNetapp(f"ONTAP zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka_bez})") from None

    def obiekt(self, sciezka: str, pola: str) -> dict:
        """Pojedynczy obiekt (np. /api/cluster). Nieznane pole - ponownie z fields=*."""
        for wybor in (pola, "*"):
            try:
                return self.get(sciezka + "?" + urllib.parse.urlencode({"fields": wybor})) or {}
            except _NieznanePole as exc:
                log.info("ONTAP: %s nie zna czesci pol%s - ponawiam z fields=*", sciezka, exc)
        return {}

    def lista(self, sciezka: str, pola: str, opcjonalne: bool = True) -> list[dict]:
        """Wszystkie rekordy kolekcji. Nieznane pole (starszy ONTAP) - ponownie z fields=*."""
        for wybor in (pola, "*"):
            try:
                return self._strony(sciezka, wybor, opcjonalne)
            except _NieznanePole as exc:
                log.info("ONTAP: %s nie zna czesci pol%s - ponawiam z fields=*", sciezka, exc)
        return []

    def _strony(self, sciezka: str, pola: str, opcjonalne: bool) -> list[dict]:
        wynik: list[dict] = []
        nastepna = sciezka + "?" + urllib.parse.urlencode({"fields": pola, "max_records": STRONA})
        while nastepna:
            dane = self.get(nastepna, opcjonalne=opcjonalne)
            if dane is None:
                return []
            wynik.extend(r for r in dane.get("records") or [] if isinstance(r, dict))
            if len(wynik) > MAKS_OBIEKTOW:
                raise BladNetapp(f"ponad {MAKS_OBIEKTOW} obiektow w {sciezka} - odczyt przerwany")
            nastepna = _z(dane, "_links.next.href")
            if nastepna and not str(nastepna).startswith("/api/"):
                nastepna = None  # nie idziemy pod adres spoza API
        return wynik


# --- splaszczenie ------------------------------------------------------------

def _bajty(wartosc) -> int | None:
    liczba = _liczba(wartosc)
    return liczba if liczba is None or liczba >= 0 else None


def _wersja(dane: dict) -> str | None:
    """'NetApp Release 9.12.1P3: Thu Apr 27 ...' -> '9.12.1P3'."""
    pelna = str(_z(dane, "version.full") or "")
    if pelna.startswith("NetApp Release "):
        return _tekst(pelna[len("NetApp Release "):].split(":")[0], 64)
    gen, maj, mn = (_z(dane, f"version.{x}") for x in ("generation", "major", "minor"))
    return _tekst(f"{gen}.{maj}.{mn}", 64) if gen is not None else _tekst(pelna, 64)


def _stan(wartosc, dobre=("online", "up", "ok", "normal", "present", "true")) -> str | None:
    """Stan inny niz poprawny - pusty dla sprawnych (jak przy OME i Cephie)."""
    if wartosc is None:
        return None
    tekst = str(wartosc).lower()
    return None if tekst in dobre else _tekst(wartosc, 32)


def wezel(d: dict) -> dict:
    partnerzy = [_z(p, "name") for p in _z(d, "ha.partners") or [] if isinstance(p, dict)]
    ip = next((_z(i, "ip.address") for i in d.get("management_interfaces") or [] if isinstance(i, dict)), None)
    return {"nazwa": _tekst(d.get("name")) or "", "model": _tekst(d.get("model"), 64),
            "numer_seryjny": _tekst(d.get("serial_number"), 64), "system_id": _tekst(d.get("system_id"), 32),
            "wersja": _wersja(d), "stan": _stan(d.get("state")), "czas_pracy_s": _liczba(d.get("uptime")),
            "partner_ha": _tekst(", ".join(p for p in partnerzy if p), 128),
            "przejecie": _tekst(_z(d, "ha.takeover.state"), 32), "ip": _tekst(ip, 64),
            "lokalizacja": _tekst(d.get("location"), 128)}


def agregat(d: dict) -> dict:
    return {"nazwa": _tekst(d.get("name")) or "", "wezel": _tekst(_z(d, "node.name"), 128),
            "rozmiar_bajty": _bajty(_z(d, "space.block_storage.size")),
            "zajete_bajty": _bajty(_z(d, "space.block_storage.used")),
            "dostepne_bajty": _bajty(_z(d, "space.block_storage.available")),
            "dyski": _liczba(_z(d, "block_storage.primary.disk_count")),
            "raid": _tekst(_z(d, "block_storage.primary.raid_type"), 32),
            "klasa": _tekst(_z(d, "block_storage.primary.disk_class"), 32),
            "stan": _stan(d.get("state"))}


def svm(d: dict) -> dict:
    protokoly = [p for p in ("nfs", "cifs", "iscsi", "fcp", "nvme") if _z(d, f"{p}.enabled") is True]
    adresy = [_z(i, "ip.address") for i in d.get("ip_interfaces") or [] if isinstance(i, dict)]
    return {"nazwa": _tekst(d.get("name")) or "", "stan": _stan(d.get("state"), ("running",)),
            "podtyp": _tekst(d.get("subtype"), 32), "protokoly": protokoly,
            "ip": [a for a in adresy if a][:32]}


def wolumen(d: dict) -> dict:
    return {"nazwa": _tekst(d.get("name")) or "", "svm": _tekst(_z(d, "svm.name"), 128),
            "agregat": _tekst(", ".join(_z(a, "name") or "" for a in d.get("aggregates") or [] if isinstance(a, dict)), 255),
            "rozmiar_bajty": _bajty(_z(d, "space.size")), "zajete_bajty": _bajty(_z(d, "space.used")),
            "typ": _tekst(d.get("type"), 16), "styl": _tekst(d.get("style"), 32),
            "sciezka": _tekst(_z(d, "nas.path"), 255), "polityka_snapshotow": _tekst(_z(d, "snapshot_policy.name"), 64),
            "root_svm": bool(d.get("is_svm_root")), "stan": _stan(d.get("state"))}


def lun(d: dict) -> dict:
    return {"nazwa": _tekst(d.get("name")) or "", "svm": _tekst(_z(d, "svm.name"), 128),
            "rozmiar_bajty": _bajty(_z(d, "space.size")), "zajete_bajty": _bajty(_z(d, "space.used")),
            "os": _tekst(d.get("os_type"), 32), "numer_seryjny": _tekst(d.get("serial_number"), 64),
            "zmapowany": _z(d, "status.mapped"), "stan": _stan(_z(d, "status.state"))}


def dysk(d: dict) -> dict:
    aggr = ", ".join(_z(a, "name") or "" for a in d.get("aggregates") or [] if isinstance(a, dict))
    return {"nazwa": _tekst(d.get("name")) or "", "numer_seryjny": _tekst(d.get("serial_number"), 64),
            "model": _tekst(d.get("model"), 64), "producent": _tekst(d.get("vendor"), 64),
            "typ": _tekst(d.get("type"), 16), "klasa": _tekst(d.get("class"), 16),
            "rola": _tekst(d.get("container_type"), 32), "rozmiar_bajty": _bajty(d.get("usable_size")),
            "wezel": _tekst(_z(d, "node.name", "home_node.name"), 128),
            "polka": _tekst(_z(d, "shelf.uid"), 64), "zatoka": _liczba(d.get("bay")),
            "firmware": _tekst(d.get("firmware_version"), 32), "agregat": _tekst(aggr, 255),
            "stan": _stan(d.get("state"), ("present", "ok", "normal"))}


def polka(d: dict) -> dict:
    return {"nazwa": _tekst(d.get("name") or d.get("id")) or "", "model": _tekst(d.get("model"), 64),
            "numer_seryjny": _tekst(d.get("serial_number"), 64), "modul": _tekst(d.get("module_type"), 32),
            "dyski": _liczba(d.get("disk_count")), "polaczenie": _tekst(d.get("connection_type"), 32),
            "stan": _stan(d.get("state"), ("ok", "online"))}


def interfejs(d: dict) -> dict:
    uslugi = d.get("services") or []
    return {"nazwa": _tekst(d.get("name")) or "", "ip": _tekst(_z(d, "ip.address"), 64),
            "maska": _tekst(_z(d, "ip.netmask"), 16), "svm": _tekst(_z(d, "svm.name"), 128),
            "wezel": _tekst(_z(d, "location.home_node.name", "location.node.name"), 128),
            "port": _tekst(_z(d, "location.home_port.name", "location.port.name"), 32),
            "uslugi": _tekst(", ".join(str(u) for u in uslugi[:10]), 255) if isinstance(uslugi, list) else None,
            "stan": _stan(d.get("state"))}


def snapmirror(d: dict) -> dict:
    return {"zrodlo": _tekst(_z(d, "source.path"), 255), "cel": _tekst(_z(d, "destination.path"), 255),
            "stan": _tekst(d.get("state"), 32), "zdrowy": d.get("healthy"),
            "opoznienie": _tekst(d.get("lag_time"), 32), "polityka": _tekst(_z(d, "policy.name"), 64)}


def wykonaj(ontap: Ontap, rodzaj: str) -> dict:
    """Test (klaster + wezly) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    k = ontap.obiekt("/api/cluster", "name,uuid,version,serial_number,location,contact,management_interfaces")
    if not k.get("uuid"):
        raise BladNetapp("ONTAP nie podal identyfikatora klastra (uuid)")
    ip = next((_z(i, "ip.address") for i in k.get("management_interfaces") or [] if isinstance(i, dict)), None)
    wezly = [wezel(w) for w in ontap.lista(
        "/api/cluster/nodes", "name,uuid,serial_number,model,system_id,version,state,uptime,ha,"
        "management_interfaces,location", opcjonalne=False)]
    klaster = {"ext_id": _tekst(k["uuid"], 64), "nazwa": _tekst(k.get("name")) or "",
               "wersja": _wersja(k), "numer_seryjny": _tekst(k.get("serial_number"), 64),
               "lokalizacja": _tekst(k.get("location"), 128), "kontakt": _tekst(k.get("contact"), 128),
               "ip": _tekst(ip, 64), "wezly": [w for w in wezly if w["nazwa"]]}
    if rodzaj == "odczyt":
        klaster["agregaty"] = [agregat(x) for x in ontap.lista(
            "/api/storage/aggregates", "name,uuid,node.name,space.block_storage,block_storage.primary,state")]
        klaster["svm"] = [svm(x) for x in ontap.lista(
            "/api/svm/svms", "name,uuid,state,subtype,ip_interfaces,nfs.enabled,cifs.enabled,iscsi.enabled,"
            "fcp.enabled,nvme.enabled")]
        klaster["wolumeny"] = [wolumen(x) for x in ontap.lista(
            "/api/storage/volumes", "name,uuid,svm.name,aggregates.name,state,type,style,space.size,space.used,"
            "nas.path,snapshot_policy.name,is_svm_root")]
        klaster["luny"] = [lun(x) for x in ontap.lista(
            "/api/storage/luns", "name,svm.name,space.size,space.used,os_type,serial_number,status")]
        klaster["dyski"] = [dysk(x) for x in ontap.lista(
            "/api/storage/disks", "name,serial_number,model,vendor,type,class,container_type,state,usable_size,"
            "node.name,home_node.name,shelf.uid,bay,firmware_version,aggregates.name")]
        surowe_polki = ontap.lista(
            "/api/storage/shelves", "name,id,uid,serial_number,model,module_type,state,disk_count,connection_type")
        klaster["polki"] = [polka(x) for x in surowe_polki]
        # Dysk wskazuje polke wewnetrznym uid ("16017705184660070400") - pokazujemy jej nazwe ("1.0").
        nazwy_polek = {str(x.get("uid")): str(x.get("name") or x.get("id")) for x in surowe_polki if x.get("uid")}
        for d in klaster["dyski"]:
            d["polka"] = nazwy_polek.get(str(d["polka"]), d["polka"])
        klaster["interfejsy"] = [interfejs(x) for x in ontap.lista(
            "/api/network/ip/interfaces", "name,ip,svm.name,location,state,services")]
        klaster["snapmirror"] = [snapmirror(x) for x in ontap.lista(
            "/api/snapmirror/relationships", "source.path,destination.path,state,healthy,lag_time,policy.name")]
    wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": klaster["wersja"], "klastry": [klaster],
             "czas_ms": int((time.monotonic() - start) * 1000)}
    return wynik


# --- praca w petli monitora -------------------------------------------------

class CzytnikNetapp(CzytnikNutanix):
    nazwa = "NetApp"
    sciezka_polityki = "/api/v1/agent/netapp-policy"
    sciezka_wyniku = "/api/v1/agent/netapp"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or Ontap)

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
