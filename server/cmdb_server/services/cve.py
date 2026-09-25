"""Zestawianie inwentarza z danymi o podatnosciach (CVE).

Trzy zasady, ktore ksztaltuja caly modul.

**Tylko dane dystrybucji.** Debian, Ubuntu i RHEL lataja wstecznie, nie
zmieniajac numeru wersji: pakiet "22.08.8-6+deb12u1" zawiera poprawke,
o ktorej numer upstream 22.08.8 nic nie mowi. Porownywanie wersji z baza NVD
dalo by lawine falszywych alarmow, bo NVD opisuje wersje upstream. Wiarygodna
odpowiedz na pytanie "czy TA wersja jest podatna" ma wylacznie dystrybucja,
ktora te poprawke wydala.

**Dopasowanie po pakiecie zrodlowym.** Dystrybucje indeksuja podatnosci po
pakiecie zrodlowym ("openssl"), a zainstalowane sa pakiety binarne
("libssl3"). Agent podaje jedno i drugie, wiec dopasowujemy po zrodlowym,
a pokazujemy binarny - ten, ktory administrator faktycznie widzi.

**Brak wiedzy to nie jest brak podatnosci.** Nieznana dystrybucja, nieudane
pobranie kanalu albo dane sprzed miesiecy koncza sie stanem "nieznany",
a nie liczba zero. W narzedziu do oceny bezpieczenstwa zero, ktore znaczy
"nie sprawdzilismy", jest gorsze niz brak jakiejkolwiek informacji.

Czego to NIE jest: skanowania podatnosci. Raport mowi "zainstalowana wersja
jest starsza niz ta z poprawka na CVE-X". Nie sprawdza konfiguracji, nie
bada uslug sieciowych i nie wie, czy podatny pakiet jest w ogole uzywany.
"""
from __future__ import annotations

import bz2
import io
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import CveEntry, CveFeed, as_utc, utcnow
from . import wersje_pakietow

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_NIEZNANY = "nieznany"

ADRES_DEBIAN = "https://security-tracker.debian.org/tracker/data/json"
ADRES_UBUNTU = "https://usn.ubuntu.com/usn-db/database.json.bz2"
# OVAL v2 Red Hata - jeden plik na glowne wydanie RHEL. Wariant bez
# "including-unpatched", czyli wylacznie wydane poprawki (RHSA).
ADRES_RHEL = "https://security.access.redhat.com/data/oval/v2/RHEL{wydanie}/rhel-{wydanie}.oval.xml.bz2"

NAGLOWKI = {"User-Agent": "CMDB/0.5 (inwentaryzacja)"}
LIMIT_POBIERANIA = 256 * 1024 * 1024
CZAS_POBIERANIA = 180

# Po ilu godzinach kanal uznajemy za przestarzaly. Dystrybucje wydaja poprawki
# codziennie, wiec tydzien to juz istotna luka w wiedzy.
PRZESTARZALY_PO_GODZINACH = 168

# Wagi z Debiana. "unimportant" oznacza problem bez znaczenia praktycznego -
# pokazywanie go razem z reszta zaszumia liste tak, ze przestanie byc czytana.
POMIJANE_WAGI = {"unimportant", "end-of-life"}


class BladKanalu(RuntimeError):
    """Nie udalo sie pobrac albo odczytac kanalu danych."""


def _pobierz(adres: str) -> bytes:
    try:
        zadanie = urllib.request.Request(adres, headers=NAGLOWKI)
        with urllib.request.urlopen(zadanie, timeout=CZAS_POBIERANIA) as odpowiedz:
            dane = odpowiedz.read(LIMIT_POBIERANIA + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BladKanalu(f"nie udalo sie pobrac {adres}: {exc}") from exc
    if len(dane) > LIMIT_POBIERANIA:
        raise BladKanalu(f"kanal {adres} przekracza dozwolony rozmiar")
    return dane


# --- odczyt kanalow ---------------------------------------------------------

def wpisy_debian(surowe: bytes, wydania: set[str]) -> list[dict]:
    """Wpisy z Debian Security Tracker dla wskazanych wydan."""
    try:
        dane = json.loads(surowe)
    except ValueError as exc:
        raise BladKanalu(f"kanal Debiana nie jest poprawnym JSON-em: {exc}") from exc

    wpisy = []
    for pakiet, podatnosci in dane.items():
        for cve, opis in podatnosci.items():
            if not cve.startswith("CVE-"):
                continue
            for wydanie, szczegoly in (opis.get("releases") or {}).items():
                if wydanie not in wydania:
                    continue
                status = szczegoly.get("status")
                # Pole bywa nieobecne. "".split() daje pusta liste, wiec
                # siegniecie po pierwszy element wywracalo caly odczyt kanalu.
                czlony = (szczegoly.get("urgency") or "").split()
                waga = czlony[0].lower() if czlony else ""
                if waga in POMIJANE_WAGI:
                    continue
                # "undetermined" znaczy, ze sam Debian nie wie - nie zamieniamy
                # tego na twierdzenie w zadna strone.
                if status == "resolved":
                    naprawiona = szczegoly.get("fixed_version")
                    # "0" to umowny zapis "to wydanie nigdy nie bylo podatne".
                    if not naprawiona or naprawiona == "0":
                        continue
                elif status == "open":
                    naprawiona = None
                else:
                    continue
                powod = szczegoly.get("nodsa") if status == "open" else None
                wpisy.append(
                    {
                        "package": pakiet,
                        "cve": cve,
                        "release": wydanie,
                        "fixed_version": naprawiona,
                        "status": status,
                        "severity": szczegoly.get("urgency"),
                        "no_fix_reason": (powod or None) and str(powod)[:255],
                        "description": (opis.get("description") or "")[:1000] or None,
                    }
                )
    return wpisy


def wpisy_ubuntu(surowe: bytes, wydania: set[str]) -> list[dict]:
    """Wpisy z bazy biuletynow Ubuntu (USN).

    Bierzemy sekcje "sources", a nie "allbinaries". Pakiety binarne z jednego
    zrodla maja wersje w roznych schematach - z zrodla "e2fsprogs" powstaje
    binarny "comerr-dev" w wersji "2.1-1.46.5-...". Porownywanie ich ze soba
    dawalo poprawke rzekomo nowsza niz zainstalowana wersja, czyli falszywy
    alarm na pakiecie, ktory byl aktualny. Wersja zrodlowa jest jedna i to
    z nia porownuje sie dane Debiana, wiec obie dystrybucje traktujemy
    tak samo.

    Ta sama poprawka bywa w kilku biuletynach - zostawiamy NAJNOWSZA wersje
    naprawiona, bo starsza nie zamknelaby luki.
    """
    try:
        dane = json.loads(bz2.decompress(surowe))
    except (ValueError, OSError) as exc:
        raise BladKanalu(f"nie moge odczytac kanalu Ubuntu: {exc}") from exc

    najlepsze: dict[tuple[str, str, str], dict] = {}
    for biuletyn in dane.values():
        cves = [c for c in (biuletyn.get("cves") or []) if str(c).startswith("CVE-")]
        if not cves:
            continue
        for wydanie, szczegoly in (biuletyn.get("releases") or {}).items():
            if wydanie not in wydania:
                continue
            for pakiet, opis in (szczegoly.get("sources") or {}).items():
                wersja = opis.get("version")
                if not pakiet or not wersja:
                    continue
                for cve in cves:
                    klucz = (pakiet, cve, wydanie)
                    obecne = najlepsze.get(klucz)
                    if obecne is None or wersje_pakietow.starsza_niz(
                        obecne["fixed_version"], wersja
                    ):
                        najlepsze[klucz] = {
                            "package": pakiet,
                            "cve": cve,
                            "release": wydanie,
                            "fixed_version": wersja,
                            "status": "resolved",
                            "severity": None,
                            "no_fix_reason": None,
                            "description": (opis.get("description") or "")[:1000] or None,
                        }
    return list(najlepsze.values())


# Kryterium OVAL Red Hata: "openssl-libs is earlier than 1:3.0.7-27.el9".
_WCZESNIEJSZY_NIZ = re.compile(r"^(?P<pakiet>\S+) is earlier than (?P<wersja>\S+)$")


def _nazwa(element: ET.Element) -> str:
    """Nazwa znacznika bez przestrzeni nazw XML."""
    return element.tag.rsplit("}", 1)[-1]


def wpisy_rhel(surowe: bytes, wydanie: str) -> list[dict]:
    """Wpisy z OVAL v2 Red Hata dla jednego wydania RHEL.

    Trzy roznice wobec Debiana i Ubuntu, wszystkie wynikaja z danych:

    * Red Hat opisuje pakiety BINARNE ("openssl-libs"), nie zrodlowe - i po
      binarnych dopasowujemy.
    * Wersja naprawiona ma epoke ("1:3.0.7-27.el9") i porownuje sie ja
      regulami RPM, nie dpkg.
    * Plik bez "including-unpatched" zawiera wylacznie wydane poprawki, wiec
      kategorii "bez poprawki" dla RHEL nie ma - to nie znaczy, ze takich luk
      nie ma, tylko ze tego kanalu o nie nie pytamy.

    Czytamy go strumieniowo: rozpakowany OVAL RHEL 8 ma kilkaset megabajtow,
    a potrzebujemy wylacznie komentarzy kryteriow z sekcji definicji.
    """
    try:
        strumien = bz2.open(io.BytesIO(surowe))
        najlepsze: dict[tuple[str, str], dict] = {}
        glebokosc = 0
        for zdarzenie, element in ET.iterparse(strumien, events=("start", "end")):
            if zdarzenie == "start":
                glebokosc += 1
                continue
            glebokosc -= 1
            if _nazwa(element) == "definition":
                _definicja_rhel(element, wydanie, najlepsze)
                element.clear()
            elif glebokosc == 2:
                # Testy, obiekty i stany nie sa nam potrzebne - zwalniamy je
                # od razu, inaczej caly dokument zostalby w pamieci.
                element.clear()
    except (ET.ParseError, OSError, EOFError, ValueError) as exc:
        raise BladKanalu(f"nie moge odczytac kanalu RHEL {wydanie}: {exc}") from exc
    return list(najlepsze.values())


# Kryterium strumienia modulu: "Module php:8.3 is enabled".
_MODUL = re.compile(r"^Module (?P<modul>\S+:\S+) is enabled$")


def _kryteria_rhel(kryteria: ET.Element, modul: str | None,
                   wynik: list[tuple[str, str, str | None]]) -> None:
    """Zbiera (pakiet, wersja, strumien modulu) z drzewa kryteriow.

    Warunek "Module php:8.3 is enabled" stoi obok kryteriow pakietow w tej
    samej grupie - dotyczy tej grupy i wszystkiego, co w niej zagniezdzone.
    """
    for dziecko in kryteria:
        if _nazwa(dziecko) == "criterion":
            trafienie = _MODUL.match((dziecko.get("comment") or "").strip())
            if trafienie:
                modul = trafienie.group("modul")
    for dziecko in kryteria:
        nazwa = _nazwa(dziecko)
        if nazwa == "criterion":
            trafienie = _WCZESNIEJSZY_NIZ.match((dziecko.get("comment") or "").strip())
            if trafienie:
                wynik.append((trafienie.group("pakiet"), trafienie.group("wersja"), modul))
        elif nazwa == "criteria":
            _kryteria_rhel(dziecko, modul, wynik)


def _cvss_rhel(atrybut: str | None) -> tuple[float | None, str | None]:
    """Atrybut cvss3 z OVAL: "7.5/CVSS:3.1/AV:N/AC:L/...". """
    if not atrybut:
        return None, None
    ocena, _, wektor = atrybut.partition("/")
    try:
        return float(ocena), (wektor or None)
    except ValueError:
        return None, None


def _definicja_rhel(definicja: ET.Element, wydanie: str,
                    najlepsze: dict[tuple[str, str], dict]) -> None:
    if definicja.get("class") != "patch":
        return
    tytul = ""
    waga_biuletynu = None
    cves: dict[str, dict] = {}
    kryteria: list[tuple[str, str, str | None]] = []
    for element in definicja:
        if _nazwa(element) == "criteria":
            _kryteria_rhel(element, None, kryteria)
    for element in definicja.iter():
        nazwa = _nazwa(element)
        if nazwa == "title" and not tytul:
            tytul = (element.text or "").strip()
        elif nazwa == "severity":
            waga_biuletynu = (element.text or "").strip() or None
        elif nazwa == "cve":
            numer = (element.text or "").strip()
            if numer.startswith("CVE-"):
                ocena, wektor = _cvss_rhel(element.get("cvss3"))
                cves[numer] = {"waga": (element.get("impact") or "").strip() or None,
                               "ocena": ocena, "wektor": wektor,
                               "cwe": ",".join(f"CWE-{n}" for n in dict.fromkeys(
                                   _NUMER_CWE.findall(element.get("cwe") or ""))) or None}
        elif nazwa == "reference" and element.get("source") == "CVE":
            numer = (element.get("ref_id") or "").strip()
            if numer.startswith("CVE-"):
                cves.setdefault(numer, {"waga": None, "ocena": None, "wektor": None, "cwe": None})
    if not cves or not kryteria:
        return

    for pakiet, wersja, modul in kryteria:
        # Poprawka z modulu dotyczy jednego strumienia - klucz niesie strumien,
        # zeby php:8.2 i php:8.3 byly osobnymi wpisami, a nie nadpisywaly sie.
        klucz_pakietu = f"{pakiet}@{modul}" if modul else pakiet
        for cve, dane in cves.items():
            klucz = (klucz_pakietu, cve)
            nowy = {
                "package": klucz_pakietu,
                "cve": cve,
                "release": wydanie,
                "fixed_version": wersja,
                "status": "resolved",
                "severity": (dane["waga"] or waga_biuletynu or "").capitalize() or None,
                "no_fix_reason": None,
                "description": tytul[:1000] or None,
                "cvss_score": dane["ocena"],
                "cvss_vector": dane["wektor"],
                "cwe": dane["cwe"],
            }
            obecny = najlepsze.get(klucz)
            if obecny is None or _lepsza_poprawka_rhel(nowy["fixed_version"], obecny["fixed_version"]):
                najlepsze[klucz] = nowy


def _lepsza_poprawka_rhel(nowa: str, obecna: str) -> bool:
    """Ktora z dwoch poprawek tego samego pakietu zostawic.

    Wersje z modulow ("...module+el9.2.0+...") dotycza konkretnego strumienia
    (nodejs:18, postgresql:15) i obowiazuja tylko przy nim, wiec wpis spoza
    modulu ma pierwszenstwo. W obrebie jednego rodzaju zostaje najnowsza
    poprawka, bo starsza nie zamknelaby luki.
    """
    modul_nowa, modul_obecna = _z_modulu(nowa), _z_modulu(obecna)
    if modul_nowa != modul_obecna:
        return not modul_nowa
    return wersje_pakietow.porownaj_rpm(obecna, nowa) < 0


def _z_modulu(wersja: str | None) -> bool:
    return ".module" in (wersja or "")


# --- zapis do bazy ----------------------------------------------------------

def zapisz_kanal(db: Session, source: str, release: str, wpisy: list[dict]) -> int:
    """Podmienia wpisy kanalu dla wydania. Zwraca liczbe zapisanych."""
    db.execute(
        delete(CveEntry).where(CveEntry.source == source, CveEntry.release == release)
    )
    # Duplikaty w kanale zdarzaja sie - klucz (pakiet, cve) musi byc jeden.
    unikalne: dict[tuple[str, str], dict] = {}
    for w in wpisy:
        unikalne[(w["package"], w["cve"])] = w

    db.bulk_save_objects(
        [
            CveEntry(
                source=source,
                release=release,
                package=w["package"],
                cve=w["cve"],
                fixed_version=w["fixed_version"],
                status=w["status"],
                severity=w["severity"],
                no_fix_reason=w.get("no_fix_reason"),
                description=w["description"],
                cvss_score=w.get("cvss_score"),
                cvss_vector=w.get("cvss_vector"),
                cwe=w.get("cwe"),
            )
            for w in unikalne.values()
        ]
    )
    _zapisz_stan(db, source, release, STATUS_OK, None, len(unikalne))
    db.commit()
    log.info("kanal %s/%s: zapisano %d wpisow", source, release, len(unikalne))
    return len(unikalne)


def _zapisz_stan(db: Session, source: str, release: str, status: str,
                 detail: str | None, entries: int | None = None) -> None:
    stan = db.execute(
        select(CveFeed).where(CveFeed.source == source, CveFeed.release == release)
    ).scalar_one_or_none()
    if stan is None:
        stan = CveFeed(source=source, release=release)
        db.add(stan)
    stan.status = status
    stan.detail = detail
    if status == STATUS_OK:
        stan.fetched_at = utcnow()
        if entries is not None:
            stan.entries = entries


def odswiez(db: Session, wydania: dict[str, set[str]]) -> dict:
    """Pobiera kanaly dla wskazanych wydan. Blad jednego nie psuje pozostalych.

    Debian i Ubuntu publikuja jeden plik na wszystkie wydania, Red Hat - osobny
    plik na kazde glowne wydanie RHEL.
    """
    podsumowanie: dict[str, str] = {}

    for source, adres, czytnik in (
        ("debian", ADRES_DEBIAN, wpisy_debian),
        ("ubuntu", ADRES_UBUNTU, wpisy_ubuntu),
    ):
        wybrane = wydania.get(source) or set()
        if not wybrane:
            continue
        try:
            surowe = _pobierz(adres)
            wszystkie = czytnik(surowe, wybrane)
        except BladKanalu as exc:
            log.warning("kanal %s: %s", source, exc)
            for wydanie in wybrane:
                _zapisz_stan(db, source, wydanie, "blad", str(exc)[:500])
            db.commit()
            podsumowanie[source] = f"blad: {exc}"
            continue

        for wydanie in wybrane:
            zapisz_kanal(db, source, wydanie,
                         [w for w in wszystkie if w["release"] == wydanie])
        podsumowanie[source] = f"{len(wszystkie)} wpisow"

    for wydanie in sorted(wydania.get("rhel") or set()):
        klucz = f"rhel/{wydanie}"
        try:
            wpisy = wpisy_rhel(_pobierz(ADRES_RHEL.format(wydanie=wydanie)), wydanie)
        except BladKanalu as exc:
            log.warning("kanal %s: %s", klucz, exc)
            _zapisz_stan(db, "rhel", wydanie, "blad", str(exc)[:500])
            db.commit()
            podsumowanie[klucz] = f"blad: {exc}"
            continue
        zapisz_kanal(db, "rhel", wydanie, wpisy)
        podsumowanie[klucz] = f"{len(wpisy)} wpisow"

    return podsumowanie


def kanaly_do_odswiezenia(db: Session, co_ile_godzin: float) -> dict[str, set[str]]:
    """Wydania we flocie, ktorych dane sa starsze niz podany odstep.

    Obejmuje tez wydania, dla ktorych nic jeszcze nie pobrano, i te, ktorych
    ostatnie pobranie sie nie udalo - fetched_at wskazuje wtedy ostatni
    sukces, wiec proba powtarza sie przy kolejnym obiegu.
    """
    granica = utcnow() - timedelta(hours=co_ile_godzin)
    stany = {
        (k.source, k.release): as_utc(k.fetched_at)
        for k in db.execute(select(CveFeed)).scalars()
    }
    wynik: dict[str, set[str]] = {}
    for source, wydania in wydania_we_flocie(db).items():
        for wydanie in wydania:
            pobrano = stany.get((source, wydanie))
            if pobrano is None or pobrano < granica:
                wynik.setdefault(source, set()).add(wydanie)
    return wynik


# --- dopasowanie ------------------------------------------------------------

# Wersja Ubuntu jednoznacznie wyznacza nazwe kodowa - to stala tabela, a nie
# domysl. Sluzy maszynom z agentem starszym niz 0.5.4, ktory nie przysylal
# jeszcze VERSION_CODENAME.
UBUNTU_NAZWY = {
    "18.04": "bionic", "20.04": "focal", "22.04": "jammy",
    "24.04": "noble", "24.10": "oracular", "25.04": "plucky",
}

# Debian podaje nazwe kodowa wprost w nazwie systemu: "Debian GNU/Linux 12
# (bookworm)". Odczytanie jej z nawiasu to odczyt, nie zgadywanie.
_NAWIAS = re.compile(r"\(([a-z]+)\)")
_WERSJA_UBUNTU = re.compile(r"(\d+\.\d+)")
_GLOWNA_WERSJA = re.compile(r"^(\d+)")

# Systemy, ktore dopasowujemy do danych Red Hata. Rocky, Alma i CentOS
# przebudowuja pakiety RHEL z tych samych zrodel i z tymi samymi numerami
# wersji, wiec poprawka "3.0.7-27.el9_4" oznacza u nich to samo. Swiadomie
# NIE ma tu Oracle Linux ani Fedory: pierwszy wydaje wlasne poprawki z innymi
# numerami, druga to zupelnie inne wydania.
RODZINA_RHEL = {"rhel", "centos", "rocky", "almalinux"}


def wydanie_maszyny(payload: dict) -> tuple[str, str] | None:
    """Zwraca (zrodlo, wydanie) dla maszyny albo None, gdy nie rozpoznajemy.

    Pierwszenstwo ma to, co poda agent - czyta ID i VERSION_CODENAME wprost
    z /etc/os-release, wiec nie ma tam miejsca na pomylke. Dla starszych
    agentow schodzimy do nazwy wyswietlanej, ale wylacznie tam, gdzie da sie
    ja odczytac jednoznacznie. Zla nazwa kodowa oznaczalaby porownanie
    z danymi innego wydania, czyli wyniki gorsze niz zadne.
    """
    system = payload.get("os") or {}
    identyfikator = (system.get("distro_id") or "").strip().lower()
    nazwa_kodowa = (system.get("codename") or "").strip().lower()
    if identyfikator in {"debian", "ubuntu"} and nazwa_kodowa:
        return identyfikator, nazwa_kodowa

    nazwa = (system.get("name") or "").strip()
    niska = nazwa.lower()
    # RHEL nie ma nazw kodowych - wydanie to glowny numer wersji ("9.4" -> "9"),
    # bo Red Hat publikuje jeden kanal na cale wydanie glowne.
    if identyfikator in RODZINA_RHEL or "red hat enterprise linux" in niska:
        dopasowanie = _GLOWNA_WERSJA.match((system.get("version") or "").strip())
        if dopasowanie:
            return "rhel", dopasowanie.group(1)
        return None

    if "debian" in niska:
        dopasowanie = _NAWIAS.search(niska)
        if dopasowanie:
            return "debian", dopasowanie.group(1)
    elif "ubuntu" in niska:
        dopasowanie = _WERSJA_UBUNTU.search(system.get("version") or nazwa)
        if dopasowanie:
            kodowa = UBUNTU_NAZWY.get(dopasowanie.group(1))
            if kodowa:
                return "ubuntu", kodowa
    return None


# Zrodla jadra w Debianie i Ubuntu: "linux", "linux-aws", "linux-hwe-6.8",
# "linux-signed-oem" i podobne. Wszystkie zaczynaja sie od "linux".
_ZRODLA_JADRA = re.compile(r"^linux(-|$)")


def _to_jadro(pakiet_zrodlowy: str) -> bool:
    return bool(_ZRODLA_JADRA.match((pakiet_zrodlowy or "").lower()))


# Pakiety jadra RHEL instalowane rownolegle w kilku wersjach (installonly):
# "kernel", "kernel-core", "kernel-modules-extra", "kernel-rt-core"... dnf
# trzyma domyslnie trzy jadra naraz, wiec starsze leza na dysku jak w Ubuntu.
# "kernel-tools" czy "kernel-headers" pochodza z tego samego zrodla, ale sa
# zwyklymi pakietami w jednej wersji - do nich regula nie ma zastosowania.
_JADRO_RPM = re.compile(
    r"^kernel(-rt|-64k)?(-debug)?(-core|-modules(-core|-extra|-internal)?"
    r"|-devel(-matched)?|-uki-virt)?$"
)


def _to_jadro_rpm(nazwa: str, pakiet_zrodlowy: str) -> bool:
    return bool(_JADRO_RPM.match((nazwa or "").lower()))


def _klucz_pakietu(pakiet: dict, rpm: bool) -> str:
    """Po czym szukamy pakietu w danych dystrybucji.

    Debian i Ubuntu indeksuja po pakiecie zrodlowym, Red Hat - po binarnym.
    """
    nazwa = (pakiet.get("name") or "").strip()
    if rpm:
        return nazwa
    return (pakiet.get("source_package") or nazwa).strip()


def _strumien_pasuje(modul: str, wersja: str, zainstalowane: dict[str, str]) -> bool:
    """Czy na maszynie jest wlaczony strumien "php:8.3" modulu.

    Agent nie raportuje wlaczonych strumieni, wiec poznajemy go po wersji
    glownego pakietu modulu: php 8.3.19 to strumien 8.3, nodejs 20.11 - 20.
    Pakiety poboczne (php-pecl-apcu 5.1.23) maja wlasne numery, dlatego
    patrzymy na pakiet nazwany jak modul, a tylko gdy go nie ma - na wlasna
    wersje pakietu. Niepewnosc konczy sie brakiem dopasowania: poprawka dla
    php 8.3 zgloszona na maszynie z php 8.2 byla falszywym alarmem.
    """
    nazwa, _, strumien = modul.partition(":")
    if not strumien:
        return True
    _, gorna, _ = wersje_pakietow.rozbierz_rpm(zainstalowane.get(nazwa) or wersja)
    return gorna == strumien or gorna.startswith(strumien + ".")


def _modul_pasuje(zainstalowana: str, naprawiona: str | None) -> bool:
    """Czy poprawka z modulu RHEL dotyczy zainstalowanego pakietu.

    Poprawka dla strumienia nodejs:20 ("1:20.11.1-1.module+el9...") nie
    dotyczy nodejs:18, choc wersja 18 jest "starsza". Bez tej reguly kazdy
    starszy strumien modulu bylby zglaszany jako podatny. Strumien poznajemy
    po glownym numerze wersji - to przyblizenie, ale bezpieczne: przy
    niepewnosci wpis z modulu po prostu nie jest dopasowany.
    """
    if not _z_modulu(naprawiona):
        return True
    if not _z_modulu(zainstalowana):
        return False
    _, wersja_a, _ = wersje_pakietow.rozbierz_rpm(zainstalowana)
    _, wersja_b, _ = wersje_pakietow.rozbierz_rpm(naprawiona or "")
    return wersja_a.split(".")[0] == wersja_b.split(".")[0]


def dopasuj(db: Session, payload: dict) -> dict:
    """Podatnosci maszyny na podstawie jej ostatniego raportu."""
    wydanie = wydanie_maszyny(payload)
    if wydanie is None:
        return _wynik(
            STATUS_NIEZNANY,
            "nie rozpoznaje dystrybucji tej maszyny - obslugiwane sa Debian, "
            "Ubuntu oraz RHEL z pochodnymi (Rocky, Alma, CentOS); agent musi "
            "podac ID i wersje z /etc/os-release (od wersji 0.5.4)",
        )
    source, release = wydanie

    stan = db.execute(
        select(CveFeed).where(CveFeed.source == source, CveFeed.release == release)
    ).scalar_one_or_none()
    if stan is None or stan.fetched_at is None:
        return _wynik(STATUS_NIEZNANY, f"nie pobrano jeszcze danych dla {source}/{release}")

    pakiety = (payload.get("software") or {}).get("packages") or []
    if not pakiety:
        return _wynik(STATUS_NIEZNANY, "raport nie zawiera listy pakietow")

    rpm = source == "rhel"

    # Jedno zapytanie na maszyne zamiast jednego na pakiet.
    klucze = {_klucz_pakietu(p, rpm) for p in pakiety} - {""}
    warunek = CveEntry.package.in_(klucze)
    if rpm:
        # Wpisy z modulow maja klucz "php@php:8.3" - szukamy po nazwie przed "@".
        from sqlalchemy import func, or_
        warunek = or_(warunek, func.split_part(CveEntry.package, "@", 1).in_(klucze))
    wpisy = db.execute(
        select(CveEntry).where(
            CveEntry.source == source,
            CveEntry.release == release,
            warunek,
        )
    ).scalars().all()

    wedlug_pakietu: dict[str, list[CveEntry]] = {}
    for wpis in wpisy:
        wedlug_pakietu.setdefault(wpis.package.split("@", 1)[0], []).append(wpis)

    # Wersje zainstalowanych pakietow - do rozpoznania strumienia modulu
    # po jego glownym pakiecie (php, nodejs, postgresql...).
    zainstalowane = {
        (p.get("name") or "").strip(): (p.get("evr") or p.get("version") or "").strip()
        for p in pakiety
    } if rpm else {}

    # Jadro wymaga osobnego traktowania. Ubuntu zostawia stare pakiety jadra
    # zainstalowane po aktualizacji, wiec pakiety wycofanego ABI leza na dysku
    # jeszcze dlugo po tym, jak maszyna uruchomila sie z nowszego. Porownywanie
    # ich wersji zglaszalo podatnosci jadra, ktore juz nie dziala.
    dzialajace_jadro = ((payload.get("os") or {}).get("kernel") or "").strip()

    # Grupujemy po pakiecie ZRODLOWYM. Jedno zrodlo daje kilkanascie pakietow
    # binarnych - bez grupowania ta sama podatnosc jest wypisana tyle razy, ile
    # binariow jest zainstalowanych, i lista przestaje byc do przeczytania.
    grupy: dict[tuple[str, str], dict] = {}
    for pakiet in pakiety:
        nazwa = (pakiet.get("name") or "").strip()
        wersja = (pakiet.get("version") or "").strip()
        zrodlo = (pakiet.get("source_package") or nazwa).strip()
        # Porownujemy wersje ZRODLOWA, bo taka podaja dane dystrybucji.
        # Starsze agenty jej nie przysylaja - wtedy zostaje binarna, co dla
        # wiekszosci pakietow jest ta sama wartoscia.
        # Na RHEL porownujemy wersje pakietu BINARNEGO z epoka, bo tak opisuje
        # je Red Hat. Starsze agenty epoki nie podaja - wtedy zostaje
        # "wersja-wydanie", a porownanie pomija epoke (starsza_niz_rpm).
        if rpm:
            wersja_porownywana = (pakiet.get("evr") or wersja).strip()
        else:
            wersja_porownywana = (pakiet.get("source_version") or wersja).strip()
        if not nazwa or not wersja_porownywana:
            continue

        for wpis in wedlug_pakietu.get(_klucz_pakietu(pakiet, rpm), []):
            nieaktywne_jadro = False
            if wpis.status == "resolved":
                if rpm:
                    if not wersje_pakietow.starsza_niz_rpm(wersja_porownywana, wpis.fixed_version):
                        continue
                    if "@" in wpis.package:
                        if not _strumien_pasuje(wpis.package.split("@", 1)[1],
                                                wersja_porownywana, zainstalowane):
                            continue
                    elif not _modul_pasuje(wersja_porownywana, wpis.fixed_version):
                        continue
                elif not wersje_pakietow.starsza_niz(wersja_porownywana, wpis.fixed_version):
                    continue
                if rpm and _to_jadro_rpm(nazwa, zrodlo):
                    starsze = wersje_pakietow.dzialajace_jadro_starsze_rpm(
                        dzialajace_jadro, wpis.fixed_version
                    )
                    if starsze is False:
                        nieaktywne_jadro = True
                elif not rpm and _to_jadro(zrodlo):
                    starsze = wersje_pakietow.dzialajace_jadro_starsze(
                        dzialajace_jadro, wpis.fixed_version
                    )
                    # Dziala nowsze jadro niz to z poprawka - maszyna nie jest
                    # podatna teraz. Stary pakiet nadal lezy na dysku, wiec
                    # odnotowujemy to osobno, zamiast przemilczec albo straszyc.
                    if starsze is False:
                        nieaktywne_jadro = True

            klucz = (wpis.cve, zrodlo)
            grupa = grupy.get(klucz)
            if grupa is None:
                grupy[klucz] = {
                    "cve": wpis.cve,
                    "package": zrodlo,
                    "source_package": zrodlo,
                    "packages": [nazwa],
                    "installed_version": wersja,
                    "compared_version": wersja_porownywana,
                    "fixed_version": wpis.fixed_version,
                    "status": wpis.status,
                    "severity": wpis.severity,
                    "no_fix_reason": wpis.no_fix_reason,
                    # Ubuntu podaje tu opis PAKIETU ("Linux kernel"), nie luki -
                    # jako opis CVE bylby mylacy.
                    "description": wpis.description if source != "ubuntu" else None,
                    "inactive_kernel": nieaktywne_jadro,
                    "running_kernel": dzialajace_jadro or None,
                    "distro_score": wpis.cvss_score,
                    "distro_vector": wpis.cvss_vector,
                }
            else:
                if nazwa not in grupa["packages"]:
                    grupa["packages"].append(nazwa)
                # Grupa jest "tylko nieaktywnym jadrem" dopiero wtedy, gdy
                # WSZYSTKIE jej pakiety takie sa. Jeden podatny pakiet
                # dzialajacego jadra wystarczy, zeby pozycja byla prawdziwa.
                grupa["inactive_kernel"] = grupa["inactive_kernel"] and nieaktywne_jadro

    wszystkie = list(grupy.values())
    for pozycja in wszystkie:
        pozycja["packages"].sort()
        pozycja["package_count"] = len(pozycja["packages"])

    # Podatnosci juz niedzialajacego jadra NIE trafiaja na liste. Maszyna nie
    # jest przez nie podatna, a jest ich tyle, ze zaslaniaja wszystko, na co
    # faktycznie da sie zareagowac. Zostaje liczba i wersje zalegajacych
    # pakietow - to wystarczy, zeby wiedziec, ze warto posprzatac.
    nieaktywne = [z for z in wszystkie if z.get("inactive_kernel")]
    znalezione = [z for z in wszystkie if not z.get("inactive_kernel")]
    zalegajace_jadra = sorted({z["compared_version"] for z in nieaktywne})

    # Oceny doklejamy jednym zapytaniem - z bufora, bez ruchu sieciowego.
    from ..models import CveScore

    from ..models import CveUbuntu

    oceny = {}
    ubuntu = {}
    if znalezione:
        numery = {z["cve"] for z in znalezione}
        oceny = {o.cve: o for o in db.execute(
            select(CveScore).where(CveScore.cve.in_(numery))).scalars()}
        if source == "ubuntu":
            ubuntu = {o.cve: o for o in db.execute(
                select(CveUbuntu).where(CveUbuntu.cve.in_(numery))).scalars()}
    for z in znalezione:
        ocena = oceny.get(z["cve"])
        u = ubuntu.get(z["cve"])
        z["base_score"] = ocena.base_score if ocena else None
        z["cvss_severity"] = ocena.severity if ocena else None
        z["vector"] = ocena.vector if ocena else None
        z["score_source"] = "NVD" if z["base_score"] is not None else None
        # NVD ocenia swieze CVE z opoznieniem - wtedy ocena Ubuntu.
        if z["base_score"] is None and u and u.base_score is not None:
            z["base_score"], z["cvss_severity"], z["vector"] = u.base_score, u.severity, u.vector
            z["score_source"] = "Ubuntu"
        if z["base_score"] is None and z.get("distro_score") is not None:
            z["base_score"], z["vector"] = z["distro_score"], z.get("distro_vector")
            z["cvss_severity"] = _waga_cvss(z["base_score"])
            z["score_source"] = "Red Hat"
        z["ubuntu_priority"] = (u.priority or None) if u else None
        # Priorytet nadany przez dystrybucje - pokazywany obok oceny CVSS.
        if rpm and z.get("severity"):
            z["priority_label"], z["priority"] = "Red Hat", z["severity"]
        elif z["ubuntu_priority"]:
            z["priority_label"], z["priority"] = "Ubuntu", z["ubuntu_priority"].capitalize()
        else:
            z["priority_label"] = z["priority"] = None
        z["summary"] = (ocena.summary if ocena else None) or (u.summary if u else None) or None
        z["link"] = odnosnik(f"{source}/{release}", z["cve"])
        z["nvd_link"] = ODNOSNIK_NVD.format(cve=z["cve"])

    # Najpierw to, co da sie naprawic, potem najgrozniejsze. Podatnosc bez
    # oceny lezy na koncu swojej grupy, a nie udaje najlagodniejszej.
    znalezione.sort(
        key=lambda z: (
            z["status"] != "resolved",
            -(z["base_score"] if z["base_score"] is not None else -1),
            z["cve"],
        )
    )
    wiek = None
    pobrano = as_utc(stan.fetched_at)
    if pobrano:
        # Data przepuszczona przez as_utc - odejmowanie daty bez strefy
        # od daty ze strefa konczy sie TypeError.
        wiek = round((utcnow() - pobrano) / timedelta(hours=1), 1)

    return _wynik(
        STATUS_OK,
        None,
        packages_summary=_podsumowanie_pakietow(znalezione, rpm),
        entries=znalezione,
        stale_kernels=zalegajace_jadra,
        stale_kernel_count=len(nieaktywne),
        source=f"{source}/{release}",
        feed_age_hours=wiek,
        feed_entries=stan.entries,
    )


def _waga_cvss(ocena: float | None) -> str | None:
    """Nazwa progu CVSS 3 dla oceny liczbowej."""
    if ocena is None:
        return None
    if ocena >= 9.0:
        return "CRITICAL"
    if ocena >= 7.0:
        return "HIGH"
    if ocena >= 4.0:
        return "MEDIUM"
    return "LOW" if ocena > 0 else "NONE"


def _podsumowanie_pakietow(znalezione: list[dict], rpm: bool) -> list[dict]:
    """Ile podatnosci zamyka aktualizacja kazdego pakietu.

    Liczba CVE mowi malo: jedno nieaktualne jadro to setki pozycji, a zamyka
    je jedna aktualizacja i restart. Zestawienie po pakiecie zrodlowym
    pokazuje, co faktycznie trzeba zrobic i w jakiej kolejnosci.
    """
    grupy: dict[str, dict] = {}
    for z in znalezione:
        if z["status"] != "resolved":
            continue
        g = grupy.setdefault(z["source_package"], {
            "package": z["source_package"], "count": 0, "critical": 0,
            "max_score": None, "fixed_version": None,
            "kernel": _to_jadro(z["source_package"]) if not rpm
                      else z["source_package"] in {"kernel", "kernel-rt"},
            "installed_version": z["compared_version"],
        })
        g["count"] += 1
        wynik = z.get("base_score")
        if wynik is not None:
            g["max_score"] = wynik if g["max_score"] is None else max(g["max_score"], wynik)
            if wynik >= 7.0:
                g["critical"] += 1
        wersja = z.get("fixed_version")
        if wersja and (g["fixed_version"] is None or (
                wersje_pakietow.porownaj_rpm(g["fixed_version"], wersja) < 0 if rpm
                else wersje_pakietow.starsza_niz(g["fixed_version"], wersja))):
            g["fixed_version"] = wersja
    return sorted(grupy.values(),
                  key=lambda g: (-g["critical"], -g["count"], g["package"]))


def _wynik(status: str, detail: str | None, entries: list | None = None,
           source: str | None = None, feed_age_hours: float | None = None,
           feed_entries: int | None = None, stale_kernels: list | None = None,
           stale_kernel_count: int | None = None,
           packages_summary: list | None = None) -> dict:
    pozycje = entries or []
    return {
        # Pakiety do aktualizacji, od tych zamykajacych najwiecej powaznych luk.
        "packages_summary": packages_summary or [],
        "status": status,
        "detail": detail,
        "source": source,
        "feed_age_hours": feed_age_hours,
        "feed_entries": feed_entries,
        "checked_at": utcnow().isoformat(timespec="seconds"),
        # Przy stanie "nieznany" liczby sa puste, a nie zerowe: zero znaczyloby
        # "sprawdzone, nic nie ma", a tego wlasnie nie wiemy.
        "count": len(pozycje) if status == STATUS_OK else None,
        # Do zrobienia teraz: poprawka istnieje, a maszyna jej nie ma.
        "fixable_count": (
            sum(1 for p in pozycje
                if p["status"] == "resolved" and not p.get("inactive_kernel"))
            if status == STATUS_OK else None
        ),
        # Dotyczy jadra, ktore juz nie dziala - maszyna uruchomila sie
        # z nowszego, a stare pakiety tylko zalegaja na dysku. Tych pozycji
        # nie ma na liscie, wiec liczba przychodzi z zewnatrz.
        "stale_kernel_count": stale_kernel_count if status == STATUS_OK else None,
        "stale_kernels": stale_kernels or [],
        # Bez poprawki, ale dystrybucja uznaje rzecz za istotna.
        "open_count": (
            sum(1 for p in pozycje if p["status"] == "open" and not p.get("no_fix_reason"))
            if status == STATUS_OK else None
        ),
        # Bez poprawki i dystrybucja swiadomie jej nie wyda ("Minor issue").
        "minor_count": (
            sum(1 for p in pozycje if p.get("no_fix_reason")) if status == STATUS_OK else None
        ),
        # Do naprawienia i powazne wedlug CVSS - to jest lista, od ktorej
        # zaczyna sie prace.
        "critical_count": (
            sum(1 for p in pozycje
                if p["status"] == "resolved" and not p.get("inactive_kernel")
                and (p.get("base_score") or 0) >= 7.0)
            if status == STATUS_OK else None
        ),
        "bez_oceny": (
            sum(1 for p in pozycje if p.get("base_score") is None)
            if status == STATUS_OK else None
        ),
        "entries": pozycje,
    }


def wydania_we_flocie(db: Session) -> dict[str, set[str]]:
    """Ktore wydania dystrybucji sa faktycznie w uzyciu.

    Pobieramy dane tylko dla nich: kanal Debiana ma 86 MB, a wpisy dla
    wydania, ktorego nikt nie uzywa, sa czystym obciazeniem bazy.
    """
    from ..models import Asset, InventorySnapshot

    wynik: dict[str, set[str]] = {}
    podzapytanie = (
        select(InventorySnapshot.payload)
        .join(Asset, Asset.id == InventorySnapshot.asset_id)
        .where(Asset.os_family == "linux")
        .order_by(InventorySnapshot.collected_at.desc())
        .limit(500)
    )
    for (payload,) in db.execute(podzapytanie):
        wydanie = wydanie_maszyny(payload or {})
        if wydanie:
            source, release = wydanie
            wynik.setdefault(source, set()).add(release)
    return wynik


# --- oceny CVSS -------------------------------------------------------------

ADRES_NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"

# NVD dopuszcza 5 zapytan na 30 sekund bez klucza i 50 z kluczem. Odstepy
# trzymamy z zapasem - przekroczenie limitu konczy sie odmowa na kilka minut,
# czyli strata wieksza niz zysk z pospiechu.
ODSTEP_BEZ_KLUCZA = 6.5
ODSTEP_Z_KLUCZEM = 0.7

# Ile ocen pobieramy w jednym przebiegu. Bez limitu pierwsze uruchomienie
# na duzej flocie trwaloby godzine, a administrator nie wiedzialby, czy cos
# sie dzieje. Kolejne klikniecie dobiera nastepna porcje.
LIMIT_NA_PRZEBIEG = 200

# Strony opisujace podatnosc. Dystrybucja wie, co zrobila z konkretnym
# pakietem; NVD opisuje sama luke.
ODNOSNIKI = {
    "debian": "https://security-tracker.debian.org/tracker/{cve}",
    "ubuntu": "https://ubuntu.com/security/{cve}",
    "rhel": "https://access.redhat.com/security/cve/{cve}",
}
ODNOSNIK_NVD = "https://nvd.nist.gov/vuln/detail/{cve}"


def odnosnik(source: str | None, cve: str) -> str:
    """Adres strony opisujacej podatnosc u danej dystrybucji."""
    wzorzec = ODNOSNIKI.get((source or "").split("/")[0], ODNOSNIK_NVD)
    return wzorzec.format(cve=cve)


def _opis_z_odpowiedzi(wpis: dict) -> str:
    """Angielski opis luki z NVD, przyciety do rozsadnej dlugosci."""
    for opis in wpis.get("descriptions") or []:
        if opis.get("lang") == "en" and opis.get("value"):
            return " ".join(str(opis["value"]).split())[:4000]
    return ""


_NUMER_CWE = re.compile(r"CWE-(\d+)")


def _cwe_z_odpowiedzi(wpis: dict) -> str:
    """Numery slabosci z NVD: "CWE-787,CWE-20". Pomija NVD-CWE-Other/noinfo."""
    numery: list[str] = []
    for slabosc in wpis.get("weaknesses") or []:
        for opis in slabosc.get("description") or []:
            for numer in _NUMER_CWE.findall(str(opis.get("value") or "")):
                if f"CWE-{numer}" not in numery:
                    numery.append(f"CWE-{numer}")
    return ",".join(numery)[:255]


def _ocena_z_odpowiedzi(dane: dict) -> dict | None:
    """Wyciaga ocene bazowa i opis z odpowiedzi NVD.

    Bierzemy najnowsza dostepna wersje CVSS - starsze wydania maja tylko 2.0,
    nowsze 3.1 albo 4.0, a mieszanie ich w jednej kolumnie dawaloby liczby
    nieporownywalne miedzy soba bez podania wersji.

    None, gdy NVD nie zna tego CVE. Znany CVE bez oceny zwraca sam opis
    (base_score None) - swieze luki dostaja ocene po kilku dniach.
    """
    podatnosci = dane.get("vulnerabilities") or []
    if not podatnosci:
        return None
    wpis = podatnosci[0].get("cve") or {}
    wynik_bazowy = {
        "base_score": None, "severity": None, "vector": None,
        "published": (wpis.get("published") or "")[:10] or None,
        "summary": _opis_z_odpowiedzi(wpis),
        "cwe": _cwe_z_odpowiedzi(wpis),
    }
    metryki = wpis.get("metrics") or {}
    for klucz in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for pozycja in metryki.get(klucz) or []:
            cvss = pozycja.get("cvssData") or {}
            wynik = cvss.get("baseScore")
            if wynik is None:
                continue
            return {
                **wynik_bazowy,
                "base_score": float(wynik),
                "severity": (cvss.get("baseSeverity") or pozycja.get("baseSeverity") or "").upper()
                or None,
                "vector": (cvss.get("vectorString") or "")[:512] or None,
            }
    return wynik_bazowy


def pobierz_oceny(db: Session, cves: list[str], klucz_api: str = "",
                  limit: int = LIMIT_NA_PRZEBIEG, postep=None) -> dict:
    """Uzupelnia brakujace oceny CVSS. Zwraca podsumowanie przebiegu."""
    from ..models import CveScore

    znane: set[str] = set()
    bez_opisu: list[str] = []
    for ocena in db.execute(select(CveScore).where(CveScore.cve.in_(cves))).scalars():
        # Oceny pobrane przed dodaniem opisow dobieramy ponownie - najpierw
        # jednak te, ktorych w ogole nie ma.
        if ocena.found and (ocena.summary is None or ocena.cwe is None):
            bez_opisu.append(ocena.cve)
        else:
            znane.add(ocena.cve)
    brakujace = [c for c in dict.fromkeys(cves) if c not in znane and c not in bez_opisu]
    brakujace += [c for c in dict.fromkeys(cves) if c in bez_opisu]
    do_pobrania = brakujace[:limit]

    naglowki = dict(NAGLOWKI)
    if klucz_api:
        naglowki["apiKey"] = klucz_api
    odstep = ODSTEP_Z_KLUCZEM if klucz_api else ODSTEP_BEZ_KLUCZA

    pobrane = puste = bledy = 0
    for numer, cve in enumerate(do_pobrania):
        if numer:
            time.sleep(odstep)
        try:
            zadanie = urllib.request.Request(
                f"{ADRES_NVD}?cveId={urllib.parse.quote(cve)}", headers=naglowki
            )
            with urllib.request.urlopen(zadanie, timeout=60) as odpowiedz:
                dane = json.loads(odpowiedz.read())
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("nie udalo sie pobrac oceny %s: %s", cve, exc)
            bledy += 1
            # Przerywamy po serii bledow - to zwykle limit zapytan albo brak
            # lacznosci, a dalsze proby tylko go pogleboia.
            if bledy >= 5:
                log.warning("przerywam pobieranie ocen po %d bledach", bledy)
                break
            continue

        ocena = _ocena_z_odpowiedzi(dane)
        if ocena is None or ocena["base_score"] is None:
            # Zapamietujemy takze brak wyniku - inaczej przy kazdym odswiezeniu
            # pytalibysmy o te same, nieopisane jeszcze podatnosci.
            db.merge(CveScore(cve=cve, found=False,
                              summary=(ocena or {}).get("summary") or None,
                              cwe=(ocena or {}).get("cwe") or ""))
            puste += 1
        else:
            db.merge(CveScore(cve=cve, found=True, **ocena))
            pobrane += 1
        if postep:
            postep(numer + 1, len(do_pobrania))
        # Zapis co kilkanascie ocen - przerwany przebieg nie traci wszystkiego.
        if numer % 20 == 19:
            db.commit()
    db.commit()

    return {
        "pobrane": pobrane,
        "bez_oceny": puste,
        "bledy": bledy,
        "pozostalo": max(0, len(brakujace) - len(do_pobrania)),
    }


def cve_we_flocie(db: Session, source: str | None = None) -> list[str]:
    """Podatnosci faktycznie dopasowane do maszyn - i tylko dla nich pobieramy
    oceny. Kanal Debiana ma 45 tysiecy wpisow, maszyny dotyczy kilkaset.

    source zaweza do maszyn jednej dystrybucji ("ubuntu").
    """
    from ..models import Asset, InventorySnapshot

    znalezione: list[str] = []
    widziane: set[str] = set()
    podzapytanie = (
        select(InventorySnapshot.payload, InventorySnapshot.asset_id)
        .join(Asset, Asset.id == InventorySnapshot.asset_id)
        .where(Asset.os_family == "linux")
        .order_by(InventorySnapshot.collected_at.desc())
    )
    obsluzone: set[str] = set()
    for payload, asset_id in db.execute(podzapytanie):
        if asset_id in obsluzone:
            continue        # tylko najnowszy raport kazdej maszyny
        obsluzone.add(asset_id)
        wynik = dopasuj(db, payload or {})
        if wynik["status"] != STATUS_OK:
            continue
        if source and (wynik["source"] or "").split("/")[0] != source:
            continue
        for pozycja in wynik["entries"]:
            if pozycja["cve"] not in widziane:
                widziane.add(pozycja["cve"])
                znalezione.append(pozycja["cve"])
    return znalezione


# --- oceny Ubuntu -------------------------------------------------------------

ADRES_UBUNTU_CVE = "https://ubuntu.com/security/cves/{cve}.json"
ODSTEP_UBUNTU = 0.5


def _ocena_ubuntu(dane: dict) -> dict:
    cvss = ((dane.get("impact") or {}).get("baseMetricV3") or {}).get("cvssV3") or {}
    wynik = cvss.get("baseScore", dane.get("cvss3"))
    opis = " ".join(str(dane.get("description") or "").split())[:1500]
    return {
        "priority": (dane.get("priority") or "").strip().lower() or "",
        "base_score": float(wynik) if wynik is not None else None,
        "severity": (cvss.get("baseSeverity") or "").upper() or None,
        "vector": (cvss.get("vectorString") or "")[:512] or None,
        "summary": opis or None,
    }


def pobierz_oceny_ubuntu(db: Session, cves: list[str], limit: int = 300,
                         postep=None) -> dict:
    """Priorytet i CVSS z ubuntu.com dla podatnosci znalezionych na Ubuntu.

    Swieze oceny sie zmieniaja (priorytet "needs-triage" dostaje wartosc po
    kilku dniach), wiec wpisy starsze niz tydzien albo bez priorytetu
    pobieramy ponownie - najpierw jednak te, ktorych nie ma wcale.
    """
    from ..models import CveUbuntu

    granica = utcnow() - timedelta(days=7)
    obecne = {o.cve: o for o in db.execute(
        select(CveUbuntu).where(CveUbuntu.cve.in_(cves))).scalars()}
    brakujace = [c for c in dict.fromkeys(cves) if c not in obecne]
    do_odswiezenia = [
        c for c, o in obecne.items()
        if o.priority in (None, "needs-triage")
        or (as_utc(o.fetched_at) or granica) < granica
    ]
    kolejka = (brakujace + do_odswiezenia)[:limit]

    pobrane = bledy = 0
    for numer, cve in enumerate(kolejka):
        if numer:
            time.sleep(ODSTEP_UBUNTU)
        try:
            zadanie = urllib.request.Request(
                ADRES_UBUNTU_CVE.format(cve=urllib.parse.quote(cve)), headers=NAGLOWKI)
            with urllib.request.urlopen(zadanie, timeout=60) as odpowiedz:
                dane = json.loads(odpowiedz.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                db.merge(CveUbuntu(cve=cve, priority="", fetched_at=utcnow()))
                continue
            bledy += 1
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("nie udalo sie pobrac oceny Ubuntu %s: %s", cve, exc)
            bledy += 1
        else:
            db.merge(CveUbuntu(cve=cve, fetched_at=utcnow(), **_ocena_ubuntu(dane)))
            pobrane += 1
            if postep:
                postep(numer + 1, len(kolejka))
            if numer % 20 == 19:
                db.commit()
            continue
        if bledy >= 5:
            log.warning("przerywam pobieranie ocen Ubuntu po %d bledach", bledy)
            break
    db.commit()
    return {"pobrane": pobrane, "bledy": bledy,
            "pozostalo": max(0, len(brakujace) + len(do_odswiezenia) - len(kolejka))}


# --- katalog slabosci MITRE CWE -------------------------------------------------

ADRES_CWE = "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip"
# Katalog zmienia sie kilka razy w roku - miesiac to az nadto czesto.
CWE_CO_ILE_DNI = 30


def wpisy_cwe(surowe: bytes) -> list[dict]:
    """Slabosci z katalogu MITRE: nazwa, opis i skutki (Common_Consequences).

    Czytamy strumieniowo - rozpakowany katalog ma kilkanascie megabajtow,
    a z kazdej slabosci potrzebujemy kilku pol.
    """
    import zipfile

    try:
        archiwum = zipfile.ZipFile(io.BytesIO(surowe))
        nazwa_pliku = next(n for n in archiwum.namelist() if n.endswith(".xml"))
        wynik: list[dict] = []
        with archiwum.open(nazwa_pliku) as plik:
            for _, element in ET.iterparse(plik, events=("end",)):
                if _nazwa(element) != "Weakness":
                    continue
                opis = ""
                skutki = []
                for dziecko in element:
                    nazwa = _nazwa(dziecko)
                    if nazwa == "Description":
                        opis = " ".join("".join(dziecko.itertext()).split())
                    elif nazwa == "Common_Consequences":
                        for skutek in dziecko:
                            if _nazwa(skutek) != "Consequence":
                                continue
                            pola: dict[str, list[str]] = {"Scope": [], "Impact": [], "Note": []}
                            for pole in skutek:
                                if _nazwa(pole) in pola:
                                    tekst = " ".join("".join(pole.itertext()).split())
                                    if tekst:
                                        pola[_nazwa(pole)].append(tekst)
                            skutki.append({"scopes": pola["Scope"], "impacts": pola["Impact"],
                                           "note": " ".join(pola["Note"])[:1500] or None})
                if element.get("ID"):
                    wynik.append({"id": element.get("ID"), "name": (element.get("Name") or "")[:255],
                                  "description": opis[:4000] or None, "consequences": skutki})
                element.clear()
        return wynik
    except (zipfile.BadZipFile, StopIteration, ET.ParseError, OSError, KeyError) as exc:
        raise BladKanalu(f"nie moge odczytac katalogu CWE: {exc}") from exc


def odswiez_cwe(db: Session) -> str | None:
    """Pobiera katalog CWE, gdy go nie ma albo ma ponad miesiac. Zwraca opis."""
    from ..models import CweEntry

    stan = db.execute(
        select(CveFeed).where(CveFeed.source == "cwe", CveFeed.release == "mitre")
    ).scalar_one_or_none()
    pobrano = as_utc(stan.fetched_at) if stan else None
    if pobrano and utcnow() - pobrano < timedelta(days=CWE_CO_ILE_DNI):
        return None
    try:
        wpisy = wpisy_cwe(_pobierz(ADRES_CWE))
    except BladKanalu as exc:
        log.warning("katalog CWE: %s", exc)
        _zapisz_stan(db, "cwe", "mitre", "blad", str(exc)[:500])
        db.commit()
        return f"blad: {exc}"
    db.execute(delete(CweEntry))
    db.bulk_save_objects([CweEntry(**w) for w in wpisy])
    _zapisz_stan(db, "cwe", "mitre", STATUS_OK, None, len(wpisy))
    db.commit()
    return f"{len(wpisy)} slabosci"


# --- szczegoly jednej podatnosci ------------------------------------------------

_NUMER_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")


def szczegoly_cve(db: Session, numer: str, zrodlo: str | None = None) -> dict | None:
    """Wszystko, co wiemy o jednym CVE - do okna szczegolow.

    zrodlo ("ubuntu/noble", "rhel/9") dobiera dane dystrybucji maszyny, z ktorej
    otwarto okno: jej wage, ocene i odnosnik.
    """
    from ..models import CveScore, CveUbuntu, CweEntry

    numer = (numer or "").strip().upper()
    if not _NUMER_CVE.match(numer):
        return None
    dystrybucja, _, wydanie = (zrodlo or "").partition("/")
    nvd = db.get(CveScore, numer)
    ubuntu = db.get(CveUbuntu, numer)
    wpis = None
    if dystrybucja and wydanie:
        wpis = db.execute(
            select(CveEntry).where(CveEntry.source == dystrybucja, CveEntry.release == wydanie,
                                   CveEntry.cve == numer).limit(1)
        ).scalar_one_or_none()

    ocena, waga, wektor, zrodlo_oceny = None, None, None, None
    if nvd and nvd.base_score is not None:
        ocena, waga, wektor, zrodlo_oceny = nvd.base_score, nvd.severity, nvd.vector, "NVD"
    elif ubuntu and ubuntu.base_score is not None:
        ocena, waga, wektor, zrodlo_oceny = ubuntu.base_score, ubuntu.severity, ubuntu.vector, "Ubuntu"
    elif wpis and wpis.cvss_score is not None:
        ocena, wektor, zrodlo_oceny = wpis.cvss_score, wpis.cvss_vector, "Red Hat"
        waga = _waga_cvss(ocena)

    priorytet = None
    if dystrybucja == "rhel" and wpis and wpis.severity:
        priorytet = ("Red Hat", wpis.severity)
    elif ubuntu and ubuntu.priority:
        priorytet = ("Ubuntu", ubuntu.priority.capitalize())
    elif dystrybucja == "debian" and wpis and wpis.severity and wpis.severity != "not yet assigned":
        priorytet = ("Debian", wpis.severity)

    opis = (nvd.summary if nvd else None) or (ubuntu.summary if ubuntu else None)
    if not opis and wpis and dystrybucja != "ubuntu":
        opis = wpis.description

    numery_cwe: list[str] = []
    for zrodlo_cwe in ((nvd.cwe if nvd else None), (wpis.cwe if wpis else None)):
        for n in _NUMER_CWE.findall(zrodlo_cwe or ""):
            if n not in numery_cwe:
                numery_cwe.append(n)
    katalog = {c.id: c for c in db.execute(
        select(CweEntry).where(CweEntry.id.in_(numery_cwe))).scalars()} if numery_cwe else {}
    slabosci = []
    for n in numery_cwe:
        c = katalog.get(n)
        slabosci.append({
            "id": f"CWE-{n}", "name": c.name if c else None,
            "description": c.description if c else None,
            "consequences": (c.consequences or []) if c else [],
            "link": f"https://cwe.mitre.org/data/definitions/{n}.html",
        })

    return {
        "cve": numer,
        "published": nvd.published if nvd else None,
        "base_score": ocena, "severity": waga, "vector": wektor, "score_source": zrodlo_oceny,
        "priority": priorytet,
        "description": opis,
        "advisory": wpis.description if (wpis and dystrybucja == "rhel") else None,
        "fixed_version": wpis.fixed_version if wpis else None,
        "weaknesses": slabosci,
        "link": odnosnik(zrodlo, numer) if dystrybucja else None,
        "distro": {"debian": "Debian", "ubuntu": "Ubuntu", "rhel": "Red Hat"}.get(dystrybucja),
        "nvd_link": ODNOSNIK_NVD.format(cve=numer),
    }
