"""Czy usluga monitorowania w ogole istnieje w tym systemie.

Sam brak pliku ``public/monitoring.json`` mowi tylko tyle, ze petla nigdy sie
nie odezwala. Nie odpowiada natomiast na pytanie, ktore zadaje operator: czemu
nie wystartowala. Odpowiedzi sa dwie i naprawia sie je inaczej:

* **jednostki nie ma** - agent byl aktualizowany w miejscu. Aktualizacja
  podmienia plik programu i nie zaklada jednostek systemowych, wiec maszyna
  z agentem sprzed wydania z monitorowaniem nigdy tej uslugi nie dostala.
  Naprawa: ponowne uruchomienie instalatora.
* **jednostka jest, ale nie chodzi** - ktos ja zatrzymal, wylaczyl albo proces
  padl i nie wstal. Naprawa: uruchomienie jej jednym poleceniem.

Pytamy system TYLKO na zadanie, z wiersza polecen. Okno statusu odswieza sie co
kilka sekund i uruchamianie tam procesu potomnego byloby marnotrawstwem.

Nie udajemy wiedzy, ktorej nie mamy: gdy zapytanie sie nie uda (brak uprawnien,
brak systemd, przekroczony czas), zwracamy "nieznany", a nie "brak". Falszywe
"nie zainstalowano" wyslaloby kogos do instalatora bez powodu.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from . import proces

log = logging.getLogger(__name__)

USLUGA_LINUX = "cmdb-agent-monitor.service"
ZADANIE_WINDOWS = "CMDB Agent Monitor"

# Zapytanie o stan uslugi ma byc szybkie albo zadne - to tylko ozdobnik do
# statusu, a nie powod, dla ktorego polecenie ma stac minute.
LIMIT_SEKUND = 10


def _linux() -> dict:
    wynik = proces.uruchom(
        ["systemctl", "show", USLUGA_LINUX,
         "--property=LoadState", "--property=ActiveState", "--property=UnitFileState"],
        timeout=LIMIT_SEKUND,
    )
    if wynik.returncode != 0:
        return {"stan": "nieznany", "szczegol": "systemctl odmowil odpowiedzi"}
    pola = {}
    for linia in wynik.stdout.decode("utf-8", errors="replace").splitlines():
        klucz, _, wartosc = linia.partition("=")
        pola[klucz.strip()] = wartosc.strip()
    if pola.get("LoadState") == "not-found":
        return {"stan": "brak", "szczegol": f"{USLUGA_LINUX} nie jest zainstalowana"}
    if pola.get("ActiveState") == "active":
        return {"stan": "dziala", "szczegol": USLUGA_LINUX}
    szczegol = f"{USLUGA_LINUX}: {pola.get('ActiveState') or 'stan nieznany'}"
    if pola.get("UnitFileState") == "disabled":
        szczegol += ", nie wystartuje po restarcie (disabled)"
    return {"stan": "zatrzymana", "szczegol": szczegol}


def _windows() -> dict:
    from .collectors.windows import powershell_executable

    # Get-ScheduledTask zwraca nazwe stanu z wyliczenia (Ready/Running/Disabled),
    # a nie tekst tlumaczony na jezyk systemu - inaczej parsowalibysmy polski
    # wydruk schtasks i psulo by sie na kazdej innej lokalizacji.
    skrypt = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$z = Get-ScheduledTask -TaskName '{ZADANIE_WINDOWS}'; "
        "if ($z) { $z.State.ToString() } else { 'BRAK' }"
    )
    wynik = proces.uruchom(
        [powershell_executable(), "-NoProfile", "-NonInteractive",
         "-OutputFormat", "Text", "-Command", skrypt],
        timeout=LIMIT_SEKUND,
    )
    if wynik.returncode != 0:
        return {"stan": "nieznany", "szczegol": "harmonogram zadan nie odpowiedzial"}
    stan = wynik.stdout.decode("utf-8-sig", errors="replace").strip()
    if stan == "BRAK" or not stan:
        return {"stan": "brak", "szczegol": f'zadanie "{ZADANIE_WINDOWS}" nie istnieje'}
    if stan == "Running":
        return {"stan": "dziala", "szczegol": f'zadanie "{ZADANIE_WINDOWS}"'}
    # "Ready" znaczy: zarejestrowane i czekajace na wyzwalacz. Wyzwalaczem jest
    # start systemu, wiec po instalacji bez restartu zadanie potrafi tkwic
    # w tym stanie i nic nie sprawdzac.
    opis = {"Ready": "zarejestrowane, ale nie uruchomione",
            "Disabled": "wylaczone"}.get(stan, stan)
    return {"stan": "zatrzymana", "szczegol": f'zadanie "{ZADANIE_WINDOWS}": {opis}'}


def stan_uslugi_monitora() -> dict:
    """Zwraca {"stan": brak|zatrzymana|dziala|nieznany, "szczegol": tekst}."""
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform.startswith("linux") and os.path.isdir("/run/systemd/system"):
            return _linux()
        return {"stan": "nieznany", "szczegol": "nie wiem, jak zapytac ten system"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # Status ma sie wypisac takze wtedy, gdy zapytanie sie nie powiodlo.
        log.debug("nie moge sprawdzic uslugi monitorowania: %s", exc)
        return {"stan": "nieznany", "szczegol": "nie udalo sie zapytac systemu"}


def podpowiedz(stan: str) -> list[str]:
    """Co konkretnie zrobic - polecenia dla systemu, na ktorym stoimy."""
    if sys.platform == "win32":
        wlacz = [f'schtasks /Run /TN "{ZADANIE_WINDOWS}"']
        instaluj = "Uruchom ponownie instalator agenta - założy zadanie obok inwentaryzacji."
    else:
        wlacz = ["systemctl enable --now cmdb-agent-monitor",
                 "systemctl status cmdb-agent-monitor"]
        instaluj = "Uruchom ponownie install-agent.sh - założy jednostkę obok inwentaryzacji."

    if stan == "brak":
        return [instaluj,
                "Aktualizacja w miejscu podmienia program, ale NIE zakłada usług,",
                "więc maszyna sprzed wydania z monitorowaniem nigdy jej nie dostała."]
    if stan == "zatrzymana":
        return ["Usługa jest zainstalowana, ale nie działa. Uruchom ją:", *wlacz]
    if stan == "nieznany":
        return ["Nie udało się zapytać systemu o stan usługi. Sprawdź ręcznie:", *wlacz]
    # "dziala" - usluga chodzi, a mimo to nie publikuje statusu.
    return ["Usługa działa, ale nie opublikowała statusu — zajrzyj do dziennika agenta."]
