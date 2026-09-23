"""Odczyt VMware vCenter (REST API, vSphere 7.0 U2 i nowsze) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto wystarczy z rola Read-only; haslo tylko w pamieci;
certyfikat weryfikowany zawsze; ksztalt API zostaje w tym pliku, a serwer
dostaje te same plaskie pola co z Nutanixa.

Jedyny zapis po stronie vCenter to sesje: POST /api/session i logowanie
VI/JSON zakladaja je (to wymog API - bez nich nie ma odczytu), na koncu
obie sa zamykane.
Wszystko pozostale to GET.

Hosty uzupelnia VI/JSON API (vSphere 8.0 U1+): REST podaje o nich tylko
nazwe i stan, a sprzet, numer seryjny i wersja ESXi sa tylko tam. Gdy
VI/JSON nie ma (starszy vCenter), hosty zostaja z samym REST - bez bledu.

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
# VI/JSON API: najstarsza wersja z tym API (8.0 U1) - nowsze vCentry ja obsluguja.
VI_JSON = "/sdk/vim25/8.0.1.0"
_TYPY_SERIALA = ("SerialNumberTag", "ServiceTag", "EnclosureSerialNumberTag")


class BladVcenter(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladVcenter(f"vCenter przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


def _opis_bledu(exc: urllib.error.HTTPError) -> str:
    """Nazwa bledu z tresci odpowiedzi - VI/JSON zglasza kazdy blad jako HTTP 500
    z typem w tresci (np. InvalidLogin, NoPermission), REST - jako error_type."""
    try:
        dane = json.loads(exc.read(4096).decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    if not isinstance(dane, dict):
        return ""
    typ = dane.get("_typeName") or dane.get("error_type")
    komunikat = dane.get("faultMessage") or dane.get("messages") or dane.get("localizedMessage")
    if isinstance(komunikat, list):
        komunikat = "; ".join(str(_z(m, "default_message", "message") or m) for m in komunikat[:2]
                              if isinstance(m, (dict, str)))
    tekst = ": ".join(str(x) for x in (typ, komunikat) if x)
    return f" ({tekst[:300]})" if tekst else ""


class VCenter:
    """Minimalny klient REST: sesja z Basic, potem GET z naglowkiem sesji."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        self.adres = adres.rstrip("/")
        self._basic = "Basic " + base64.b64encode(
            f"{uzytkownik}:{haslo}".encode("utf-8")).decode("ascii")
        # VI/JSON (szczegoly hostow) ma wlasne logowanie - dane zostaja w
        # pamieci tylko do konca tego odczytu.
        self._konto = (uzytkownik, haslo)
        self._vi: str | None | bool = None  # None - jeszcze nie probowano, False - niedostepne
        self._vi_wspolna = False
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
        self._basic = ""
        return self

    def __exit__(self, *exc) -> None:
        # Wspolnej sesji nie zamykamy osobno - zamyka ja DELETE /api/session.
        if isinstance(self._vi, str) and not self._vi_wspolna:
            try:
                self._zadanie("POST", f"{VI_JSON}/SessionManager/SessionManager/Logout",
                              naglowki={"vmware-api-session-id": self._vi})
            except Exception:
                pass
        self._vi = False
        self._konto = ("", "")
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

    def vi_json(self, sciezka: str):
        """GET w VI/JSON API (vSphere 8.0 U1+) - dane, ktorych REST nie ma.

        Zawsze opcjonalne: starszy vCenter albo brak uprawnien daje None,
        a odczyt idzie dalej z tym, co dal REST.
        """
        if self._vi is None:
            self._vi = self._sesja_vi(sciezka)
        if not self._vi:
            return None
        try:
            return self._zadanie("GET", f"{VI_JSON}/{sciezka}", naglowki={"vmware-api-session-id": self._vi})
        except BladVcenter:
            return None

    def _sesja_vi(self, sciezka: str) -> str | bool:
        """Sesja VI/JSON: najpierw ta sama co REST (vCenter 8 ja przyjmuje),
        a gdy nie - osobne logowanie tym samym kontem."""
        try:
            self._zadanie("GET", f"{VI_JSON}/{sciezka}")
            self._vi_wspolna = True
            return self._sesja or False
        except BladVcenter as exc:
            log.debug("VI/JSON nie przyjmuje sesji REST: %s", exc)
        uzytkownik, haslo = self._konto
        try:
            _, naglowki = self._zadanie(
                "POST", f"{VI_JSON}/SessionManager/SessionManager/Login",
                tresc={"userName": uzytkownik, "password": haslo}, z_naglowkami=True)
            return naglowki.get("vmware-api-session-id") or False
        except BladVcenter as exc:
            log.info("VI/JSON niedostepne (%s) - hosty tylko z REST API", exc)
            return False

    def _zadanie(self, metoda: str, sciezka: str, parametry: dict | None = None,
                 naglowki: dict | None = None, tresc: dict | None = None, z_naglowkami: bool = False):
        adres = self.adres + sciezka
        if parametry:
            adres += "?" + urllib.parse.urlencode(parametry, doseq=True)
        wszystkie = {"Accept": "application/json", "User-Agent": f"cmdb-agent/{__version__}"}
        if self._sesja:
            wszystkie["vmware-api-session-id"] = self._sesja
        wszystkie.update(naglowki or {})
        dane = None
        if tresc is not None:
            dane = json.dumps(tresc).encode("utf-8")
            wszystkie["Content-Type"] = "application/json"
        zadanie = urllib.request.Request(adres, data=dane, method=metoda, headers=wszystkie)
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                surowe = odpowiedz.read().decode("utf-8")
                wynik = json.loads(surowe) if surowe.strip() else None
                return (wynik, odpowiedz.headers) if z_naglowkami else wynik
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
            raise BladVcenter(f"vCenter odpowiedzial bledem HTTP {exc.code} dla {sciezka}"
                              f"{_opis_bledu(exc)}") from None
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


def szczegoly_hosta(vc: VCenter, ident: str) -> dict:
    """Sprzet i ESXi hosta z VI/JSON. Pusty slownik, gdy API niedostepne."""
    ident = urllib.parse.quote(ident, safe="")
    podsumowanie = vc.vi_json(f"HostSystem/{ident}/summary")
    if not isinstance(podsumowanie, dict):
        return {}
    sprzet = podsumowanie.get("hardware") or {}
    seryjny = None
    for wpis in sprzet.get("otherIdentifyingInfo") or []:
        typ = _z(wpis, "identifierType.key") if isinstance(wpis, dict) else None
        wartosc = _tekst(wpis.get("identifierValue"), 128) if isinstance(wpis, dict) else None
        if typ in _TYPY_SERIALA and wartosc and wartosc.lower() not in ("none", "unknown", "0"):
            seryjny = seryjny if (seryjny and typ != "SerialNumberTag") else wartosc
    wynik = {
        "producent": _tekst(sprzet.get("vendor")),
        "model": _tekst(sprzet.get("model")),
        "numer_seryjny": seryjny,
        "cpu_model": _tekst(sprzet.get("cpuModel")),
        "gniazda": _liczba(sprzet.get("numCpuPkgs")),
        "rdzenie": _liczba(sprzet.get("numCpuCores")),
        "watki": _liczba(sprzet.get("numCpuThreads")),
        "ram_bajty": _liczba(sprzet.get("memorySize")),
        "hipernadzorca": _tekst(_z(podsumowanie, "config.product.fullName")) or "VMware ESXi",
        "tryb_serwisowy": _z(podsumowanie, "runtime.inMaintenanceMode"),
        "uruchomiony_o": _tekst(_z(podsumowanie, "runtime.bootTime"), 64),
    }
    ip = _adres_zarzadzania(vc.vi_json(f"HostSystem/{ident}/config"))
    if ip:
        wynik["ip"] = ip
    return {k: v for k, v in wynik.items() if v is not None}


def _adres_zarzadzania(konfiguracja) -> str | None:
    """IP interfejsu zarzadzania (vmk z ruchem "management", zwykle vmk0)."""
    if not isinstance(konfiguracja, dict):
        return None
    karty = [k for k in (_z(konfiguracja, "network.vnic") or []) if isinstance(k, dict)]
    zarzadzanie = set()
    for wpis in _z(konfiguracja, "virtualNicManagerInfo.netConfig") or []:
        if isinstance(wpis, dict) and wpis.get("nicType") == "management":
            zarzadzanie.update(str(x).rsplit("-", 1)[-1] for x in wpis.get("selectedVnic") or [])
    karty.sort(key=lambda k: (str(k.get("key", "")).rsplit("-", 1)[-1] not in zarzadzanie,
                              "management" not in str(k.get("portgroup", "")).lower(),
                              k.get("device") != "vmk0"))
    for karta in karty:
        adres = _tekst(_z(karta, "spec.ip.ipAddress"), 64)
        if adres:
            return adres
    return None


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
                wpis = host(h, klaster_hosta.get(h.get("host")))
                if wpis["ext_id"]:
                    wpis.update(szczegoly_hosta(vc, wpis["ext_id"]))
                hosty.append(wpis)
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
