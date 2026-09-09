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
from pathlib import Path

from . import proces

log = logging.getLogger(__name__)

USLUGA_LINUX = "cmdb-agent-monitor.service"
ZADANIE_WINDOWS = "CMDB Agent Monitor"

# Inwentaryzacja. Pytamy o nia tylko po to, zeby stwierdzic, ze instalator
# w ogole na tej maszynie byl - jej obecnosc jest warunkiem zalozenia
# brakujacej uslugi monitorowania.
USLUGA_LINUX_INWENTARYZACJA = "cmdb-agent.service"
ZADANIE_WINDOWS_INWENTARYZACJA = "CMDB Agent"

# Slad po jednorazowym zalozeniu uslugi. Administrator, ktory ja potem usunie
# swiadomie, ma miec spokoj - agent zaklada ja RAZ, a nie co godzine.
ZNACZNIK_ZALOZENIA = ".monitor-usluga-zalozona"

# Zapytanie o stan uslugi ma byc szybkie albo zadne - to tylko ozdobnik do
# statusu, a nie powod, dla ktorego polecenie ma stac minute.
LIMIT_SEKUND = 10


def _linux(nazwa: str = USLUGA_LINUX) -> dict:
    wynik = proces.uruchom(
        ["systemctl", "show", nazwa,
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
        return {"stan": "brak", "szczegol": f"{nazwa} nie jest zainstalowana"}
    if pola.get("ActiveState") == "active":
        return {"stan": "dziala", "szczegol": nazwa}
    szczegol = f"{nazwa}: {pola.get('ActiveState') or 'stan nieznany'}"
    if pola.get("UnitFileState") == "disabled":
        szczegol += ", nie wystartuje po restarcie (disabled)"
    return {"stan": "zatrzymana", "szczegol": szczegol}


def _windows(nazwa: str = ZADANIE_WINDOWS) -> dict:
    from .collectors.windows import powershell_executable

    # Get-ScheduledTask zwraca nazwe stanu z wyliczenia (Ready/Running/Disabled),
    # a nie tekst tlumaczony na jezyk systemu - inaczej parsowalibysmy polski
    # wydruk schtasks i psulo by sie na kazdej innej lokalizacji.
    skrypt = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$z = Get-ScheduledTask -TaskName '{nazwa}'; "
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
        return {"stan": "brak", "szczegol": f'zadanie "{nazwa}" nie istnieje'}
    if stan == "Running":
        return {"stan": "dziala", "szczegol": f'zadanie "{nazwa}"'}
    # "Ready" znaczy: zarejestrowane i czekajace na wyzwalacz. Wyzwalaczem jest
    # start systemu, wiec po instalacji bez restartu zadanie potrafi tkwic
    # w tym stanie i nic nie sprawdzac.
    opis = {"Ready": "zarejestrowane, ale nie uruchomione",
            "Disabled": "wylaczone"}.get(stan, stan)
    return {"stan": "zatrzymana", "szczegol": f'zadanie "{nazwa}": {opis}'}


def _stan(nazwa_linux: str, nazwa_windows: str) -> dict:
    try:
        if sys.platform == "win32":
            return _windows(nazwa_windows)
        if sys.platform.startswith("linux") and os.path.isdir("/run/systemd/system"):
            return _linux(nazwa_linux)
        return {"stan": "nieznany", "szczegol": "nie wiem, jak zapytac ten system"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # Status ma sie wypisac takze wtedy, gdy zapytanie sie nie powiodlo.
        log.debug("nie moge sprawdzic uslugi %s: %s", nazwa_linux, exc)
        return {"stan": "nieznany", "szczegol": "nie udalo sie zapytac systemu"}


def stan_uslugi_monitora() -> dict:
    """Zwraca {"stan": brak|zatrzymana|dziala|nieznany, "szczegol": tekst}."""
    return _stan(USLUGA_LINUX, ZADANIE_WINDOWS)


def stan_inwentaryzacji() -> dict:
    """To samo dla uslugi inwentaryzacji - dowod, ze instalator tu byl."""
    return _stan(USLUGA_LINUX_INWENTARYZACJA, ZADANIE_WINDOWS_INWENTARYZACJA)


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


# --- naprawa: zalozenie brakujacej uslugi ------------------------------------
#
# Aktualizacja agenta podmienia plik programu i nie zaklada jednostek
# systemowych. Maszyna, ktora dostala monitorowanie przez samoaktualizacje,
# a nie przez instalator, nigdy tej uslugi nie dostala - i nie ma jak z tego
# wyjsc bez wizyty administratora. To domykamy tutaj.
#
# Warunki sa celowo ostre. Agent zaklada TYLKO brakujaca siostrzana usluge
# istniejacej instalacji: gdy inwentaryzacja jest zarejestrowana (dowod, ze
# instalator na tej maszynie byl), monitorowania nie ma, a my mamy prawa
# zapisu. Na katalogu roboczym programisty nie stanie sie nic.

# Odpowiednik tego, co pisze install-agent.sh. Rozjazd z instalatorem lapie
# test - jedno zachowanie opisane w dwoch jezykach musi byc pilnowane.
JEDNOSTKA_LINUX = """[Unit]
Description=Monitorowanie dostepnosci uslug CMDB
Documentation=https://github.com/jaqb76/CMDB
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cmdb-agent --config {konfiguracja} monitor
# Zatrzymanie ma dokonczyc raport, a nie uciac go w polowie: agent lapie
# SIGTERM i wysyla ostatnie podsumowanie, zanim zakonczy prace.
KillSignal=SIGTERM
TimeoutStopSec=30
Restart=always
RestartSec=30

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={katalog_danych}

[Install]
WantedBy=multi-user.target
"""


def _zaloz_linux(konfiguracja, katalog_danych) -> str:
    jednostka = Path("/etc/systemd/system") / USLUGA_LINUX
    jednostka.write_text(
        JEDNOSTKA_LINUX.format(konfiguracja=konfiguracja, katalog_danych=katalog_danych),
        encoding="utf-8",
    )
    for polecenie in (["systemctl", "daemon-reload"],
                      ["systemctl", "enable", "--now", USLUGA_LINUX]):
        wynik = proces.uruchom(polecenie, timeout=30)
        if wynik.returncode != 0:
            blad = wynik.stderr.decode("utf-8", errors="replace").strip()
            raise OSError(f"{' '.join(polecenie)}: {blad or wynik.returncode}")
    return f"zalozylem i uruchomilem {USLUGA_LINUX}"


def _zaloz_windows(konfiguracja, katalog_programu, plik_programu) -> str:
    from .collectors.windows import powershell_executable

    # Te same ustawienia, co w install-agent.ps1. Start-ScheduledTask na koncu
    # jest istotny: wyzwalaczem zadania jest start systemu, wiec bez tego
    # zadanie tkwiloby w stanie Ready az do restartu i nic nie sprawdzalo.
    skrypt = (
        "$ErrorActionPreference='Stop'; "
        f"$a = New-ScheduledTaskAction -Execute '{plik_programu}' "
        f"-Argument '--config \"{konfiguracja}\" monitor' -WorkingDirectory '{katalog_programu}'; "
        "$t = New-ScheduledTaskTrigger -AtStartup; "
        "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries "
        "-AllowStartIfOnBatteries -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{ZADANIE_WINDOWS}' "
        "-Description 'Monitorowanie dostepnosci uslug CMDB - cele ustawia sie w panelu.' "
        "-Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{ZADANIE_WINDOWS}'"
    )
    wynik = proces.uruchom(
        [powershell_executable(), "-NoProfile", "-NonInteractive",
         "-OutputFormat", "Text", "-Command", skrypt],
        timeout=60,
    )
    if wynik.returncode != 0:
        blad = wynik.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"Register-ScheduledTask: {blad or wynik.returncode}")
    return f'zalozylem i uruchomilem zadanie "{ZADANIE_WINDOWS}"'


def zapewnij_usluge_monitora(config, sciezka_konfiguracji) -> str | None:
    """Zaklada brakujaca usluge monitorowania. Zwraca opis zmiany albo None.

    Wolane raz na przebieg inwentaryzacji, wiec maszyna naprawia sie sama
    w ciagu godziny, bez czekania na kolejna aktualizacje.

    Blad nigdy nie przerywa inwentaryzacji: to naprawa poboczna, a nie powod,
    dla ktorego raport o sprzecie mialby nie pojsc.
    """
    znacznik = Path(config.data_dir) / ZNACZNIK_ZALOZENIA
    if znacznik.exists():
        # Zakladamy RAZ. Jesli administrator usunal usluge po tym, jak ja
        # zalozylismy, to byla jego decyzja - status i tak o niej powie.
        return None
    try:
        if stan_uslugi_monitora()["stan"] != "brak":
            return None
        # Warunek, ktory pilnuje, zeby nie tworzyc uslug na cudzej maszynie:
        # dokladamy siostrzana usluge do INSTALACJI, a nie stawiamy pierwszej.
        if stan_inwentaryzacji()["stan"] == "brak":
            log.debug("nie zakladam monitorowania: brak sladu instalatora na tej maszynie")
            return None

        if sys.platform == "win32":
            from .upgrade import wlasny_plik

            plik = wlasny_plik()
            if plik is None:
                log.debug("nie zakladam zadania: agent nie dziala z pliku wykonywalnego")
                return None
            opis = _zaloz_windows(sciezka_konfiguracji, plik.parent, plik)
        else:
            opis = _zaloz_linux(sciezka_konfiguracji, config.data_dir)

        znacznik.parent.mkdir(parents=True, exist_ok=True)
        znacznik.write_text("zalozona przez agenta po aktualizacji\n", encoding="utf-8")
        log.info("monitorowanie uslug: %s", opis)
        return opis
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("nie udalo sie zalozyc uslugi monitorowania: %s", exc)
        return None
