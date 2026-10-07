"""Odczyt Dell OpenManage Enterprise (OME) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto w OME wystarczy z rola VIEWER; haslo tylko w pamieci;
certyfikat weryfikowany zawsze (appliance OME ma zwykle certyfikat
self-signed - wklejony w konfiguracji zostaje przypiety, patrz
nutanix.kontekst_tls).

Jedyny zapis po stronie OME to sesja: POST /api/SessionService/Sessions
zaklada ja, na koncu DELETE ja zamyka. Wszystko pozostale to GET.

Czytamy wylacznie serwery (Type 1000): Service Tag, model, stan zdrowia
i zasilania, adres iDRAC, wersje iDRAC/BIOS, CPU, RAM, dyski, adresy MAC
i system wg OME. Po Service Tagu (numer seryjny) serwer CMDB dopisuje te
dane do karty, ktora juz jest w ewidencji (agent, host z vCenter...).
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .nutanix import BladPrism, CzytnikNutanix, _liczba, _tekst, _z, kontekst_tls

log = logging.getLogger(__name__)

LIMIT_CZASU = 60
STRONA = 100
MAKS_URZADZEN = 5000
TYP_SERWERA = 1000

# Kody OME: zdrowie i zasilanie urzadzenia.
ZDROWIE = {1000: "OK", 2000: "UNKNOWN", 3000: "WARNING", 4000: "CRITICAL", 5000: "NO_STATUS"}
ZASILANIE = {17: "ON", 18: "OFF", 20: "POWERING_ON", 21: "POWERING_OFF"}


class BladOme(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladOme(f"OME przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


def _opis_bledu(exc: urllib.error.HTTPError) -> str:
    """OME zglasza blad jako {"error": {"@Message.ExtendedInfo": [{"Message": ...}]}}."""
    try:
        dane = json.loads(exc.read(4096).decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    blad = dane.get("error") if isinstance(dane, dict) else None
    if not isinstance(blad, dict):
        return ""
    # Klucz z kropka w nazwie - _z rozdzielilby go na dwie czesci.
    komunikat, info = blad.get("message"), blad.get("@Message.ExtendedInfo")
    if isinstance(info, list) and info and isinstance(info[0], dict):
        komunikat = info[0].get("Message") or komunikat
    return f" ({str(komunikat)[:300]})" if komunikat else ""


class Ome:
    """Minimalny klient OME: sesja API, potem GET z X-Auth-Token i stronicowaniem OData."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        self.adres = adres.rstrip("/")
        self.uzytkownik = uzytkownik
        self._haslo = haslo
        # Bez przekierowan: przekierowanie mogloby zaniesc token na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst_tls(self.adres, ca_pem)), _BezPrzekierowan())
        self.limit_czasu = limit_czasu
        self.token: str | None = None
        self.sesja: str | None = None

    def __enter__(self):
        self.zaloguj()
        return self

    def __exit__(self, *exc):
        self.wyloguj()

    def zaloguj(self) -> None:
        tresc = json.dumps({"UserName": self.uzytkownik, "Password": self._haslo, "SessionType": "API"}).encode()
        self._haslo = ""  # dalej potrzebny tylko token
        dane, naglowki = self._zadanie("/api/SessionService/Sessions", "POST", tresc, uwierzytelnij=False)
        self.token = naglowki.get("X-Auth-Token")
        self.sesja = str((dane or {}).get("Id") or "") or None
        if not self.token:
            raise BladOme("OME nie zwrocil tokenu sesji (brak naglowka X-Auth-Token)")

    def wyloguj(self) -> None:
        if not self.token:
            return
        try:
            if self.sesja:
                self._zadanie(f"/api/SessionService/Sessions('{urllib.parse.quote(self.sesja)}')", "DELETE")
        except Exception as exc:  # sesja i tak wygasnie sama
            log.debug("zamkniecie sesji OME: %s", exc)
        self.token = None

    def _zadanie(self, sciezka: str, metoda: str = "GET", tresc: bytes | None = None,
                 uwierzytelnij: bool = True, opcjonalne: bool = False):
        adres = self.adres + sciezka
        if not adres.startswith("https://"):
            raise BladOme("adres OME musi byc po https")
        naglowki = {"Accept": "application/json", "User-Agent": f"cmdb-agent/{__version__}"}
        if tresc is not None:
            naglowki["Content-Type"] = "application/json"
        if uwierzytelnij:
            naglowki["X-Auth-Token"] = self.token or ""
        zadanie = urllib.request.Request(adres, data=tresc, method=metoda, headers=naglowki)
        sciezka_bez = urllib.parse.unquote(sciezka.split("?")[0])
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                surowe = odpowiedz.read().decode("utf-8")
                return (json.loads(surowe) if surowe else {}), odpowiedz.headers
        except urllib.error.HTTPError as exc:
            if opcjonalne and exc.code in (400, 403, 404):
                return None, {}
            opis = _opis_bledu(exc)
            if exc.code in (400, 401) and sciezka_bez.startswith("/api/SessionService"):
                raise BladOme(f"OME odrzucil dane logowania (HTTP {exc.code}) - sprawdz uzytkownika i haslo{opis}") from None
            if exc.code == 401:
                raise BladOme(f"OME odrzucil token sesji (HTTP 401){opis}") from None
            if exc.code == 403:
                raise BladOme(f"konto nie ma uprawnien do {sciezka_bez} (HTTP 403){opis} - nadaj mu role VIEWER") from None
            if exc.code == 404:
                raise BladOme(f"OME nie zna {sciezka_bez} (HTTP 404) - czy adres wskazuje OpenManage Enterprise?") from None
            raise BladOme(f"OME odpowiedzial bledem HTTP {exc.code} dla {sciezka_bez}{opis}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladOme(f"certyfikat OME nie jest zaufany na tej maszynie: {powod.verify_message}"
                              " - wklej w konfiguracji CA albo sam certyfikat OME (zostanie przypiety)") from None
            raise BladOme(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladOme(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladOme(f"OME zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka_bez})") from None

    def get(self, sciezka: str, opcjonalne: bool = False):
        return self._zadanie(sciezka, opcjonalne=opcjonalne)[0]

    def serwery(self, maks: int | None = None) -> list[dict]:
        """Serwery (Type 1000), stronami po STRONA ($top/$skip)."""
        wynik: list[dict] = []
        while True:
            zapytanie = urllib.parse.urlencode({"$filter": f"Type eq {TYP_SERWERA}", "$top": STRONA,
                                               "$skip": len(wynik)}, quote_via=urllib.parse.quote)
            dane = self.get("/api/DeviceService/Devices?" + zapytanie) or {}
            strona = dane.get("value")
            if not isinstance(strona, list):
                raise BladOme("nieoczekiwany format odpowiedzi /api/DeviceService/Devices")
            wynik.extend(d for d in strona if isinstance(d, dict))
            if maks is not None and len(wynik) >= maks:
                return wynik[:maks]
            if len(wynik) > MAKS_URZADZEN:
                raise BladOme(f"ponad {MAKS_URZADZEN} serwerow w OME - odczyt przerwany")
            razem = dane.get("@odata.count")
            if len(strona) < STRONA or (isinstance(razem, int) and len(wynik) >= razem):
                return wynik


# --- splaszczenie ------------------------------------------------------------

_JEDNOSTKI = {"": 2**30, "B": 1, "BYTES": 1, "KB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40, "PB": 2**50,
              "KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "TIB": 2**40}
_NOSNIKI = {"solid state drive": "SSD", "hard disk drive": "HDD"}


def _rozmiar_bajty(tekst) -> int | None:
    """Rozmiar dysku z OME -> bajty.

    OME podaje go napisem, zwykle bez jednostki ("698.64" = GB, dysk 750 GB),
    w starszych wersjach z jednostka ("558.38 GB", "1.75 TB"). Separatory
    tysiecy ("1,788.50") i przecinek dziesietny ("1,75") tez sie zdarzaja.
    """
    m = re.match(r"^\s*([\d][\d\s.,]*?)\s*([KMGTP]?I?B|BYTES)?\s*$", str(tekst or ""), re.I)
    if not m:
        return None
    liczba = m.group(1).replace(" ", "")
    if "," in liczba and "." in liczba:
        liczba = liczba.replace(",", "")                      # 1,788.50
    elif "," in liczba:
        liczba = liczba.replace(",", "" if re.fullmatch(r"\d{1,3}(,\d{3})+", liczba) else ".")
    try:
        wartosc = float(liczba)
    except ValueError:
        return None
    wynik = int(wartosc * _JEDNOSTKI[(m.group(2) or "").upper()])
    return wynik or None


def _inwentarz(szczegoly) -> dict[str, list[dict]]:
    """InventoryDetails: lista {InventoryType, InventoryInfo[]} -> slownik typ -> wpisy."""
    wynik: dict[str, list[dict]] = {}
    for blok in (szczegoly or {}).get("value") or []:
        if isinstance(blok, dict) and blok.get("InventoryType"):
            wynik[str(blok["InventoryType"])] = [w for w in blok.get("InventoryInfo") or [] if isinstance(w, dict)]
    return wynik


def _stan(wpis: dict, *pola: str) -> str | None:
    """Stan komponentu, gdy inny niz OK (kod OME 1000 = OK) - pusty przy sprawnym."""
    kod = _liczba(wpis.get("Status"))
    if kod in (1000, None):
        return None
    return _tekst(next((wpis.get(p) for p in pola if wpis.get(p)), None) or ZDROWIE.get(kod, kod), 32)


def pamiec(inw: dict) -> list[dict]:
    wynik = []
    for m in inw.get("serverMemoryDevices", [])[:128]:
        rozmiar = _liczba(m.get("Size"))
        wynik.append({"slot": _tekst(m.get("DeviceDescription") or m.get("Name"), 64),
                      "rozmiar_bajty": rozmiar * 2**20 if rozmiar else None,
                      "typ": _tekst(m.get("TypeDetails"), 32), "predkosc": _liczba(m.get("Speed")),
                      "predkosc_robocza": _liczba(m.get("CurrentOperatingSpeed")),
                      "producent": _tekst(m.get("Manufacturer"), 64), "part": _tekst(m.get("PartNumber"), 64),
                      "numer_seryjny": _tekst(m.get("SerialNumber"), 64), "ranga": _tekst(m.get("Rank"), 32),
                      "stan": _stan(m)})
    return wynik


def porty_sieciowe(inw: dict) -> list[dict]:
    wynik = []
    for karta in inw.get("serverNetworkInterfaces", []):
        for port in karta.get("Ports") or []:
            if not isinstance(port, dict):
                continue
            partycja = next(iter(port.get("Partitions") or []), {}) or {}
            mac = _tekst(_z(partycja, "CurrentMacAddress", "PermanentMacAddress"), 32)
            # "Broadcom NetXtreme ... (BCM5720) - 34:73:5A:..." - MAC jest osobno.
            opis = str(port.get("ProductName") or "").rsplit(" - ", 1)[0] if mac else port.get("ProductName")
            wynik.append({"port": _tekst(port.get("PortId"), 64), "karta": _tekst(karta.get("NicId"), 64),
                          "producent": _tekst(karta.get("VendorName"), 64), "opis": _tekst(opis),
                          "mac": mac.lower() if mac else None, "lacze": _tekst(port.get("LinkStatus"), 16),
                          "predkosc_mbps": _liczba(port.get("LinkSpeed")) or None})
    return wynik[:64]


def kontrolery(inw: dict) -> list[dict]:
    wynik = []
    for k in inw.get("serverRaidControllers", [])[:16]:
        wirtualne = [w for w in k.get("ServerVirtualDisks") or [] if isinstance(w, dict)]
        uklady: dict[str, int] = {}
        for w in wirtualne:
            uklady[str(w.get("Layout") or "?")] = uklady.get(str(w.get("Layout") or "?"), 0) + 1
        wynik.append({"nazwa": _tekst(k.get("Name"), 128), "opis": _tekst(k.get("DeviceDescription"), 128),
                      "firmware": _tekst(k.get("FirmwareVersion"), 64), "cache_mb": _liczba(k.get("CacheSizeInMb")),
                      "stan": _stan(k, "StatusTypeString"),
                      "dyski_wirtualne": len(wirtualne),
                      "uklady": ", ".join(f"{n} × {u}" for u, n in sorted(uklady.items()))[:255] or None,
                      "wirtualne_z_bledem": [_tekst(f"{w.get('Name')}: {w.get('State')}", 128) for w in wirtualne
                                             if _liczba(w.get("Status")) not in (1000, None)][:32]})
    return wynik


def zasilacze(inw: dict) -> list[dict]:
    return [{"nazwa": _tekst(z.get("Name"), 64), "model": _tekst(z.get("Model"), 128),
             "moc_w": _liczba(z.get("OutputWatts")), "firmware": _tekst(z.get("FirmwareVersion"), 32),
             "numer_seryjny": _tekst(z.get("SerialNumber"), 64), "napiecie": _liczba(z.get("InputVoltage")),
             "stan": _stan(z, "OperationalStatus", "State")}
            for z in inw.get("serverPowerSupplies", [])[:16]]


def karty_pcie(inw: dict) -> list[dict]:
    return [{"slot": _tekst(k.get("SlotNumber"), 64), "producent": _tekst(k.get("Manufacturer"), 128),
             "opis": _tekst(k.get("Description"))}
            for k in inw.get("serverDeviceCards", [])[:64]]


def licencje(inw: dict) -> list[dict]:
    return [{"opis": _tekst(lic.get("LicenseDescription"), 128), "typ": _tekst(_z(lic, "LicenseType.Name"), 32),
             "stan": None if _liczba(lic.get("LicenseStatus")) in (1000, None) else _tekst(lic.get("LicenseStatus"), 32)}
            for lic in inw.get("deviceLicense", [])[:8]]


def _idrac_ip(d: dict) -> str | None:
    for zarzadzanie in d.get("DeviceManagement") or []:
        if isinstance(zarzadzanie, dict) and zarzadzanie.get("NetworkAddress"):
            return _tekst(zarzadzanie["NetworkAddress"], 64)
    return None


def serwer(d: dict, szczegoly: dict | None = None) -> dict:
    inw = _inwentarz(szczegoly)
    procesory = inw.get("serverProcessors", [])
    moduly = inw.get("serverMemoryDevices", [])
    dyski = []
    for dysk in inw.get("serverArrayDisks", [])[:64]:
        nosnik = str(dysk.get("MediaType") or "").strip()
        nosnik = _NOSNIKI.get(nosnik.lower(), nosnik)
        magistrala = dysk.get("BusType") or dysk.get("BusProtocol")
        stan = None if _liczba(dysk.get("Status")) in (1000, None) else str(dysk.get("StatusString") or dysk.get("Status"))
        raid = str(dysk.get("RaidStatus") or "")
        if raid and raid.lower() not in ("online", "ready", "non-raid", "unknown"):
            stan = f"{stan or 'OK'}, RAID: {raid}"  # np. "Error, RAID: Failed"
        stan = _tekst(stan, 32)
        dyski.append({"rozmiar_bajty": _rozmiar_bajty(dysk.get("Size")),
                      "typ": _tekst(" ".join(str(x) for x in (nosnik, magistrala) if x), 64),
                      "model": _tekst(dysk.get("ModelNumber"), 128),
                      "producent": _tekst(dysk.get("VendorName"), 64),
                      "numer_seryjny": _tekst(dysk.get("SerialNumber"), 64),
                      "stan": stan,
                      "miejsce": _tekst(dysk.get("DiskNumber"), 128)})
    mac = []
    for karta in inw.get("serverNetworkInterfaces", []):
        for port in karta.get("Ports") or []:
            for partycja in (port.get("Partitions") or []) if isinstance(port, dict) else []:
                adres = _tekst(_z(partycja, "CurrentMacAddress", "PermanentMacAddress"), 32)
                if adres and adres.lower() not in mac:
                    mac.append(adres.lower())
    oprogramowanie = inw.get("deviceSoftware", [])

    def wersja(*fragmenty):
        for o in oprogramowanie:
            opis = str(o.get("DeviceDescription") or o.get("ComponentId") or "").lower()
            if any(f in opis for f in fragmenty):
                return _tekst(o.get("Version"), 64)
        return None

    system = next(iter(inw.get("serverOperatingSystems", [])), {})
    pierwszy_cpu = next(iter(procesory), {})
    ram_mb = sum(_liczba(p.get("Size")) or 0 for p in moduly)
    return {
        "ext_id": _tekst(d.get("DeviceServiceTag"), 64),
        "nazwa": _tekst(d.get("DeviceName")) or "",
        "model": _tekst(d.get("Model")),
        "zdrowie": ZDROWIE.get(_liczba(d.get("Status")), _tekst(d.get("Status"), 32)),
        "zasilanie": ZASILANIE.get(_liczba(d.get("PowerState")), None),
        "polaczony": bool(d.get("ConnectionState")) if d.get("ConnectionState") is not None else None,
        "idrac_ip": _idrac_ip(d),
        "idrac_wersja": wersja("remote access controller", "idrac"),
        "bios_wersja": wersja("bios"),
        "cpu_model": _tekst(pierwszy_cpu.get("ModelName") or pierwszy_cpu.get("Family")),
        "gniazda": len(procesory) or None,
        "rdzenie": sum(_liczba(p.get("NumberOfCores")) or 0 for p in procesory) or None,
        "watki": sum(_liczba(p.get("NumberOfEnabledThreads")) or 0 for p in procesory) or None,
        "ram_bajty": ram_mb * 2**20 if ram_mb else None,
        "dyski": dyski,
        "mac": mac[:32],
        "system": _tekst(system.get("OsName")),
        "hostname_os": _tekst(system.get("Hostname")),
        "obudowa": _tekst(d.get("ChassisServiceTag"), 64),
        "inwentaryzacja_o": _tekst(d.get("LastInventoryTime"), 64),
        # Szczegoly do zakladki "Sprzet (OME)" na karcie.
        "pamiec": pamiec(inw),
        "porty": porty_sieciowe(inw),
        "kontrolery": kontrolery(inw),
        "zasilacze": zasilacze(inw),
        "karty_pcie": karty_pcie(inw),
        "licencje": licencje(inw),
    }


def wykonaj(ome: Ome, rodzaj: str) -> dict:
    """Test (sesja + pierwszy serwer) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    with ome:
        info = ome.get("/api/ApplicationService/Info", opcjonalne=True) or {}
        wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": _tekst(info.get("Version"), 128)}
        if rodzaj == "test":
            wynik["urzadzenia"] = [s for s in (serwer(d) for d in ome.serwery(maks=1)) if s["ext_id"]]
        else:
            urzadzenia = []
            for d in ome.serwery():
                szczegoly = None
                if d.get("Id") is not None:
                    szczegoly = ome.get(f"/api/DeviceService/Devices({int(d['Id'])})/InventoryDetails",
                                        opcjonalne=True)
                wpis = serwer(d, szczegoly)
                if wpis["ext_id"]:
                    urzadzenia.append(wpis)
            wynik["urzadzenia"] = urzadzenia
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


# --- praca w petli monitora -------------------------------------------------

class CzytnikOme(CzytnikNutanix):
    nazwa = "OME"
    sciezka_polityki = "/api/v1/agent/ome-policy"
    sciezka_wyniku = "/api/v1/agent/ome"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or Ome)

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
