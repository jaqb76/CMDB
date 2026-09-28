"""Podatnosci Windows z danych Microsoftu (MSRC, format CVRF).

Windows nie ma listy pakietow, ktora dalo by sie porownac z kanalem jak na
Linuksie. Microsoft laci system zbiorczymi aktualizacjami, a kazda podnosi
numer poprawki kompilacji (UBR): 10.0.20348.2700. Biuletyn MSRC podaje przy
kazdym CVE i kazdym produkcie kompilacje, w ktorej luke naprawiono
("FixedBuild"). Maszyna jest podatna, gdy jej kompilacja jest od niej nizsza.

Dlaczego po numerze kompilacji, a nie po nazwie produktu: nazwy w MSRC
("Windows 11 Version 23H2 for x64-based Systems") i nazwy z WMI ("Microsoft
Windows 11 Pro") nie maja wspolnego klucza, a numer kompilacji wydania
(22631) jest ten sam po obu stronach. Klient i serwer z ta sama kompilacja
(Windows 11 24H2 i Server 2025 - 26100) to rozne produkty, wiec w kluczu
jest jeszcze klasa: "26100-client", "26100-server".

Aktualizacje sa zbiorcze: najnowsza zamyka wszystko, co naprawily
poprzednie. Dlatego lista "co zainstalowac" dla Windows ma jedna pozycje.

Czego to NIE obejmuje: Office, .NET, przegladarki i innych produktow
Microsoftu - maja wlasne numery wersji, ktorych agent nie zbiera w sposob
pozwalajacy na porownanie. Liczymy wylacznie sam system.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import CveEntry, CveFeed, utcnow

log = logging.getLogger(__name__)

ZRODLO = "msrc"
# Wpisy CveFeed z tym zrodlem pamietaja przetworzone biuletyny miesieczne -
# biuletyn bez zmian nie jest pobierany ponownie (kazdy ma kilka MB).
ZRODLO_BIULETYNOW = "msrc-biuletyn"

ADRES_LISTY = "https://api.msrc.microsoft.com/cvrf/v3.0/updates"
ADRES_BIULETYNU = "https://api.msrc.microsoft.com/cvrf/v3.0/cvrf/{id}"
NAGLOWKI = {"Accept": "application/json"}

# Ile miesiecy wstecz. Maszyna nieaktualizowana od roku jest podatna na
# wszystko, co naprawiono przez ten rok - krotsze okno zanizaloby wynik
# wlasnie tam, gdzie jest najgorzej.
MIESIECY = 24

# CVRF: typ zagrozenia 3 to waga ("Critical"), typ poprawki 2 to poprawka
# producenta (w odroznieniu od obejscia albo lagodzenia).
ZAGROZENIE_WAGA = 3
POPRAWKA_PRODUCENTA = 2

_KOMPILACJA = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


def klucz_kompilacji(tekst: str | None) -> tuple[int, ...] | None:
    """"10.0.20348.2700" -> (10, 0, 20348, 2700); cokolwiek innego -> None."""
    dopasowanie = _KOMPILACJA.match((tekst or "").strip())
    return tuple(int(c) for c in dopasowanie.groups()) if dopasowanie else None


def klasa_produktu(nazwa: str) -> str:
    return "server" if "server" in (nazwa or "").lower() else "client"


def _produkt_systemu(nazwa: str) -> bool:
    """Sam system, a nie ".NET Framework 4.8 on Windows Server 2022"."""
    return (nazwa or "").startswith("Windows ") and " on Windows" not in nazwa


def wydanie_maszyny(payload: dict) -> str | None:
    """Klucz wydania ("20348-server") albo None, gdy to nie Windows."""
    system = payload.get("os") or {}
    nazwa = system.get("name") or ""
    # Identyfikator dystrybucji podaje wylacznie agent linuksowy.
    if "windows" not in nazwa.lower() or system.get("distro_id"):
        return None
    kompilacja = str(system.get("build") or "").strip()
    if not kompilacja.isdigit():
        # Starsze raporty: "10.0.20348" w polu version.
        czesci = str(system.get("version") or "").split(".")
        kompilacja = czesci[2] if len(czesci) >= 3 and czesci[2].isdigit() else ""
    if not kompilacja:
        return None
    return f"{kompilacja}-{klasa_produktu(nazwa)}"


def kompilacja_maszyny(payload: dict) -> str | None:
    """Pelna kompilacja "10.0.20348.2700" albo None, gdy agent nie podal UBR."""
    system = payload.get("os") or {}
    poprawka = str(system.get("build_revision") or "").strip()
    wersja = str(system.get("version") or "").strip().split(".")
    kompilacja = str(system.get("build") or "").strip() or (wersja[2] if len(wersja) >= 3 else "")
    if not poprawka.isdigit() or not kompilacja.isdigit():
        return None
    glowna = ".".join(wersja[:2]) if len(wersja) >= 2 and all(c.isdigit() for c in wersja[:2]) else "10.0"
    return f"{glowna}.{kompilacja}.{poprawka}"


# --- odczyt biuletynu ---------------------------------------------------------

def _wartosc(pole) -> str:
    if isinstance(pole, dict):
        return str(pole.get("Value") or "").strip()
    return str(pole or "").strip()


def wpisy_biuletynu(dane: dict) -> list[dict]:
    """Wpisy "CVE w wydaniu W naprawione w kompilacji K przez KB" z biuletynu."""
    produkty = {
        str(p.get("ProductID")): _wartosc(p.get("Value") if "Value" in p else p)
        for p in ((dane.get("ProductTree") or {}).get("FullProductName") or [])
    }
    wpisy: list[dict] = []
    for podatnosc in dane.get("Vulnerability") or []:
        numer = str(podatnosc.get("CVE") or "").strip().upper()
        if not numer.startswith("CVE-"):
            continue
        tytul = _wartosc(podatnosc.get("Title"))
        wagi = {}
        for zagrozenie in podatnosc.get("Threats") or []:
            if zagrozenie.get("Type") == ZAGROZENIE_WAGA:
                for produkt in zagrozenie.get("ProductID") or []:
                    wagi[str(produkt)] = _wartosc(zagrozenie.get("Description"))
        oceny = {}
        for zestaw in podatnosc.get("CVSSScoreSets") or []:
            for produkt in zestaw.get("ProductID") or []:
                oceny[str(produkt)] = (zestaw.get("BaseScore"), zestaw.get("Vector"))

        for poprawka in podatnosc.get("Remediations") or []:
            if poprawka.get("Type") != POPRAWKA_PRODUCENTA:
                continue
            kompilacja = klucz_kompilacji(poprawka.get("FixedBuild"))
            # Tylko system: jego kompilacje zaczynaja sie od 6.x albo 10.0.
            if kompilacja is None or kompilacja[0] not in (6, 10):
                continue
            kb = _wartosc(poprawka.get("Description"))
            if not kb.isdigit():
                continue
            for produkt in poprawka.get("ProductID") or []:
                nazwa = produkty.get(str(produkt), "")
                if not _produkt_systemu(nazwa):
                    continue
                ocena, wektor = oceny.get(str(produkt), (None, None))
                try:
                    ocena = float(ocena) if ocena not in (None, "") else None
                except (TypeError, ValueError):
                    ocena = None
                wpisy.append({
                    "release": f"{kompilacja[2]}-{klasa_produktu(nazwa)}",
                    "package": f"KB{kb}",
                    "cve": numer,
                    "fixed_version": ".".join(str(c) for c in kompilacja),
                    "status": "resolved",
                    "severity": wagi.get(str(produkt)) or None,
                    "description": tytul or None,
                    "cvss_score": ocena,
                    "cvss_vector": (str(wektor)[:512] if wektor else None),
                    "product": nazwa,
                })
    return wpisy


def zapisz_biuletyn(db: Session, identyfikator: str, wpisy: list[dict],
                    wersja: str | None) -> int:
    """Podmienia wpisy biuletynu. Wpis to (wydanie, KB, CVE) - klucz tabeli."""
    # Kasujemy tylko pary (CVE, KB) z tego biuletynu. To samo CVE bywa
    # wznowione w kolejnym miesiacu z nowym KB - tamte wpisy maja zostac.
    numery = {w["cve"] for w in wpisy}
    poprawki = {w["package"] for w in wpisy}
    if numery:
        db.execute(delete(CveEntry).where(CveEntry.source == ZRODLO, CveEntry.cve.in_(numery),
                                          CveEntry.package.in_(poprawki)))
    unikalne: dict[tuple[str, str, str], dict] = {}
    for w in wpisy:
        unikalne[(w["release"], w["package"], w["cve"])] = w
    db.bulk_save_objects([
        CveEntry(source=ZRODLO, release=w["release"], package=w["package"], cve=w["cve"],
                 fixed_version=w["fixed_version"], status=w["status"], severity=w["severity"],
                 description=w["description"], cvss_score=w["cvss_score"],
                 cvss_vector=w["cvss_vector"])
        for w in unikalne.values()
    ])
    stan = db.execute(select(CveFeed).where(
        CveFeed.source == ZRODLO_BIULETYNOW, CveFeed.release == identyfikator)).scalar_one_or_none()
    if stan is None:
        stan = CveFeed(source=ZRODLO_BIULETYNOW, release=identyfikator)
        db.add(stan)
    stan.status, stan.detail, stan.fetched_at, stan.entries = "ok", wersja, utcnow(), len(unikalne)
    return len(unikalne)


def _odnotuj_wydania(db: Session, status: str = "ok", detail: str | None = None,
                     wydania: set[str] | None = None) -> None:
    """Stan kanalu dla kazdego wydania - tego pilnuje dopasowanie i panel."""
    liczby = dict(db.execute(
        select(CveEntry.release, func.count(CveEntry.id))
        .where(CveEntry.source == ZRODLO).group_by(CveEntry.release)).all())
    for wydanie in (wydania or set()) | set(liczby):
        stan = db.execute(select(CveFeed).where(
            CveFeed.source == ZRODLO, CveFeed.release == wydanie)).scalar_one_or_none()
        if stan is None:
            stan = CveFeed(source=ZRODLO, release=wydanie)
            db.add(stan)
        stan.status, stan.detail = status, detail
        if status == "ok":
            stan.fetched_at = utcnow()
            stan.entries = liczby.get(wydanie, 0)


def _data(tekst: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(tekst).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def odswiez(db: Session, pobierz, wydania: set[str] | None = None) -> str:
    """Pobiera biuletyny z ostatnich MIESIECY. pobierz(adres, naglowki) -> bytes.

    Biuletyn, ktorego data zmiany sie nie zmienila, jest pomijany - pelne
    pobranie dwoch lat to kilkaset megabajtow, a zmienia sie zwykle jeden.
    """
    try:
        lista = json.loads(pobierz(ADRES_LISTY, NAGLOWKI))
        biuletyny = sorted(
            (b for b in (lista.get("value") or []) if _data(b.get("InitialReleaseDate"))),
            key=lambda b: _data(b.get("InitialReleaseDate")), reverse=True,
        )[:MIESIECY]
        znane = {k.release: k.detail for k in db.execute(
            select(CveFeed).where(CveFeed.source == ZRODLO_BIULETYNOW)).scalars()}
        pobrane = 0
        for biuletyn in biuletyny:
            identyfikator = str(biuletyn.get("ID") or "")
            wersja = str(biuletyn.get("CurrentReleaseDate") or "")
            if not identyfikator or (znane.get(identyfikator) == wersja and wersja):
                continue
            adres = biuletyn.get("CvrfUrl") or ADRES_BIULETYNU.format(id=identyfikator)
            dane = json.loads(pobierz(adres, NAGLOWKI))
            zapisz_biuletyn(db, identyfikator, wpisy_biuletynu(dane), wersja)
            db.commit()
            pobrane += 1
    except Exception as exc:  # noqa: BLE001 - blad sieci albo formatu, stare dane zostaja
        db.rollback()
        log.warning("kanal msrc: %s", exc)
        _odnotuj_wydania(db, "blad", str(exc)[:500], wydania)
        db.commit()
        return f"blad: {exc}"
    _odnotuj_wydania(db, wydania=wydania)
    db.commit()
    return f"{pobrane} nowych biuletynow z {len(biuletyny)}"


# --- dopasowanie --------------------------------------------------------------

def znajdz(db: Session, wydanie: str, kompilacja: str) -> list[dict]:
    """Podatnosci systemu w kompilacji `kompilacja` wydania `wydanie`.

    CVE bywa naprawione kilkoma KB (zwykla aktualizacja i hotpatch) z roznymi
    kompilacjami. Maszyna jest bezpieczna, jesli dogonila KTORAKOLWIEK -
    liczymy wiec od najnizszej, zeby nie zglaszac luk juz zamknietych.
    """
    obecna = klucz_kompilacji(kompilacja)
    if obecna is None:
        return []
    najnizsze: dict[str, CveEntry] = {}
    for wpis in db.execute(select(CveEntry).where(
            CveEntry.source == ZRODLO, CveEntry.release == wydanie)).scalars():
        klucz = klucz_kompilacji(wpis.fixed_version)
        if klucz is None:
            continue
        dotychczasowy = najnizsze.get(wpis.cve)
        if dotychczasowy is None or klucz < klucz_kompilacji(dotychczasowy.fixed_version):
            najnizsze[wpis.cve] = wpis

    znalezione = []
    for wpis in najnizsze.values():
        if obecna >= klucz_kompilacji(wpis.fixed_version):
            continue
        znalezione.append({
            "cve": wpis.cve,
            "package": wpis.package,
            "source_package": wpis.package,
            "packages": [wpis.package],
            "package_count": 1,
            "installed_version": kompilacja,
            "compared_version": kompilacja,
            "fixed_version": wpis.fixed_version,
            "status": "resolved",
            "severity": wpis.severity,
            "no_fix_reason": None,
            "description": wpis.description,
            "inactive_kernel": False,
            "running_kernel": None,
            "distro_score": wpis.cvss_score,
            "distro_vector": wpis.cvss_vector,
        })
    return znalezione


def podsumowanie(znalezione: list[dict], kompilacja: str) -> list[dict]:
    """Jedna pozycja: najnowsza aktualizacja zbiorcza zamyka wszystkie luki."""
    if not znalezione:
        return []
    najnowsza = max(znalezione, key=lambda z: klucz_kompilacji(z["fixed_version"]))
    oceny = [z["base_score"] for z in znalezione if z.get("base_score") is not None]
    return [{
        "package": f"Aktualizacja zbiorcza {najnowsza['package']} (lub nowsza)",
        "count": len(znalezione),
        "critical": sum(1 for o in oceny if o >= 7.0),
        "max_score": max(oceny) if oceny else None,
        "fixed_version": najnowsza["fixed_version"],
        "kernel": False,
        "installed_version": kompilacja,
        "restart": True,
    }]


def _miesiac_biuletynu(identyfikator: str) -> datetime | None:
    """"2024-Oct" -> 2024-10-01. Nazwy miesiecy MSRC sa angielskie."""
    try:
        return datetime.strptime(identyfikator, "%Y-%b")
    except ValueError:
        return None


def kontekst(db: Session, wydanie: str, kompilacja: str, payload: dict) -> dict:
    """Czego sama liczba CVE nie mowi: czy maszyna nie jest starsza niz dane
    i kiedy dostala ostatnia aktualizacje zbiorcza.

    Biuletyny siegaja MIESIECY wstecz. Maszyna, ktorej kompilacja jest nizsza
    niz najstarsza poprawka w danych, nie ma rowniez tego, co naprawiono
    wczesniej - a tego juz nie liczymy. Liczba jest wtedy zanizona i trzeba
    to powiedziec wprost, zamiast pokazywac ja jak pelny wynik.
    """
    obecna = klucz_kompilacji(kompilacja)
    wpisy = db.execute(select(CveEntry.fixed_version, CveEntry.package).where(
        CveEntry.source == ZRODLO, CveEntry.release == wydanie)).all()
    kompilacje = [(klucz_kompilacji(w), kb) for w, kb in wpisy if klucz_kompilacji(w)]
    najstarsza = min((k for k, _ in kompilacje), default=None)

    miesiace = [m for m in (_miesiac_biuletynu(k.release) for k in db.execute(
        select(CveFeed).where(CveFeed.source == ZRODLO_BIULETYNOW)).scalars()) if m]
    od = min(miesiace) if miesiace else None

    # Aktualizacja zbiorcza, ktora dala maszynie jej kompilacje - o ile jest
    # w danych - i data jej instalacji z listy poprawek Windows.
    kb = next((kb for k, kb in kompilacje if k == obecna), None)
    zainstalowano = None
    if kb:
        for poprawka in (payload.get("software") or {}).get("updates") or []:
            if str(poprawka.get("id") or "").upper() == kb.upper():
                zainstalowano = poprawka.get("installed_on")
                break
    return {
        "kompilacja": kompilacja,
        "poza_oknem": bool(obecna and najstarsza and obecna < najstarsza),
        "dane_od": od,
        "miesiecy": MIESIECY,
        "ostatnia_zbiorcza": kb,
        "zainstalowano": zainstalowano,
    }
