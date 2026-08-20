"""Konfiguracja i trwaly stan agenta."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from cmdb_agent.config import AgentConfig, load_config
from cmdb_agent.state import AgentState, StateWriteError, load_state, save_state


def test_http_server_url_is_rejected():
    """Token wedruje w naglowku - po http wyciekalby otwartym tekstem."""
    config = AgentConfig(server_url="http://cmdb.firma.pl")
    with pytest.raises(ValueError, match="https"):
        config.validate()


def test_missing_server_url_is_rejected():
    with pytest.raises(ValueError, match="adresu serwera"):
        AgentConfig().validate()


def test_missing_ca_file_is_rejected(tmp_path):
    config = AgentConfig(server_url="https://cmdb.firma.pl", ca_bundle=str(tmp_path / "brak.pem"))
    with pytest.raises(ValueError, match="CA"):
        config.validate()


def test_malformed_pin_is_rejected():
    config = AgentConfig(server_url="https://cmdb.firma.pl", pin_sha256="za-krotki")
    with pytest.raises(ValueError, match="SHA-256"):
        config.validate()


def test_valid_config_passes(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    config = AgentConfig(
        server_url="https://cmdb.firma.pl", ca_bundle=str(ca), pin_sha256="ab" * 32
    )
    config.validate()


def test_precedence_file_then_env_then_cli(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.conf"
    config_file.write_text(
        json.dumps({"server_url": "https://z-pliku.pl", "report_interval_seconds": 111}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CMDB_AGENT_SERVER_URL", "https://ze-srodowiska.pl")

    config = load_config(config_file, overrides={"server_url": "https://z-cli.pl"})
    assert config.server_url == "https://z-cli.pl"          # CLI wygrywa
    assert config.report_interval_seconds == 111            # z pliku, nienadpisane

    config = load_config(config_file, overrides={"server_url": None})
    assert config.server_url == "https://ze-srodowiska.pl"  # srodowisko nad plikiem


def test_state_round_trip_and_permissions(tmp_path):
    """Zapis i odczyt stanu, z uprawnieniami zawezonymi do wlasciciela.

    Na Windows katalog jest zastrzegany dla SYSTEM i administratorow, wiec
    zwykly uzytkownik dostaje czytelny blad zamiast zapisu - to zamierzone,
    bo w pliku lezy poswiadczenie maszyny.
    """
    path = tmp_path / "sub" / "agent-state.json"
    state = AgentState(
        agent_token="cmdb_agt_abcdefghijkl_sekret",
        asset_id="asset-1",
        tenant_slug="firma",
        machine_id="win-uuid-1",
    )

    try:
        save_state(path, state)
    except StateWriteError as exc:
        assert os.name == "nt", "na POSIX zapis do wlasnego katalogu musi sie udac"
        assert "administrator" in str(exc).lower()
        return

    loaded = load_state(path)
    assert loaded.agent_token == state.agent_token
    assert loaded.is_enrolled

    if os.name == "posix":
        # Plik zawiera sekret - nie moze byc czytelny dla innych uzytkownikow.
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_state_fails_fast_without_permissions(tmp_path):
    """Regresja: przy braku uprawnien tempfile.mkstemp na Windows nie zglaszal
    bledu, tylko ponawial probe do 10 000 razy - agent wygladal na zawieszony.
    Zapis ma sie konczyc natychmiast, powodzeniem albo czytelnym bledem."""
    path = tmp_path / "sub" / "agent-state.json"
    started = time.monotonic()
    try:
        save_state(path, AgentState(agent_token="t", asset_id="a"))
    except StateWriteError:
        pass
    assert time.monotonic() - started < 5.0, "zapis stanu trwal podejrzanie dlugo"


def test_corrupted_state_is_treated_as_not_enrolled(tmp_path):
    path = tmp_path / "agent-state.json"
    path.write_text("{to nie jest json", encoding="utf-8")
    assert load_state(path).is_enrolled is False


def test_unknown_fields_in_state_are_ignored(tmp_path):
    """Stan zapisany przez nowsza wersje agenta nie moze wywrocic starszej."""
    path = tmp_path / "agent-state.json"
    path.write_text(
        json.dumps({"agent_token": "t", "asset_id": "a", "pole_z_przyszlosci": 1}),
        encoding="utf-8",
    )
    assert load_state(path).agent_token == "t"
