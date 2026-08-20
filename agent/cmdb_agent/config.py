"""Konfiguracja agenta.

Priorytet: argumenty CLI > zmienne srodowiskowe > plik konfiguracyjny > domyslne.
Plik konfiguracyjny lezy obok stanu agenta:
  Windows: %ProgramData%\\CMDB\\agent.conf
  Linux:   /etc/cmdb-agent/agent.conf
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


# --- budzet rejestracji uruchamianej z okna ustawien -------------------------
# Rejestracja w tle moze sobie pozwolic na cierpliwosc (4 proby po 60 s).
# Rejestracja z okna jest interaktywna - ktos na nia patrzy - wiec skracamy
# budzet, zeby uzytkownik dostal odpowiedz w kilkanascie sekund zamiast po
# czterech minutach.
INTERACTIVE_TIMEOUT_SECONDS = 15
INTERACTIVE_MAX_RETRIES = 2

# Limit podprocesu MUSI byc wiekszy niz budzet agenta, inaczej okno ubija go,
# zanim ten zdazy zwrocic zrozumialy blad - i zamiast "brak lacznosci"
# uzytkownik widzi surowy wyjatek Pythona. Pilnuje tego test.
INTERACTIVE_PROCESS_TIMEOUT = 120


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "CMDB"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/cmdb-agent")
    return Path("/var/lib/cmdb-agent")


def default_config_path() -> Path:
    if sys.platform == "win32":
        return default_data_dir() / "agent.conf"
    return Path("/etc/cmdb-agent/agent.conf")


@dataclass
class AgentConfig:
    # Adres serwera CMDB. Wylacznie https - agent odmawia wysylki po http.
    server_url: str = ""
    # Token rejestracyjny firmy (uzywany tylko przy pierwszym uruchomieniu).
    enrollment_token: str = ""
    # Wlasny plik CA - dla wewnetrznego PKI firmy.
    ca_bundle: str | None = None
    # Opcjonalne przypiecie certyfikatu: SHA-256 certyfikatu serwera (hex).
    pin_sha256: str | None = None

    report_interval_seconds: int = 3600
    # Losowe rozproszenie startu, zeby 500 maszyn nie uderzylo w serwer naraz.
    jitter_seconds: int = 300
    timeout_seconds: int = 60
    max_retries: int = 4

    # Zbieranie listy procesow bywa uznawane za nadmiarowe - mozna wylaczyc.
    collect_processes: bool = True
    collect_services: bool = True
    collect_updates: bool = True
    # Maksymalna liczba pozycji na liste - zabezpieczenie przed gigantycznym raportem.
    max_items_per_section: int = 5000

    data_dir: Path = field(default_factory=default_data_dir)
    log_level: str = "INFO"

    # Ustawiane, gdy plik konfiguracyjny istnieje, ale biezacy uzytkownik nie
    # ma do niego dostepu - agent dziala dalej na wartosciach domyslnych,
    # a polecenie moze o tym poinformowac.
    config_access_denied: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.data_dir / "agent-state.json"

    @property
    def spool_dir(self) -> Path:
        return self.data_dir / "spool"

    def validate(self) -> None:
        if not self.server_url:
            raise ValueError("nie ustawiono adresu serwera (--server / CMDB_AGENT_SERVER_URL)")
        if not self.server_url.startswith("https://"):
            raise ValueError(
                "adres serwera musi zaczynac sie od https:// - agent nie wysyla "
                "danych ani tokenow po nieszyfrowanym polaczeniu"
            )
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ValueError(f"nie znaleziono pliku CA: {self.ca_bundle}")
        if self.pin_sha256 and len(self.pin_sha256.replace(":", "")) != 64:
            raise ValueError("pin_sha256 musi byc 64-znakowym skrotem SHA-256 w zapisie hex")


_ENV_MAP = {
    "CMDB_AGENT_SERVER_URL": "server_url",
    "CMDB_AGENT_TOKEN": "enrollment_token",
    "CMDB_AGENT_CA_BUNDLE": "ca_bundle",
    "CMDB_AGENT_PIN_SHA256": "pin_sha256",
    "CMDB_AGENT_INTERVAL": "report_interval_seconds",
    "CMDB_AGENT_TIMEOUT": "timeout_seconds",
    "CMDB_AGENT_MAX_RETRIES": "max_retries",
    "CMDB_AGENT_LOG_LEVEL": "log_level",
}

_INT_FIELDS = {
    "report_interval_seconds",
    "jitter_seconds",
    "timeout_seconds",
    "max_retries",
    "max_items_per_section",
}
_BOOL_FIELDS = {"collect_processes", "collect_services", "collect_updates"}


def load_config(config_path: Path | None = None, overrides: dict | None = None) -> AgentConfig:
    config = AgentConfig()
    path = config_path or default_config_path()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw_text = None
    except PermissionError:
        # Sytuacja normalna na Windows: agent.conf zawiera token firmowy, wiec
        # instalator zaweza do niego dostep do SYSTEM i administratorow.
        # Zwykly uzytkownik uruchamiajacy 'show' czy 'status' ma dostac
        # zrozumiala informacje, a nie slad stosu.
        config.config_access_denied = path
        raw_text = None
    except OSError as exc:
        raise ValueError(f"nie moge odczytac pliku konfiguracyjnego {path}: {exc}") from exc

    if raw_text is not None:
        try:
            _apply(config, json.loads(raw_text))
        except json.JSONDecodeError as exc:
            raise ValueError(f"niepoprawny plik konfiguracyjny {path}: {exc}") from exc

    env_values = {
        field_name: os.environ[env_name]
        for env_name, field_name in _ENV_MAP.items()
        if os.environ.get(env_name)
    }
    _apply(config, env_values)

    if overrides:
        _apply(config, {k: v for k, v in overrides.items() if v is not None})

    return config


def _apply(config: AgentConfig, values: dict) -> None:
    for key, value in values.items():
        if not hasattr(config, key):
            continue
        if key in _INT_FIELDS:
            value = int(value)
        elif key in _BOOL_FIELDS:
            value = str(value).strip().lower() in {"1", "true", "yes", "tak", "on"}
        elif key == "data_dir":
            value = Path(value)
        setattr(config, key, value)
