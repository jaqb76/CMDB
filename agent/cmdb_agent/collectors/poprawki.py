"""Wykrywanie brakujacych aktualizacji na Linuksie.

Zainstalowane poprawki to tylko polowa obrazu. Do oceny bezpieczenstwa
potrzebne jest to, czego BRAKUJE - i to z rozroznieniem, ktore z brakow
sa poprawkami bezpieczenstwa.

Dwie zasady, ktore ksztaltuja caly ten modul:

1. Nigdy nie ruszamy SYSTEMOWEGO indeksu pakietow. "apt update" w zwyklej
   postaci bierze blokade, podmienia /var/lib/apt/lists i uruchamia skrypty
   po aktualizacji - agent ma maszyne inwentaryzowac, a nie modyfikowac.

   Ale na maszynach, na ktorych nikt nie robi "apt update", systemowy indeks
   ma miesiace i wynik niczego nie mowi. Dlatego agent pobiera
   indeks do WLASNEGO katalogu danych: apt dostaje osobny Dir::State::Lists,
   dnf i yum osobny cachedir. Zrodla repozytoriow, klucze i dane
   uwierzytelniajace sa systemowe, wiec wynik jest taki, jaki dalby
   "apt update", tyle ze stan systemu zostaje nietkniety. Nieudane
   odswiezenie konczy sie powrotem do indeksu systemowego - z adnotacja.

2. Wiek indeksu jest czescia wyniku. "Brak brakujacych aktualizacji" przy
   indeksie sprzed trzech miesiecy nie znaczy "maszyna aktualna" - znaczy
   "nie wiemy". Bez tej informacji raport uspokajalby zamiast ostrzegac.

Stad rozroznienie miedzy pusta lista brakow a stanem "nieznany": pierwsze
znaczy, ze sprawdzilismy i nic nie brakuje, drugie - ze sprawdzic sie nie
udalo. Zlanie tych dwoch przypadkow w jeden jest w narzedziu do oceny
bezpieczenstwa blednem, ktory kosztuje najwiecej.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .common import CommandError, run_command_with_code as _uruchom

STATUS_OK = "ok"
STATUS_NIEZNANY = "nieznany"

# Katalogi, po ktorych poznajemy wiek indeksu pakietow.
ZNACZNIKI_APT = (
    "/var/lib/apt/periodic/update-success-stamp",
    "/var/cache/apt/pkgcache.bin",
    "/var/lib/apt/lists",
)
ZNACZNIKI_DNF = (
    "/var/cache/dnf",
    "/var/cache/yum",
)

# Linia symulacji apt-get:
#   Inst libssl3 [3.0.2-0ubuntu1.10] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
WZORZEC_APT = re.compile(
    r"^Inst\s+(?P<pakiet>\S+)"
    r"(?:\s+\[(?P<obecna>[^\]]+)\])?"
    r"\s+\((?P<nowa>\S+)\s+(?P<zrodlo>.+?)\)\s*$"
)


def _teraz() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wynik(
    source: str,
    status: str = STATUS_OK,
    detail: str | None = None,
    entries: list | None = None,
    index_age_hours: float | None = None,
    index_refresh: dict | None = None,
) -> dict:
    """Jednolita postac wyniku, niezalezna od menedzera pakietow."""
    pozycje = entries or []
    return {
        "status": status,
        "detail": detail,
        "source": source,
        "checked_at": _teraz(),
        "index_age_hours": index_age_hours,
        # Wynik odswiezania wlasnego indeksu agenta: {"status", "detail"}.
        # None znaczy, ze agent indeksu nie odswieza (wylaczone).
        "index_refresh": index_refresh,
        "count": len(pozycje) if status == STATUS_OK else None,
        "security_count": (
            sum(1 for p in pozycje if p.get("security")) if status == STATUS_OK else None
        ),
        "entries": pozycje,
    }


def wiek_indeksu(sciezki: tuple[str, ...]) -> float | None:
    """Ile godzin temu ostatnio odswiezono indeks pakietow."""
    najnowszy = None
    for sciezka in sciezki:
        plik = Path(sciezka)
        try:
            if plik.is_dir():
                czasy = [wpis.stat().st_mtime for wpis in plik.iterdir()]
                if not czasy:
                    continue
                znacznik = max(czasy)
            elif plik.exists():
                znacznik = plik.stat().st_mtime
            else:
                continue
        except OSError:
            continue
        najnowszy = znacznik if najnowszy is None else max(najnowszy, znacznik)

    if najnowszy is None:
        return None
    return round(max(0.0, (time.time() - najnowszy) / 3600), 1)


def parsuj_apt(wyjscie: str) -> list[dict]:
    """Braki z symulacji "apt-get dist-upgrade".

    Poprawki bezpieczenstwa poznajemy po zrodle: Debian i Ubuntu wydaja je
    z osobnej kieszeni, ktorej nazwa konczy sie na "-security".
    """
    braki = []
    for linia in wyjscie.splitlines():
        dopasowanie = WZORZEC_APT.match(linia.strip())
        if dopasowanie is None:
            continue
        zrodlo = dopasowanie.group("zrodlo")
        # Ostatni czlon to architektura w nawiasach kwadratowych - odcinamy.
        zrodlo = re.sub(r"\s*\[[^\]]*\]\s*$", "", zrodlo).strip()
        braki.append(
            {
                "id": dopasowanie.group("pakiet"),
                "title": dopasowanie.group("pakiet"),
                "current_version": dopasowanie.group("obecna"),
                "new_version": dopasowanie.group("nowa"),
                "source_repo": zrodlo,
                "security": "-security" in zrodlo.lower(),
                "severity": None,
            }
        )
    return braki


def parsuj_dnf(wyjscie: str, bezpieczenstwa: set[str] | None = None) -> list[dict]:
    """Braki z "dnf check-update".

    Format to trzy kolumny: nazwa.architektura, wersja, repozytorium.
    Naglowki i puste linie pomijamy, tak samo sekcje "Obsoleting Packages".
    """
    braki = []
    bezpieczenstwa = bezpieczenstwa or set()
    for linia in wyjscie.splitlines():
        surowa = linia.rstrip()
        if not surowa or surowa.startswith((" ", "\t")):
            continue
        if surowa.lower().startswith(("last metadata", "obsoleting", "security:")):
            continue
        czesci = surowa.split()
        if len(czesci) < 3:
            continue
        nazwa, wersja, repo = czesci[0], czesci[1], czesci[2]
        if "." not in nazwa:
            continue
        pakiet = nazwa.rsplit(".", 1)[0]
        braki.append(
            {
                "id": pakiet,
                "title": pakiet,
                "current_version": None,
                "new_version": wersja,
                "source_repo": repo,
                "security": pakiet in bezpieczenstwa or "security" in repo.lower(),
                "severity": None,
            }
        )
    return braki



# --- wlasny indeks agenta ----------------------------------------------------

# Po nieudanym odswiezeniu nie probujemy przy kazdym raporcie: niedostepne
# lustro to zwykle kilkadziesiat sekund czekania na limit czasu, co godzine.
PONOW_PO_BLEDZIE_GODZIN = 6


def _stan_odswiezenia(katalog: Path) -> dict:
    try:
        return json.loads((katalog / "odswiezenie.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _zapisz_stan_odswiezenia(katalog: Path, stan: dict) -> None:
    try:
        (katalog / "odswiezenie.json").write_text(json.dumps(stan), encoding="utf-8")
    except OSError:
        pass


def _pora_odswiezyc(stan: dict, co_ile_godzin: float) -> bool:
    teraz = time.time()
    udane = stan.get("udane")
    if udane is None or teraz - udane >= co_ile_godzin * 3600:
        proba = stan.get("proba")
        if proba is None or stan.get("status") == STATUS_OK:
            return True
        return teraz - proba >= min(co_ile_godzin, PONOW_PO_BLEDZIE_GODZIN) * 3600
    return False


def _ogon(tekst: str, dlugosc: int = 300) -> str:
    """Ostatnie linie wyjscia - tam narzedzia pisza, co poszlo nie tak."""
    linie = [l.strip() for l in (tekst or "").splitlines() if l.strip()]
    return " | ".join(linie[-3:])[-dlugosc:]


def _konfiguracja_apt(katalog: Path) -> Path:
    """Plik konfiguracji apt kierujacy indeks do katalogu agenta.

    "#clear" wylacza skrypty uruchamiane po "apt update" (command-not-found,
    update-notifier i podobne) - pisza one do systemu, a to wlasnie jest
    tym, czego nie chcemy. pkgcache puste: apt buduje bufor w pamieci
    zamiast nadpisywac /var/cache/apt.
    """
    listy = katalog / "lists"
    (listy / "partial").mkdir(parents=True, exist_ok=True)
    plik = katalog / "apt.conf"
    plik.write_text(
        "#clear APT::Update::Pre-Invoke;\n"
        "#clear APT::Update::Post-Invoke;\n"
        "#clear APT::Update::Post-Invoke-Success;\n"
        f'Dir::State::Lists "{listy}/";\n'
        'Dir::Cache::pkgcache "";\n'
        'Dir::Cache::srcpkgcache "";\n',
        encoding="utf-8",
    )
    return plik


def odswiez_indeks_apt(katalog: Path, co_ile_godzin: float, timeout: int = 300) -> tuple[Path | None, dict]:
    """Pobiera indeks apt do katalogu agenta, gdy jest starszy niz podany wiek.

    Zwraca (plik konfiguracji albo None, gdy wlasnego indeksu nie ma; stan).
    """
    katalog.mkdir(parents=True, exist_ok=True)
    konfiguracja = _konfiguracja_apt(katalog)
    stan = _stan_odswiezenia(katalog)

    if _pora_odswiezyc(stan, co_ile_godzin):
        stan["proba"] = time.time()
        try:
            wyjscie, kod = _uruchom(
                ["apt-get", "-q", "-c", str(konfiguracja), "update"], timeout=timeout
            )
        except (CommandError, OSError) as exc:
            stan.update(status="blad", detail=f"apt-get update: {exc}")
        else:
            if kod == 0:
                stan.update(status=STATUS_OK, detail=None, udane=time.time())
            else:
                stan.update(status="blad",
                            detail=f"apt-get update zakonczyl sie kodem {kod}: {_ogon(wyjscie)}")
        _zapisz_stan_odswiezenia(katalog, stan)

    ma_indeks = stan.get("udane") is not None
    return (konfiguracja if ma_indeks else None), stan


def _wiek_z_stanu(stan: dict) -> float | None:
    udane = stan.get("udane")
    if udane is None:
        return None
    return round(max(0.0, (time.time() - udane) / 3600), 1)


def _opis_odswiezenia(stan: dict) -> dict:
    return {"status": stan.get("status") or STATUS_NIEZNANY, "detail": stan.get("detail")}


def braki_apt(timeout: int = 120, katalog: Path | None = None,
              co_ile_godzin: float = 24) -> dict:
    """Brakujace aktualizacje w Debianie i Ubuntu.

    Z katalogiem - najpierw odswieza wlasny indeks agenta i z niego czyta.
    Bez katalogu - czyta indeks systemowy taki, jaki jest.
    """
    if not shutil.which("apt-get"):
        return wynik("apt", STATUS_NIEZNANY, "brak apt-get")

    konfiguracja = None
    odswiezenie = None
    wiek = None
    if katalog is not None:
        try:
            konfiguracja, stan = odswiez_indeks_apt(katalog, co_ile_godzin)
        except OSError as exc:
            stan = {"status": "blad", "detail": f"katalog indeksu {katalog}: {exc}"}
        odswiezenie = _opis_odswiezenia(stan)
        if konfiguracja is not None:
            wiek = _wiek_z_stanu(stan)
    if konfiguracja is None:
        wiek = wiek_indeksu(ZNACZNIKI_APT)

    # -s symuluje, NoLocking pozwala dzialac bez blokady - nie wchodzimy
    # w droge aktualizacjom uruchomionym przez administratora.
    polecenie = ["apt-get", "-s", "-q"]
    if konfiguracja is not None:
        polecenie += ["-c", str(konfiguracja)]
    polecenie += [
        "-o", "Debug::NoLocking=1",
        "-o", "APT::Get::Show-User-Simulation-Note=0",
        "dist-upgrade",
    ]
    try:
        wyjscie, kod = _uruchom(polecenie, timeout=timeout)
    except (CommandError, OSError) as exc:
        return wynik("apt", STATUS_NIEZNANY, f"apt-get nie odpowiedzial: {exc}",
                     index_age_hours=wiek, index_refresh=odswiezenie)

    if kod != 0:
        return wynik("apt", STATUS_NIEZNANY, f"apt-get zakonczyl sie kodem {kod}",
                     index_age_hours=wiek, index_refresh=odswiezenie)

    return wynik("apt", STATUS_OK, entries=parsuj_apt(wyjscie), index_age_hours=wiek,
                 index_refresh=odswiezenie)


def _opcje_dnf(narzedzie: str, katalog: Path, co_ile_godzin: float) -> list[str]:
    """Opcje kierujace metadane dnf/yum do katalogu agenta.

    metadata_expire oddaje decyzje o odswiezeniu samemu dnf: metadane
    mlodsze niz podany wiek sa brane z bufora, starsze - pobierane.
    """
    bufor = katalog / "cache"
    logi = katalog / "log"
    bufor.mkdir(parents=True, exist_ok=True)
    logi.mkdir(parents=True, exist_ok=True)
    opcje = [
        f"--setopt=cachedir={bufor}",
        f"--setopt=metadata_expire={int(co_ile_godzin * 3600)}",
    ]
    if narzedzie == "dnf":
        opcje.append(f"--setopt=logdir={logi}")
        # dnf5 (Fedora 41+) jako root korzysta z system_cachedir, nie cachedir.
        sciezka = shutil.which("dnf") or ""
        if os.path.basename(os.path.realpath(sciezka)).startswith("dnf5"):
            opcje.append(f"--setopt=system_cachedir={bufor}")
    return opcje


def _sprawdz_dnf(narzedzie: str, opcje: list[str], timeout: int) -> tuple[str, int, set[str]]:
    wyjscie, kod = _uruchom([narzedzie, "-q", *opcje, "check-update"], timeout=timeout)
    bezpieczenstwa: set[str] = set()
    if kod == 100:
        try:
            wyjscie_sec, kod_sec = _uruchom(
                [narzedzie, "-q", *opcje, "check-update", "--security"], timeout=timeout
            )
            if kod_sec in (0, 100):
                bezpieczenstwa = {p["id"] for p in parsuj_dnf(wyjscie_sec)}
        except (CommandError, OSError):
            # Lista ogolna zostaje - brak podzialu na bezpieczenstwo nie jest
            # powodem, zeby wyrzucic caly wynik.
            pass
    return wyjscie, kod, bezpieczenstwa


def braki_dnf(timeout: int = 180, katalog: Path | None = None,
              co_ile_godzin: float = 24) -> dict:
    """Brakujace aktualizacje w RHEL, Fedorze i pochodnych.

    Z katalogiem - metadane repozytoriow ida do katalogu agenta i odswiezaja
    sie, gdy sa starsze niz podany wiek. Bez katalogu (albo gdy to sie nie
    uda) - "-C", czyli wylacznie bufor systemowy, bez ruchu sieciowego.
    """
    narzedzie = "dnf" if shutil.which("dnf") else ("yum" if shutil.which("yum") else None)
    if narzedzie is None:
        return wynik("dnf", STATUS_NIEZNANY, "brak dnf i yum")

    odswiezenie = None
    if katalog is not None:
        try:
            opcje = _opcje_dnf(narzedzie, katalog, co_ile_godzin)
            wyjscie, kod, bezpieczenstwa = _sprawdz_dnf(narzedzie, opcje, timeout)
        except (CommandError, OSError) as exc:
            odswiezenie = {"status": "blad", "detail": f"{narzedzie}: {exc}"}
        else:
            if kod in (0, 100):
                odswiezenie = {"status": STATUS_OK, "detail": None}
                wiek = wiek_indeksu((str(katalog / "cache"),))
                return wynik(narzedzie, STATUS_OK,
                             entries=parsuj_dnf(wyjscie, bezpieczenstwa) if kod == 100 else [],
                             index_age_hours=wiek, index_refresh=odswiezenie)
            odswiezenie = {
                "status": "blad",
                "detail": f"{narzedzie} zakonczyl sie kodem {kod}: {_ogon(wyjscie)}",
            }

    wiek = wiek_indeksu(ZNACZNIKI_DNF)
    try:
        # -C uzywa wylacznie danych z cache - bez ruchu sieciowego.
        wyjscie, kod, bezpieczenstwa = _sprawdz_dnf(narzedzie, ["-C"], timeout)
    except (CommandError, OSError) as exc:
        return wynik(narzedzie, STATUS_NIEZNANY, f"{narzedzie} nie odpowiedzial: {exc}",
                     index_age_hours=wiek, index_refresh=odswiezenie)

    # 0 = nic do aktualizacji, 100 = sa aktualizacje. Reszta to blad.
    if kod not in (0, 100):
        return wynik(narzedzie, STATUS_NIEZNANY, f"{narzedzie} zakonczyl sie kodem {kod}",
                     index_age_hours=wiek, index_refresh=odswiezenie)
    if kod == 0:
        return wynik(narzedzie, STATUS_OK, entries=[], index_age_hours=wiek,
                     index_refresh=odswiezenie)

    return wynik(narzedzie, STATUS_OK, entries=parsuj_dnf(wyjscie, bezpieczenstwa),
                 index_age_hours=wiek, index_refresh=odswiezenie)


def braki_linux(timeout: int = 180, katalog: Path | None = None,
                co_ile_godzin: float = 24) -> dict:
    """Brakujace aktualizacje - dobiera narzedzie do dystrybucji.

    katalog - gdzie agent trzyma wlasny indeks pakietow; None wylacza
    odswiezanie i zostaje odczyt indeksu systemowego.
    """
    if shutil.which("apt-get"):
        return braki_apt(timeout=timeout, katalog=katalog and katalog / "apt",
                         co_ile_godzin=co_ile_godzin)
    if shutil.which("dnf") or shutil.which("yum"):
        return braki_dnf(timeout=timeout, katalog=katalog and katalog / "dnf",
                         co_ile_godzin=co_ile_godzin)
    return wynik(
        "nieznany",
        STATUS_NIEZNANY,
        "nie rozpoznano menedzera pakietow (obslugiwane: apt, dnf, yum)",
    )
