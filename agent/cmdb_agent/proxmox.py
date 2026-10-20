"""Odczyt Proxmox VE (API /api2/json, PVE 7 i nowsze) - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto wystarczy z rola PVEAuditor; haslo albo sekret tokenu tylko
w pamieci; certyfikat weryfikowany zawsze; ksztalt API zostaje w tym pliku,
a serwer dostaje te same plaskie pola co z Nutanixa.

Logowanie na dwa sposoby, rozpoznawane po uzytkowniku:

* **token API** (zalecany) - uzytkownik w postaci "cmdb@pve!cmdb", haslo to
  sekret tokenu; same GET-y z naglowkiem PVEAPIToken, bez zadnego zapisu,
* **haslo** - uzytkownik z realmem ("cmdb@pve"); POST /access/ticket wydaje
  bilet (to jedyny POST i nic w klastrze nie zmienia), potem GET z ciasteczkiem.
  Bilet wygasa sam po dwoch godzinach - API nie ma wylogowania.

Wszystko pozostale to GET.

Klastrem jest klaster Proxmoxa (nazwa z /cluster/status), hostem - wezel.
Wezel bez klastra (pojedynczy serwer) nie ma klastra, tak jak host ESXi
poza klastrem w vCenter. Maszyny wirtualne QEMU i kontenery LXC to "VM";
rozroznia je pole typ. Szablony pomijamy - nie dzialaja i nie sa maszynami.

Kolejnosc: lista wszystkich VM to jedno zapytanie (/cluster/resources), a
szczegoly (UUID z SMBIOS, dyski, karty) - jedno na maszyne. Adresy IP i system
podaje QEMU Guest Agent (dwa zapytania, tylko wlaczone maszyny z wlaczonym
agentem goscia), a w LXC - lista interfejsow kontenera.
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
from datetime import datetime, timedelta, timezone

from . import __version__
from .nutanix import BladPrism, CzytnikNutanix, _liczba, _tekst, _z, kontekst_tls

log = logging.getLogger(__name__)

LIMIT_CZASU = 30
MAKS_VM = 20000  # ten sam limit przyjmuje serwer
API = "/api2/json"

STANY = {"running": "ON", "stopped": "OFF", "paused": "SUSPENDED", "suspended": "SUSPENDED"}
TYPY = {"qemu": "QEMU/KVM", "lxc": "LXC"}
# ostype maszyny QEMU - tylko podpowiedz z konfiguracji, gdy agent goscia milczy.
SYSTEMY_QEMU = {
    "l26": "Linux", "l24": "Linux 2.4", "win11": "Windows 11/2022/2025", "win10": "Windows 10/2016/2019",
    "win8": "Windows 8/2012", "win7": "Windows 7/2008 R2", "wvista": "Windows Vista/2008",
    "w2k8": "Windows Vista/2008", "w2k3": "Windows XP/2003", "wxp": "Windows XP/2003",
    "w2k": "Windows 2000", "solaris": "Solaris",
}
_DYSK_QEMU = re.compile(r"^(ide|sata|scsi|virtio)\d+$")
_DYSK_LXC = re.compile(r"^(rootfs|mp\d+)$")
_KARTA = re.compile(r"^net\d+$")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.I)
_JEDNOSTKI = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}


class BladProxmox(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BladProxmox(f"Proxmox przekierowuje na {newurl} - podaj docelowy adres w konfiguracji")


class ProxmoxVE:
    """Minimalny klient API: token albo bilet z hasla, potem GET."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 limit_czasu: int = LIMIT_CZASU):
        self.adres = adres.rstrip("/")
        self.token = "!" in uzytkownik
        self._konto = (uzytkownik, haslo)
        self._naglowki: dict[str, str] = {}
        # Bez przekierowan: przekierowanie mogloby zaniesc token albo bilet
        # na inny host.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst_tls(self.adres, ca_pem)), _BezPrzekierowan())
        self.limit_czasu = limit_czasu

    # --- logowanie ---

    def __enter__(self) -> "ProxmoxVE":
        uzytkownik, haslo = self._konto
        if "@" not in uzytkownik.split("!", 1)[0]:
            raise BladProxmox("podaj uzytkownika z realmem, np. cmdb@pve albo cmdb@pam"
                              " (token API: cmdb@pve!nazwa-tokenu)")
        if self.token:
            self._naglowki = {"Authorization": f"PVEAPIToken={uzytkownik}={haslo}"}
        else:
            bilet = self._zadanie("POST", "/access/ticket", tresc={"username": uzytkownik, "password": haslo})
            if isinstance(bilet, dict) and bilet.get("NeedTFA"):
                raise BladProxmox("konto ma wlaczone 2FA - uzyj tokenu API (uzytkownik@realm!token)")
            if not isinstance(bilet, dict) or not bilet.get("ticket"):
                raise BladProxmox("Proxmox nie zwrocil biletu logowania")
            # Bilet wprost, jak klienci API Proxmoxa - nie ma w nim ";" ani spacji.
            self._naglowki = {"Cookie": "PVEAuthCookie=" + str(bilet["ticket"])}
        self._konto = ("", "")
        return self

    def __exit__(self, *exc) -> None:
        self._naglowki = {}
        self._konto = ("", "")

    # --- zapytania ---

    def get(self, sciezka: str, parametry: dict | None = None, opcjonalne: bool = False):
        """GET; przy opcjonalne=True blad (np. agent goscia nie dziala) daje None."""
        try:
            return self._zadanie("GET", sciezka, parametry)
        except BladProxmox:
            if opcjonalne:
                return None
            raise

    def lista(self, sciezka: str, parametry: dict | None = None) -> list[dict]:
        wynik = self.get(sciezka, parametry)
        if not isinstance(wynik, list):
            raise BladProxmox(f"nieoczekiwany format odpowiedzi {sciezka}")
        return [x for x in wynik if isinstance(x, dict)]

    def _zadanie(self, metoda: str, sciezka: str, parametry: dict | None = None, tresc: dict | None = None):
        adres = self.adres + API + sciezka
        if parametry:
            adres += "?" + urllib.parse.urlencode(parametry)
        naglowki = {"Accept": "application/json", "User-Agent": f"cmdb-agent/{__version__}", **self._naglowki}
        dane = None
        if tresc is not None:
            dane = urllib.parse.urlencode(tresc).encode("utf-8")
            naglowki["Content-Type"] = "application/x-www-form-urlencoded"
        zadanie = urllib.request.Request(adres, data=dane, method=metoda, headers=naglowki)
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                surowe = odpowiedz.read().decode("utf-8")
                wynik = json.loads(surowe) if surowe.strip() else {}
                return wynik.get("data") if isinstance(wynik, dict) else None
        except urllib.error.HTTPError as exc:
            # Proxmox podaje przyczyne w wierszu statusu (np. "permission denied").
            powod = f" ({str(exc.reason)[:200]})" if exc.reason else ""
            if exc.code == 401:
                raise BladProxmox("Proxmox odrzucil dane logowania (HTTP 401) - sprawdz uzytkownika z realmem"
                                  " (np. cmdb@pve) i haslo albo identyfikator i sekret tokenu") from None
            if exc.code == 403:
                raise BladProxmox("konto nie ma uprawnien do odczytu (HTTP 403) - nadaj mu role PVEAuditor"
                                  f" na sciezce / z propagacja{powod}") from None
            raise BladProxmox(f"Proxmox odpowiedzial bledem HTTP {exc.code} dla {sciezka}{powod}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladProxmox(f"certyfikat Proxmoxa nie jest zaufany na tej maszynie: {powod.verify_message}"
                                  " - wklej w konfiguracji CA klastra (/etc/pve/pve-root-ca.pem)") from None
            raise BladProxmox(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladProxmox(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladProxmox(f"Proxmox zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka})") from None


# --- splaszczenie odpowiedzi -------------------------------------------------

def _opcje(wartosc) -> tuple[str, dict[str, str]]:
    """"local-lvm:vm-100-disk-0,size=32G,ssd=1" -> ("local-lvm:vm-100-disk-0", {"size": "32G", "ssd": "1"}).

    Pierwszy element bez "=" to wartosc glowna (wolumen dysku); w karcie
    sieciowej QEMU pierwszy element to "model=MAC" i trafia do slownika.
    """
    glowna, opcje = "", {}
    for i, czesc in enumerate(str(wartosc or "").split(",")):
        klucz, rowna, reszta = czesc.partition("=")
        if rowna:
            opcje[klucz.strip()] = reszta.strip()
        elif i == 0:
            glowna = czesc.strip()
    return glowna, opcje


def _rozmiar(tekst) -> int | None:
    """"32G" -> bajty. Bez jednostki - bajty (tak liczy Proxmox)."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)\s*$", str(tekst or ""), re.I)
    return int(float(m.group(1)) * _JEDNOSTKI[m.group(2).upper()]) if m else None


def _magazyn(wolumen: str) -> str | None:
    """"local-lvm:vm-100-disk-0" -> "local-lvm"; urzadzenie (/dev/...) nie ma magazynu."""
    if ":" not in wolumen or wolumen.startswith("/"):
        return None
    return _tekst(wolumen.split(":", 1)[0])


def _bez_lokalnych(adres) -> str | None:
    """Adres bez prefiksu; lokalne (petla, lacze) nic nie mowia o tym, gdzie maszyna stoi."""
    adres = str(adres or "").split("/", 1)[0].strip()
    if not adres or adres.startswith("127.") or adres.lower() in ("::1",) or adres.lower().startswith("fe80"):
        return None
    return adres


def _siec(opcje: dict) -> str | None:
    most = opcje.get("bridge")
    if not most:
        return None
    return _tekst(f"{most} (VLAN {opcje['tag']})" if opcje.get("tag") else most)


def _pamiec_mib(wartosc) -> int | None:
    """memory z konfiguracji: "4096" albo (PVE 8.1+) "current=4096"."""
    if isinstance(wartosc, str) and "=" in wartosc:
        wartosc = _opcje(wartosc)[1].get("current")
    return _liczba(wartosc)


def klaster(d: dict, wersja: str | None, liczba_hostow: int) -> dict:
    return {
        "ext_id": _tekst(d.get("name"), 64),
        "nazwa": _tekst(d.get("name")) or "",
        "wersja": wersja,
        "hipernadzorca": "Proxmox VE",
        "liczba_hostow": _liczba(d.get("nodes")) or liczba_hostow,
    }


def host(d: dict, klaster_id: str | None) -> dict:
    return {
        "ext_id": _tekst(d.get("name"), 64),
        "nazwa": _tekst(d.get("name")) or "",
        "klaster_id": klaster_id,
        "hipernadzorca": "Proxmox VE",
        "ip": _tekst(d.get("ip"), 64),
    }


def szczegoly_wezla(status: dict | None) -> dict:
    """Procesor, RAM, wersja i czas uruchomienia z /nodes/{wezel}/status."""
    if not isinstance(status, dict):
        return {}
    cpu = status.get("cpuinfo") or {}
    gniazda = _liczba(cpu.get("sockets"))
    rdzenie = _liczba(cpu.get("cores"))  # na gniazdo
    wersja = str(status.get("pveversion") or "").split("/")
    jadro = _tekst(_z(status, "current-kernel.release")) or (
        str(status.get("kversion") or "").split()[1:2] or [None])[0]
    hipernadzorca = f"Proxmox VE {wersja[1]}" if len(wersja) > 1 and wersja[1] else "Proxmox VE"
    czas = _liczba(status.get("uptime"))
    wynik = {
        "cpu_model": _tekst(cpu.get("model")),
        "gniazda": gniazda,
        "rdzenie": rdzenie * gniazda if rdzenie and gniazda else rdzenie,
        "watki": _liczba(cpu.get("cpus")),
        "ram_bajty": _liczba(_z(status, "memory.total")),
        "hipernadzorca": _tekst(f"{hipernadzorca} (jądro {jadro})" if jadro else hipernadzorca),
        "uruchomiony_o": (datetime.now(timezone.utc) - timedelta(seconds=czas)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if czas else None,
    }
    return {k: v for k, v in wynik.items() if v is not None}


def _ip_agenta_goscia(interfejsy) -> dict[str, list[str]]:
    """MAC -> adresy z QEMU Guest Agent (network-get-interfaces)."""
    wynik: dict[str, list[str]] = {}
    lista = interfejsy.get("result") if isinstance(interfejsy, dict) else interfejsy
    for interfejs in lista or []:
        if not isinstance(interfejs, dict):
            continue
        mac = str(interfejs.get("hardware-address") or "").lower()
        for wpis in interfejs.get("ip-addresses") or []:
            adres = _bez_lokalnych(wpis.get("ip-address")) if isinstance(wpis, dict) else None
            if adres:
                wynik.setdefault(mac, []).append(adres)
    return wynik


def _ip_kontenera(interfejsy) -> dict[str, list[str]]:
    """MAC -> adresy z /lxc/{vmid}/interfaces (dziala bez agenta w kontenerze)."""
    wynik: dict[str, list[str]] = {}
    for interfejs in interfejsy or []:
        if not isinstance(interfejs, dict):
            continue
        mac = str(interfejs.get("hwaddr") or "").lower()
        for klucz in ("inet", "inet6"):
            wartosci = interfejs.get(klucz)
            for adres in wartosci if isinstance(wartosci, list) else [wartosci]:
                adres = _bez_lokalnych(adres)
                if adres:
                    wynik.setdefault(mac, []).append(adres)
    return wynik


def _system_goscia(osinfo) -> str | None:
    dane = osinfo.get("result") if isinstance(osinfo, dict) else None
    if not isinstance(dane, dict):
        return None
    return _tekst(dane.get("pretty-name")) or _tekst(
        " ".join(str(dane[k]) for k in ("name", "version") if dane.get(k)) or None)


def vm(zasob: dict, konfiguracja: dict | None, klaster_id: str | None,
       adresy: dict[str, list[str]] | None = None, system_goscia: str | None = None,
       agent_goscia: bool | None = None) -> dict:
    """Maszyna QEMU albo kontener LXC.

    agent_goscia: True - QEMU Guest Agent odpowiedzial, False - maszyna dziala,
    a agent nie odpowiada (albo jest wylaczony), None - nie dotyczy.
    """
    k = konfiguracja or {}
    typ = str(zasob.get("type") or "")
    lxc = typ == "lxc"
    adresy = adresy or {}

    dyski = []
    for klucz in sorted(k, key=lambda x: (x != "rootfs", x)):
        if not (_DYSK_LXC if lxc else _DYSK_QEMU).match(klucz):
            continue
        wolumen, opcje = _opcje(k[klucz])
        if not lxc and (opcje.get("media") == "cdrom" or wolumen == "none"):
            continue
        rozmiar = _rozmiar(opcje.get("size"))
        if lxc and rozmiar is None:
            continue  # montowanie katalogu hosta, nie dysk kontenera
        dyski.append({
            "rozmiar_bajty": rozmiar,
            "magistrala": _tekst(klucz if lxc else re.sub(r"\d+$", "", klucz).upper(), 32),
            "kontener": _magazyn(wolumen),
        })

    karty = []
    for klucz in sorted(k, key=lambda x: _liczba(x[3:]) or 0):
        if not _KARTA.match(klucz):
            continue
        _, opcje = _opcje(k[klucz])
        if lxc:
            mac = opcje.get("hwaddr")
        else:
            mac = opcje.get("macaddr") or next((v for v in opcje.values() if _MAC.match(v)), None)
        mac = _tekst(mac.lower() if mac else None, 32)
        ip = list(adresy.get(mac or "", []))
        if lxc:
            for statyczny in (opcje.get("ip"), opcje.get("ip6")):
                statyczny = _bez_lokalnych(statyczny) if statyczny not in ("dhcp", "auto", "manual") else None
                if statyczny and statyczny not in ip:
                    ip.append(statyczny)
        karty.append({"mac": mac, "ip": ip[:32], "siec": _siec(opcje)})

    if lxc:
        rdzenie = _liczba(k.get("cores")) or _liczba(zasob.get("maxcpu"))
        gniazda = 1 if rdzenie else None
        system = _tekst(str(k["ostype"]).capitalize()) if k.get("ostype") else None
        uuid = None
    else:
        gniazda = _liczba(k.get("sockets")) or (1 if k else None)
        rdzenie = _liczba(k.get("cores")) or (1 if k else None)
        if not k and _liczba(zasob.get("maxcpu")):
            gniazda, rdzenie = 1, _liczba(zasob.get("maxcpu"))
        system = system_goscia or SYSTEMY_QEMU.get(str(k.get("ostype") or ""))
        uuid = _opcje(k.get("smbios1"))[1].get("uuid") if k.get("smbios1") else None
    ram_mib = _pamiec_mib(k.get("memory"))
    ram = ram_mib * 2**20 if ram_mib else _liczba(zasob.get("maxmem"))
    if not dyski and _liczba(zasob.get("maxdisk")):
        dyski.append({"rozmiar_bajty": _liczba(zasob.get("maxdisk")), "magistrala": None, "kontener": None})

    return {
        "ext_id": _tekst(zasob.get("vmid"), 64),
        "nazwa": _tekst(k.get("hostname") if lxc else k.get("name")) or _tekst(zasob.get("name")) or "",
        "klaster_id": klaster_id,
        "host_id": _tekst(zasob.get("node"), 64),
        "stan": STANY.get(str(zasob.get("status") or ""), _tekst(zasob.get("status"), 32)),
        "gniazda": gniazda,
        "rdzenie_na_gniazdo": rdzenie,
        "ram_bajty": ram,
        "dyski": dyski[:64],
        "karty": karty[:32],
        "ngt": agent_goscia,
        "system": _tekst(system),
        "bios_uuid": _tekst(uuid, 64),
        "opis": _tekst(k.get("description"), 1000),
        "projekt": _tekst(zasob.get("pool")),
        "typ": TYPY.get(typ, _tekst(typ)),
    }


def _bez_pustych(lista: list[dict]) -> list[dict]:
    return [x for x in lista if x.get("ext_id")]


def _wersja(pve: ProxmoxVE) -> str | None:
    dane = pve.get("/version", opcjonalne=True)
    return _tekst(dane.get("version"), 128) if isinstance(dane, dict) else None


def wykonaj(pve: ProxmoxVE, rodzaj: str) -> dict:
    """Test (logowanie, klaster i wezly) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    with pve:
        wersja = _wersja(pve)
        stan = pve.lista("/cluster/status")
        wpis_klastra = next((s for s in stan if s.get("type") == "cluster" and s.get("name")), None)
        wezly = [s for s in stan if s.get("type") == "node" and s.get("name")]
        if not wezly:
            raise BladProxmox("konto nie widzi zadnego wezla - nadaj role PVEAuditor na sciezce /"
                              " (token z oddzielnymi uprawnieniami potrzebuje wlasnego wpisu ACL)")
        klaster_id = _tekst(wpis_klastra.get("name"), 64) if wpis_klastra else None
        klastry = [klaster(wpis_klastra, wersja, len(wezly))] if wpis_klastra else []
        hosty = [host(w, klaster_id) for w in wezly]
        wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": wersja, "klastry": _bez_pustych(klastry),
                 "hosty": _bez_pustych(hosty)}
        if rodzaj != "test":
            online = {w["name"] for w in wezly if w.get("online") in (1, True, "1")}
            for h in wynik["hosty"]:
                if h["ext_id"] in online:
                    h.update(szczegoly_wezla(pve.get(f"/nodes/{_seg(h['ext_id'])}/status", opcjonalne=True)))
            maszyny = []
            for zasob in pve.lista("/cluster/resources", {"type": "vm"}):
                if zasob.get("template") in (1, True, "1") or zasob.get("vmid") is None:
                    continue
                if zasob.get("type") not in ("qemu", "lxc") or not zasob.get("node"):
                    continue
                if len(maszyny) >= MAKS_VM:
                    raise BladProxmox(f"ponad {MAKS_VM} maszyn wirtualnych - odczyt przerwany")
                maszyny.append(_maszyna(pve, zasob, klaster_id, zasob.get("node") in online))
            wynik["vm"] = _bez_pustych(maszyny)
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


def _seg(tekst) -> str:
    return urllib.parse.quote(str(tekst), safe="")


def _maszyna(pve: ProxmoxVE, zasob: dict, klaster_id: str | None, wezel_online: bool) -> dict:
    typ = zasob["type"]
    sciezka = f"/nodes/{_seg(zasob['node'])}/{typ}/{_seg(zasob['vmid'])}"
    konfiguracja = pve.get(sciezka + "/config", opcjonalne=True) if wezel_online else None
    konfiguracja = konfiguracja if isinstance(konfiguracja, dict) else None
    dziala = zasob.get("status") == "running" and wezel_online
    adresy, system, agent_goscia = {}, None, None
    if dziala and typ == "lxc":
        adresy = _ip_kontenera(pve.get(sciezka + "/interfaces", opcjonalne=True))
    elif dziala:
        agent_goscia = False
        wlaczony = _opcje((konfiguracja or {}).get("agent"))
        if wlaczony[0] == "1" or wlaczony[1].get("enabled") == "1":
            # Wymaga VM.GuestAgent.Audit (PVE 9) albo VM.Monitor (PVE 8) - bez
            # niego maszyna jest odczytana, tylko bez adresow i systemu.
            interfejsy = pve.get(sciezka + "/agent/network-get-interfaces", opcjonalne=True)
            osinfo = pve.get(sciezka + "/agent/get-osinfo", opcjonalne=True)
            if interfejsy is not None or osinfo is not None:
                agent_goscia = True
                adresy, system = _ip_agenta_goscia(interfejsy), _system_goscia(osinfo)
    return vm(zasob, konfiguracja, klaster_id, adresy, system, agent_goscia)


# --- praca w petli monitora -------------------------------------------------

class CzytnikProxmox(CzytnikNutanix):
    nazwa = "Proxmox"
    sciezka_polityki = "/api/v1/agent/proxmox-policy"
    sciezka_wyniku = "/api/v1/agent/proxmox"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or ProxmoxVE)

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
