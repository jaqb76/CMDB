"""Odczyt klastra Ceph przez Ceph Dashboard REST API - strona agenta.

Te same zasady co przy Prism Central (patrz nutanix.py): laczy sie agent,
nie serwer; konto w Dashboardzie wystarczy z rola read-only; haslo tylko
w pamieci; certyfikat weryfikowany zawsze, a http tylko po jawnej zgodzie
w panelu (Dashboard bywa wystawiony bez TLS, zwykle na porcie 8080).

Jedyny zapis po stronie Cepha to sesja: POST /api/auth wydaje token JWT,
na koncu POST /api/auth/logout go uniewaznia. Wszystko pozostale to GET.

Serwer dostaje jeden klaster na polaczenie (Dashboard to jeden klaster):
fsid, wersja, zdrowie z ostrzezeniami, pojemnosc, OSD, monitory, wezly
z rolami i pule z zajetoscia. Po fsid serwer laczy klaster z regionem
OpenStacka, ktorego Cinder trzyma wolumeny w tym Cephie.
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

LIMIT_CZASU = 30
MAKS_HOSTOW = 2000
MAKS_PUL = 2000
# Wersja API Dashboardu - wspolna dla wszystkich uzywanych tu zasobow od Pacific.
ACCEPT = "application/vnd.ceph.api.v1.0+json"
_FSID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class BladCeph(BladPrism):
    """Odczyt sie nie udal - komunikat trafia do panelu, wiec bez hasel."""


class _Przekierowanie(Exception):
    def __init__(self, adres: str):
        super().__init__(adres)
        self.adres = adres


class _BezPrzekierowan(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Dashboard na mgr w trybie standby przekierowuje na aktywny mgr - o tym,
        # czy pojsc za przekierowaniem, decyduje klient (tylko po zweryfikowanym https).
        raise _Przekierowanie(newurl)


def _opis_bledu(exc: urllib.error.HTTPError) -> str:
    try:
        dane = json.loads(exc.read(4096).decode("utf-8", "replace") or "{}")
    except Exception:
        return ""
    if isinstance(dane, dict) and (dane.get("detail") or dane.get("message")):
        return f" ({str(dane.get('detail') or dane.get('message'))[:300]})"
    return ""


class CephDashboard:
    """Minimalny klient: token z /api/auth, potem GET z naglowkiem Bearer."""

    def __init__(self, adres: str, uzytkownik: str, haslo: str, ca_pem: str = "",
                 bez_tls: bool = False, limit_czasu: int = LIMIT_CZASU):
        self.uzytkownik = uzytkownik
        self._haslo = haslo
        self.bez_tls = bez_tls
        self._ca_pem = ca_pem
        self.limit_czasu = limit_czasu
        self.token: str | None = None
        self.przekierowano_na: str | None = None
        self._ustaw_adres(adres)

    def _ustaw_adres(self, adres: str) -> None:
        self.adres = adres.rstrip("/")
        # Przekierowan nie wykonuje urllib - patrz _zadanie (tylko standby -> aktywny mgr).
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=kontekst_tls(self.adres, self._ca_pem)), _BezPrzekierowan())

    def __enter__(self):
        self.zaloguj()
        return self

    def __exit__(self, *exc):
        self.wyloguj()

    def zaloguj(self) -> None:
        tresc = json.dumps({"username": self.uzytkownik, "password": self._haslo}).encode()
        self._haslo = ""  # dalej potrzebny tylko token
        odpowiedz = self._zadanie("/api/auth", "POST", tresc, uwierzytelnij=False)
        self.token = (odpowiedz or {}).get("token") if isinstance(odpowiedz, dict) else None
        if not self.token:
            raise BladCeph("Ceph Dashboard nie zwrocil tokenu po zalogowaniu")

    def wyloguj(self) -> None:
        if not self.token:
            return
        try:
            self._zadanie("/api/auth/logout", "POST", b"{}")
        except Exception as exc:  # token i tak wygasnie sam
            log.debug("wylogowanie z Ceph Dashboard: %s", exc)
        self.token = None

    def _zadanie(self, sciezka: str, metoda: str = "GET", tresc: bytes | None = None,
                 uwierzytelnij: bool = True, opcjonalne: bool = False):
        adres = self.adres + sciezka
        if not (adres.startswith("https://") or (self.bez_tls and adres.startswith("http://"))):
            raise BladCeph(f"adres bez https ({self.adres}) - haslo nie moze isc otwartym tekstem;"
                           " zezwol na polaczenie bez TLS w konfiguracji albo uzyj https")
        naglowki = {"Accept": ACCEPT, "User-Agent": f"cmdb-agent/{__version__}"}
        if tresc is not None:
            naglowki["Content-Type"] = "application/json"
        if uwierzytelnij:
            naglowki["Authorization"] = f"Bearer {self.token or ''}"
        zadanie = urllib.request.Request(adres, data=tresc, method=metoda, headers=naglowki)
        sciezka_bez = sciezka.split("?")[0]
        try:
            with self._opener.open(zadanie, timeout=self.limit_czasu) as odpowiedz:
                surowe = odpowiedz.read().decode("utf-8")
                return json.loads(surowe) if surowe else {}
        except _Przekierowanie as przekierowanie:
            return self._po_przekierowaniu(przekierowanie.adres, sciezka, metoda, tresc, uwierzytelnij, opcjonalne)
        except urllib.error.HTTPError as exc:
            if opcjonalne and exc.code in (403, 404):
                return None
            opis = _opis_bledu(exc)
            if exc.code in (400, 401) and sciezka_bez == "/api/auth":
                raise BladCeph(f"Ceph Dashboard odrzucil dane logowania (HTTP {exc.code}) - sprawdz"
                               f" uzytkownika i haslo{opis}") from None
            if exc.code == 401:
                raise BladCeph(f"Ceph Dashboard odrzucil token (HTTP 401){opis}") from None
            if exc.code == 403:
                raise BladCeph(f"konto nie ma uprawnien do {sciezka_bez} (HTTP 403){opis}"
                               " - nadaj mu role read-only") from None
            if exc.code == 404:
                raise BladCeph(f"Ceph Dashboard nie zna {sciezka_bez} (HTTP 404) - czy adres wskazuje"
                               " Dashboard (mgr, zwykle port 8443)?") from None
            if exc.code == 415 or exc.code == 406:
                raise BladCeph(f"Ceph Dashboard nie obsluguje API v1.0 ({sciezka_bez}, HTTP {exc.code})"
                               " - wymagany Ceph Pacific lub nowszy") from None
            raise BladCeph(f"Ceph Dashboard odpowiedzial bledem HTTP {exc.code} dla {sciezka_bez}{opis}") from None
        except urllib.error.URLError as exc:
            powod = exc.reason
            if isinstance(powod, ssl.SSLCertVerificationError):
                raise BladCeph(f"certyfikat Ceph Dashboard nie jest zaufany na tej maszynie: {powod.verify_message}"
                               " - wklej w konfiguracji CA albo sam certyfikat Dashboardu (zostanie przypiety)") from None
            raise BladCeph(f"brak polaczenia z {self.adres}: {powod}") from None
        except (TimeoutError, OSError) as exc:
            raise BladCeph(f"brak polaczenia z {self.adres}: {exc}") from None
        except ValueError:
            raise BladCeph(f"Ceph Dashboard zwrocil odpowiedz, ktora nie jest JSON-em ({sciezka_bez})") from None

    def _po_przekierowaniu(self, cel: str, sciezka: str, metoda: str, tresc, uwierzytelnij: bool, opcjonalne: bool):
        """Mgr w trybie standby wskazuje aktywny mgr ("https://<ip>:8443/").

        Idziemy tam raz i tylko po https: zrodlo przekierowania przeszlo
        weryfikacje TLS, a nowy adres przechodzi ja osobno (ten sam certyfikat
        Dashboardu albo zaufane CA). Po http przekierowanie moglby podstawic
        ktokolwiek w sieci - wtedy blad, nie haslo pod obcy adres.
        """
        czesci = urllib.parse.urlsplit(cel)
        nowy = f"{czesci.scheme}://{czesci.netloc}"
        if (self.przekierowano_na is not None or not self.adres.startswith("https://")
                or czesci.scheme != "https" or not czesci.netloc or nowy == self.adres):
            raise BladCeph(f"Ceph Dashboard przekierowuje na {cel} - podaj adres aktywnego mgr"
                           " albo adres za load balancerem")
        log.info("Ceph Dashboard: %s to mgr w trybie standby, aktywny: %s", self.adres, nowy)
        self.przekierowano_na = nowy
        self._ustaw_adres(nowy)
        return self._zadanie(sciezka, metoda, tresc, uwierzytelnij, opcjonalne)

    def get(self, sciezka: str, opcjonalne: bool = False):
        return self._zadanie(sciezka, opcjonalne=opcjonalne)


# --- splaszczenie ------------------------------------------------------------

def _wersja(tekst) -> str | None:
    """'ceph version 18.2.1 (7fe91d5d...) reef (stable)' -> '18.2.1 reef'."""
    if not tekst:
        return None
    m = re.search(r"(\d+\.\d+\.\d+)\S*(?:\s+\([0-9a-f]+\))?\s+([a-z]+)", str(tekst))
    return _tekst(f"{m.group(1)} {m.group(2)}" if m else tekst, 128)


def _ostrzezenia(zdrowie: dict) -> list[str]:
    """Kontrole zdrowia: Dashboard podaje je jako liste albo slownik (zaleznie od wersji)."""
    kontrole = zdrowie.get("checks") or []
    if isinstance(kontrole, dict):
        kontrole = [dict(v, type=k) for k, v in kontrole.items() if isinstance(v, dict)]
    wynik = []
    for k in kontrole if isinstance(kontrole, list) else []:
        if not isinstance(k, dict):
            continue
        opis = _z(k, "summary.message", "message") or k.get("type") or ""
        waga = str(k.get("severity") or "").replace("HEALTH_", "")
        wynik.append(_tekst(f"{waga}: {opis}" if waga else opis, 255))
    return [w for w in wynik if w][:50]


def _fsid(dash: CephDashboard, minimal: dict) -> str | None:
    """fsid z kilku miejsc - zalezy od wersji Cepha, co Dashboard podaje."""
    kandydaci = [_z(minimal, "mon_status.monmap.fsid", "fsid")]
    if not any(kandydaci):
        dane = dash.get("/api/health/get_cluster_fsid", opcjonalne=True)
        kandydaci.append(dane if isinstance(dane, str) else _z(dane or {}, "fsid"))
    if not any(kandydaci):
        dane = dash.get("/api/monitor", opcjonalne=True) or {}
        kandydaci.append(_z(dane, "mon_status.monmap.fsid"))
    return next((str(f).lower() for f in kandydaci if f and _FSID.match(str(f).lower())), None)


def host(d: dict) -> dict:
    uslugi = [u for u in d.get("services") or [] if isinstance(u, dict)]
    role = sorted({str(u.get("type")) for u in uslugi if u.get("type")})
    return {
        "nazwa": _tekst(d.get("hostname")) or "",
        "adres": _tekst(d.get("addr"), 64),
        "role": role[:20],
        "osd": sum(1 for u in uslugi if u.get("type") == "osd"),
        "wersja": _wersja(d.get("ceph_version")),
        "stan": _tekst(d.get("status"), 32),
    }


def pula(d: dict) -> dict:
    aplikacje = d.get("application_metadata") or []
    if isinstance(aplikacje, dict):
        aplikacje = list(aplikacje)
    return {
        "nazwa": _tekst(d.get("pool_name")) or "",
        "typ": _tekst(d.get("type"), 32),
        "rozmiar": _liczba(d.get("size")),
        "min_rozmiar": _liczba(d.get("min_size")),
        "pg": _liczba(d.get("pg_num")),
        "aplikacje": [str(a) for a in aplikacje][:10],
        "zajete_bajty": _liczba(_z(d, "stats.bytes_used.latest", "stats.bytes_used")),
        "dostepne_bajty": _liczba(_z(d, "stats.max_avail.latest", "stats.max_avail")),
    }


def wykonaj(dash: CephDashboard, rodzaj: str) -> dict:
    """Test (logowanie + podsumowanie) albo pelny odczyt. Zwraca tresc wyniku."""
    start = time.monotonic()
    with dash:
        podsumowanie = dash.get("/api/summary") or {}
        wersja = _wersja(podsumowanie.get("version"))
        wynik = {"rodzaj": rodzaj, "ok": True, "wersja_pc": wersja}
        minimal = dash.get("/api/health/minimal") or {}
        zdrowie = minimal.get("health") or {}
        fsid = _fsid(dash, minimal)
        if not fsid:
            raise BladCeph("Ceph Dashboard nie podal fsid klastra - nie da sie go jednoznacznie wpisac do ewidencji")
        osd = [o for o in _z(minimal, "osd_map.osds") or [] if isinstance(o, dict)]
        mony = _z(minimal, "mon_status.monmap.mons") or []
        kworum = _z(minimal, "mon_status.quorum") or []
        klaster = {
            "ext_id": fsid,
            "wersja": wersja,
            "zdrowie": _tekst(zdrowie.get("status") or podsumowanie.get("health_status"), 32),
            "ostrzezenia": _ostrzezenia(zdrowie),
            "pojemnosc_bajty": _liczba(_z(minimal, "df.stats.total_bytes")),
            "zajete_bajty": _liczba(_z(minimal, "df.stats.total_used_raw_bytes", "df.stats.total_used_bytes")),
            "wolne_bajty": _liczba(_z(minimal, "df.stats.total_avail_bytes")),
            "liczba_osd": len(osd) if osd else None,
            "osd_up": sum(1 for o in osd if o.get("up")) if osd else None,
            "osd_in": sum(1 for o in osd if o.get("in")) if osd else None,
            "liczba_mon": len(mony) if isinstance(mony, list) and mony else None,
            "mon_kworum": len(kworum) if isinstance(kworum, list) and kworum else None,
            "liczba_hostow": _liczba(minimal.get("hosts")),
            "hosty": [],
            "pule": [],
        }
        if rodzaj == "odczyt":
            hosty = dash.get("/api/host", opcjonalne=True) or []
            if not isinstance(hosty, list):
                raise BladCeph("nieoczekiwany format odpowiedzi /api/host")
            klaster["hosty"] = [h for h in (host(x) for x in hosty[:MAKS_HOSTOW] if isinstance(x, dict))
                                if h["nazwa"]]
            if klaster["hosty"]:
                klaster["liczba_hostow"] = len(klaster["hosty"])
            pule = dash.get("/api/pool?stats=true") or []
            if not isinstance(pule, list):
                raise BladCeph("nieoczekiwany format odpowiedzi /api/pool")
            klaster["pule"] = [p for p in (pula(x) for x in pule[:MAKS_PUL] if isinstance(x, dict)) if p["nazwa"]]
        wynik["klastry"] = [klaster]
    wynik["czas_ms"] = int((time.monotonic() - start) * 1000)
    return wynik


# --- praca w petli monitora -------------------------------------------------

class CzytnikCeph(CzytnikNutanix):
    nazwa = "Ceph"
    sciezka_polityki = "/api/v1/agent/ceph-policy"
    sciezka_wyniku = "/api/v1/agent/ceph"

    def __init__(self, client, state, fabryka=None):
        super().__init__(client, state, fabryka or CephDashboard)

    def klient(self, polityka: dict):
        return self.fabryka(polityka["adres"], polityka["uzytkownik"], polityka["haslo"],
                            polityka.get("ca_pem", ""), bez_tls=polityka.get("bez_tls", False))

    @staticmethod
    def wykonaj(klient, rodzaj: str) -> dict:
        return wykonaj(klient, rodzaj)
