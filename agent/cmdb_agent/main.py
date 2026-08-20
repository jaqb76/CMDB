"""Agent CMDB - interfejs wiersza polecen.

  cmdb-agent enroll --server https://cmdb.firma.pl --token cmdb_ent_...
  cmdb-agent run              jednorazowe zebranie i wyslanie raportu
  cmdb-agent loop             praca ciagla (dla uslugi / zadania harmonogramu)
  cmdb-agent show             tylko zbierz i wypisz JSON (diagnostyka, bez wysylki)
  cmdb-agent status           co agent wie o sobie
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import random
import sys
import time
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .collectors import get_collector
from .collectors.common import utc_now_iso
from .config import AgentConfig, default_config_path, load_config
from .report import build_report, report_hash, report_size
from . import status, upgrade
from .spool import load_report, pending_reports, spool_report
from .state import AgentState, StateWriteError, load_state, save_state
from .transport import ApiError, CertificatePinError, CmdbClient, TransportError

log = logging.getLogger("cmdb_agent")


def setup_logging(config: AgentConfig) -> None:
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.data_dir / "agent.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        log.warning("nie moge pisac do dziennika w %s: %s", config.data_dir, exc)


def make_client(config: AgentConfig) -> CmdbClient:
    return CmdbClient(
        server_url=config.server_url,
        ca_bundle=config.ca_bundle,
        pin_sha256=config.pin_sha256,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )


def do_enroll(config: AgentConfig, state: AgentState, client: CmdbClient) -> AgentState:
    """Wymienia firmowy token rejestracyjny na wlasne poswiadczenie maszyny."""
    if not config.enrollment_token:
        raise SystemExit(
            "brak tokenu rejestracyjnego - podaj --token albo wpisz go do pliku konfiguracyjnego"
        )

    collector = get_collector(config)

    # Mierzymy etapy osobno - gdy rejestracja sie przeciaga, z dziennika ma
    # wynikac CO trwa: odczyt danych maszyny czy polaczenie z serwerem.
    started = time.monotonic()
    machine_id = collector.machine_id()
    identity = collector.identity()
    collect_ms = int((time.monotonic() - started) * 1000)
    log.info("odczyt identyfikacji maszyny zajal %d ms", collect_ms)

    log.info("rejestruje maszyne %s (%s) w %s", identity["hostname"], machine_id, config.server_url)
    started = time.monotonic()
    response = client.post(
        "/api/v1/agents/enroll",
        token=config.enrollment_token,
        payload={
            "machine_id": machine_id,
            "identity": identity,
            "agent_version": __version__,
        },
    )

    log.info("odpowiedz serwera po %d ms", int((time.monotonic() - started) * 1000))

    state.agent_token = response["agent_token"]
    state.asset_id = response["asset_id"]
    state.tenant_slug = response.get("tenant_slug", "")
    state.machine_id = machine_id
    state.server_url = config.server_url
    state.enrolled_at = utc_now_iso()
    save_state(config.state_path, state)

    status.publish(config, state)
    log.info(
        "zarejestrowano w firmie '%s' jako zasob %s; poswiadczenie zapisane w %s",
        state.tenant_slug, state.asset_id, config.state_path,
    )
    return state


def send_report(config: AgentConfig, state: AgentState, client: CmdbClient, report: dict) -> dict:
    return client.post("/api/v1/inventory", token=state.agent_token, payload=report)


def record_attempt(
    config: AgentConfig,
    state: AgentState,
    outcome: str,
    error: str = "",
    changed: bool = False,
) -> None:
    """Zapisuje wynik proby i publikuje status dla ikony w zasobniku.

    last_sync_at przesuwamy WYLACZNIE przy powodzeniu - to data, ktora
    operator czyta jako "kiedy dane w CMDB byly ostatnio aktualne".
    """
    state.last_attempt_at = utc_now_iso()
    state.last_status = outcome
    state.last_error = error[:500]
    if outcome == "ok":
        state.last_sync_at = state.last_attempt_at
        state.last_sync_changed = changed
    save_state(config.state_path, state)
    status.publish(config, state, spooled=len(pending_reports(config.spool_dir)))


def flush_spool(config: AgentConfig, state: AgentState, client: CmdbClient) -> None:
    """Probuje wyslac raporty zebrane w czasie braku lacznosci."""
    for path in pending_reports(config.spool_dir):
        report = load_report(path)
        if report is None:
            continue
        try:
            send_report(config, state, client, report)
        except (TransportError, ApiError) as exc:
            log.warning("bufor: nadal nie moge wyslac %s (%s)", path.name, exc)
            return
        path.unlink(missing_ok=True)
        log.info("bufor: wyslano zalegly raport %s", path.name)


def do_run(config: AgentConfig, state: AgentState, client: CmdbClient) -> int:
    if not state.is_enrolled:
        log.info("agent nie jest jeszcze zarejestrowany - rejestruje automatycznie")
        state = do_enroll(config, state, client)

    collector = get_collector(config)
    report = build_report(collector)

    if report["machine_id"] != state.machine_id:
        log.warning(
            "identyfikator maszyny zmienil sie (%s -> %s) - wymagana ponowna rejestracja",
            state.machine_id, report["machine_id"],
        )
        state = do_enroll(config, state, client)

    flush_spool(config, state, client)

    try:
        response = send_report(config, state, client, report)
    except ApiError as exc:
        if exc.status == 401 and config.enrollment_token:
            # Poswiadczenie wycofane lub uniewaznione ponowna rejestracja innej instalacji.
            log.warning("serwer odrzucil poswiadczenie (%s) - probuje zarejestrowac ponownie", exc)
            state = do_enroll(config, state, client)
            response = send_report(config, state, client, report)
        elif exc.status == 409:
            log.warning("konflikt identyfikatora maszyny - rejestruje ponownie")
            state = do_enroll(config, state, client)
            response = send_report(config, state, client, report)
        else:
            log.error("serwer odrzucil raport: %s", exc)
            record_attempt(config, state, "error", str(exc))
            return 2
    except TransportError as exc:
        log.error("brak lacznosci z serwerem: %s - raport trafia do bufora", exc)
        spool_report(config.spool_dir, report)
        record_attempt(config, state, "offline", str(exc))
        return 3

    state.last_report_hash = report_hash(report)
    record_attempt(config, state, "ok", changed=bool(response.get("changed")))

    log.info(
        "raport wyslany (%d B, zmiana: %s), nastepny za ~%d s",
        report_size(report),
        "tak" if response.get("changed") else "nie",
        response.get("report_interval_seconds", config.report_interval_seconds),
    )
    if response.get("report_interval_seconds"):
        config.report_interval_seconds = int(response["report_interval_seconds"])
    return 0


def do_loop(config: AgentConfig, state: AgentState, client: CmdbClient) -> int:
    log.info("start w trybie ciaglym, interwal %d s", config.report_interval_seconds)
    while True:
        try:
            do_run(config, load_state(config.state_path), client)
        except CertificatePinError as exc:
            # Niezgodny odcisk to potencjalny atak MITM - nie ponawiamy w petli szybko.
            log.error("PRZERWANO: %s", exc)
            return 4
        except Exception as exc:  # agent nie moze umrzec z powodu jednego cyklu
            log.exception("nieoczekiwany blad cyklu: %s", exc)

        # Rozproszenie, zeby cala flota nie uderzala w serwer w tej samej sekundzie.
        delay = config.report_interval_seconds + random.uniform(0, config.jitter_seconds)
        log.debug("spie %.0f s", delay)
        time.sleep(delay)


def do_show(config: AgentConfig, output: str | None) -> int:
    report = build_report(get_collector(config))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"raport zapisany do {output} ({report_size(report)} B)")
    else:
        print(text)
    return 0


def do_status(config: AgentConfig, state: AgentState, as_json: bool = False) -> int:
    spooled = len(pending_reports(config.spool_dir))
    snapshot = status.build_status(config, state, spooled)

    if as_json:
        print(json.dumps(asdict(snapshot), indent=2, ensure_ascii=False))
        return 0

    print(f"agent                : {__version__}")
    print(f"plik konfiguracyjny  : {default_config_path()}")
    print(f"katalog danych       : {config.data_dir}")
    print(f"serwer               : {config.server_url or '(nie ustawiono)'}")
    print(f"zarejestrowany       : {'tak' if state.is_enrolled else 'nie'}")
    if state.is_enrolled:
        print(f"firma                : {state.tenant_slug}")
        print(f"identyfikator zasobu : {state.asset_id}")
        print(f"identyfikator maszyny: {state.machine_id}")
    print()
    print(f"stan                 : {snapshot.status_label}")
    print(
        "ostatnia udana synchr: "
        f"{status.format_local(snapshot.last_sync_at)}"
        f" ({status.format_relative(snapshot.last_sync_at)})"
    )
    if snapshot.last_attempt_at and snapshot.last_attempt_at != snapshot.last_sync_at:
        print(
            "ostatnia proba       : "
            f"{status.format_local(snapshot.last_attempt_at)}"
            f" ({status.format_relative(snapshot.last_attempt_at)})"
        )
    if snapshot.last_error:
        print(f"ostatni blad         : {snapshot.last_error}")
    if snapshot.next_sync_estimate:
        print(f"nastepna okolo       : {status.format_local(snapshot.next_sync_estimate)}")
    if spooled:
        print(f"raporty w buforze    : {spooled}")
    for warning in snapshot.warnings:
        print(f"uwaga                : {warning}")
    return 0


def do_gui(config: AgentConfig) -> int:
    """Uruchamia ikone w zasobniku. Import lokalny - wiersz polecen ma dzialac
    takze tam, gdzie nie ma bibliotek graficznych."""
    try:
        from .gui.tray_app import run_tray
    except ImportError as exc:
        log.error(
            "interfejs graficzny jest niedostepny (%s). "
            "Zainstaluj zaleznosci: pip install -r requirements-gui.txt",
            exc,
        )
        return 1
    return run_tray(config)


def do_doctor(config: AgentConfig) -> int:
    """Rozklada polaczenie na etapy i pokazuje, ktory zawodzi."""
    from .diagnose import render_report

    if not config.server_url:
        print("Nie ustawiono adresu serwera. Podaj --server albo skonfiguruj agenta.")
        return 1
    report, healthy = render_report(config.server_url, config.ca_bundle)
    print(report)
    return 0 if healthy else 3


def do_configure(config: AgentConfig, config_path: Path | None) -> int:
    """Okno konfiguracji: adres serwera i token. Wymaga uprawnien administratora,
    bo zapisuje konfiguracje w katalogu programu."""
    try:
        from .gui.settings_window import open_settings
    except ImportError as exc:
        log.error("interfejs graficzny jest niedostepny (%s)", exc)
        log.error(
            "skonfiguruj agenta z wiersza polecen: "
            "cmdb-agent --server https://... --token cmdb_ent_... enroll"
        )
        return 1
    return 0 if open_settings(config, config_path) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmdb-agent", description="Agent inwentaryzacyjny CMDB")
    parser.add_argument("--config", type=Path, help="sciezka do pliku konfiguracyjnego")
    parser.add_argument("--server", help="adres serwera, np. https://cmdb.firma.pl")
    parser.add_argument("--token", help="firmowy token rejestracyjny")
    parser.add_argument("--ca-bundle", help="wlasny plik CA (wewnetrzne PKI)")
    parser.add_argument("--pin-sha256", help="przypiety odcisk SHA-256 certyfikatu serwera")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"cmdb-agent {__version__}")
    # Ustawiany przez agenta, ktory sam siebie uruchomil po podmianie pliku -
    # zapobiega petli sprawdzania wersji.
    parser.add_argument("--po-aktualizacji", dest="po_aktualizacji",
                        action="store_true", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("enroll", help="rejestruje maszyne i zapisuje wlasne poswiadczenie")
    sub.add_parser("run", help="zbiera i wysyla jeden raport")
    sub.add_parser("loop", help="dziala w petli z ustawionym interwalem")
    show = sub.add_parser("show", help="zbiera raport i wypisuje go bez wysylania")
    show.add_argument("--out", help="zapisz do pliku zamiast na standardowe wyjscie")
    status_cmd = sub.add_parser("status", help="pokazuje stan agenta i ostatnia synchronizacje")
    status_cmd.add_argument("--json", action="store_true", help="wypisz w formacie JSON")
    sub.add_parser("gui", help="uruchamia ikone w zasobniku systemowym")
    sub.add_parser("configure", help="otwiera okno konfiguracji (adres serwera i token)")
    sub.add_parser("doctor", help="sprawdza polaczenie z serwerem etap po etapie")
    return parser


def _argumenty_cyklu(args) -> list[str]:
    """Argumenty, ktore nowa wersja ma powtorzyc przy tym samym cyklu.

    Kolejnosc ma znaczenie: --po-aktualizacji jest opcja globalna, wiec musi
    poprzedzac nazwe polecenia. Doklejona na koncu trafia do podparsera
    polecenia "run", ktory jej nie zna, i proces konczy sie bledem
    "unrecognized arguments" - aktualizacja wychodzi wtedy poprawnie,
    ale obiecany cykl na nowej wersji cicho nie dochodzi do skutku.
    """
    argumenty: list[str] = []
    if args.config:
        argumenty += ["--config", str(args.config)]
    if args.log_level:
        argumenty += ["--log-level", args.log_level]
    argumenty.append("--po-aktualizacji")
    argumenty.append("run")
    return argumenty


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides = {
        "server_url": args.server,
        "enrollment_token": args.token,
        "ca_bundle": args.ca_bundle,
        "pin_sha256": args.pin_sha256,
        "log_level": args.log_level,
    }
    try:
        config = load_config(args.config, overrides)
    except ValueError as exc:
        # Uszkodzony plik konfiguracyjny nie moze konczyc sie sladem stosu -
        # w wersji spakowanej PyInstaller dokleja do niego jeszcze "Failed to
        # execute script", z czego nie wynika nic uzytecznego.
        logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
        log.error("%s", exc)
        log.error(
            "popraw plik konfiguracyjny albo usun go i skonfiguruj agenta na nowo: "
            "cmdb-agent --server https://... --token cmdb_ent_... enroll"
        )
        return 1

    setup_logging(config)

    if config.config_access_denied is not None:
        log.warning(
            "brak dostepu do %s - plik zawiera token firmowy i jest zastrzezony "
            "dla SYSTEM i administratorow",
            config.config_access_denied,
        )
        log.warning(
            "dziala na ustawieniach domyslnych; aby uzyc zapisanej konfiguracji, "
            "uruchom wiersz polecen jako administrator"
        )

    if args.command == "show":
        return do_show(config, getattr(args, "out", None))

    state = load_state(config.state_path)
    if args.command == "status":
        return do_status(config, state, as_json=getattr(args, "json", False))
    if args.command == "gui":
        return do_gui(config)
    if args.command == "configure":
        return do_configure(config, args.config)
    if args.command == "doctor":
        return do_doctor(config)

    try:
        config.validate()
    except ValueError as exc:
        log.error("bledna konfiguracja: %s", exc)
        return 1

    try:
        client = make_client(config)
        if args.command == "enroll":
            do_enroll(config, state, client)
            return 0
        if args.command == "run":
            # Wersje sprawdzamy PRZED zebraniem danych, zeby raport powstal
            # juz z tej wersji, ktora ma byc na maszynie - a nie dopiero
            # w kolejnym cyklu.
            if state.is_enrolled and not args.po_aktualizacji:
                nowa = upgrade.zastosuj(config, state, client)
                if nowa:
                    return upgrade.uruchom_ponownie(_argumenty_cyklu(args))
            return do_run(config, state, client)
        if args.command == "loop":
            return do_loop(config, state, client)
    except CertificatePinError as exc:
        log.error("PRZERWANO: %s", exc)
        return 4
    except ApiError as exc:
        log.error("serwer odrzucil zadanie: %s", exc)
        return 2
    except TransportError as exc:
        log.error("brak lacznosci z serwerem: %s", exc)
        return 3
    except StateWriteError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:  # w dzienniku agenta lepszy komunikat niz goly traceback
        log.exception("nieoczekiwany blad agenta: %s", exc)
        return 1
    return 0


def main_tray(argv: list[str] | None = None) -> int:
    """Punkt wejscia dla cmdb-agent-tray: to samo co 'cmdb-agent gui'."""
    return main([*(argv or []), "gui"])


if __name__ == "__main__":
    sys.exit(main())
