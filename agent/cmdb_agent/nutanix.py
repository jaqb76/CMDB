"""Odczyt Nutanix Prism Central (API v4) - strona agenta.

**Laczy sie agent, nie serwer.** Prism stoi w sieci firmy, ktorej serwer CMDB
nie musi widziec; agent juz tam jest. Serwer wydaje mu konfiguracje (adres,
konto, haslo), agent czyta klastry, hosty i maszyny wirtualne i odsyla je
w splaszczonej postaci. Kierunek polaczen sie nie zmienia: agent pyta serwer,
serwer nigdy nie wola agenta.

Zasady:

* **tylko odczyt** - wylacznie zapytania GET; konto w Prism wystarczy z rola
  Viewer, wiec nawet blad w tym kodzie nie moze niczego zmienic w klastrze,
* **haslo tylko w pamieci** - przychodzi w swiezej konfiguracji (60 s) przy
  kazdym przebiegu, nie trafia na dysk ani do dziennika,
* **weryfikacja certyfikatu zawsze** - zaufane urzedy systemu albo wskazany
  certyfikat CA; wylaczenia nie ma,
* **ksztalt API zostaje tutaj** - serwer dostaje proste pola, wiec zmiana
  po stronie Nutanixa to poprawka tego pliku, a nie migracja serwera.

Chodzi w petli uslugi monitorowania (patrz monitoring.Monitor.krok): ta zyje
caly czas i sprawdza konfiguracje co kilka minut, wiec zlecony z panelu test
polaczenia wraca szybko, a nie po godzinie przy nastepnej inwentaryzacji.
Sam odczyt idzie w osobnym watku, zeby duzy klaster nie wstrzymywal sond.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger(__name__)

# Co ile agent pyta o konfiguracje. Tyle trwa najdluzej, zanim zadziala
# zmiana w panelu albo zlecony test.
ODSTEP_POLITYKI = 300
# Ponowienie po nieudanym odczycie - krotsze niz zwykly odstep, ale nie
# szybsze niz co kwadrans, zeby blednym haslem nie zablokowac konta w Prism.
PONOWIENIE_PO_BLEDZIE = 900

STRONA = 100          # maksimum $limit w API v4
MAKS_OBIEKTOW = 20000  # ten sam limit przyjmuje serwer
LIMIT_CZASU = 30

SCIEZKA_KLASTROW = "/api/clustermgmt/v4.0/config/clusters"
SCIEZKA_HOSTOW = "/api/clustermgmt/v4.0/config/hosts"
SCIEZKA_VM = "/api/vmm/v4.0/ahv/config/vms"


class BladPrism(RuntimeError):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


# --- konfiguracja z serwera --------------------------------------------------

def pobierz_polityke(client, state, sciezka: str = "/api/v1/agent/nutanix-policy",
                     nazwa: str = "Nutanix") -> dict:
    """Swieza, zwiazana z jednorazowa wartoscia konfiguracja odczytu.

    Wspolna dla Prism Central i vCenter - rozni je tylko adres konfiguracji.
    """
    if client is None or not state.is_enrolled:
        raise ValueError("brak uwierzytelnionego polaczenia z CMDB")
    nonce = secrets.token_hex(16)
    payload = client.get(sciezka + "?nonce=" + nonce, state.agent_token)
    if (not isinstance(payload, dict) or payload.get("protocol") != 1 or
            payload.get("nonce") != nonce or payload.get("asset_id") != state.asset_id or
            payload.get("machine_id") != state.machine_id):
        raise ValueError(f"niezgodna odpowiedz konfiguracji {nazwa}")
    wygasa = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
    if wygasa.tzinfo is None or not 0 < (wygasa - _teraz()).total_seconds() <= 65:
        raise ValueError(f"wygasla lub niepoprawna waznosc konfiguracji {nazwa}")
    polityka = payload.get("policy")
    if not isinstance(polityka, dict):
        raise ValueError(f"niepoprawny format konfiguracji {nazwa}")
    wynik = {"enabled": bool(polityka.get("enabled")), "test": bool(polityka.get("test")),
             "odczyt_teraz": bool(polityka.get("odczyt_teraz")),
             "revision": str(payload.get("revision") or "")}
    if wynik["enabled"] or wynik["test"]:
        adres = str(polityka.get("adres") or "")
        if not adres.startswith("https://"):
            raise ValueError(f"adres {nazwa} musi byc po https")
        wynik.update(
            adres=adres.rstrip("/"),
            uzytkownik=str(polityka.get("uzytkownik") or ""),
            haslo=str(polityka.get("haslo") or ""),
            ca_pem=str(polityka.get("ca_pem") or ""),
            interwal_sekund=max(900, min(86400, int(polityka.get("interwal_sekund") or 3600))),
        )
    return wynik


# --- klient Prism Central ------------------------------------------------------

class PrismCentral:
    """Minimalny klient API v4: GET z uwierzytelnieniem Basic i stronicowaniem."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        self.adres = adres.rstrip("/")
        self._autoryzacja = "Basic " + base64.b64encode(
            f"{uzytkownik}:{haslo}".encode("utf-8")).decode("ascii")
        kontekst = ssl.create_default_context(cadata=ca_pem or None)
        kontekst.minimum_version = ssl.TLSVersion.TLSv1_2
        kontekst.check_hostname = True
        kontekst.verify_mode = ssl.CERT_REQUIRED
        # Bez przekierowan: przekierowanie mogloby zaniesc naglowek z haslem
        # na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst), _BezPrzekierowan())
        self.limit_czasu = limit_czasu

    def get(self, sciezka: str, parametry: dict | None = None) -> dict:
        adres = self.adres + sciezka
        if parametry:
            adres += "?" + urllib.parse.urlencode(parametry)
        zadanie = urllib.request.Request(adres, method="GET", headers={
            "Authorization": self._autoryzacja,
            "Accept": "application/json",
            "User-Agent": f"cmdb-agent/{__version__}",
        })
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                return json.loads(odpowiedz.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BladPrism("Prism odrzucil dane logowania (HTTP 401) - sprawdz uzytkownika i haslo") from None
            if exc.code == 403:
                raise BladPrism("konto nie ma uprawnien do odczytu (HTTP 403) - nadaj mu role Viewer") from None
            if exc.code == 404:
                raise BladPrism(f"Prism nie zna {sciezka} (HTTP 404) - czy to Prism Central z API v4 (pc.2024.3 lub nowszy)?") from None
            raise BladPrism(f"Prism odpowiedzial bledem HTTP {exc.code} dla {sciezka}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladPrism(f"certyfikat Prism nie jest zaufany na tej maszynie: {powod.verify_message}"
                                " - dodaj firmowe CA do systemu albo wklej je w konfiguracji") from None
            raise BladPrism(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladPrism(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladPrism(f"Prism zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka})") from None

    def lista(self, sciezka: str, maks_stron: int | None = None) -> list[dict]:
        wynik: list[dict] = []
        strona = 0
        while True:
            odpowiedz = self.get(sciezka, {"$page": strona, "$limit": STRONA})
            dane = odpowiedz.get("data") or []
            if not isinstance(dane, list):
                raise BladPrism(f"nieoczekiwany format odpowiedzi {sciezka}")
            wynik.extend(d for d in dane if isinstance(d, dict))
            razem = _z(odpowiedz, "metadata.totalAvailableResults")
            strona += 1
            if (len(dane) < STRONA or (isinstance(razem, int) and len(wynik) >= razem)
                    or (maks_stron is not None and strona >= maks_stron)):
                return wynik
            if len(wynik) >= MAKS_OBIEKTOW:
                raise BladPrism(f"ponad {MAKS_OBIEKTOW} obiektow w {sciezka} - odczyt przerwany")


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladPrism(f"Prism przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


# --- splaszczenie odpowiedzi API v4 ------------------------------------------

def _z(dane, *sciezki, domyslnie=None):
    """Pierwsza istniejaca wartosc z kilku sciezek "a.b.c".

    Kilka sciezek, bo nazwy pol roznia sie miedzy wersjami API v4
    (np. cluster.uuid i cluster.extId) - lepiej przyjac oba warianty niz
    zgubic dane po aktualizacji Prism.
    """
    for sciezka in sciezki:
        wartosc = dane
        for czesc in sciezka.split("."):
            if isinstance(wartosc, dict) and czesc in wartosc:
                wartosc = wartosc[czesc]
            else:
                wartosc = None
                break
        if wartosc not in (None, "", [], {}):
            return wartosc
    return domyslnie


def _tekst(wartosc, dlugosc=255):
    if wartosc is None:
        return None
    if isinstance(wartosc, list):
        wartosc = ", ".join(str(w) for w in wartosc if w)
    tekst = str(wartosc).strip()
    return tekst[:dlugosc] or None


def _liczba(wartosc):
    try:
        return int(wartosc) if wartosc is not None else None
    except (TypeError, ValueError):
        return None


def jest_prism_central(klaster: dict) -> bool:
    """Prism Central wystepuje na liscie klastrow jako "klaster" samego siebie."""
    funkcje = _z(klaster, "config.clusterFunction") or []
    return "PRISM_CENTRAL" in [str(f).upper() for f in funkcje]


def klaster(d: dict) -> dict:
    return {
        "ext_id": _tekst(_z(d, "extId"), 64),
        "nazwa": _tekst(_z(d, "name")) or "",
        "wersja": _tekst(_z(d, "config.buildInfo.version", "config.buildInfo.fullVersion"), 128),
        "hipernadzorca": _tekst(_z(d, "config.hypervisorTypes"), 128),
        "liczba_hostow": _liczba(_z(d, "nodes.numberOfNodes")),
    }


def host(d: dict) -> dict:
    return {
        "ext_id": _tekst(_z(d, "extId"), 64),
        "nazwa": _tekst(_z(d, "hostName", "name")) or "",
        "klaster_id": _tekst(_z(d, "cluster.uuid", "cluster.extId", "clusterExtId"), 64),
        "model": _tekst(_z(d, "blockModel", "blockModelName")),
        "numer_seryjny": _tekst(_z(d, "blockSerial", "nodeSerial"), 128),
        "cpu_model": _tekst(_z(d, "cpuModel")),
        "gniazda": _liczba(_z(d, "numberOfCpuSockets")),
        "rdzenie": _liczba(_z(d, "numberOfCpuCores")),
        "ram_bajty": _liczba(_z(d, "memorySizeBytes")),
        "hipernadzorca": _tekst(_z(d, "hypervisor.fullName", "hypervisor.type")),
        "ip": _tekst(_z(d, "hypervisor.externalAddress.ipv4.value"), 64),
        "ipmi_ip": _tekst(_z(d, "ipmi.ip.ipv4.value"), 64),
    }


def vm(d: dict) -> dict:
    dyski = []
    for dysk in _z(d, "disks") or []:
        if not isinstance(dysk, dict):
            continue
        dyski.append({
            "rozmiar_bajty": _liczba(_z(dysk, "backingInfo.diskSizeBytes", "diskSizeBytes")),
            "magistrala": _tekst(_z(dysk, "diskAddress.busType"), 32),
            "kontener": _tekst(_z(dysk, "backingInfo.storageContainer.extId"), 255),
        })
    karty = []
    for karta in _z(d, "nics") or []:
        if not isinstance(karta, dict):
            continue
        adresy = []
        for sciezka in ("networkInfo.ipv4Config.ipAddress.value",):
            adres = _z(karta, sciezka)
            if adres:
                adresy.append(str(adres))
        for wpis in _z(karta, "networkInfo.ipv4Info.learnedIpAddresses") or []:
            adres = _z(wpis, "value") if isinstance(wpis, dict) else None
            if adres and adres not in adresy:
                adresy.append(str(adres))
        karty.append({
            "mac": _tekst(_z(karta, "backingInfo.macAddress", "macAddress"), 32),
            "ip": adresy[:32],
            "siec": _tekst(_z(karta, "networkInfo.subnet.extId")),
        })
    ngt = _z(d, "guestTools.isInstalled", "guestTools.isEnabled")
    return {
        "ext_id": _tekst(_z(d, "extId"), 64),
        "nazwa": _tekst(_z(d, "name")) or "",
        "klaster_id": _tekst(_z(d, "cluster.extId"), 64),
        "host_id": _tekst(_z(d, "host.extId"), 64),
        "stan": _tekst(_z(d, "powerState"), 32),
        "gniazda": _liczba(_z(d, "numSockets")),
        "rdzenie_na_gniazdo": _liczba(_z(d, "numCoresPerSocket")),
        "ram_bajty": _liczba(_z(d, "memorySizeBytes")),
        "dyski": dyski[:64],
        "karty": karty[:32],
        "ngt": bool(ngt) if ngt is not None else None,
        "system": _tekst(_z(d, "guestTools.guestOsVersion")),
        "bios_uuid": _tekst(_z(d, "biosUuid"), 64),
        "opis": _tekst(_z(d, "description"), 1000),
    }


def _bez_pustych(lista: list[dict]) -> list[dict]:
    return [x for x in lista if x.get("ext_id")]


def wykonaj(prism: PrismCentral, rodzaj: str) -> dict:
    """Test (jedna strona klastrow) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    surowe = prism.lista(SCIEZKA_KLASTROW, maks_stron=1 if rodzaj == "test" else None)
    wersja_pc = next((_tekst(_z(k, "config.buildInfo.version"), 128)
                      for k in surowe if jest_prism_central(k)), None)
    klastry = _bez_pustych([klaster(k) for k in surowe if not jest_prism_central(k)])
    wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": wersja_pc, "klastry": klastry}
    if rodzaj == "odczyt":
        wynik["hosty"] = _bez_pustych([host(h) for h in prism.lista(SCIEZKA_HOSTOW)])
        wynik["vm"] = _bez_pustych([vm(v) for v in prism.lista(SCIEZKA_VM)])
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


# --- praca w petli monitora -------------------------------------------------

class CzytnikNutanix:
    """Pilnuje konfiguracji i uruchamia odczyty. Wolany z petli monitora.

    Ten sam czytnik obsluguje vCenter (vmware.CzytnikVmware) - dostawca
    wnosi tylko adresy w CMDB, klienta platformy i funkcje odczytu.
    """

    nazwa = "Nutanix"
    sciezka_polityki = "/api/v1/agent/nutanix-policy"
    sciezka_wyniku = "/api/v1/agent/nutanix"

    def __init__(self, client, state, fabryka=None):
        self.client = client
        self.state = state
        self.fabryka = fabryka or PrismCentral
        self.nastepna_polityka = 0.0
        self.nastepny_odczyt = 0.0
        self.revision = ""
        self.watek: threading.Thread | None = None
        self.ostatni_odczyt_o = ""
        self.ostatni_blad = ""
        self.wlaczony = False

    def krok(self, teraz: float | None = None) -> None:
        teraz = time.monotonic() if teraz is None else teraz
        if self.watek is not None and self.watek.is_alive():
            return
        if teraz < self.nastepna_polityka:
            return
        self.nastepna_polityka = teraz + ODSTEP_POLITYKI
        try:
            polityka = pobierz_polityke(self.client, self.state, self.sciezka_polityki, self.nazwa)
        except Exception as exc:
            log.warning("konfiguracja %s niedostepna: %s", self.nazwa, exc)
            return
        if polityka["revision"] != self.revision:
            # Nowa konfiguracja (np. inny adres) - czytamy od razu, a nie po
            # odstepie liczonym od odczytu starej.
            self.revision = polityka["revision"]
            self.nastepny_odczyt = 0.0
        self.wlaczony = polityka["enabled"]
        if polityka["test"]:
            self._uruchom(polityka, "test")
        elif polityka["enabled"] and (polityka["odczyt_teraz"] or teraz >= self.nastepny_odczyt):
            self.nastepny_odczyt = teraz + polityka["interwal_sekund"]
            self._uruchom(polityka, "odczyt")

    def _uruchom(self, polityka: dict, rodzaj: str) -> None:
        self.watek = threading.Thread(target=self._przebieg, args=(polityka, rodzaj),
                                      name=f"{self.nazwa.lower()}-{rodzaj}", daemon=True)
        self.watek.start()

    def _przebieg(self, polityka: dict, rodzaj: str) -> None:
        tresc = {"protocol": 1, "revision": polityka["revision"]}
        try:
            prism = self.fabryka(polityka["adres"], polityka["uzytkownik"], polityka["haslo"],
                                 polityka.get("ca_pem", ""))
            tresc.update(self.wykonaj(prism, rodzaj))
            self.ostatni_blad = ""
            log.info("%s %s: klastry %d, hosty %d, VM %d w %d ms", self.nazwa, rodzaj,
                     len(tresc["klastry"]), len(tresc.get("hosty", [])), len(tresc.get("vm", [])),
                     tresc["czas_ms"])
        except BladPrism as exc:
            tresc.update(rodzaj=rodzaj, ok=False, blad=str(exc)[:2000])
            self.ostatni_blad = str(exc)
            log.warning("%s %s nieudany: %s", self.nazwa, rodzaj, exc)
            if rodzaj == "odczyt":
                self.nastepny_odczyt = min(self.nastepny_odczyt,
                                           time.monotonic() + PONOWIENIE_PO_BLEDZIE)
        except Exception as exc:  # nieznany ksztalt odpowiedzi nie moze zabic monitora
            tresc.update(rodzaj=rodzaj, ok=False, blad=f"blad agenta: {type(exc).__name__}: {exc}"[:2000])
            self.ostatni_blad = tresc["blad"]
            log.exception("%s %s: blad agenta", self.nazwa, rodzaj)
        finally:
            polityka.pop("haslo", None)
        try:
            self.client.post(self.sciezka_wyniku, token=self.state.agent_token, payload=tresc)
            if rodzaj == "odczyt" and tresc.get("ok"):
                self.ostatni_odczyt_o = _teraz().isoformat(timespec="seconds")
        except Exception as exc:
            log.warning("nie udalo sie wyslac wyniku %s: %s", self.nazwa, exc)

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)

    def status(self) -> dict:
        return {"wlaczony": self.wlaczony, "ostatni_odczyt_o": self.ostatni_odczyt_o,
                "ostatni_blad": self.ostatni_blad}
