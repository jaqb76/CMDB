"""Monitorowanie ciaglosci uslug i waznosci certyfikatow SSL - strona serwera.

**Sonduje agent, nie serwer.** To jest sedno tego modulu i powod, dla ktorego
nie ma tu ani jednego gniazda sieciowego. Usluga zyje w sieci klienta, za
NAT-em i firewallem; serwer CMDB tej sieci zwykle nie widzi, a gdyby widzial
sieci wszystkich firm naraz, bylby jednym miejscem, z ktorego da sie zajrzec
do kazdej z nich. Agent juz tam stoi - to on nawiazuje polaczenie i to on
mierzy to, co zobaczy uzytkownik uslugi.

Podzial pracy wychodzi z tego sam:

* **agent** - polityke pobiera, sondy wykonuje, przerwy sklada, raportuje;
* **serwer** - polityke wydaje, wyniki przyjmuje, certyfikat rozbiera, stan
  ocenia i powiadamia.

Certyfikat rozbiera serwer, bo agent chodzi na samej bibliotece standardowej,
w ktorej nie ma parsera X.509. Agent liczy odcisk SHA-256 (hashlib wystarczy)
i przysyla CALY certyfikat tylko wtedy, gdy odcisk sie zmienil - certyfikat
wazny rok nie ma po co jechac przez siec co dobe.

**Dostepnosc i certyfikat oceniamy osobno.** To dwie niezalezne awarie:
usluga potrafi odpowiadac z certyfikatem wygasajacym jutro i potrafi milczec
z certyfikatem waznym rok. Widoczny stan celu to gorszy z dwoch skladowych.

Czego to NIE jest: systemu monitorowania z prawdziwego zdarzenia. Nie ma tu
zaleznosci miedzy celami, okien serwisowych ani eskalacji. Odpowiadamy na dwa
pytania - "czy odpowiada" i "do kiedy wazny certyfikat" - i powiadamiamy, gdy
odpowiedz sie zmieni.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
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
    Asset,
    MonitorUslugi,
    OknoMonitorowania,
    PrzerwaDostepnosci,
    as_utc,
    utcnow,
)

log = logging.getLogger(__name__)

# Granice ustawien celu. Nie sa ozdoba: odstep krotszy niz minuta zamienia
# monitorowanie w generator ruchu, a limit czasu dluzszy niz odstep sprawia,
# ze sonda nie zdazy sie skonczyc przed nastepna.
MIN_INTERWAL = 60
MAKS_INTERWAL = 86400
MIN_INTERWAL_CERT = 3600
MAKS_INTERWAL_CERT = 604800
MIN_LIMIT = 1
MAKS_LIMIT = 60
MIN_PROB = 1
MAKS_PROB = 10
MAKS_PROG_DNI = 365

# Jak czesto agent ma przysylac podsumowanie. Awarie ida osobno i natychmiast,
# wiec ten odstep dotyczy wylacznie potwierdzen, ze wszystko dziala.
MIN_INTERWAL_RAPORTU = 60
MAKS_INTERWAL_RAPORTU = 3600

# Po ilu odstepach raportowania milczenie agenta znaczy "nie wiem".
# Dwa, a nie jeden: pojedynczy zgubiony raport zdarza sie przy restarcie
# maszyny i nie jest jeszcze utrata monitorowania.
TOLERANCJA_RAPORTU = 2


# --- adresy -----------------------------------------------------------------

def powod_odmowy(adres: str) -> str | None:
    """Dlaczego nie wolno monitorowac tego adresu. None znaczy "wolno".

    Sonduje agent, wiec petla zwrotna nie jest juz problemem serwera - ale
    nadal jest problemem: 127.0.0.1 wpisany w panelu znaczy "maszyna agenta",
    a nie to, co mial na mysli wpisujacy, i cel bylby niesprawdzalny z zewnatrz.
    Adres link-local zostaje zabroniony u zrodla: pod 169.254.169.254 odpowiada
    usluga metadanych chmury, a agent bywa maszyna wirtualna u dostawcy.
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
        return "adres petli zwrotnej wskazuje na sama maszyne agenta"
    if ip.is_reserved:
        return "adres zarezerwowany"
    return None


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


def opisz_certyfikat(pem: str) -> dict:
    """Rozbiera certyfikat przyslany przez agenta.

    Robi to serwer, bo agent chodzi na samej bibliotece standardowej, w ktorej
    nie ma parsera X.509. Agent umie policzyc odcisk (hashlib) i tyle mu
    potrzeba, zeby wiedziec, czy jest sens cokolwiek wysylac.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    certyfikat = x509.load_pem_x509_certificate(pem.encode("ascii"))
    nazwy: list[str] = []
    try:
        rozszerzenie = certyfikat.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        nazwy = [str(n) for n in rozszerzenie.value.get_values_for_type(x509.DNSName)]
        nazwy += [str(n) for n in rozszerzenie.value.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return {
        "podmiot": _nazwa_z_x509(certyfikat.subject)[:500],
        "wystawca": _nazwa_z_x509(certyfikat.issuer)[:500],
        "wazny_od": _chwila(certyfikat, "not_valid_before_utc", "not_valid_before"),
        "wazny_do": _chwila(certyfikat, "not_valid_after_utc", "not_valid_after"),
        "odcisk": certyfikat.fingerprint(hashes.SHA256()).hex(),
        "nazwy": nazwy[:50],
    }


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


def milczy(monitor: MonitorUslugi, teraz: datetime | None = None) -> bool:
    """Czy agent przestal raportowac o tym celu.

    Cisza agenta to nie jest sprawna usluga ani awaria uslugi - to brak
    wiedzy. Pokazanie w takiej sytuacji ostatniego znanego stanu bylo by
    twierdzeniem o czyms, czego nikt od godziny nie mierzyl.
    """
    if not monitor.aktywny or monitor.ostatni_raport is None:
        return False
    prog = timedelta(seconds=get_settings().monitoring_report_seconds * TOLERANCJA_RAPORTU)
    return (teraz or utcnow()) - as_utc(monitor.ostatni_raport) > prog


def bez_wykonawcy(monitor: MonitorUslugi) -> str | None:
    """Dlaczego tego celu nikt nie sprawdza. None znaczy "jest kto".

    Agent jest jedynym wykonawca - serwer nie ma czym sondowac. Cel bez
    czynnego agenta nie jest wiec sprawdzany W OGOLE, i to musi byc widac
    od razu, a nie dopiero po dwoch pominietych raportach: "brak raportow"
    mowi, ze cos sie zepsulo, a tu nic sie nie psulo - po prostu nikomu
    tego nie zlecono.
    """
    if not monitor.aktywny:
        return None  # cel wylaczony swiadomie; to inna sprawa
    if monitor.wykonawca_id is None or monitor.wykonawca is None:
        return "nie wskazano maszyny, ktora ma sprawdzac"
    if not monitor.wykonawca.is_active or monitor.wykonawca.lifecycle != "aktywny":
        return f"maszyna {monitor.wykonawca.hostname} jest wycofana albo nieaktywna"
    return None


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


# --- przyjmowanie raportu agenta --------------------------------------------

def przyjmij_raport(db: Session, monitor: MonitorUslugi, wpis: dict, teraz: datetime) -> dict:
    """Przenosi jeden cel z raportu agenta na stan, historie i powiadomienia.

    ``wpis`` jest juz sprawdzony przez schemat API - tu zajmujemy sie
    znaczeniem, nie ksztaltem.
    """
    poprzedni_stan = monitor.stan
    monitor.ostatni_raport = teraz

    okno = wpis.get("okno")
    if okno is not None:
        _zapisz_okno(db, monitor, okno)
    for przerwa in wpis.get("przerwy") or []:
        _zapisz_przerwe(db, monitor, przerwa)

    ostatnia = wpis.get("ostatnia_sonda")
    if ostatnia is not None:
        monitor.ostatnie_sprawdzenie = as_utc(_czas(ostatnia["kiedy"]))
        monitor.czas_odpowiedzi_ms = ostatnia.get("czas_ms")
        monitor.ostatni_kod = ostatnia.get("kod")
        monitor.ostatni_adres = (ostatnia.get("adres") or None)
        monitor.ostatni_blad = (ostatnia.get("blad") or None)
        # Stan dostepnosci bierzemy z agenta: to on zna licznik kolejnych
        # nieudanych prob i to on wie, czy przerwa jest juz potwierdzona.
        monitor.stan_dostepnosci = (
            STAN_OK if ostatnia.get("dostepna") else
            STAN_AWARIA if ostatnia.get("potwierdzona_awaria") else STAN_OSTRZEZENIE
        )

    _zapisz_certyfikat(db, monitor, wpis)

    monitor.stan_certyfikatu = stan_certyfikatu(monitor, teraz)
    monitor.stan = stan_celu(monitor.stan_dostepnosci, monitor.stan_certyfikatu)
    if monitor.stan != poprzedni_stan:
        monitor.ostatnia_zmiana_stanu = teraz

    return {"stan": monitor.stan, "zmiana": monitor.stan != poprzedni_stan,
            "powiadomienia": powiadom(db, monitor)}


def _czas(wartosc: str) -> datetime:
    return datetime.fromisoformat(wartosc.replace("Z", "+00:00"))


def _zapisz_okno(db: Session, monitor: MonitorUslugi, okno: dict) -> None:
    """Zapisuje podsumowanie okresu. Powtorzony raport nie dubluje wiersza.

    Agent moze wyslac to samo okno ponownie po nieudanej probie wysylki -
    poczatek okna jest jego tozsamoscia, wiec drugi raz tylko poprawia liczby.
    """
    poczatek = as_utc(_czas(okno["od"]))
    istniejace = db.execute(
        select(OknoMonitorowania).where(
            OknoMonitorowania.monitor_id == monitor.id,
            OknoMonitorowania.poczatek == poczatek,
        )
    ).scalar_one_or_none()
    if istniejace is None:
        istniejace = OknoMonitorowania(
            tenant_id=monitor.tenant_id, monitor_id=monitor.id, poczatek=poczatek)
        db.add(istniejace)
    istniejace.koniec = as_utc(_czas(okno["do"]))
    istniejace.sond = int(okno["sond"])
    istniejace.udanych = int(okno["udanych"])
    istniejace.przyjeto = utcnow()


def _zapisz_przerwe(db: Session, monitor: MonitorUslugi, przerwa: dict) -> None:
    """Zapisuje albo domyka przerwe.

    Przerwa trwajaca przy poprzednim raporcie przychodzi ponownie - tym razem
    z koncem. Rozpoznajemy ja po poczatku, bo poczatek przerwy jest jej
    tozsamoscia; wstawienie drugiego wiersza rozbiloby jedna awarie na dwie.
    """
    poczatek = as_utc(_czas(przerwa["od"]))
    istniejaca = db.execute(
        select(PrzerwaDostepnosci).where(
            PrzerwaDostepnosci.monitor_id == monitor.id,
            PrzerwaDostepnosci.poczatek == poczatek,
        )
    ).scalar_one_or_none()
    if istniejaca is None:
        istniejaca = PrzerwaDostepnosci(
            tenant_id=monitor.tenant_id, monitor_id=monitor.id, poczatek=poczatek)
        db.add(istniejaca)
    koniec = przerwa.get("do")
    istniejaca.koniec = as_utc(_czas(koniec)) if koniec else None
    istniejaca.blad = (przerwa.get("blad") or None)
    istniejaca.sond = int(przerwa.get("sond") or 0)


def _zapisz_certyfikat(db: Session, monitor: MonitorUslugi, wpis: dict) -> None:
    """Zapisuje certyfikat, gdy agent przyslal nowy.

    Agent wysyla caly certyfikat wylacznie przy zmianie odcisku. Brak
    certyfikatu w raporcie znaczy wiec "bez zmian", a nie "nie ma" - i nie
    wolno tego pomylic, bo skasowanie zapisanej daty waznosci przy kazdym
    raporcie zgasiloby monitorowanie certyfikatow zupelnie.
    """
    if monitor.protokol not in PROTOKOLY_Z_TLS:
        monitor.cert_zaufany = monitor.cert_blad = None
        return

    stan_tls = wpis.get("tls")
    if stan_tls is None:
        # Do TLS w ogole nie doszlo (usluga nie odpowiada). Ocena lancucha
        # zostaje sprzed awarii: wyzerowanie jej znaczyloby "lancuch juz
        # w porzadku" i wyslaloby wiadomosc o naprawie, ktorej nie bylo.
        return

    monitor.cert_zaufany = stan_tls.get("zaufany")
    monitor.cert_blad = (stan_tls.get("blad") or None)

    pem = stan_tls.get("pem")
    if not pem:
        return
    try:
        opis = opisz_certyfikat(pem)
    except Exception as exc:
        log.warning("cel %s: nie moge rozebrac certyfikatu: %s", monitor.nazwa, exc)
        monitor.cert_blad = f"nie moge odczytac certyfikatu: {exc}"[:1000]
        return
    # Odnowiony certyfikat to nowy certyfikat: progi powiadomien licza sie
    # od poczatku, inaczej po odnowieniu nikt nie dostalby juz ostrzezenia.
    if opis["odcisk"] != monitor.cert_odcisk:
        monitor.powiadomiony_prog = None
    monitor.cert_podmiot = opis["podmiot"]
    monitor.cert_wystawca = opis["wystawca"]
    monitor.cert_od = opis["wazny_od"]
    monitor.cert_do = opis["wazny_do"]
    monitor.cert_odcisk = opis["odcisk"]
    monitor.cert_nazwy = opis["nazwy"]


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
        ("Sprawdzil", monitor.wykonawca.hostname if monitor.wykonawca else "agent"),
        ("Sprawdzono", as_utc(monitor.ostatnie_sprawdzenie).strftime("%Y-%m-%d %H:%M:%S UTC")
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
    except Exception as exc:  # blad wysylki nie moze zatrzymac przyjmowania raportu
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
    zostaje zauwazony. Agent przysyla poczatek i koniec awarii natychmiast,
    wiec ta funkcja dostaje je bez czekania na podsumowanie okresu.

    Nieudana wysylka NIE przesuwa znacznika: powiadomienie, ktore nie doszlo,
    nie jest powiadomieniem, wiec probujemy ponownie przy kolejnym raporcie.
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
                powod = ("Usluga nie odpowiada. Przyczyna ostatniej proby: "
                         f"{monitor.ostatni_blad or 'nieznana'}.")
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


# --- dostepnosc i historia --------------------------------------------------

def dostepnosc(db: Session, monitor_id: str, tenant_id: str, godziny: int = 24) -> dict:
    """Procent udanych sond w oknie czasu, liczony z podsumowan agenta.

    Sumujemy okna, a nie pojedyncze sondy: agent przysyla "15 sond, 9 udanych"
    zamiast pietnastu wierszy, a procent wychodzi z tego dokladnie ten sam.
    """
    od = utcnow() - timedelta(hours=godziny)
    sond, udanych = db.execute(
        select(func.coalesce(func.sum(OknoMonitorowania.sond), 0),
               func.coalesce(func.sum(OknoMonitorowania.udanych), 0))
        .where(OknoMonitorowania.monitor_id == monitor_id,
               OknoMonitorowania.tenant_id == tenant_id,
               OknoMonitorowania.koniec >= od)
    ).one()
    przerwy = db.execute(
        select(func.count(PrzerwaDostepnosci.id))
        .where(PrzerwaDostepnosci.monitor_id == monitor_id,
               PrzerwaDostepnosci.tenant_id == tenant_id,
               PrzerwaDostepnosci.poczatek >= od)
    ).scalar_one()
    return {
        "godziny": godziny,
        "sond": sond,
        "udanych": udanych,
        "przerw": przerwy,
        # Brak sond to nie jest 100% - to brak wiedzy. Zerowa liczba sprawdzen
        # z wynikiem "wszystko dziala" byla by po prostu klamstwem.
        "procent": round(udanych * 100 / sond, 2) if sond else None,
    }


def przerwy(db: Session, monitor_id: str, tenant_id: str, limit: int = 50) -> list:
    return db.execute(
        select(PrzerwaDostepnosci)
        .where(PrzerwaDostepnosci.monitor_id == monitor_id,
               PrzerwaDostepnosci.tenant_id == tenant_id)
        .order_by(PrzerwaDostepnosci.poczatek.desc())
        .limit(limit)
    ).scalars().all()


def usun_stara_historie(db: Session) -> dict:
    """Kasuje okna i przerwy starsze niz okno retencji."""
    granica = utcnow() - timedelta(days=get_settings().monitoring_history_days)
    okna = db.execute(
        delete(OknoMonitorowania).where(OknoMonitorowania.koniec < granica)).rowcount
    stare = db.execute(
        delete(PrzerwaDostepnosci).where(
            PrzerwaDostepnosci.poczatek < granica,
            PrzerwaDostepnosci.koniec.is_not(None))).rowcount
    db.commit()
    return {"okna": okna or 0, "przerwy": stare or 0}


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
    # Cele, o ktorych agent przestal raportowac. Osobno od stanow, bo to nie
    # jest awaria uslugi - to utrata monitorowania, i naprawia sie ja gdzie
    # indziej: przy agencie, a nie przy usludze.
    teraz = utcnow()
    aktywne = cele_firmy(db, tenant_id, tylko_aktywne=True)
    licznik["milczace"] = sum(1 for monitor in aktywne if milczy(monitor, teraz))
    # Cel bez czynnego agenta nie jest sprawdzany w ogole. Osobno od
    # milczacych, bo to inna usterka i naprawia sie ja inaczej: tam agent
    # przestal gadac, tu nikomu nie zlecono sprawdzania.
    licznik["bez_wykonawcy"] = sum(1 for monitor in aktywne if bez_wykonawcy(monitor))
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


# --- polityka wydawana agentowi ---------------------------------------------

def polityka_dla_agenta(db: Session, asset: Asset) -> dict:
    """Cele przypisane tej maszynie, w postaci, ktora rozumie agent.

    Agent nie dostaje niczego poza tym, co ma sprawdzac - ani nazw celow
    innych maszyn, ani niczego o pozostalych firmach. Zakres wynika z
    wykonawca_id, a nie z tego, o co agent poprosi.
    """
    teraz = utcnow()
    cele = db.execute(
        select(MonitorUslugi).where(
            MonitorUslugi.wykonawca_id == asset.id,
            MonitorUslugi.tenant_id == asset.tenant_id,
            MonitorUslugi.aktywny.is_(True),
        ).order_by(MonitorUslugi.nazwa)
    ).scalars().all()
    return {
        "enabled": bool(cele),
        "interwal_raportu": get_settings().monitoring_report_seconds,
        "cele": [
            {
                "id": cel.id,
                # Nazwa jedzie po to, zeby "cmdb-agent status" na maszynie
                # pokazal, CO jest sprawdzane, a nie sam adres z portem. To
                # nadal wylacznie cele tej maszyny - zakres wyznacza
                # wykonawca_id, nie to, o co agent poprosi.
                "nazwa": cel.nazwa,
                "host": cel.host,
                "port": cel.port,
                "protokol": cel.protokol,
                "sciezka": cel.sciezka or "/",
                "oczekiwany_kod": cel.oczekiwany_kod or 0,
                "nazwa_tls": cel.nazwa_tls or "",
                "weryfikuj_lancuch": bool(cel.weryfikuj_lancuch),
                "interwal_sekund": cel.interwal_sekund,
                "interwal_certyfikatu": cel.interwal_certyfikatu,
                "limit_sekund": cel.limit_sekund,
                "liczba_prob": cel.liczba_prob,
                # Odcisk, ktory serwer juz zna. Agent przysle caly certyfikat
                # tylko wtedy, gdy zobaczy inny - to jest cala redukcja ruchu
                # przy certyfikatach.
                "znany_odcisk": cel.cert_odcisk or "",
                # Sprawdzenie poza kolejnoscia, zlozone z panelu. Gasi sie
                # samo, gdy przyjdzie wynik nowszy niz zadanie.
                "wymus": bool(
                    cel.wymuszone_o is not None
                    and (cel.ostatnie_sprawdzenie is None
                         or as_utc(cel.ostatnie_sprawdzenie) < as_utc(cel.wymuszone_o))
                ),
            }
            for cel in cele
        ],
        "wydano": teraz.isoformat(),
    }


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

    interwal = zakres("interwal_sekund", "odstep sprawdzen", MIN_INTERWAL, MAKS_INTERWAL, 60)
    interwal_cert = zakres("interwal_certyfikatu", "odstep sprawdzania certyfikatu",
                           MIN_INTERWAL_CERT, MAKS_INTERWAL_CERT, 86400)
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
        "interwal_certyfikatu": interwal_cert,
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
    a rozwiazuje ja i tak agent - z wlasnej sieci, w ktorej ta sama nazwa
    moze wskazywac gdzie indziej niz z serwera.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return powod_odmowy(host) is None
