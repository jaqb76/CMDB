"""Odczyt VMware vCenter (REST API, vSphere 7.0 U2 i nowsze) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto wystarczy z rola Read-only; haslo tylko w pamieci;
certyfikat weryfikowany zawsze; ksztalt API zostaje w tym pliku, a serwer
dostaje te same plaskie pola co z Nutanixa.

Jedyny zapis po stronie vCenter to sesja: POST /api/session zaklada ja
(to wymog API - bez niej nie ma odczytu), DELETE /api/session ja zamyka.
Wszystko pozostale to GET.

Kolejnosc odczytu omija limity list vCenter (lista VM odmawia powyzej
kilku tysiecy wynikow): klastry, hosty kazdego klastra, a VM - osobno
dla kazdego hosta. Szczegoly VM (UUID z BIOS-u, dyski, karty) to jedno
zapytanie na maszyne, a adresy IP i system wg VMware Tools - jeszcze
dwa, tylko dla wlaczonych.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .nutanix import BladPrism, CzytnikNutanix, _liczba, _tekst, _z

log = logging.getLogger(__name__)

LIMIT_CZASU = 30
MAKS_VM = 20000  # ten sam limit przyjmuje serwer

STANY = {"POWERED_ON": "ON", "POWERED_OFF": "OFF", "SUSPENDED": "SUSPENDED"}


class BladVcenter(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladVcenter(f"vCenter przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


class VCenter:
    """Minimalny klient REST: sesja z Basic, potem GET z naglowkiem sesji."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        self.adres = adres.rstrip("/")
        self._basic = "Basic " + base64.b64encode(
            f"{uzytkownik}:{haslo}".encode("utf-8")).decode("ascii")
        kontekst = ssl.create_default_context(cadata=ca_pem or None)
        kontekst.minimum_version = ssl.TLSVersion.TLSv1_2
        kontekst.check_hostname = True
        kontekst.verify_mode = ssl.CERT_REQUIRED
        # Bez przekierowan: przekierowanie mogloby zaniesc haslo albo
        # identyfikator sesji na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst), _BezPrzekierowan())
        self.limit_czasu = limit_czasu
        self._sesja: str | None = None

    # --- sesja ---

    def __enter__(self) -> "VCenter":
        wynik = self._zadanie("POST", "/api/session", naglowki={"Authorization": self._basic})
        if not isinstance(wynik, str) or not wynik:
            raise BladVcenter("vCenter nie zwrocil identyfikatora sesji")
        self._sesja = wynik
        # Hasla nie trzymamy dluzej niz to konieczne.
        self._basic = ""
        return self

    def __exit__(self, *exc) -> None:
        if self._sesja:
            try:
                self._zadanie("DELETE", "/api/session")
            except Exception:  # sesja i tak wygasnie sama
                pass
            self._sesja = None

    # --- zapytania ---

    def get(self, sciezka: str, parametry: dict | None = None, opcjonalne: bool = False):
        """GET; przy opcjonalne=True brak danych (np. VMware Tools nie dziala) daje None."""
        try:
            return self._zadanie("GET", sciezka, parametry)
        except BladVcenter:
            if opcjonalne:
                return None
            raise

    def _zadanie(self, metoda: str, sciezka: str, parametry: dict | None = None,
                 naglowki: dict | None = None):
        adres = self.adres + sciezka
        if parametry:
            adres += "?" + urllib.parse.urlencode(parametry, doseq=True)
        wszystkie = {"Accept": "application/json", "User-Agent": f"cmdb-agent/{__version__}"}
        if self._sesja:
            wszystkie["vmware-api-session-id"] = self._sesja
        wszystkie.update(naglowki or {})
        zadanie = urllib.request.Request(adres, method=metoda, headers=wszystkie)
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                tresc = odpowiedz.read().decode("utf-8")
                return json.loads(tresc) if tresc.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BladVcenter("vCenter odrzucil dane logowania (HTTP 401) - sprawdz uzytkownika"
                                  " (np. cmdb-ro@vsphere.local) i haslo") from None
            if exc.code == 403:
                raise BladVcenter("konto nie ma uprawnien do odczytu (HTTP 403) - nadaj mu role"
                                  " Read-only na poziomie vCenter z propagacja") from None
            if exc.code == 404 and sciezka == "/api/session":
                raise BladVcenter("vCenter nie zna /api/session (HTTP 404) - wymagany vSphere 7.0 U2"
                                  " lub nowszy") from None
            raise BladVcenter(f"vCenter odpowiedzial bledem HTTP {exc.code} dla {sciezka}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladVcenter(f"certyfikat vCenter nie jest zaufany na tej maszynie: {powod.verify_message}"
                                  " - dodaj CA vCenter (VMCA) do systemu albo wklej je w konfiguracji") from None
            raise BladVcenter(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladVcenter(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladVcenter(f"vCenter zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka})") from None

    def lista(self, sciezka: str, parametry: dict | None = None) -> list[dict]:
        wynik = self.get(sciezka, parametry)
        if not isinstance(wynik, list):
            raise BladVcenter(f"nieoczekiwany format odpowiedzi {sciezka}")
        return [x for x in wynik if isinstance(x, dict)]


# --- splaszczenie odpowiedzi -------------------------------------------------

def klaster(d: dict, liczba_hostow: int | None = None) -> dict:
    return {
        "ext_id": _tekst(d.get("cluster"), 64),
        "nazwa": _tekst(d.get("name")) or "",
        "wersja": None,
        "hipernadzorca": "ESXi",
        "liczba_hostow": liczba_hostow,
    }


def _czy_ip(tekst: str | None) -> bool:
    try:
        ipaddress.ip_address(tekst or "")
        return True
    except ValueError:
        return False


def host(d: dict, klaster_id: str | None) -> dict:
    nazwa = _tekst(d.get("name")) or ""
    return {
        "ext_id": _tekst(d.get("host"), 64),
        "nazwa": nazwa,
        "klaster_id": klaster_id,
        "hipernadzorca": "VMware ESXi",
        # vCenter zna host po nazwie albo adresie, z ktorym go dodano.
        "ip": nazwa if _czy_ip(nazwa) else None,
    }


def _magazyn(plik: str | None) -> str | None:
    """"[datastore1] vm/vm.vmdk" -> "datastore1"."""
    m = re.match(r"^\[([^\]]+)\]", plik or "")
    return _tekst(m.group(1)) if m else None


def _system(kod: str | None) -> str | None:
    """Kod guest_OS ("RHEL_8_64") w czytelnej postaci, gdy Tools nic nie mowia."""
    return _tekst(kod.replace("_", " ")) if kod else None


def vm(podsumowanie: dict, szczegoly: dict | None, host_id: str | None, klaster_id: str | None,
       tozsamosc: dict | None = None, interfejsy: list | None = None) -> dict:
    szczegoly = szczegoly or {}
    cpu = szczegoly.get("cpu") or {}
    liczba_cpu = _liczba(cpu.get("count") or podsumowanie.get("cpu_count"))
    rdzenie = _liczba(cpu.get("cores_per_socket")) or 1
    ram_mib = _liczba(_z(szczegoly, "memory.size_MiB") or podsumowanie.get("memory_size_MiB"))

    dyski = []
    for dysk in (szczegoly.get("disks") or {}).values() if isinstance(szczegoly.get("disks"), dict) else []:
        if not isinstance(dysk, dict):
            continue
        dyski.append({
            "rozmiar_bajty": _liczba(dysk.get("capacity")),
            "magistrala": _tekst(dysk.get("type"), 32),
            "kontener": _magazyn(_z(dysk, "backing.vmdk_file")),
        })

    adresy_wg_mac: dict[str, list[str]] = {}
    for interfejs in interfejsy or []:
        if not isinstance(interfejs, dict):
            continue
        mac = str(interfejs.get("mac_address") or "").lower()
        for wpis in _z(interfejs, "ip.ip_addresses") or []:
            adres = wpis.get("ip_address") if isinstance(wpis, dict) else None
            # Adresy lokalne lacza nic nie mowia o tym, gdzie maszyna stoi.
            if adres and not str(adres).lower().startswith("fe80"):
                adresy_wg_mac.setdefault(mac, []).append(str(adres))
    karty = []
    for karta in (szczegoly.get("nics") or {}).values() if isinstance(szczegoly.get("nics"), dict) else []:
        if not isinstance(karta, dict):
            continue
        mac = _tekst(karta.get("mac_address"), 32)
        karty.append({
            "mac": mac,
            "ip": adresy_wg_mac.get((mac or "").lower(), [])[:32],
            "siec": _tekst(_z(karta, "backing.network_name", "backing.network")),
        })
    glowny_ip = _tekst((tozsamosc or {}).get("ip_address"), 64)
    if glowny_ip and karty and not any(k["ip"] for k in karty):
        karty[0]["ip"] = [glowny_ip]

    return {
        "ext_id": _tekst(podsumowanie.get("vm"), 64),
        "nazwa": _tekst(podsumowanie.get("name") or szczegoly.get("name")) or "",
        "klaster_id": klaster_id,
        "host_id": host_id,
        "stan": STANY.get(str(podsumowanie.get("power_state") or szczegoly.get("power_state")),
                          _tekst(podsumowanie.get("power_state"), 32)),
        "gniazda": (liczba_cpu // rdzenie) if liczba_cpu else None,
        "rdzenie_na_gniazdo": rdzenie if liczba_cpu else None,
        "ram_bajty": ram_mib * 1024 * 1024 if ram_mib else None,
        "dyski": dyski[:64],
        "karty": karty[:32],
        # Tozsamosc goscia jest tylko wtedy, gdy dzialaja VMware Tools.
        "ngt": True if tozsamosc else None,
        "system": _tekst(_z(tozsamosc or {}, "full_name.default_message"))
                  or _system(szczegoly.get("guest_OS")),
        "bios_uuid": _tekst(_z(szczegoly, "identity.bios_uuid"), 64),
        "opis": None,
    }


def _bez_pustych(lista: list[dict]) -> list[dict]:
    return [x for x in lista if x.get("ext_id")]


def _wersja(vc: VCenter) -> str | None:
    """Wersja vCenter - nie kazde konto ja widzi, wiec bez niej tez sie da."""
    dane = vc.get("/api/appliance/system/version", opcjonalne=True)
    if isinstance(dane, dict) and dane.get("version"):
        return _tekst(f"{dane['version']}" + (f" (build {dane['build']})" if dane.get("build") else ""), 128)
    return None


def wykonaj(vc: VCenter, rodzaj: str) -> dict:
    """Test (sesja + lista klastrow) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    with vc:
        surowe_klastry = vc.lista("/api/vcenter/cluster")
        wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": _wersja(vc)}
        if rodzaj == "test":
            wynik["klastry"] = _bez_pustych([klaster(k) for k in surowe_klastry])
        else:
            klastry, hosty, maszyny = [], [], []
            klaster_hosta: dict[str, str | None] = {}
            for k in surowe_klastry:
                czlonkowie = vc.lista("/api/vcenter/host", {"clusters": k.get("cluster")}) if k.get("cluster") else []
                klastry.append(klaster(k, len(czlonkowie)))
                for h in czlonkowie:
                    klaster_hosta[h.get("host")] = k.get("cluster")
            for h in vc.lista("/api/vcenter/host"):
                hosty.append(host(h, klaster_hosta.get(h.get("host"))))
            for h in _bez_pustych(hosty):
                for podsumowanie in vc.lista("/api/vcenter/vm", {"hosts": h["ext_id"]}):
                    if not podsumowanie.get("vm"):
                        continue
                    if len(maszyny) >= MAKS_VM:
                        raise BladVcenter(f"ponad {MAKS_VM} maszyn wirtualnych - odczyt przerwany")
                    maszyny.append(_maszyna(vc, podsumowanie, h))
            wynik.update(klastry=_bez_pustych(klastry), hosty=_bez_pustych(hosty),
                         vm=_bez_pustych(maszyny))
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


def _maszyna(vc: VCenter, podsumowanie: dict, h: dict) -> dict:
    ident = urllib.parse.quote(str(podsumowanie["vm"]), safe="")
    szczegoly = vc.get(f"/api/vcenter/vm/{ident}", opcjonalne=True)
    tozsamosc = interfejsy = None
    if podsumowanie.get("power_state") == "POWERED_ON":
        tozsamosc = vc.get(f"/api/vcenter/vm/{ident}/guest/identity", opcjonalne=True)
        interfejsy = vc.get(f"/api/vcenter/vm/{ident}/guest/networking/interfaces", opcjonalne=True)
    return vm(podsumowanie, szczegoly if isinstance(szczegoly, dict) else None,
              h["ext_id"], h.get("klaster_id"),
              tozsamosc if isinstance(tozsamosc, dict) else None,
              interfejsy if isinstance(interfejsy, list) else None)


# --- praca w petli monitora -------------------------------------------------

class CzytnikVmware(CzytnikNutanix):
    nazwa = "VMware"
    sciezka_polityki = "/api/v1/agent/vmware-policy"
    sciezka_wyniku = "/api/v1/agent/vmware"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or VCenter)

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
