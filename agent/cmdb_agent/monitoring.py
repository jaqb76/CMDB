"""Monitorowanie ciaglosci uslug i certyfikatow - strona agenta.

**To agent sonduje, nie serwer.** Usluga zyje w sieci klienta, za NAT-em
i firewallem; serwer CMDB tej sieci zwykle nie widzi, a gdyby widzial sieci
wszystkich firm naraz, bylby jednym miejscem, z ktorego da sie zajrzec do
kazdej z nich. Agent juz tam stoi - i mierzy dokladnie to, co zobaczy
uzytkownik uslugi.

Modul jest samodzielny: chodzi na samej bibliotece standardowej, nie zaglada
do kolektorow ani do raportu inwentaryzacji i rozmawia ze swiatem przez dwie
rzeczy podane mu z zewnatrz - klienta HTTP i polityke. Dzieki temu da sie go
w przyszlosci dostarczyc jako osobny modul rozszerzen.

Dwa rytmy, ktore trzeba trzymac osobno:

* **sonda** idzie we wlasnym odstepie kazdego celu - "czy odpowiada" ma sens
  co minute, "do kiedy wazny certyfikat" raz na dobe;
* **raport** idzie co kwadrans i nie zawiera sond, tylko ich podsumowanie
  ("15 sond, 15 udanych") oraz to, czego brakowalo ("12:34:34 - 12:40:22").
  Doba monitorowania co minute to 96 wierszy zamiast 1440.

Wyjatkiem od kwadransa sa **zdarzenia**: poczatek i koniec potwierdzonej
awarii ida natychmiast, osobnym malym zadaniem. Redukcja ruchu dotyczy
potwierdzen, ze wszystko dziala - alarm, ktory czeka kwadrans, przestaje byc
alarmem.

Certyfikatu agent nie rozbiera: w bibliotece standardowej nie ma parsera
X.509. Liczy jego odcisk SHA-256 i wysyla CALY certyfikat wylacznie wtedy,
gdy odcisk rozni sie od znanego serwerowi. Rozbiera go serwer.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Ile znakow odpowiedzi HTTP wolno wczytac. Interesuje nas wylacznie linia
# statusu - reszta strony nie jest nam do niczego potrzebna i nie ma powodu
# wciagac jej do pamieci maszyny klienta.
LIMIT_ODPOWIEDZI = 4096

# Granice, ktorych nie przyjmiemy nawet z podpisanej polityki. Serwer sprawdza
# je u siebie, ale agent nie moze na tym polegac: to on wykonuje polaczenia
# i to jego maszyne obciazy odstep ustawiony na sekunde.
MIN_INTERWAL = 60
MAKS_INTERWAL = 86400
MIN_INTERWAL_CERT = 3600
MAKS_INTERWAL_CERT = 604800
MIN_LIMIT = 1
MAKS_LIMIT = 60
MAKS_CELOW = 500

PROTOKOLY = ("tcp", "tls", "http", "https")
PROTOKOLY_Z_TLS = ("tls", "https")
PROTOKOLY_Z_HTTP = ("http", "https")

# Co ile agent siega po polityke. Krocej niz odstep raportu, zeby cel dodany
# w panelu zaczal byc sprawdzany w rozsadnym czasie, a cel odebrany przestal.
ODSTEP_POLITYKI = 300


class BladCelu(RuntimeError):
    """Celu nie da sie sprawdzic - zly adres albo adres zabroniony."""


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


def _iso(chwila: datetime) -> str:
    return chwila.isoformat()


# --- adresy -----------------------------------------------------------------

def powod_odmowy(adres: str) -> str | None:
    """Dlaczego agent nie pojdzie pod ten adres. None znaczy "wolno".

    Cel wpisuje czlowiek w panelu, a polaczenie nawiazuje agent na maszynie
    klienta. Adres 169.254.169.254 znaczy w chmurze "usluga metadanych
    instancji" i agent bywa maszyna wirtualna u dostawcy - to jedyny adres,
    pod ktorym cel monitorowania moglby wyciagnac cos, czego nie powinien.
    """
    try:
        ip = ipaddress.ip_address(adres)
    except ValueError:
        return "nieczytelny adres"
    # Adres IPv4 zapisany jako IPv6 (::ffff:169.254.169.254) to ten sam adres -
    # sprawdzanie samej postaci zapisu przepuscilo by go bez pytania.
    zmapowany = getattr(ip, "ipv4_mapped", None)
    if zmapowany is not None:
        ip = zmapowany
    if ip.is_unspecified:
        return "adres nieokreslony"
    if ip.is_multicast:
        return "adres rozgloszeniowy"
    if ip.is_link_local:
        return "adres link-local - pod nim odpowiada usluga metadanych chmury"
    if ip.is_reserved:
        return "adres zarezerwowany"
    return None


def rozwiaz(host: str, port: int) -> tuple[int, tuple]:
    """Zwraca (rodzina, sockaddr) pierwszego dozwolonego adresu pod nazwa.

    Laczymy sie potem z TYM adresem, a nie ponownie z nazwa: inaczej miedzy
    sprawdzeniem a polaczeniem odpowiedz DNS moglaby sie zmienic i agent
    poszedlby pod adres, ktorego nikt nie sprawdzil.
    """
    try:
        wyniki = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BladCelu(f"nie moge rozwiazac nazwy: {exc.strerror or exc}") from exc
    if not wyniki:
        raise BladCelu("nazwa nie wskazuje na zaden adres")

    odmowy = []
    for rodzina, _typ, _proto, _kanon, sockaddr in wyniki:
        powod = powod_odmowy(sockaddr[0])
        if powod is None:
            return rodzina, sockaddr
        odmowy.append(f"{sockaddr[0]} - {powod}")
    raise BladCelu("adres jest zabroniony: " + "; ".join(dict.fromkeys(odmowy)))


# --- sonda ------------------------------------------------------------------

@dataclass
class Wynik:
    """Wynik jednej sondy. Nie dotyka ani dysku, ani sieci CMDB."""

    dostepna: bool
    czas_ms: int = 0
    kod: int | None = None
    blad: str | None = None
    adres: str | None = None
    # Czy uscisk dloni TLS doszedl do skutku. Nie wynika z samego certyfikatu:
    # uscisk moze sie udac, a certyfikat okazac sie nie do odczytania.
    tls_zestawione: bool = False
    odcisk: str | None = None
    pem: str | None = None
    cert_zaufany: bool | None = None
    cert_blad: str | None = None


def _kontekst(weryfikuj: bool) -> ssl.SSLContext:
    kontekst = ssl.create_default_context()
    if not weryfikuj:
        # Certyfikat zdejmujemy z bajtow, wiec brak weryfikacji nie oznacza
        # tu zaufania - oznacza tylko tyle, ze uscisk dloni ma sie udac takze
        # z certyfikatem wlasnego urzedu firmy, ktorego agent nie zna.
        kontekst.check_hostname = False
        kontekst.verify_mode = ssl.CERT_NONE
    return kontekst


def _linia_statusu(gniazdo, cel: dict) -> int:
    """Wysyla zadanie HTTP i zwraca kod odpowiedzi.

    Piszemy to recznie zamiast uzyc klienta HTTP z biblioteki standardowej:
    klient poszedlby za przekierowaniem, a przekierowanie prowadzi pod adres,
    ktorego nikt nie sprawdzil. Tresci odpowiedzi nie czytamy - pytanie brzmi
    "czy aplikacja odpowiada", a nie "co odpowiada".
    """
    naglowek = cel.get("nazwa_tls") or cel["host"]
    if ":" in naglowek and not naglowek.startswith("["):
        naglowek = f"[{naglowek}]"
    domyslny = 443 if cel["protokol"] == "https" else 80
    if cel["port"] != domyslny:
        naglowek = f"{naglowek}:{cel['port']}"

    zadanie = (
        f"GET {cel['sciezka']} HTTP/1.1\r\n"
        f"Host: {naglowek}\r\n"
        "User-Agent: CMDB-agent/monitor\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n"
    )
    gniazdo.sendall(zadanie.encode("ascii"))

    bufor = b""
    while b"\r\n" not in bufor and len(bufor) < LIMIT_ODPOWIEDZI:
        kawalek = gniazdo.recv(LIMIT_ODPOWIEDZI - len(bufor))
        if not kawalek:
            break
        bufor += kawalek
    linia = bufor.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    czesci = linia.split(" ", 2)
    if len(czesci) < 2 or not czesci[0].upper().startswith("HTTP/") or not czesci[1].isdigit():
        raise BladCelu(f"odpowiedz nie wyglada na HTTP: {linia[:120] or '(pusta)'}")
    return int(czesci[1])


def _po_tls(gniazdo, cel: dict, wynik: Wynik, rodzina: int, sockaddr: tuple, zdejmij_cert: bool):
    """Zestawia TLS i zapisuje w wyniku certyfikat oraz ocene lancucha."""
    serwer = cel.get("nazwa_tls") or cel["host"]
    weryfikuj = bool(cel.get("weryfikuj_lancuch"))
    try:
        tunel = _kontekst(weryfikuj).wrap_socket(gniazdo, server_hostname=serwer)
        wynik.cert_zaufany = True if weryfikuj else None
    except ssl.SSLCertVerificationError as exc:
        # Niezaufany lancuch nie konczy sondy: certyfikat nadal chcemy
        # zobaczyc, bo to on odpowiada na pytanie "do kiedy wazny". Powtorka
        # bez weryfikacji jest jedynym sposobem, zeby go zdjac - i kosztuje
        # drugie polaczenie tylko wtedy, gdy naprawde cos jest nie tak.
        wynik.cert_zaufany = False
        wynik.cert_blad = str(getattr(exc, "verify_message", None) or exc)[:500]
        gniazdo.close()
        # Ten sam adres co za pierwszym razem, a nie ponowne rozwiazanie nazwy:
        # opisujemy certyfikat serwera, ktory wlasnie odmowil.
        gniazdo = socket.socket(rodzina, socket.SOCK_STREAM)
        gniazdo.settimeout(cel["limit_sekund"])
        gniazdo.connect(sockaddr)
        tunel = _kontekst(False).wrap_socket(gniazdo, server_hostname=serwer)

    wynik.tls_zestawione = True
    der = tunel.getpeercert(binary_form=True)
    if not der:
        wynik.cert_blad = wynik.cert_blad or "serwer nie przedstawil certyfikatu"
        return tunel
    # Odcisk liczymy zawsze - to po nim poznajemy, czy jest sens cokolwiek
    # wysylac. Sam certyfikat dokladamy wylacznie przy zmianie.
    wynik.odcisk = hashlib.sha256(der).hexdigest()
    if zdejmij_cert:
        wynik.pem = ssl.DER_cert_to_PEM_cert(der)
    return tunel


def sonduj(cel: dict, zdejmij_cert: bool = False) -> Wynik:
    """Jedno sprawdzenie celu. Bez dysku, bez stanu, bez polaczenia z CMDB."""
    wynik = Wynik(dostepna=False)
    start = time.monotonic()
    gniazdo = None
    try:
        rodzina, sockaddr = rozwiaz(cel["host"], cel["port"])
        wynik.adres = sockaddr[0]
        gniazdo = socket.socket(rodzina, socket.SOCK_STREAM)
        gniazdo.settimeout(cel["limit_sekund"])
        gniazdo.connect(sockaddr)
        if cel["protokol"] in PROTOKOLY_Z_TLS:
            gniazdo = _po_tls(gniazdo, cel, wynik, rodzina, sockaddr, zdejmij_cert)
        if cel["protokol"] in PROTOKOLY_Z_HTTP:
            wynik.kod = _linia_statusu(gniazdo, cel)
            oczekiwany = cel.get("oczekiwany_kod") or 0
            if oczekiwany and wynik.kod != oczekiwany:
                raise BladCelu(f"kod odpowiedzi {wynik.kod}, oczekiwano {oczekiwany}")
            if not oczekiwany and wynik.kod >= 400:
                raise BladCelu(f"kod odpowiedzi {wynik.kod}")
        wynik.dostepna = True
    except BladCelu as exc:
        wynik.blad = str(exc)[:500]
    except ssl.SSLError as exc:
        wynik.blad = f"blad TLS: {exc.reason or exc}"[:500]
    except socket.timeout:
        wynik.blad = f"brak odpowiedzi w {cel['limit_sekund']} s"
    except OSError as exc:
        # Komunikat systemu ("Connection refused") mowi wiecej niz nazwa klasy.
        wynik.blad = f"{exc.strerror or exc}"[:500]
    except Exception as exc:  # zaden cel nie moze wywrocic calej petli
        log.exception("sonda celu %s zakonczyla sie bledem", cel.get("id"))
        wynik.blad = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if gniazdo is not None:
            try:
                gniazdo.close()
            except OSError:
                pass
    wynik.czas_ms = int((time.monotonic() - start) * 1000)
    return wynik


# --- stan celu miedzy sondami -----------------------------------------------

@dataclass
class StanCelu:
    """Co agent pamieta o jednym celu miedzy sondami.

    Tu powstaje redukcja ruchu: zamiast listy sond trzymamy liczniki i jedna
    otwarta przerwe. Wysylka zabiera to, co sie uzbieralo, i zeruje liczniki.
    """

    id: str
    # Kiedy nastepna sonda i kiedy nastepne zdjecie certyfikatu. Osobno, bo
    # to dwa rozne pytania o dwoch roznych rytmach.
    nastepna_sonda: float = 0.0
    nastepny_certyfikat: float = 0.0

    okno_od: datetime = field(default_factory=_teraz)
    sond: int = 0
    udanych: int = 0
    kolejne_bledy: int = 0

    # Przerwy zamkniete w tym okresie i ta, ktora trwa nadal.
    zamkniete: list = field(default_factory=list)
    otwarta: dict | None = None

    ostatnia_sonda: dict | None = None
    tls: dict | None = None
    znany_odcisk: str = ""

    def zapisz(self, wynik: Wynik, liczba_prob: int, teraz: datetime) -> str | None:
        """Wciaga wynik sondy. Zwraca "awaria"/"powrot" przy zmianie, inaczej None.

        Zwrocona zmiana jest powodem, dla ktorego raport pojdzie natychmiast
        zamiast czekac na koniec okresu.
        """
        self.sond += 1
        self.ostatnia_sonda = {
            "kiedy": _iso(teraz),
            "dostepna": wynik.dostepna,
            "czas_ms": wynik.czas_ms,
            "kod": wynik.kod,
            "adres": wynik.adres,
            "blad": wynik.blad,
            "potwierdzona_awaria": False,
        }
        if wynik.tls_zestawione and wynik.odcisk:
            self.tls = {"odcisk": wynik.odcisk, "zaufany": wynik.cert_zaufany,
                        "blad": wynik.cert_blad}
            # Caly certyfikat tylko przy zmianie - certyfikat wazny rok nie ma
            # po co jechac przez siec co dobe.
            if wynik.pem and wynik.odcisk != self.znany_odcisk:
                self.tls["pem"] = wynik.pem

        if wynik.dostepna:
            self.udanych += 1
            self.kolejne_bledy = 0
            if self.otwarta is not None:
                # Koniec przerwy to chwila OSTATNIEJ nieudanej sondy, a nie ta
                # udana: miedzy nimi uslugi nie bylo, a po niej juz byla.
                self.otwarta["do"] = self.otwarta.get("ostatni_blad_o") or _iso(teraz)
                self.otwarta.pop("ostatni_blad_o", None)
                self.zamkniete.append(self.otwarta)
                self.otwarta = None
                return "powrot"
            return None

        self.kolejne_bledy += 1
        potwierdzona = self.kolejne_bledy >= liczba_prob
        self.ostatnia_sonda["potwierdzona_awaria"] = potwierdzona
        if self.otwarta is None:
            # Przerwa zaczyna sie przy PIERWSZYM bledzie, nie przy
            # potwierdzeniu - inaczej z sesnu przerwy zniknelaby ta jej czesc,
            # w ktorej usluga juz nie dzialala, a agent jeszcze nie mial pewnosci.
            self.otwarta = {"od": _iso(teraz), "blad": wynik.blad, "sond": 0}
        self.otwarta["sond"] += 1
        self.otwarta["blad"] = wynik.blad
        self.otwarta["ostatni_blad_o"] = _iso(teraz)
        # Zdarzeniem jest dopiero potwierdzenie: jedna zgubiona odpowiedz
        # zdarza sie w kazdej sieci, a powiadomienie o niej uczy ludzi
        # ignorowania powiadomien.
        return "awaria" if self.kolejne_bledy == liczba_prob else None

    def do_wyslania(self, teraz: datetime) -> dict | None:
        """Sklada wpis raportu i zeruje liczniki. None, gdy nie ma o czym mowic."""
        if not self.sond and not self.zamkniete and self.otwarta is None:
            return None
        przerwy = list(self.zamkniete)
        if self.otwarta is not None:
            otwarta = {k: v for k, v in self.otwarta.items() if k != "ostatni_blad_o"}
            otwarta["do"] = None
            przerwy.append(otwarta)
        wpis = {
            "id": self.id,
            "okno": {"od": _iso(self.okno_od), "do": _iso(teraz),
                     "sond": self.sond, "udanych": self.udanych},
            "przerwy": przerwy,
            "ostatnia_sonda": self.ostatnia_sonda,
            "tls": self.tls,
        }
        self.okno_od = teraz
        self.sond = self.udanych = 0
        self.zamkniete = []
        self.tls = None
        return wpis

    def potwierdz_wyslanie(self, wpis: dict) -> None:
        """Zapamietuje odcisk, ktory serwer juz zna - po udanej wysylce."""
        tls = wpis.get("tls")
        if tls and tls.get("pem"):
            self.znany_odcisk = tls["odcisk"]


# --- polityka ---------------------------------------------------------------

def sprawdz_cel(surowy: dict) -> dict:
    """Sprawdza jeden cel z polityki. Rzuca ValueError.

    Polityka przychodzi z serwera po uwierzytelnionym HTTPS, ale i tak ja
    sprawdzamy: to agent wykonuje polaczenia i to jego maszyne obciazy
    odstep ustawiony na sekunde albo tysiac celow naraz.
    """
    cel = {
        "id": str(surowy["id"])[:36],
        "host": str(surowy["host"]).strip()[:255],
        "port": int(surowy["port"]),
        "protokol": str(surowy["protokol"]),
        "sciezka": str(surowy.get("sciezka") or "/")[:500],
        "oczekiwany_kod": int(surowy.get("oczekiwany_kod") or 0),
        "nazwa_tls": str(surowy.get("nazwa_tls") or "")[:255],
        "weryfikuj_lancuch": bool(surowy.get("weryfikuj_lancuch")),
        "interwal_sekund": int(surowy["interwal_sekund"]),
        "interwal_certyfikatu": int(surowy.get("interwal_certyfikatu") or 86400),
        "limit_sekund": int(surowy["limit_sekund"]),
        "liczba_prob": max(1, min(10, int(surowy.get("liczba_prob") or 2))),
        "znany_odcisk": str(surowy.get("znany_odcisk") or "")[:95],
        "wymus": bool(surowy.get("wymus")),
    }
    if not cel["id"] or not cel["host"]:
        raise ValueError("cel bez identyfikatora albo adresu")
    if cel["protokol"] not in PROTOKOLY:
        raise ValueError(f"nieznany protokol: {cel['protokol']}")
    if not 1 <= cel["port"] <= 65535:
        raise ValueError("port poza zakresem")
    if not MIN_INTERWAL <= cel["interwal_sekund"] <= MAKS_INTERWAL:
        raise ValueError("odstep sond poza dozwolonym zakresem")
    if not MIN_INTERWAL_CERT <= cel["interwal_certyfikatu"] <= MAKS_INTERWAL_CERT:
        raise ValueError("odstep sprawdzania certyfikatu poza dozwolonym zakresem")
    if not MIN_LIMIT <= cel["limit_sekund"] <= MAKS_LIMIT:
        raise ValueError("limit czasu poza dozwolonym zakresem")
    if cel["limit_sekund"] >= cel["interwal_sekund"]:
        raise ValueError("limit czasu nie moze byc dluzszy niz odstep sond")
    if not cel["sciezka"].startswith("/") or any(z in cel["sciezka"] for z in "\r\n \t"):
        raise ValueError("niepoprawna sciezka HTTP")
    return cel


def pobierz_polityke(client, state) -> dict:
    """Swieza, zwiazana z jednorazowa wartoscia polityka monitorowania.

    Ta sama zasada co przy skanowaniu sieci: zapisany stan NIGDY nie jest
    upowaznieniem. Bez tego odebranie celu w panelu nie odbieraloby go
    naprawde - agent chodzilby dalej po ostatniej znanej liscie.
    """
    if client is None or not state.is_enrolled:
        raise ValueError("brak uwierzytelnionego polaczenia z CMDB")
    nonce = secrets.token_hex(16)
    payload = client.get("/api/v1/agent/monitoring-policy?nonce=" + nonce, state.agent_token)
    if (not isinstance(payload, dict) or payload.get("protocol") != 1 or
            payload.get("nonce") != nonce or payload.get("asset_id") != state.asset_id or
            payload.get("machine_id") != state.machine_id):
        raise ValueError("niezgodna odpowiedz polityki monitorowania")
    wygasa = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    if wygasa.tzinfo is None or not 0 < (wygasa - _teraz()).total_seconds() <= 65:
        raise ValueError("wygasla lub niepoprawna waznosc polityki monitorowania")

    polityka = payload["policy"]
    if not isinstance(polityka, dict) or not isinstance(polityka.get("cele"), list):
        raise ValueError("niepoprawny format polityki monitorowania")
    if len(polityka["cele"]) > MAKS_CELOW:
        raise ValueError("polityka zawiera zbyt wiele celow")

    cele = []
    for surowy in polityka["cele"]:
        try:
            cele.append(sprawdz_cel(surowy))
        except (ValueError, KeyError, TypeError) as exc:
            # Jeden bledny cel nie moze wylaczyc monitorowania pozostalych.
            log.warning("pomijam cel z polityki: %s", exc)
    odstep = int(polityka.get("interwal_raportu") or 900)
    return {
        "enabled": bool(polityka.get("enabled")) and bool(cele),
        "interwal_raportu": max(60, min(3600, odstep)),
        "cele": cele,
    }


# --- petla ------------------------------------------------------------------

class Monitor:
    """Ciagle monitorowanie celow przypisanych tej maszynie.

    Jedna instancja na proces. Nie wspoldzieli niczego z inwentaryzacja:
    ta chodzi raz na kilka godzin i konczy sie, a ta petla musi zyc miedzy
    jej przebiegami, bo inaczej nie ma mowy o sondzie co minute.
    """

    def __init__(self, client, state, sciezka_stanu=None):
        self.client = client
        self.state = state
        self.sciezka_stanu = sciezka_stanu
        self.cele: dict[str, dict] = {}
        self.stany: dict[str, StanCelu] = {}
        self.interwal_raportu = 900
        self.nastepna_polityka = 0.0
        self.nastepny_raport = 0.0
        self.ostatni_blad = ""
        self.zatrzymaj = threading.Event()
        self._wczytaj_stan()

    # --- trwalosc miedzy uruchomieniami ---

    def _wczytaj_stan(self) -> None:
        """Wczytuje otwarte przerwy i znane odciski z poprzedniego uruchomienia.

        Bez tego restart maszyny gubilby trwajaca awarie (przerwa zaczelaby
        sie od nowa) i wysylalby ponownie wszystkie certyfikaty.
        """
        if self.sciezka_stanu is None:
            return
        try:
            zapis = json.loads(self.sciezka_stanu.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, ValueError, OSError):
            return
        for wpis in zapis.get("cele") or []:
            try:
                stan = StanCelu(id=str(wpis["id"]))
                stan.znany_odcisk = str(wpis.get("znany_odcisk") or "")
                stan.otwarta = wpis.get("otwarta")
                stan.kolejne_bledy = int(wpis.get("kolejne_bledy") or 0)
                self.stany[stan.id] = stan
            except (KeyError, TypeError, ValueError):
                continue

    def _zapisz_stan(self) -> None:
        if self.sciezka_stanu is None:
            return
        zapis = {"cele": [
            {"id": stan.id, "znany_odcisk": stan.znany_odcisk,
             "otwarta": stan.otwarta, "kolejne_bledy": stan.kolejne_bledy}
            for stan in self.stany.values()
        ]}
        try:
            self.sciezka_stanu.parent.mkdir(parents=True, exist_ok=True)
            tymczasowy = self.sciezka_stanu.with_suffix(".tmp")
            tymczasowy.write_text(json.dumps(zapis, ensure_ascii=False), encoding="utf-8")
            tymczasowy.replace(self.sciezka_stanu)
        except OSError as exc:
            log.debug("nie moge zapisac stanu monitorowania: %s", exc)

    # --- polityka ---

    def odswiez_polityke(self) -> bool:
        try:
            polityka = pobierz_polityke(self.client, self.state)
        except Exception as exc:
            # Nieudane pobranie nie wylacza monitorowania i NIE przedluza
            # waznosci poprzedniej polityki - po prostu sondujemy dalej to,
            # co juz mamy, i probujemy ponownie za chwile.
            self.ostatni_blad = str(exc)[:500]
            log.warning("polityka monitorowania niedostepna: %s", exc)
            return False

        self.ostatni_blad = ""
        self.interwal_raportu = polityka["interwal_raportu"]
        nowe = {cel["id"]: cel for cel in polityka["cele"]} if polityka["enabled"] else {}

        # Cel odebrany w panelu przestaje byc sondowany od razu - razem ze
        # swoim stanem, zeby jego ponowne dodanie zaczynalo od czystej karty.
        for zniknięty in set(self.stany) - set(nowe):
            self.stany.pop(zniknięty, None)

        teraz = time.monotonic()
        for identyfikator, cel in nowe.items():
            stan = self.stany.get(identyfikator)
            if stan is None:
                stan = StanCelu(id=identyfikator)
                self.stany[identyfikator] = stan
            # Odcisk znany SERWEROWI, nie agentowi: gdy serwer stracil zapis,
            # agent musi przyslac certyfikat ponownie, choc sam go zna.
            stan.znany_odcisk = cel["znany_odcisk"]
            poprzedni = self.cele.get(identyfikator)
            if cel["wymus"] or poprzedni is None or _rytm_zmieniony(poprzedni, cel):
                stan.nastepna_sonda = teraz
                if cel["protokol"] in PROTOKOLY_Z_TLS:
                    stan.nastepny_certyfikat = teraz
        self.cele = nowe
        return True

    # --- sondowanie ---

    def sonduj_zalegle(self) -> list[str]:
        """Sonduje cele, ktorych termin minal. Zwraca powody natychmiastowej wysylki."""
        teraz = time.monotonic()
        chwila = _teraz()
        zdarzenia = []
        for identyfikator, cel in self.cele.items():
            stan = self.stany.get(identyfikator)
            if stan is None or teraz < stan.nastepna_sonda:
                continue
            zdejmij = (cel["protokol"] in PROTOKOLY_Z_TLS
                       and teraz >= stan.nastepny_certyfikat)
            wynik = sonduj(cel, zdejmij_cert=zdejmij)
            zmiana = stan.zapisz(wynik, cel["liczba_prob"], chwila)
            stan.nastepna_sonda = teraz + cel["interwal_sekund"]
            if zdejmij:
                stan.nastepny_certyfikat = teraz + cel["interwal_certyfikatu"]
            if zmiana:
                zdarzenia.append(f"{identyfikator}:{zmiana}")
        return zdarzenia

    # --- raportowanie ---

    def wyslij(self, powod: str = "okresowy") -> bool:
        """Wysyla podsumowanie i przerwy. Zwraca True, gdy serwer przyjal."""
        teraz = _teraz()
        wpisy = []
        for identyfikator in self.cele:
            stan = self.stany.get(identyfikator)
            if stan is None:
                continue
            wpis = stan.do_wyslania(teraz)
            if wpis is not None:
                wpisy.append((stan, wpis))
        if not wpisy:
            return True

        raport = {"protocol": 1, "wyslano": _iso(teraz), "powod": powod,
                  "cele": [wpis for _, wpis in wpisy]}
        try:
            odpowiedz = self.client.post("/api/v1/agent/monitoring",
                                         self.state.agent_token, raport)
        except Exception as exc:
            # Wysylka sie nie udala - liczniki juz wyzerowalismy, wiec ten
            # okres przepadl. Swiadomie: alternatywa jest kolejka rosnaca bez
            # ograniczen na maszynie klienta, ktora przy dluzszej awarii
            # laczy zjadlaby dysk. Przerwa OTWARTA nie przepada, bo zyje
            # w stanie celu i pojdzie z nastepnym raportem.
            self.ostatni_blad = str(exc)[:500]
            log.warning("raport monitorowania nie poszedl: %s", exc)
            return False

        self.ostatni_blad = ""
        for stan, wpis in wpisy:
            stan.potwierdz_wyslanie(wpis)
        if isinstance(odpowiedz, dict) and odpowiedz.get("interwal_raportu"):
            self.interwal_raportu = max(60, min(3600, int(odpowiedz["interwal_raportu"])))
        self._zapisz_stan()
        return True

    # --- petla ---

    def krok(self) -> None:
        """Jeden obrot petli. Wydzielony, zeby dalo sie go przetestowac."""
        teraz = time.monotonic()
        if teraz >= self.nastepna_polityka:
            self.odswiez_polityke()
            self.nastepna_polityka = teraz + ODSTEP_POLITYKI
        zdarzenia = self.sonduj_zalegle()
        if zdarzenia:
            # Poczatek i koniec potwierdzonej awarii ida natychmiast. Redukcja
            # ruchu dotyczy potwierdzen, ze wszystko dziala - alarm, ktory
            # czeka kwadrans, przestaje byc alarmem.
            log.info("monitorowanie: zdarzenia %s", ", ".join(zdarzenia))
            self.wyslij(powod="zdarzenie")
            self.nastepny_raport = time.monotonic() + self.interwal_raportu
        elif time.monotonic() >= self.nastepny_raport:
            self.wyslij()
            self.nastepny_raport = time.monotonic() + self.interwal_raportu

    def petla(self, odstep_petli: int = 5) -> None:
        """Chodzi do zatrzymania. Blad jednego obrotu nie konczy monitorowania."""
        log.info("monitorowanie wystartowalo")
        self.nastepny_raport = time.monotonic() + self.interwal_raportu
        while not self.zatrzymaj.is_set():
            try:
                self.krok()
            except Exception as exc:
                # Petla monitorowania nie moze umrzec na bledzie jednego
                # obrotu: to jedyne zrodlo wiedzy o dostepnosci, a jej cisza
                # wyglada tak samo jak sprawna usluga.
                log.exception("blad obrotu monitorowania: %s", exc)
            self.zatrzymaj.wait(odstep_petli)
        # Ostatnia wysylka przy zatrzymaniu: inaczej kwadrans pomiarow
        # przepadalby przy kazdym restarcie uslugi.
        try:
            self.wyslij(powod="okresowy")
        except Exception:
            pass
        self._zapisz_stan()
        log.info("monitorowanie zatrzymane")

    def status(self) -> dict:
        """Skrot dla okna statusu i polecenia diagnostycznego."""
        return {
            "cele": len(self.cele),
            "sondowane": sum(1 for s in self.stany.values() if s.sond),
            "otwarte_przerwy": sum(1 for s in self.stany.values() if s.otwarta),
            "interwal_raportu": self.interwal_raportu,
            "ostatni_blad": self.ostatni_blad,
        }


def _rytm_zmieniony(poprzedni: dict, nowy: dict) -> bool:
    """Czy zmienilo sie cos, co kaze sondowac od razu zamiast czekac."""
    return any(poprzedni.get(pole) != nowy.get(pole) for pole in
               ("host", "port", "protokol", "sciezka", "nazwa_tls", "interwal_sekund"))
