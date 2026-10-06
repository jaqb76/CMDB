"""Odczyt OpenStack (Keystone v3, Nova, Cinder) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; haslo tylko w pamieci; certyfikat weryfikowany zawsze; ksztalt
API zostaje w tym pliku, a serwer dostaje te same plaskie pola co z Nutanixa.

Logowanie na dwa sposoby, oba do Keystone v3:

* **application credential** (zalecane) - w polu uzytkownika jego ID,
  w hasle sekret; zakres (projekt, role) zapisany jest w samym
  poswiadczeniu, wiec domena i projekt zostaja puste,
* **uzytkownik i haslo** - z domena (pusta = "Default") i projektem,
  w ktorym konto ma role.

Polaczenie idzie po https z weryfikacja certyfikatu. Po http - tylko gdy
administrator jawnie na to zezwolil w panelu (bez_tls); wtedy haslo i token
ida otwartym tekstem, wiec zalecane jest application credential.

Adresy Novy i Cindera pochodza z katalogu uslug, chyba ze panel podal je
recznie (katalog bywa z nazwami, ktorych agent nie rozwiazuje, np. "controller").

Jedyny zapis po stronie OpenStacka to token: POST /v3/auth/tokens zaklada
go (bez tokenu nie ma odczytu), na koncu DELETE go uniewaznia. Wszystko
pozostale to GET.

Klaster w CMDB to region z katalogu uslug, host - hypervisor Novy,
maszyna wirtualna - serwer Novy ze wszystkich projektow (all_tenants).
Na KVM/libvirt UUID instancji jest tez UUID-em SMBIOS maszyny, wiec VM
z agentem CMDB laczy sie z jego karta tak samo jak na AHV.

Lista hypervisorow i serwery wszystkich projektow wymagaja w Novie roli
admin (albo wlasnej reguly polityki) - bez nich odczyt konczy sie bledem,
a nie niepelna lista, bo niepelna lista wycofalaby cudze maszyny.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .nutanix import BladPrism, CzytnikNutanix, _liczba, _tekst, _z

log = logging.getLogger(__name__)

LIMIT_CZASU = 30
STRONA = 1000
MAKS_OBIEKTOW = 20000  # ten sam limit przyjmuje serwer

# 2.53 (Pike): identyfikatory hypervisorow to UUID i dzialaja marker/limit.
# Nowszych nie pytamy, bo od 2.88 hypervisor nie podaje juz CPU ani RAM.
MIKROWERSJA = "2.53"

STANY = {"ACTIVE": "ON", "SHUTOFF": "OFF", "SUSPENDED": "SUSPENDED", "PAUSED": "SUSPENDED",
         "SHELVED": "OFF", "SHELVED_OFFLOADED": "OFF"}
INTERFEJSY = ("public", "internal")


class BladOpenstack(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladOpenstack(f"OpenStack przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


def _opis_bledu(exc: urllib.error.HTTPError) -> str:
    """Komunikat z tresci - Keystone i Nova podaja go w {"error"|"<typ>": {"message": ...}}."""
    try:
        dane = json.loads(exc.read(4096).decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    if not isinstance(dane, dict):
        return ""
    for wartosc in dane.values():
        if isinstance(wartosc, dict) and wartosc.get("message"):
            return f" ({str(wartosc['message'])[:300]})"
    return ""


def _wersja_hipernadzorcy(typ, wersja) -> str | None:
    """Nova podaje wersje jako liczbe: 6002000 to 6.2.0."""
    numer = _liczba(wersja)
    if numer and numer > 0:
        wersja = f"{numer // 1_000_000}.{numer // 1000 % 1000}.{numer % 1000}"
    return _tekst(" ".join(str(x) for x in (typ, wersja) if x))


def _z_wersja(adres: str, wersja: str) -> str:
    adres = (adres or "").rstrip("/")
    if adres and not urllib.parse.urlsplit(adres).path:
        adres += wersja
    return adres


class Openstack:
    """Minimalny klient: token z Keystone, potem GET z X-Auth-Token."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 domena: str = "", projekt: str = "", bez_tls: bool = False,
                 adres_compute: str = "", adres_volumes: str = "", limit_czasu: int = LIMIT_CZASU):
        adres = adres.rstrip("/")
        self.adres = adres if adres.endswith("/v3") else adres + "/v3"
        self.uzytkownik, self.domena, self.projekt = uzytkownik, domena, projekt
        self._haslo = haslo
        self.bez_tls = bez_tls
        # Sam host:port (bez sciezki) to w Novie i Cinderze tylko lista wersji API.
        self.adres_compute = _z_wersja(adres_compute, "/v2.1")
        self.adres_volumes = _z_wersja(adres_volumes, "/v3")
        self.projekt_id = ""
        kontekst = ssl.create_default_context(cadata=ca_pem or None)
        kontekst.minimum_version = ssl.TLSVersion.TLSv1_2
        kontekst.check_hostname = True
        kontekst.verify_mode = ssl.CERT_REQUIRED
        # Bez przekierowan: przekierowanie mogloby zaniesc token na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst), _BezPrzekierowan())
        self.limit_czasu = limit_czasu
        self.token: str | None = None
        self.katalog: list[dict] = []

    # --- sesja ---

    def __enter__(self):
        self.zaloguj()
        return self

    def __exit__(self, *exc):
        self.wyloguj()

    def _cialo_logowania(self) -> dict:
        if not self.projekt:
            return {"auth": {"identity": {"methods": ["application_credential"], "application_credential": {
                "id": self.uzytkownik, "secret": self._haslo}}}}
        domena = {"name": self.domena or "Default"}
        return {"auth": {
            "identity": {"methods": ["password"], "password": {"user": {
                "name": self.uzytkownik, "domain": domena, "password": self._haslo}}},
            "scope": {"project": {"name": self.projekt, "domain": domena}},
        }}

    def zaloguj(self) -> None:
        tresc = json.dumps(self._cialo_logowania()).encode()
        self._haslo = ""  # dalej potrzebny tylko token
        odpowiedz, naglowki = self._zadanie(self.adres + "/auth/tokens", "POST", tresc, uwierzytelnij=False)
        self.token = naglowki.get("X-Subject-Token")
        if not self.token:
            raise BladOpenstack("Keystone nie zwrocil tokenu (brak naglowka X-Subject-Token)")
        self.katalog = [u for u in _z(odpowiedz, "token.catalog") or [] if isinstance(u, dict)]
        self.projekt_id = str(_z(odpowiedz, "token.project.id") or "")
        if not self.katalog and not self.adres_compute:
            self.wyloguj()  # wyjatek z __enter__ omija __exit__
            raise BladOpenstack("token bez katalogu uslug - konto nie ma roli w projekcie"
                                " albo application credential nie ma zakresu")

    def wyloguj(self) -> None:
        if not self.token:
            return
        try:
            self._zadanie(self.adres + "/auth/tokens", "DELETE",
                          naglowki={"X-Subject-Token": self.token})
        except Exception as exc:  # token i tak wygasnie sam
            log.debug("uniewaznienie tokenu OpenStack: %s", exc)
        self.token = None

    # --- zapytania ---

    def _zadanie(self, adres: str, metoda: str = "GET", tresc: bytes | None = None,
                 naglowki: dict | None = None, uwierzytelnij: bool = True,
                 opcjonalne: bool = False, nova: bool = True):
        if not (adres.startswith("https://") or (self.bez_tls and adres.startswith("http://"))):
            raise BladOpenstack(f"adres bez https ({adres.split('?')[0]}) - token nie moze isc otwartym"
                                " tekstem; zezwol na polaczenie bez TLS w konfiguracji albo uzyj https")
        wszystkie = {"Accept": "application/json", "User-Agent": f"cmdb-agent/{__version__}"}
        if nova:  # mikrowersja Novy - innym uslugom nic do niej (Cinder odpowiadal 400)
            wszystkie.update({"OpenStack-API-Version": f"compute {MIKROWERSJA}",
                              "X-OpenStack-Nova-API-Version": MIKROWERSJA})
        if tresc is not None:
            wszystkie["Content-Type"] = "application/json"
        if uwierzytelnij:
            wszystkie["X-Auth-Token"] = self.token or ""
        wszystkie.update(naglowki or {})
        zadanie = urllib.request.Request(adres, data=tresc, method=metoda, headers=wszystkie)
        sciezka = urllib.parse.urlsplit(adres).path
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                surowe = odpowiedz.read().decode("utf-8")
                return (json.loads(surowe) if surowe else {}), odpowiedz.headers
        except urllib.error.HTTPError as exc:
            if opcjonalne and exc.code in (403, 404):
                return None, {}
            opis = _opis_bledu(exc)
            if exc.code == 401:
                raise BladOpenstack("Keystone odrzucil dane logowania (HTTP 401) - sprawdz"
                                    f" uzytkownika/ID poswiadczenia, haslo, domene i projekt{opis}") from None
            if exc.code == 403:
                raise BladOpenstack(f"konto nie ma uprawnien do {sciezka} (HTTP 403){opis}"
                                    " - odczyt hypervisorow i maszyn wszystkich projektow wymaga roli admin") from None
            if exc.code == 404 and sciezka.endswith("/auth/tokens") and not self.projekt:
                # Keystone tak odpowiada na nieznane ID application credential.
                raise BladOpenstack(f"Keystone nie zna application credential o ID {self.uzytkownik!r} (HTTP 404)"
                                    " - przy logowaniu uzytkownikiem i haslem wpisz projekt (np. admin)") from None
            if exc.code == 404:
                raise BladOpenstack(f"OpenStack nie zna {sciezka} (HTTP 404){opis} - czy adres wskazuje Keystone v3,"
                                    " a adres Compute konczy sie na /v2.1?") from None
            if exc.code == 406:
                raise BladOpenstack(f"Nova nie obsluguje mikrowersji {MIKROWERSJA} - wymagany OpenStack Pike lub nowszy") from None
            raise BladOpenstack(f"OpenStack odpowiedzial bledem HTTP {exc.code} dla {sciezka}{opis}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladOpenstack(f"certyfikat OpenStack nie jest zaufany na tej maszynie: {powod.verify_message}"
                                    " - dodaj firmowe CA do systemu albo wklej je w konfiguracji") from None
            if isinstance(powod, BladOpenstack):
                raise powod from None
            raise BladOpenstack(f"brak polaczenia z {adres.split('?')[0]}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladOpenstack(f"brak polaczenia z {adres.split('?')[0]}: {exc}") from None
        except ValueError:
            raise BladOpenstack(f"OpenStack zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka})") from None

    def get(self, adres: str, parametry: dict | None = None, opcjonalne: bool = False, nova: bool = True):
        if parametry:
            adres += ("&" if "?" in adres else "?") + urllib.parse.urlencode(parametry)
        return self._zadanie(adres, opcjonalne=opcjonalne, nova=nova)[0]

    def lista(self, adres: str, klucz: str, parametry: dict | None = None,
              opcjonalne: bool = False, nova: bool = True) -> list[dict] | None:
        """Wszystkie strony listy Nova/Cinder (limit + marker)."""
        wynik: list[dict] = []
        marker = None
        while True:
            zapytanie = dict(parametry or {}, limit=STRONA)
            if marker:
                zapytanie["marker"] = marker
            odpowiedz = self.get(adres, zapytanie, opcjonalne=opcjonalne, nova=nova)
            if odpowiedz is None:
                return None
            dane = odpowiedz.get(klucz)
            if not isinstance(dane, list):
                raise BladOpenstack(f"nieoczekiwany format odpowiedzi {urllib.parse.urlsplit(adres).path}")
            wynik.extend(d for d in dane if isinstance(d, dict))
            if len(wynik) > MAKS_OBIEKTOW:
                raise BladOpenstack(f"ponad {MAKS_OBIEKTOW} obiektow w {klucz} - odczyt przerwany")
            if len(dane) < STRONA or not dane or not dane[-1].get("id"):
                return wynik
            marker = dane[-1]["id"]

    def uslugi(self) -> tuple[dict[str, str], dict[str, str]]:
        """Region -> adres Novy i region -> adres Cindera, z recznymi adresami z panelu.

        Reczny adres to jeden region: pierwszy z katalogu (albo "RegionOne",
        gdy katalog go nie podaje). Cinder v3 przed Yoga chce ID projektu
        w sciezce - adres konczacy sie na /v3 dostaje je z tokenu.
        """
        compute = self.punkty("compute")
        wolumeny = self.punkty("volumev3", "block-storage", "volumev2")
        region = next(iter(compute), "") or next(iter(wolumeny), "") or "RegionOne"
        if self.adres_compute:
            compute = {region: self.adres_compute}
        if self.adres_volumes:
            adres = self.adres_volumes
            if adres.rsplit("/", 1)[-1] in ("v2", "v3") and self.projekt_id:
                adres += "/" + self.projekt_id
            wolumeny = {region: adres}
        return compute, wolumeny

    def punkty(self, *typy: str) -> dict[str, str]:
        """Region -> adres uslugi danego typu (pierwszy pasujacy typ, public przed internal)."""
        wynik: dict[str, str] = {}
        for typ in typy:
            for usluga in self.katalog:
                if usluga.get("type") != typ:
                    continue
                for interfejs in INTERFEJSY:
                    for punkt in usluga.get("endpoints") or []:
                        region = punkt.get("region_id") or punkt.get("region") or ""
                        if punkt.get("interface") == interfejs and punkt.get("url") and region not in wynik:
                            wynik[region] = str(punkt["url"]).rstrip("/")
        return wynik


# --- splaszczenie ------------------------------------------------------------

def klaster(region: str, wersja: str | None = None, hosty: list[dict] | None = None) -> dict:
    typy = sorted({h["hipernadzorca"].split()[0] for h in hosty or [] if h.get("hipernadzorca")})
    return {
        "ext_id": _tekst(region, 64),
        "nazwa": _tekst(region) or "",
        "wersja": _tekst(wersja, 128),
        "hipernadzorca": _tekst(typy, 128),
        "liczba_hostow": len(hosty) if hosty is not None else None,
    }


def host(d: dict, region: str, strefy: dict[str, str] | None = None) -> dict:
    cpu = d.get("cpu_info")
    if isinstance(cpu, str):  # starsze mikrowersje: JSON w napisie
        try:
            cpu = json.loads(cpu)
        except ValueError:
            cpu = {}
    cpu = cpu if isinstance(cpu, dict) else {}
    gniazda = _liczba(_z(cpu, "topology.sockets"))
    rdzenie = _liczba(_z(cpu, "topology.cores"))
    usluga = _z(d, "service.host")
    ram = _liczba(d.get("memory_mb"))
    return {
        "ext_id": _tekst(d.get("id"), 64),
        "nazwa": _tekst(d.get("hypervisor_hostname") or usluga) or "",
        "klaster_id": _tekst(region, 64),
        "cpu_model": _tekst(cpu.get("model")),
        "gniazda": gniazda,
        "rdzenie": gniazda * rdzenie if gniazda and rdzenie else None,
        "watki": _liczba(d.get("vcpus")),
        "ram_bajty": ram * 2**20 if ram else None,
        "hipernadzorca": _wersja_hipernadzorcy(d.get("hypervisor_type"), d.get("hypervisor_version")),
        "ip": _tekst(d.get("host_ip"), 64),
        "tryb_serwisowy": (d.get("status") == "disabled") if d.get("status") else None,
        "strefa": _tekst((strefy or {}).get(str(usluga or ""))),
    }


def vm(d: dict, region: str, host_id: str | None, wolumeny: dict[str, dict] | None = None,
       projekty: dict[str, str] | None = None) -> dict:
    flavor = d.get("flavor") if isinstance(d.get("flavor"), dict) else {}
    dyski = []
    if _liczba(flavor.get("disk")):
        dyski.append({"rozmiar_bajty": _liczba(flavor["disk"]) * 2**30, "magistrala": None,
                      "kontener": "dysk lokalny (flavor)"})
    for zalaczony in d.get("os-extended-volumes:volumes_attached") or []:
        wolumen = (wolumeny or {}).get(str(_z(zalaczony, "id") or ""))
        if wolumen is None:
            continue
        rozmiar = _liczba(wolumen.get("size"))
        dyski.append({"rozmiar_bajty": rozmiar * 2**30 if rozmiar else None, "magistrala": None,
                      "kontener": _tekst(wolumen.get("volume_type") or wolumen.get("name"))})
    karty: dict[str, dict] = {}
    for siec, adresy in (d.get("addresses") or {}).items():
        for adres in adresy if isinstance(adresy, list) else []:
            if not isinstance(adres, dict):
                continue
            mac = _tekst(adres.get("OS-EXT-IPS-MAC:mac_addr"), 32) or f"?{siec}"
            karta = karty.setdefault(mac, {"mac": mac if not mac.startswith("?") else None,
                                           "ip": [], "siec": _tekst(siec)})
            if adres.get("addr") and adres["addr"] not in karta["ip"] and len(karta["ip"]) < 32:
                karta["ip"].append(str(adres["addr"]))
    projekt = d.get("tenant_id") or d.get("project_id")
    stan = str(d.get("status") or "").upper()
    return {
        "ext_id": _tekst(d.get("id"), 64),
        "nazwa": _tekst(d.get("name")) or "",
        "klaster_id": _tekst(region, 64),
        "host_id": _tekst(host_id, 64),
        "stan": STANY.get(stan, _tekst(stan, 32)),
        "gniazda": _liczba(flavor.get("vcpus")),
        "rdzenie_na_gniazdo": 1 if _liczba(flavor.get("vcpus")) else None,
        "ram_bajty": _liczba(flavor.get("ram")) * 2**20 if _liczba(flavor.get("ram")) else None,
        "dyski": dyski[:64],
        "karty": list(karty.values())[:32],
        "ngt": None,
        "system": None,
        "bios_uuid": _tekst(d.get("id"), 64),
        "opis": _tekst(d.get("description"), 1000),
        "projekt": _tekst((projekty or {}).get(str(projekt or "")) or projekt),
        "strefa": _tekst(d.get("OS-EXT-AZ:availability_zone")),
        "typ": _tekst(flavor.get("original_name") or flavor.get("name")),
    }


def _bez_pustych(lista: list[dict]) -> list[dict]:
    return [x for x in lista if x.get("ext_id")]


def _wersja_novy(chmura: Openstack, adres: str) -> str | None:
    dane = chmura.get(adres, opcjonalne=True)
    wersja = _z(dane or {}, "version.version")
    if not wersja:
        return None
    try:
        glowna, podrzedna = (int(x) for x in str(wersja).split(".")[:2])
    except ValueError:
        return _tekst(f"Nova API {wersja}", 128)
    if (glowna, podrzedna) < (2, 53):
        raise BladOpenstack(f"Nova obsluguje mikrowersje do {wersja}, a odczyt wymaga {MIKROWERSJA}"
                            " - wymagany OpenStack Pike lub nowszy")
    return _tekst(f"Nova API {wersja}", 128)


def _strefy(chmura: Openstack, adres: str) -> dict[str, str]:
    """Host uslugi nova-compute -> strefa dostepnosci (tylko gdy konto to widzi)."""
    dane = chmura.get(adres + "/os-availability-zone/detail", opcjonalne=True) or {}
    wynik = {}
    for strefa in dane.get("availabilityZoneInfo") or []:
        for nazwa_hosta, uslugi in (strefa.get("hosts") or {}).items():
            if isinstance(uslugi, dict) and "nova-compute" in uslugi:
                wynik[nazwa_hosta] = strefa.get("zoneName") or ""
    return wynik


def _projekty(chmura: Openstack) -> dict[str, str]:
    """ID projektu -> nazwa; konto bez uprawnien do listy projektow dostaje same ID."""
    dane = chmura.get(chmura.adres + "/projects", opcjonalne=True) or {}
    return {str(p["id"]): str(p.get("name") or p["id"]) for p in dane.get("projects") or []
            if isinstance(p, dict) and p.get("id")}


def wykonaj(chmura: Openstack, rodzaj: str) -> dict:
    """Test (token + regiony) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    with chmura:
        compute, wolumeny_url = chmura.uslugi()
        if not compute:
            raise BladOpenstack("katalog uslug nie ma Novy (typ compute) - czy to projekt z maszynami?")
        wersje = {region: _wersja_novy(chmura, adres) for region, adres in compute.items()}
        wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": next((w for w in wersje.values() if w), None)}
        if rodzaj == "test":
            wynik["klastry"] = _bez_pustych([klaster(r, wersje[r]) for r in compute])
        else:
            projekty = _projekty(chmura)
            klastry, hosty, maszyny = [], [], []
            for region, adres in compute.items():
                strefy = _strefy(chmura, adres)
                surowe_hosty = chmura.lista(adres + "/os-hypervisors/detail", "hypervisors")
                w_regionie = [host(h, region, strefy) for h in surowe_hosty]
                po_nazwie = {}
                for surowy, wpis in zip(surowe_hosty, w_regionie):
                    for nazwa in (surowy.get("hypervisor_hostname"), _z(surowy, "service.host")):
                        if nazwa:
                            po_nazwie.setdefault(str(nazwa).lower(), wpis["ext_id"])
                klastry.append(klaster(region, wersje[region], w_regionie))
                hosty.extend(w_regionie)
                wolumeny = {}
                if region in wolumeny_url:
                    # Wolumeny tylko uzupelniaja dyski VM - ich blad nie przerywa odczytu.
                    try:
                        lista_wolumenow = chmura.lista(wolumeny_url[region] + "/volumes/detail", "volumes",
                                                       {"all_tenants": 1}, opcjonalne=True, nova=False)
                    except BladOpenstack as exc:
                        log.warning("OpenStack: lista wolumenow niedostepna, dyski bez wolumenow: %s", exc)
                        lista_wolumenow = None
                    wolumeny = {str(w["id"]): w for w in lista_wolumenow or [] if w.get("id")}
                for s in chmura.lista(adres + "/servers/detail", "servers", {"all_tenants": 1}):
                    nazwa_hosta = s.get("OS-EXT-SRV-ATTR:hypervisor_hostname") or s.get("OS-EXT-SRV-ATTR:host")
                    host_id = po_nazwie.get(str(nazwa_hosta).lower()) if nazwa_hosta else None
                    maszyny.append(vm(s, region, host_id, wolumeny, projekty))
                    if len(maszyny) > MAKS_OBIEKTOW:
                        raise BladOpenstack(f"ponad {MAKS_OBIEKTOW} maszyn wirtualnych - odczyt przerwany")
            wynik.update(klastry=_bez_pustych(klastry), hosty=_bez_pustych(hosty),
                         vm=_bez_pustych(maszyny))
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


# --- praca w petli monitora -------------------------------------------------

class CzytnikOpenstack(CzytnikNutanix):
    nazwa = "OpenStack"
    sciezka_polityki = "/api/v1/agent/openstack-policy"
    sciezka_wyniku = "/api/v1/agent/openstack"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or Openstack)

    def klient(self, polityka: dict):
        return self.fabryka(polityka["adres"], polityka["uzytkownik"], polityka["haslo"],
                            polityka.get("ca_pem", ""), domena=polityka.get("domena", ""),
                            projekt=polityka.get("projekt", ""), bez_tls=polityka.get("bez_tls", False),
                            adres_compute=polityka.get("adres_compute", ""),
                            adres_volumes=polityka.get("adres_volumes", ""))

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
