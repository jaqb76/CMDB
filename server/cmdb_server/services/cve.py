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
import json
import logging
import re
import urllib.error
import urllib.request
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


def odswiez(db: Session, wydania_debian: set[str], wydania_ubuntu: set[str]) -> dict:
    """Pobiera kanaly dla wskazanych wydan. Blad jednego nie psuje pozostalych."""
    podsumowanie: dict[str, str] = {}

    for source, adres, wydania, czytnik in (
        ("debian", ADRES_DEBIAN, wydania_debian, wpisy_debian),
        ("ubuntu", ADRES_UBUNTU, wydania_ubuntu, wpisy_ubuntu),
    ):
        if not wydania:
            continue
        try:
            surowe = _pobierz(adres)
            wszystkie = czytnik(surowe, wydania)
        except BladKanalu as exc:
            log.warning("kanal %s: %s", source, exc)
            for wydanie in wydania:
                _zapisz_stan(db, source, wydanie, "blad", str(exc)[:500])
            db.commit()
            podsumowanie[source] = f"blad: {exc}"
            continue

        for wydanie in wydania:
            zapisz_kanal(db, source, wydanie,
                         [w for w in wszystkie if w["release"] == wydanie])
        podsumowanie[source] = f"{len(wszystkie)} wpisow"

    return podsumowanie


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


def dopasuj(db: Session, payload: dict) -> dict:
    """Podatnosci maszyny na podstawie jej ostatniego raportu."""
    wydanie = wydanie_maszyny(payload)
    if wydanie is None:
        return _wynik(
            STATUS_NIEZNANY,
            "nie rozpoznaje dystrybucji tej maszyny - agent musi podac ID "
            "i VERSION_CODENAME z /etc/os-release (od wersji 0.5.4)",
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

    # Jedno zapytanie na maszyne zamiast jednego na pakiet.
    zrodlowe = {
        (p.get("source_package") or p.get("name") or "").strip()
        for p in pakiety
    } - {""}
    wpisy = db.execute(
        select(CveEntry).where(
            CveEntry.source == source,
            CveEntry.release == release,
            CveEntry.package.in_(zrodlowe),
        )
    ).scalars().all()

    wedlug_pakietu: dict[str, list[CveEntry]] = {}
    for wpis in wpisy:
        wedlug_pakietu.setdefault(wpis.package, []).append(wpis)

    znalezione = []
    for pakiet in pakiety:
        nazwa = (pakiet.get("name") or "").strip()
        wersja = (pakiet.get("version") or "").strip()
        zrodlo = (pakiet.get("source_package") or nazwa).strip()
        # Porownujemy wersje ZRODLOWA, bo taka podaja dane dystrybucji.
        # Starsze agenty jej nie przysylaja - wtedy zostaje binarna, co dla
        # wiekszosci pakietow jest ta sama wartoscia.
        wersja_porownywana = (pakiet.get("source_version") or wersja).strip()
        if not nazwa or not wersja_porownywana:
            continue
        for wpis in wedlug_pakietu.get(zrodlo, []):
            if wpis.status == "resolved":
                if not wersje_pakietow.starsza_niz(wersja_porownywana, wpis.fixed_version):
                    continue
            znalezione.append(
                {
                    "cve": wpis.cve,
                    "package": nazwa,
                    "source_package": zrodlo,
                    "installed_version": wersja,
                    "compared_version": wersja_porownywana,
                    "fixed_version": wpis.fixed_version,
                    "status": wpis.status,
                    "severity": wpis.severity,
                    "no_fix_reason": wpis.no_fix_reason,
                    "description": wpis.description,
                }
            )

    znalezione.sort(key=lambda z: (z["status"] != "open", z["cve"]), reverse=False)
    wiek = None
    pobrano = as_utc(stan.fetched_at)
    if pobrano:
        # SQLite oddaje daty bez strefy - bez uzgodnienia odejmowanie wybucha.
        wiek = round((utcnow() - pobrano) / timedelta(hours=1), 1)

    return _wynik(
        STATUS_OK,
        None,
        entries=znalezione,
        source=f"{source}/{release}",
        feed_age_hours=wiek,
        feed_entries=stan.entries,
    )


def _wynik(status: str, detail: str | None, entries: list | None = None,
           source: str | None = None, feed_age_hours: float | None = None,
           feed_entries: int | None = None) -> dict:
    pozycje = entries or []
    return {
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
            sum(1 for p in pozycje if p["status"] == "resolved") if status == STATUS_OK else None
        ),
        # Bez poprawki, ale dystrybucja uznaje rzecz za istotna.
        "open_count": (
            sum(1 for p in pozycje if p["status"] == "open" and not p.get("no_fix_reason"))
            if status == STATUS_OK else None
        ),
        # Bez poprawki i dystrybucja swiadomie jej nie wyda ("Minor issue").
        "minor_count": (
            sum(1 for p in pozycje if p.get("no_fix_reason")) if status == STATUS_OK else None
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
