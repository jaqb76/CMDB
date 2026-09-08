"""Monitorowanie ciaglosci uslug i waznosci certyfikatow SSL.

Trzy rzeczy, ktore ksztaltuja ten modul.

**Sprawdzenie wychodzi z serwera CMDB, nie z agenta.** Agent widzi maszyne od
srodka i nie wie, czy usluga odpowiada komukolwiek innemu. Certyfikat, ktory
wygasl, wyglada z maszyny tak samo jak wazny - problem widac dopiero z drugiej
strony polaczenia. Dlatego cel podaje sie adresem albo nazwa, a probe nawiazuje
serwer: mierzymy to, co zobaczy uzytkownik uslugi.

**Dostepnosc i certyfikat oceniamy osobno.** To dwie niezalezne awarie:
usluga potrafi odpowiadac z certyfikatem wygasajacym jutro i potrafi milczec
z certyfikatem waznym rok. Zlanie ich w jeden stan konczy sie tym, ze jedna
sprawa przykrywa druga - a powiadomienia o nich ida do kogo innego i w innym
czasie. Widoczny stan celu to gorszy z dwoch skladowych.

**Cel wpisuje czlowiek, polaczenie nawiazuje serwer.** To znaczy, ze zle
wpisany cel jest zadaniem sieciowym wykonanym przez serwer - stad lista
adresow, pod ktore odmawiamy pojsc (petla zwrotna, link-local z usluga
metadanych chmury). Sprawdzamy adres PO rozwiazaniu nazwy i laczymy sie
dokladnie z tym, co sprawdzilismy, zeby miedzy sprawdzeniem a polaczeniem
nie dalo sie podmienic odpowiedzi DNS.

Czego to NIE jest: systemu monitorowania z prawdziwego zdarzenia. Nie ma tu
zaleznosci miedzy celami, okien serwisowych ani eskalacji. Odpowiadamy na dwa
pytania - "czy odpowiada" i "do kiedy wazny certyfikat" - i powiadamiamy, gdy
odpowiedz sie zmieni.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, literal_column, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    PROTOKOLY_MONITORA,
    PROTOKOLY_Z_HTTP,
    PROTOKOLY_Z_TLS,
    STAN_AWARIA,
    STAN_NIEZNANY,
    STAN_OK,
    STAN_OSTRZEZENIE,
    WAGA_STANU,
    MonitorUslugi,
    PomiarMonitora,
    Tenant,
    as_utc,
    utcnow,
)

log = logging.getLogger(__name__)

# Ile znakow tresci odpowiedzi wolno wczytac przy sprawdzeniu HTTP. Interesuje
# nas wylacznie linia statusu - reszta strony nie jest nam do niczego potrzebna
# i nie ma powodu wciagac jej do pamieci serwera.
LIMIT_ODPOWIEDZI = 4096

# Granice ustawien celu. Nie sa ozdoba: odstep krotszy niz minuta zamienia
# monitorowanie w generator ruchu, a limit czasu dluzszy niz minuta potrafi
# zajac watek na tyle dlugo, ze reszta celow nie zdazy sie sprawdzic.
MIN_INTERWAL = 60
MAKS_INTERWAL = 86400
MIN_LIMIT = 1
MAKS_LIMIT = 60
MIN_PROB = 1
MAKS_PROB = 10
MAKS_PROG_DNI = 365


class BladCelu(RuntimeError):
    """Celu nie da sie sprawdzic - zly adres albo adres zabroniony."""


@dataclass(frozen=True)
class Certyfikat:
    """Certyfikat zdjety z polaczenia, sprowadzony do tego, co pokazujemy."""

    podmiot: str
    wystawca: str
    wazny_od: datetime | None
    wazny_do: datetime | None
    odcisk: str
    nazwy: list[str] = field(default_factory=list)


@dataclass
class Wynik:
    """Wynik jednego sprawdzenia. Nie dotyka bazy - liczy sie w watku."""

    dostepna: bool
    czas_ms: int | None = None
    kod: int | None = None
    blad: str | None = None
    adres: str | None = None
    # Czy uscisk dloni TLS doszedl do skutku. Nie wynika z samego certyfikatu:
    # uscisk moze sie udac, a certyfikat okazac sie nie do odczytania - i na
    # odwrot nie moze, wiec to osobna informacja.
    tls_zestawione: bool = False
    certyfikat: Certyfikat | None = None
    cert_zaufany: bool | None = None
    cert_blad: str | None = None


@dataclass(frozen=True)
class Cel:
    """Kopia ustawien celu przekazywana do watku.

    Watki sprawdzajace nie dostaja obiektu ORM: jest on zwiazany z sesja,
    a sesja SQLAlchemy nie jest bezpieczna w watkach. Watek dostaje wiec
    zamrozona kopie ustawien, a wynik zapisuje watek glowny.
    """

    id: str
    tenant_id: str
    nazwa: str
    host: str
    port: int
    protokol: str
    sciezka: str
    oczekiwany_kod: int
    nazwa_tls: str | None
    weryfikuj_lancuch: bool
    limit_sekund: int


def cel_z_monitora(monitor: MonitorUslugi) -> Cel:
    return Cel(
        id=monitor.id,
        tenant_id=monitor.tenant_id,
        nazwa=monitor.nazwa,
        host=monitor.host,
        port=monitor.port,
        protokol=monitor.protokol,
        sciezka=monitor.sciezka or "/",
        oczekiwany_kod=monitor.oczekiwany_kod or 0,
        nazwa_tls=monitor.nazwa_tls,
        weryfikuj_lancuch=monitor.weryfikuj_lancuch,
        limit_sekund=monitor.limit_sekund,
    )


# --- adresy -----------------------------------------------------------------

def powod_odmowy(adres: str) -> str | None:
    """Dlaczego serwer nie pojdzie pod ten adres. None znaczy "wolno".

    Cel wpisuje administrator firmy, a polaczenie nawiazuje serwer CMDB
    wspolny dla wszystkich firm. Adres 169.254.169.254 znaczy w chmurze
    "usluga metadanych instancji", a 127.0.0.1 znaczy "sam serwer" - i ani
    jedno, ani drugie nie jest tym, co administrator mial na mysli, wpisujac
    adres swojej uslugi.
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
    if ip.is_loopback and not get_settings().monitoring_allow_loopback:
        return "adres petli zwrotnej wskazuje na sam serwer CMDB"
    if ip.is_reserved:
        return "adres zarezerwowany"
    return None


def rozwiaz(host: str, port: int) -> tuple[int, tuple]:
    """Zwraca (rodzina, sockaddr) pierwszego dozwolonego adresu pod nazwa.

    Laczymy sie potem z TYM adresem, a nie ponownie z nazwa: inaczej miedzy
    sprawdzeniem a polaczeniem odpowiedz DNS moglaby sie zmienic i serwer
    poszedlby pod adres, ktorego nikt nie sprawdzil.
    """
    try:
        wyniki = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BladCelu(f"nie moge rozwiazac nazwy: {exc.strerror or exc}") from exc
    if not wyniki:
        raise BladCelu("nazwa nie wskazuje na zaden adres")

    odmowy: list[str] = []
    for rodzina, _typ, _proto, _kanon, sockaddr in wyniki:
        powod = powod_odmowy(sockaddr[0])
        if powod is None:
            return rodzina, sockaddr
        odmowy.append(f"{sockaddr[0]} - {powod}")
    raise BladCelu("adres jest zabroniony: " + "; ".join(dict.fromkeys(odmowy)))


# --- certyfikat -------------------------------------------------------------

def _nazwa_z_x509(nazwa) -> str:
    """Czytelny opis podmiotu: CN, a gdy go nie ma - cala nazwa wyrozniona."""
    from cryptography.x509.oid import NameOID

    wspolne = nazwa.get_attributes_for_oid(NameOID.COMMON_NAME)
    if wspolne:
        return str(wspolne[0].value)
    organizacja = nazwa.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    if organizacja:
        return str(organizacja[0].value)
    return nazwa.rfc4514_string()


def _chwila(certyfikat, nowa: str, stara: str) -> datetime | None:
    """Data z certyfikatu jako chwila w UTC.

    cryptography od wersji 42 podaje daty z oznaczona strefa (``*_utc``)
    i ostrzega przy starych polach. Starsze wydania maja tylko pola bez
    strefy - czytamy wiec nowe, a gdy ich nie ma, dokladamy UTC recznie.
    """
    wartosc = getattr(certyfikat, nowa, None)
    if wartosc is None:
        wartosc = getattr(certyfikat, stara, None)
    if wartosc is None:
        return None
    return wartosc if wartosc.tzinfo else wartosc.replace(tzinfo=timezone.utc)


def opisz_certyfikat(der: bytes) -> Certyfikat:
    """Rozbiera certyfikat w postaci binarnej (DER).

    Czytamy go z surowych bajtow, a nie ze slownika ``getpeercert()``:
    tamten jest pusty, gdy polaczenie zestawiono bez weryfikacji lancucha -
    czyli dokladnie wtedy, gdy certyfikat interesuje nas najbardziej.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    certyfikat = x509.load_der_x509_certificate(der)
    nazwy: list[str] = []
    try:
        rozszerzenie = certyfikat.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        nazwy = [str(n) for n in rozszerzenie.value.get_values_for_type(x509.DNSName)]
        nazwy += [str(n) for n in rozszerzenie.value.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return Certyfikat(
        podmiot=_nazwa_z_x509(certyfikat.subject)[:500],
        wystawca=_nazwa_z_x509(certyfikat.issuer)[:500],
        wazny_od=_chwila(certyfikat, "not_valid_before_utc", "not_valid_before"),
        wazny_do=_chwila(certyfikat, "not_valid_after_utc", "not_valid_after"),
        odcisk=certyfikat.fingerprint(hashes.SHA256()).hex(),
        nazwy=nazwy[:50],
    )


def _kontekst(weryfikuj: bool) -> ssl.SSLContext:
    kontekst = ssl.create_default_context()
    if not weryfikuj:
        # Certyfikat zdejmujemy z bajtow, wiec brak weryfikacji nie oznacza
        # tu zaufania - oznacza tylko tyle, ze uscisk dloni ma sie udac takze
        # z certyfikatem wlasnego urzedu firmy, ktorego serwer CMDB nie zna.
        kontekst.check_hostname = False
        kontekst.verify_mode = ssl.CERT_NONE
    return kontekst


# --- sprawdzenie ------------------------------------------------------------

def _linia_statusu(gniazdo: socket.socket, cel: Cel) -> int:
    """Wysyla zadanie HTTP i zwraca kod odpowiedzi.

    Piszemy to recznie zamiast uzyc klienta HTTP z biblioteki standardowej:
    klient poszedlby za przekierowaniem, a przekierowanie prowadzi pod adres,
    ktorego nikt nie sprawdzil. Tresci odpowiedzi nie czytamy - pytanie brzmi
    "czy aplikacja odpowiada", a nie "co odpowiada".
    """
    naglowek_hosta = cel.nazwa_tls or cel.host
    if ":" in naglowek_hosta and not naglowek_hosta.startswith("["):
        naglowek_hosta = f"[{naglowek_hosta}]"
    domyslny = 443 if cel.protokol == "https" else 80
    if cel.port != domyslny:
        naglowek_hosta = f"{naglowek_hosta}:{cel.port}"

    zadanie = (
        f"GET {cel.sciezka} HTTP/1.1\r\n"
        f"Host: {naglowek_hosta}\r\n"
        "User-Agent: CMDB/monitor\r\n"
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


def _po_tls(gniazdo: socket.socket, cel: Cel, wynik: Wynik,
            rodzina: int, sockaddr: tuple) -> socket.socket:
    """Zestawia TLS i zapisuje w wyniku certyfikat oraz ocene lancucha."""
    serwer = cel.nazwa_tls or cel.host
    try:
        tunel = _kontekst(cel.weryfikuj_lancuch).wrap_socket(gniazdo, server_hostname=serwer)
        wynik.cert_zaufany = True if cel.weryfikuj_lancuch else None
    except ssl.SSLCertVerificationError as exc:
        # Niezaufany lancuch nie konczy sprawdzenia: certyfikat nadal chcemy
        # zobaczyc, bo to on odpowiada na pytanie "do kiedy wazny". Powtorka
        # bez weryfikacji jest jedynym sposobem, zeby go zdjac - i kosztuje
        # drugie polaczenie tylko wtedy, gdy naprawde cos jest nie tak.
        wynik.cert_zaufany = False
        wynik.cert_blad = str(getattr(exc, "verify_message", None) or exc)[:500]
        gniazdo.close()
        # Ten sam adres co za pierwszym razem, a nie ponowne rozwiazanie nazwy:
        # opisujemy certyfikat serwera, ktory wlasnie odmowil - nie kolejnego
        # z tej samej puli, ktory moze miec zupelnie inny.
        gniazdo = socket.socket(rodzina, socket.SOCK_STREAM)
        gniazdo.settimeout(cel.limit_sekund)
        gniazdo.connect(sockaddr)
        tunel = _kontekst(False).wrap_socket(gniazdo, server_hostname=serwer)

    wynik.tls_zestawione = True
    der = tunel.getpeercert(binary_form=True)
    if der:
        try:
            wynik.certyfikat = opisz_certyfikat(der)
        except Exception as exc:  # certyfikat nie do odczytania to nie awaria uslugi
            wynik.cert_blad = f"nie moge odczytac certyfikatu: {exc}"[:500]
    else:
        wynik.cert_blad = "serwer nie przedstawil certyfikatu"
    return tunel


def sprawdz(cel: Cel) -> Wynik:
    """Jedno sprawdzenie celu. Nie dotyka bazy - wolno je wolac z watku."""
    wynik = Wynik(dostepna=False)
    start = time.monotonic()
    gniazdo = None
    try:
        rodzina, sockaddr = rozwiaz(cel.host, cel.port)
        wynik.adres = sockaddr[0]
        gniazdo = socket.socket(rodzina, socket.SOCK_STREAM)
        gniazdo.settimeout(cel.limit_sekund)
        gniazdo.connect(sockaddr)
        if cel.protokol in PROTOKOLY_Z_TLS:
            gniazdo = _po_tls(gniazdo, cel, wynik, rodzina, sockaddr)
        if cel.protokol in PROTOKOLY_Z_HTTP:
            wynik.kod = _linia_statusu(gniazdo, cel)
            oczekiwany = cel.oczekiwany_kod
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
        wynik.blad = f"brak odpowiedzi w {cel.limit_sekund} s"
    except OSError as exc:
        # Komunikat systemu ("Connection refused") mowi wiecej niz nazwa klasy.
        wynik.blad = f"{exc.strerror or exc}"[:500]
    except Exception as exc:  # zaden cel nie moze wywrocic calego obiegu
        log.exception("sprawdzenie celu %s zakonczylo sie bledem", cel.nazwa)
        wynik.blad = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if gniazdo is not None:
            try:
                gniazdo.close()
            except OSError:
                pass
    wynik.czas_ms = int((time.monotonic() - start) * 1000)
    return wynik


# --- ocena stanu ------------------------------------------------------------

def dni_do_konca(monitor: MonitorUslugi, teraz: datetime | None = None) -> int | None:
    """Ile pelnych dni zostalo waznosci certyfikatu. None - nie wiemy."""
    if monitor.cert_do is None:
        return None
    pozostalo = as_utc(monitor.cert_do) - (teraz or utcnow())
    # Dzielimy na dolne pelne dni: certyfikat wazny jeszcze przez 30 godzin
    # ma "1 dzien", a nie "2 dni" - zaokraglanie w gore odbiera dzien reakcji.
    return int(pozostalo.total_seconds() // 86400)


def stan_certyfikatu(monitor: MonitorUslugi, teraz: datetime | None = None) -> str:
    """Ocena samego certyfikatu, niezalezna od tego, czy usluga odpowiada."""
    if monitor.protokol not in PROTOKOLY_Z_TLS:
        return STAN_NIEZNANY
    if monitor.weryfikuj_lancuch and monitor.cert_zaufany is False:
        return STAN_AWARIA
    dni = dni_do_konca(monitor, teraz)
    if dni is None:
        return STAN_NIEZNANY
    if dni < 0 or dni <= monitor.prog_alarmu_dni:
        return STAN_AWARIA
    if dni <= monitor.prog_ostrzezenia_dni:
        return STAN_OSTRZEZENIE
    return STAN_OK


def stan_celu(dostepnosc: str, certyfikat: str) -> str:
    """Widoczny stan celu: gorszy ze skladowych.

    Usluga, ktora odpowiada z certyfikatem wygasajacym jutro, nie moze swiecic
    na zielono - a certyfikat wazny rok nie moze przykryc tego, ze nikt pod
    tym adresem nie odbiera.
    """
    return max((dostepnosc, certyfikat), key=lambda stan: WAGA_STANU.get(stan, 0))


def _prog_certyfikatu(monitor: MonitorUslugi, dni: int | None) -> int | None:
    """Najnizszy przekroczony prog powiadomienia albo None."""
    if dni is None:
        return None
    if dni < 0:
        return 0
    if dni <= monitor.prog_alarmu_dni:
        return monitor.prog_alarmu_dni
    if dni <= monitor.prog_ostrzezenia_dni:
        return monitor.prog_ostrzezenia_dni
    return None


# --- zapis wyniku -----------------------------------------------------------

def zapisz_wynik(db: Session, monitor: MonitorUslugi, wynik: Wynik) -> dict:
    """Przenosi wynik sprawdzenia na stan celu, historie i powiadomienia.

    Zwraca opis tego, co sie zmienilo - uzywa go panel ("sprawdz teraz")
    i log harmonogramu.
    """
    teraz = utcnow()
    poprzedni_stan = monitor.stan
    poprzedni_odcisk = monitor.cert_odcisk

    monitor.ostatnie_sprawdzenie = teraz
    monitor.czas_odpowiedzi_ms = wynik.czas_ms
    monitor.ostatni_kod = wynik.kod
    monitor.ostatni_adres = wynik.adres
    monitor.ostatni_blad = wynik.blad

    if wynik.dostepna:
        monitor.kolejne_bledy = 0
        monitor.stan_dostepnosci = STAN_OK
    else:
        monitor.kolejne_bledy = (monitor.kolejne_bledy or 0) + 1
        # Jedna zgubiona odpowiedz to jeszcze nie awaria. Dopiero powtorzenie
        # oznacza, ze cos sie stalo - i dopiero wtedy idzie powiadomienie.
        monitor.stan_dostepnosci = (
            STAN_AWARIA if monitor.kolejne_bledy >= monitor.liczba_prob else STAN_OSTRZEZENIE
        )

    if wynik.certyfikat is not None:
        cert = wynik.certyfikat
        # Odnowiony certyfikat to nowy certyfikat: progi powiadomien licza sie
        # od poczatku, inaczej po odnowieniu nikt nie dostalby juz ostrzezenia.
        if cert.odcisk != poprzedni_odcisk:
            monitor.powiadomiony_prog = None
        monitor.cert_podmiot = cert.podmiot
        monitor.cert_wystawca = cert.wystawca
        monitor.cert_od = cert.wazny_od
        monitor.cert_do = cert.wazny_do
        monitor.cert_odcisk = cert.odcisk
        monitor.cert_nazwy = cert.nazwy
    if monitor.protokol not in PROTOKOLY_Z_TLS:
        monitor.cert_zaufany = None
        monitor.cert_blad = None
    elif wynik.tls_zestawione:
        monitor.cert_zaufany = wynik.cert_zaufany
        monitor.cert_blad = wynik.cert_blad
    # Gdy polaczenie w ogole nie doszlo do skutku, ocena lancucha zostaje
    # sprzed awarii. Wyzerowanie jej znaczyloby "lancuch juz w porzadku"
    # i przy niedostepnej usludze wyslaloby wiadomosc, ze certyfikat zostal
    # naprawiony - w chwili, w ktorej nikt niczego nie naprawial.

    monitor.stan_certyfikatu = stan_certyfikatu(monitor, teraz)
    monitor.stan = stan_celu(monitor.stan_dostepnosci, monitor.stan_certyfikatu)
    if monitor.stan != poprzedni_stan:
        monitor.ostatnia_zmiana_stanu = teraz

    db.add(PomiarMonitora(
        tenant_id=monitor.tenant_id,
        monitor_id=monitor.id,
        sprawdzono=teraz,
        # Do historii idzie stan DOSTEPNOSCI: procent dostepnosci nie moze
        # spadac przez certyfikat, ktory konczy sie za trzy tygodnie.
        stan=monitor.stan_dostepnosci,
        czas_odpowiedzi_ms=wynik.czas_ms,
        kod=wynik.kod,
        blad=(wynik.blad or None) if not wynik.dostepna else None,
    ))

    powiadomienia = powiadom(db, monitor)
    db.commit()
    return {
        "stan": monitor.stan,
        "poprzedni": poprzedni_stan,
        "zmiana": monitor.stan != poprzedni_stan,
        "powiadomienia": powiadomienia,
    }


# --- powiadomienia ----------------------------------------------------------

def adresy(monitor: MonitorUslugi) -> list[str]:
    """Adresaci powiadomien rozebrani z pola tekstowego."""
    surowe = (monitor.adresaci or "").replace(";", ",").replace("\n", ",")
    return [a.strip() for a in surowe.split(",") if a.strip() and "@" in a]


def _tresc(monitor: MonitorUslugi, powod: str, naglowek: str) -> tuple[str, str, str]:
    """Zwraca (temat, HTML, tekst) jednej wiadomosci.

    Wiadomosc ma odpowiadac na pytanie zadawane o trzeciej w nocy: co, gdzie
    i od kiedy. Sam temat musi wystarczyc, bo na telefonie widac tylko jego.
    """
    dni = dni_do_konca(monitor)
    wiersze = [
        ("Cel", f"{monitor.nazwa} ({monitor.host}:{monitor.port}, {monitor.protokol.upper()})"),
        ("Stan", monitor.stan),
        ("Sprawdzono", as_utc(monitor.ostatnie_sprawdzenie).strftime("%Y-%m-%d %H:%M UTC")
         if monitor.ostatnie_sprawdzenie else "-"),
    ]
    if monitor.ostatni_adres:
        wiersze.append(("Adres", monitor.ostatni_adres))
    if monitor.ostatni_blad:
        wiersze.append(("Blad", monitor.ostatni_blad))
    if monitor.cert_do is not None:
        wiersze.append((
            "Certyfikat wazny do",
            f"{as_utc(monitor.cert_do).strftime('%Y-%m-%d')}"
            + (f" (za {dni} dni)" if dni is not None and dni >= 0 else " (juz wygasl)"),
        ))
    if monitor.cert_wystawca:
        wiersze.append(("Wystawca", monitor.cert_wystawca))
    if monitor.cert_zaufany is False:
        wiersze.append(("Lancuch", monitor.cert_blad or "niezaufany"))

    temat = f"[CMDB] {naglowek}: {monitor.nazwa}"
    html = (
        f"<p style='font-size:15px'><b>{naglowek}</b></p><p>{powod}</p>"
        "<table cellpadding='4' cellspacing='0' style='font-size:13px; border-collapse:collapse'>"
        + "".join(
            f"<tr><td style='color:#6b7280'>{etykieta}</td><td><b>{wartosc}</b></td></tr>"
            for etykieta, wartosc in wiersze
        )
        + "</table><p style='font-size:12px; color:#6b7280'>"
        "Wiadomosc wyslana automatycznie przez monitorowanie CMDB.</p>"
    )
    tekst = "\n".join([naglowek, "", powod, ""]
                      + [f"{etykieta}: {wartosc}" for etykieta, wartosc in wiersze]
                      + ["", "Wiadomosc wyslana automatycznie przez monitorowanie CMDB."])
    return temat, html, tekst


def _wyslij(db: Session, monitor: MonitorUslugi, powod: str, naglowek: str) -> bool:
    from . import poczta

    odbiorcy = adresy(monitor)
    if not odbiorcy:
        monitor.blad_powiadomienia = "cel ma wlaczone powiadomienia, ale nie ma adresatow"
        return False
    temat, html, tekst = _tresc(monitor, powod, naglowek)
    try:
        poczta.wyslij(db, monitor.tenant_id, odbiorcy, temat, html, tekst)
    except Exception as exc:  # blad wysylki nie moze zatrzymac monitorowania
        log.error("powiadomienie o celu %s nie zostalo wyslane: %s", monitor.nazwa, exc)
        monitor.blad_powiadomienia = str(exc)[:1000]
        return False
    monitor.ostatnie_powiadomienie = utcnow()
    monitor.blad_powiadomienia = None
    return True


def powiadom(db: Session, monitor: MonitorUslugi) -> list[str]:
    """Wysyla to, co sie zmienilo od ostatniego zgloszenia.

    Zapisujemy stan, o KTORYM ZGLOSILISMY, a nie sam fakt wyslania. Dzieki
    temu wiadomosc idzie przy kazdej zmianie i tylko przy zmianie - awaria
    trwajaca tydzien nie zamienia sie w tysiac wiadomosci, a jej koniec
    zostaje zauwazony.

    Nieudana wysylka NIE przesuwa znacznika: powiadomienie, ktore nie doszlo,
    nie jest powiadomieniem, wiec probujemy ponownie przy kolejnym obiegu.
    """
    wyslane: list[str] = []
    if not monitor.powiadamiaj:
        return wyslane

    # 1. Dostepnosc. Pojedyncza zgubiona odpowiedz (stan "ostrzezenie") nie
    #    jest zdarzeniem - zglaszamy dopiero potwierdzona awarie i powrot.
    if monitor.stan_dostepnosci in (STAN_OK, STAN_AWARIA):
        if monitor.stan_dostepnosci != monitor.powiadomiona_dostepnosc:
            pierwsze_zgloszenie = monitor.powiadomiona_dostepnosc is None
            if monitor.stan_dostepnosci == STAN_AWARIA:
                powod = (f"Usluga nie odpowiada od {monitor.kolejne_bledy} kolejnych sprawdzen. "
                         f"Przyczyna ostatniej proby: {monitor.ostatni_blad or 'nieznana'}.")
                naglowek = "Usluga niedostepna"
            elif pierwsze_zgloszenie:
                # Pierwsze udane sprawdzenie nowego celu nie jest powrotem
                # do dzialania - nie ma o czym pisac, wystarczy zapamietac.
                monitor.powiadomiona_dostepnosc = STAN_OK
                powod = naglowek = ""
            else:
                powod = "Usluga znowu odpowiada."
                naglowek = "Usluga dziala"
            if naglowek and _wyslij(db, monitor, powod, naglowek):
                monitor.powiadomiona_dostepnosc = monitor.stan_dostepnosci
                wyslane.append(naglowek)

    # 2. Certyfikat. Dwa niezalezne powody: przekroczony kolejny prog dni
    #    (sam stan by tego nie zlapal - w "ostrzezeniu" certyfikat stoi
    #    tygodniami) oraz zmiana stanu, czyli niezaufany lancuch i powrot
    #    do porzadku po odnowieniu.
    if monitor.stan_certyfikatu != STAN_NIEZNANY:
        dni = dni_do_konca(monitor)
        prog = _prog_certyfikatu(monitor, dni)
        nowy_prog = prog is not None and (
            monitor.powiadomiony_odcisk != monitor.cert_odcisk
            or monitor.powiadomiony_prog is None
            or prog < monitor.powiadomiony_prog
        )
        nowy_stan = monitor.stan_certyfikatu != monitor.powiadomiony_stan_cert
        # Certyfikat w porzadku widziany po raz pierwszy nie jest zdarzeniem -
        # ale popsuty juz tak, i to niezaleznie od progu dni: niezaufany
        # lancuch przy certyfikacie waznym rok nie przekracza zadnego progu,
        # a jest awaria od chwili, w ktorej go zobaczylismy.
        if monitor.powiadomiony_stan_cert is None and monitor.stan_certyfikatu == STAN_OK:
            monitor.powiadomiony_stan_cert = STAN_OK
            monitor.powiadomiony_odcisk = monitor.cert_odcisk
        elif nowy_prog or nowy_stan:
            if monitor.weryfikuj_lancuch and monitor.cert_zaufany is False:
                naglowek = "Certyfikat niezaufany"
                powod = ("Lancuch certyfikatu nie przechodzi weryfikacji: "
                         f"{monitor.cert_blad or 'nieznana przyczyna'}.")
            elif dni is not None and dni < 0:
                naglowek = "Certyfikat wygasl"
                powod = f"Certyfikat stracil waznosc {abs(dni)} dni temu."
            elif dni is not None and prog is not None:
                naglowek = "Certyfikat wygasa"
                powod = f"Certyfikat traci waznosc za {dni} dni."
            else:
                naglowek = "Certyfikat w porzadku"
                powod = ("Certyfikat zostal odnowiony albo problem ustapil - "
                         f"waznosc do {as_utc(monitor.cert_do).strftime('%Y-%m-%d')}."
                         if monitor.cert_do else "Problem z certyfikatem ustapil.")
            if _wyslij(db, monitor, powod, naglowek):
                monitor.powiadomiony_prog = prog
                monitor.powiadomiony_stan_cert = monitor.stan_certyfikatu
                monitor.powiadomiony_odcisk = monitor.cert_odcisk
                wyslane.append(naglowek)
    return wyslane


# --- historia ---------------------------------------------------------------

def dostepnosc(db: Session, monitor_id: str, tenant_id: str, godziny: int = 24) -> dict:
    """Procent udanych sprawdzen w oknie czasu.

    Bez historii da sie powiedziec tylko "dziala teraz". Pytanie, ktore pada
    po awarii, brzmi jednak "jak dlugo nie dzialalo".
    """
    od = utcnow() - timedelta(hours=godziny)
    wszystkie, udane, sredni = db.execute(
        select(
            func.count(PomiarMonitora.id),
            func.count(PomiarMonitora.id).filter(PomiarMonitora.stan == STAN_OK),
            func.avg(PomiarMonitora.czas_odpowiedzi_ms).filter(PomiarMonitora.stan == STAN_OK),
        ).where(
            PomiarMonitora.monitor_id == monitor_id,
            PomiarMonitora.tenant_id == tenant_id,
            PomiarMonitora.sprawdzono >= od,
        )
    ).one()
    return {
        "godziny": godziny,
        "pomiary": wszystkie or 0,
        "udane": udane or 0,
        # Brak pomiarow to nie jest 100% - to brak wiedzy. Zerowa liczba
        # sprawdzen z wynikiem "wszystko dziala" byla by po prostu klamstwem.
        "procent": round(udane * 100 / wszystkie, 2) if wszystkie else None,
        "sredni_czas_ms": int(sredni) if sredni is not None else None,
    }


def usun_stare_pomiary(db: Session) -> int:
    """Kasuje pomiary starsze niz okno retencji."""
    granica = utcnow() - timedelta(days=get_settings().monitoring_history_days)
    usuniete = db.execute(
        delete(PomiarMonitora).where(PomiarMonitora.sprawdzono < granica)
    ).rowcount
    db.commit()
    return usuniete or 0


# --- obieg w tle ------------------------------------------------------------

# Inna niz blokada schematu i niz blokada raportow - te trzy rzeczy nie moga
# sie nawzajem blokowac.
KLUCZ_BLOKADY = 0x434D4D4F  # "CMMO"


def do_sprawdzenia(db: Session, limit: int) -> list[MonitorUslugi]:
    """Cele, ktorych termin minal - najbardziej spoznione najpierw."""
    zapas = MonitorUslugi.interwal_sekund * literal_column("interval '1 second'")
    return db.execute(
        select(MonitorUslugi)
        .where(
            MonitorUslugi.aktywny.is_(True),
            or_(
                MonitorUslugi.ostatnie_sprawdzenie.is_(None),
                MonitorUslugi.ostatnie_sprawdzenie <= func.now() - zapas,
            ),
        )
        .order_by(MonitorUslugi.ostatnie_sprawdzenie.asc().nulls_first())
        .limit(limit)
    ).scalars().all()


def sprawdz_teraz(db: Session, monitor: MonitorUslugi) -> dict:
    """Sprawdzenie na zadanie z panelu, bez czekania na harmonogram."""
    return zapisz_wynik(db, monitor, sprawdz(cel_z_monitora(monitor)))


def przebieg_w_sesji(db: Session) -> dict:
    """Jeden obieg na gotowej sesji. Wydzielony, zeby dalo sie go przetestowac."""
    settings = get_settings()
    cele = do_sprawdzenia(db, settings.monitoring_workers * 20)
    if not cele:
        return {"sprawdzone": 0, "zmiany": 0, "powiadomienia": 0}

    # Watki dostaja zamrozone kopie ustawien i nie dotykaja bazy; wyniki
    # zapisuje watek glowny, po kolei. Sesja SQLAlchemy nie jest bezpieczna
    # w watkach, a rownolegly zapis nie przyspieszylby niczego - czas idzie
    # na czekanie na siec, nie na baze.
    kopie = [cel_z_monitora(m) for m in cele]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(settings.monitoring_workers, len(kopie)),
        thread_name_prefix="cmdb-monitor",
    ) as pula:
        wyniki = list(pula.map(sprawdz, kopie))

    zmiany = powiadomienia = 0
    for monitor, wynik in zip(cele, wyniki):
        podsumowanie = zapisz_wynik(db, monitor, wynik)
        zmiany += 1 if podsumowanie["zmiana"] else 0
        powiadomienia += len(podsumowanie["powiadomienia"])
    return {"sprawdzone": len(cele), "zmiany": zmiany, "powiadomienia": powiadomienia}


def przebieg() -> dict | None:
    """Jeden obieg. None, gdy robote wykonuje wlasnie inny proces.

    Serwer produkcyjny dziala w kilku procesach roboczych i kazdy uruchamia
    ten sam kod. Bez uzgodnienia kazdy sprawdzalby te same cele i wysylal te
    same powiadomienia - a alarm powtorzony cztery razy uczy ludzi, ze alarmy
    mozna ignorowac.
    """
    from ..db import SessionLocal, engine
    from sqlalchemy import text

    with engine.connect() as conn:
        przejete = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:klucz)"), {"klucz": KLUCZ_BLOKADY}
        ).scalar())
        conn.commit()
        if not przejete:
            return None
        try:
            with SessionLocal() as db:
                wynik = przebieg_w_sesji(db)
                usuniete = usun_stare_pomiary(db)
            return {**wynik, "usuniete_pomiary": usuniete}
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:klucz)"), {"klucz": KLUCZ_BLOKADY})
            conn.commit()


async def petla() -> None:
    """Zadanie w tle uruchamiane przy starcie serwera."""
    settings = get_settings()
    if not settings.monitoring_enabled:
        log.info("monitorowanie uslug wylaczone w konfiguracji")
        return
    while True:
        try:
            await asyncio.sleep(settings.monitoring_tick_seconds)
            wynik = await asyncio.to_thread(przebieg)
            if wynik and wynik["sprawdzone"]:
                log.info(
                    "monitorowanie: sprawdzono %d celow, zmian stanu %d, powiadomien %d",
                    wynik["sprawdzone"], wynik["zmiany"], wynik["powiadomienia"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Blad jednego obiegu nie moze zatrzymac monitorowania na zawsze -
            # inaczej jedna nieudana proba wylaczalaby alarmy do restartu.
            log.error("monitorowanie: obieg zakonczyl sie bledem: %s", exc)


# --- podsumowanie dla panelu i raportow -------------------------------------

def podsumowanie(db: Session, tenant_id: str) -> dict:
    """Liczby, ktore trafiaja na pulpit i do raportu."""
    wiersze = db.execute(
        select(MonitorUslugi.stan, func.count(MonitorUslugi.id))
        .where(MonitorUslugi.tenant_id == tenant_id, MonitorUslugi.aktywny.is_(True))
        .group_by(MonitorUslugi.stan)
    ).all()
    licznik = {stan: 0 for stan in (STAN_OK, STAN_OSTRZEZENIE, STAN_AWARIA, STAN_NIEZNANY)}
    for stan, ile in wiersze:
        licznik[stan] = ile
    licznik["razem"] = sum(licznik.values())
    licznik["wylaczone"] = db.execute(
        select(func.count(MonitorUslugi.id)).where(
            MonitorUslugi.tenant_id == tenant_id, MonitorUslugi.aktywny.is_(False))
    ).scalar_one()
    return licznik


def cele_firmy(db: Session, tenant_id: str, tylko_aktywne: bool = False) -> list[MonitorUslugi]:
    zapytanie = select(MonitorUslugi).where(MonitorUslugi.tenant_id == tenant_id)
    if tylko_aktywne:
        zapytanie = zapytanie.where(MonitorUslugi.aktywny.is_(True))
    return db.execute(zapytanie.order_by(MonitorUslugi.nazwa)).scalars().all()


def limit_osiagniety(db: Session, tenant_id: str) -> bool:
    ile = db.execute(
        select(func.count(MonitorUslugi.id)).where(MonitorUslugi.tenant_id == tenant_id)
    ).scalar_one()
    return ile >= get_settings().monitoring_max_targets


# --- walidacja ustawien celu ------------------------------------------------

def sprawdz_ustawienia(dane: dict) -> dict:
    """Sprawdza i normalizuje ustawienia celu. Rzuca ValueError z opisem.

    Wspolne dla dodawania i edycji, bo formularz jest ten sam - a ustawienie,
    ktorego nie da sie wpisac przy zakladaniu, nie moze wchodzic tylnymi
    drzwiami przy poprawianiu.
    """
    nazwa = (dane.get("nazwa") or "").strip()
    if not nazwa:
        raise ValueError("podaj nazwe celu")

    host = (dane.get("host") or "").strip().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 255 or any(z in host for z in " \t/\\?#@"):
        raise ValueError("podaj adres IP albo nazwe hosta - bez schematu i sciezki")

    protokol = (dane.get("protokol") or "").strip().lower()
    if protokol not in PROTOKOLY_MONITORA:
        raise ValueError("nieznany sposob sprawdzenia")

    try:
        port = int(dane.get("port") or 0)
    except (TypeError, ValueError):
        raise ValueError("port musi byc liczba") from None
    if not 1 <= port <= 65535:
        raise ValueError("port musi miescic sie w zakresie 1-65535")

    sciezka = (dane.get("sciezka") or "/").strip() or "/"
    if protokol in PROTOKOLY_Z_HTTP:
        if not sciezka.startswith("/"):
            raise ValueError("sciezka musi zaczynac sie od /")
        # Znak nowej linii w sciezce pozwolilby dopisac wlasne naglowki do
        # zadania - to nie jest teoretyczne, tylko klasyczne wstrzykniecie.
        if any(znak in sciezka for znak in "\r\n \t") or len(sciezka) > 500:
            raise ValueError("sciezka nie moze zawierac spacji ani znakow nowej linii")

    try:
        kod = int(dane.get("oczekiwany_kod") or 0)
    except (TypeError, ValueError):
        raise ValueError("oczekiwany kod musi byc liczba") from None
    if kod and not 100 <= kod <= 599:
        raise ValueError("oczekiwany kod HTTP musi miescic sie w zakresie 100-599")

    def zakres(klucz: str, etykieta: str, minimum: int, maksimum: int, domyslna: int) -> int:
        try:
            wartosc = int(dane.get(klucz) or domyslna)
        except (TypeError, ValueError):
            raise ValueError(f"{etykieta}: podaj liczbe") from None
        if not minimum <= wartosc <= maksimum:
            raise ValueError(f"{etykieta}: dozwolony zakres to {minimum}-{maksimum}")
        return wartosc

    interwal = zakres("interwal_sekund", "odstep sprawdzen", MIN_INTERWAL, MAKS_INTERWAL, 300)
    limit = zakres("limit_sekund", "limit czasu", MIN_LIMIT, MAKS_LIMIT, 10)
    proby = zakres("liczba_prob", "liczba prob", MIN_PROB, MAKS_PROB, 2)
    ostrzezenie = zakres("prog_ostrzezenia_dni", "prog ostrzezenia", 1, MAKS_PROG_DNI, 30)
    alarm = zakres("prog_alarmu_dni", "prog alarmu", 0, MAKS_PROG_DNI, 7)
    if alarm >= ostrzezenie:
        raise ValueError("prog alarmu musi byc mniejszy niz prog ostrzezenia")
    if limit >= interwal:
        raise ValueError("limit czasu musi byc krotszy niz odstep miedzy sprawdzeniami")

    nazwa_tls = (dane.get("nazwa_tls") or "").strip().rstrip(".") or None
    if nazwa_tls and (len(nazwa_tls) > 255 or any(z in nazwa_tls for z in " \t/\\?#@")):
        raise ValueError("nazwa w certyfikacie musi byc sama nazwa hosta")

    powiadamiaj = bool(dane.get("powiadamiaj"))
    adresaci = (dane.get("adresaci") or "").strip() or None
    ustawienia = {
        "nazwa": nazwa[:200],
        "host": host,
        "port": port,
        "protokol": protokol,
        "sciezka": sciezka if protokol in PROTOKOLY_Z_HTTP else "/",
        "oczekiwany_kod": kod,
        "nazwa_tls": nazwa_tls if protokol in PROTOKOLY_Z_TLS else None,
        "weryfikuj_lancuch": bool(dane.get("weryfikuj_lancuch")),
        "interwal_sekund": interwal,
        "limit_sekund": limit,
        "liczba_prob": proby,
        "prog_ostrzezenia_dni": ostrzezenie,
        "prog_alarmu_dni": alarm,
        "powiadamiaj": powiadamiaj,
        "adresaci": adresaci,
        "aktywny": bool(dane.get("aktywny")),
    }
    if powiadamiaj:
        surowe = (adresaci or "").replace(";", ",").replace("\n", ",")
        if not [a for a in surowe.split(",") if a.strip() and "@" in a]:
            raise ValueError("powiadomienia wymagaja co najmniej jednego adresu e-mail")
    # Adres sprawdzamy dopiero na koncu: komunikat o zabronionym adresie ma
    # sens tylko wtedy, gdy reszta formularza jest juz poprawna.
    if not _adres_wpisany_recznie_dozwolony(host):
        raise ValueError(
            "ten adres jest zabroniony: monitorowanie nie chodzi pod adresy petli "
            "zwrotnej, link-local ani rozgloszeniowe"
        )
    return ustawienia


def _adres_wpisany_recznie_dozwolony(host: str) -> bool:
    """Odrzuca adres zabroniony juz przy zapisie, gdy podano go liczbowo.

    Nazwy nie rozwiazujemy w formularzu: odpowiedz DNS zmienia sie w czasie,
    wiec i tak sprawdzamy ja przy kazdym polaczeniu. Ale adres wpisany
    wprost widac od razu i nie ma powodu przyjmowac go do bazy.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return powod_odmowy(host) is None


def nazwa_tenanta(db: Session, tenant_id: str) -> str:
    tenant = db.get(Tenant, tenant_id)
    return tenant.name if tenant else tenant_id
