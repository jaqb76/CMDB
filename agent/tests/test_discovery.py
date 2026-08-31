from datetime import datetime, timezone
import errno
import json
from unittest.mock import Mock

import pytest

from cmdb_agent import discovery
from cmdb_agent.config import AgentConfig, load_config
from cmdb_agent.state import AgentState


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "8.8.8.8/32", "127.0.0.1/32", "169.254.1.1/32", "::1/128", "10.0.0.0/7"])
def test_public_loopback_and_ipv6_targets_rejected(cidr):
    with pytest.raises(ValueError):
        discovery.networks_for([cidr], 4096)


def test_large_and_overlapping_ranges():
    with pytest.raises(ValueError, match="limit"):
        discovery.networks_for(["10.1.0.0/16"], 1024)
    assert [str(n) for n in discovery.networks_for(["10.1.1.2/24", "10.1.1.0/25"], 254)] == ["10.1.1.0/24"]


def test_disabled_module_never_touches_network(monkeypatch):
    scan = Mock(side_effect=AssertionError("network must stay untouched"))
    monkeypatch.setattr(discovery, "scan", scan)
    report = {}
    discovery.attach_discovery(AgentConfig(), AgentState(), report)
    assert report == {}
    scan.assert_not_called()


def test_scan_schedule_and_errors_are_in_report(monkeypatch, tmp_path):
    scan = Mock(return_value={"errors": ["limit czasu"], "devices": []})
    monkeypatch.setattr(discovery, "scan", scan)
    config, state, report = AgentConfig(discovery_enabled=True, data_dir=tmp_path), AgentState(), {}
    monkeypatch.setattr(discovery, "authorized_config", lambda *args: (config, "policy-1"))
    discovery.attach_discovery(config, state, report)
    assert report["errors"][0]["section"] == "network_discovery"
    assert state.last_discovery_at
    discovery.attach_discovery(config, state, {})
    scan.assert_called_once()


def test_auto_subnets_filter_public_and_keep_real_prefix(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery, "_powershell", lambda _: [
        {"IPAddress": "10.1.2.3", "PrefixLength": 23}, {"IPAddress": "8.8.8.8", "PrefixLength": 24}])
    assert discovery.local_subnets() == ["10.1.2.0/23"]


def test_scan_bounds_and_partial_result(monkeypatch):
    config = AgentConfig(discovery_enabled=True, discovery_auto_subnets=False, discovery_cidrs=["10.1.1.0/30"])
    monkeypatch.setattr(discovery, "neighbors", lambda: {"10.1.1.1": {"mac": "aa:bb:cc:dd:ee:ff", "reachable": True}})
    def probe(ip, limiter):
        if ip.endswith(".2"):
            return None, False
        return {"ip": ip, "mac": "", "ports": [9100]}, True
    monkeypatch.setattr(discovery, "probe_host", probe)
    result = discovery.scan(config)
    assert result["total_hosts"] == 2
    assert result["attempted_hosts"] == 1
    assert result["complete"] is False and result["errors"]
    assert result["devices"][0]["mac"] == "aa:bb:cc:dd:ee:ff"


def test_auto_large_network_fails_before_any_probes(monkeypatch):
    monkeypatch.setattr(discovery, "local_subnets", lambda: ["10.0.0.0/8"])
    probe = Mock()
    monkeypatch.setattr(discovery, "probe_host", probe)
    result = discovery.scan(AgentConfig(discovery_enabled=True))
    assert not result["complete"] and result["errors"]
    probe.assert_not_called()


def test_expired_budget_stops_new_connections(monkeypatch):
    socket = Mock()
    monkeypatch.setattr(discovery.socket, "socket", socket)
    assert discovery.probe_host("10.0.0.1", discovery.RateLimit(32, 0)) == (None, False)
    socket.assert_not_called()


def test_closed_ports_still_discover_live_host_without_writing_print_data(monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=None)
    connection.connect_ex.return_value = errno.ECONNREFUSED
    monkeypatch.setattr(discovery.socket, "socket", lambda *args: connection)
    monkeypatch.setattr(discovery, "dns_name", lambda *args: "host.lan")
    limiter = Mock(deadline=1)
    limiter.acquire.return_value = True
    device, complete = discovery.probe_host("10.0.0.1", limiter)
    assert complete and device["hostname"] == "host.lan" and device["ports"] == []
    connection.sendall.assert_not_called()


def test_classification_is_advisory_not_port_based_os_fact():
    samba = discovery.classify([445], [])
    assert "Samba" in samba["os_hint"] and samba["confidence"] == "low"
    printer = discovery.classify([9100, 80], ["HP LaserJet P2055"])
    assert printer["device_type"] == "drukarka" and printer["manufacturer"] == "HP"
    assert printer["os_hint"] == ""
    assert discovery.classify([22], ["SSH-2.0-OpenSSH_for_Windows_9"])['os_hint'] == "Windows (prawdopodobny)"
    assert discovery.classify([22], ["SSH-2.0-OpenSSH_9.3"])['os_hint'] == "SSH — system nierozpoznany"


def test_configuration_round_trip(tmp_path):
    path = tmp_path / "agent.conf"
    path.write_text(json.dumps({"discovery_enabled": True, "discovery_auto_subnets": False,
                               "discovery_cidrs": ["10.1.1.0/24"], "discovery_rate": 8}))
    config = load_config(path)
    discovery.validate_config(config)
    assert not config.discovery_enabled and config.discovery_rate == 32
    assert config.discovery_cidrs == []  # local file is no longer a policy source


@pytest.mark.parametrize("field,value", [("discovery_rate", 0), ("discovery_max_hosts", 1000000),
                                         ("discovery_cidrs", "10.0.0.0/24"), ("discovery_budget_seconds", 9999)])
def test_invalid_limits_rejected(field, value):
    config = AgentConfig(**{field: value})
    with pytest.raises(ValueError):
        discovery.validate_config(config)


def test_active_arp_finds_filtered_host_but_stale_and_out_of_scope_do_not(monkeypatch):
    config = AgentConfig(discovery_enabled=True, discovery_auto_subnets=False, discovery_cidrs=["10.1.1.0/30"])
    monkeypatch.setattr(discovery, "probe_host", lambda *_: (None, True))
    monkeypatch.setattr(discovery, "neighbors", lambda: {
        "10.1.1.1": {"mac": "aa:bb:cc:dd:ee:01", "reachable": True},
        "10.1.1.2": {"mac": "aa:bb:cc:dd:ee:02", "reachable": False},
        "10.9.9.1": {"mac": "aa:bb:cc:dd:ee:03", "reachable": True}})
    result = discovery.scan(config)
    assert result["complete"] and len(result["devices"]) == 1
    assert result["devices"][0]["ip"] == "10.1.1.1"
    assert result["devices"][0]["device_type"] == "inne"
    assert "ARP" in result["devices"][0]["evidence"][0]


def test_untrusted_banner_control_characters_are_removed():
    assert discovery.clean_text('SSH-2.0\x00OpenSSH\r\n\x1b') == 'SSH-2.0 OpenSSH'


def test_large_banners_cannot_fill_inventory_report():
    result = {"devices": [{"ip": "10.0.0.1", "evidence": ["x" * 240] * 10}] * 2000,
              "errors": [], "complete": True}
    limited = discovery.limit_result(result)
    assert 0 < len(limited["devices"]) < 2000
    assert not limited["complete"] and limited["errors"]
    assert len(json.dumps(limited).encode()) <= 1024 * 1024


# --- wlasne adresy skanera ---------------------------------------------------

def _config(cidrs, max_hosts):
    return AgentConfig(discovery_enabled=True, discovery_auto_subnets=False,
                       discovery_cidrs=cidrs, discovery_max_hosts=max_hosts,
                       discovery_rate=128, discovery_budget_seconds=30)


def test_wlasny_adres_dostaje_mac_z_interfejsu(monkeypatch):
    """Host nie pyta ARP o samego siebie, wiec jego wlasny adres nigdy nie
    trafia do tablicy sasiadow - skaner zostawal bez MAC-a maszyny, na ktorej
    sam stoi, mimo ze zna go bezposrednio z karty sieciowej."""
    from cmdb_agent import discovery

    monkeypatch.setattr(discovery, "neighbors", lambda: {})
    monkeypatch.setattr(discovery, "own_addresses",
                        lambda: {"192.168.1.165": {"mac": "84:5c:f3:51:17:fb",
                                                   "reachable": True, "self": True}})
    monkeypatch.setattr(discovery, "probe_host",
                        lambda ip, limiter: ({"ip": ip, "hostname": "", "mac": "", "ports": [445],
                                              **discovery.classify([445], [])}, True)
                        if ip == "192.168.1.165" else (None, True))

    wynik = discovery.scan(_config(cidrs=["192.168.1.160/29"], max_hosts=8))
    znalezione = {d["ip"]: d for d in wynik["devices"]}
    assert znalezione["192.168.1.165"]["mac"] == "84:5c:f3:51:17:fb"
    assert "wlasnego interfejsu" in znalezione["192.168.1.165"]["evidence"][0]


def test_wlasny_interfejs_wygrywa_z_tablica_arp(monkeypatch):
    """Dla wlasnego adresu karta sieciowa jest pewniejszym zrodlem niz ARP."""
    from cmdb_agent import discovery

    monkeypatch.setattr(discovery, "neighbors",
                        lambda: {"192.168.1.165": {"mac": "00:00:00:aa:bb:cc", "reachable": True}})
    monkeypatch.setattr(discovery, "own_addresses",
                        lambda: {"192.168.1.165": {"mac": "84:5c:f3:51:17:fb",
                                                   "reachable": True, "self": True}})
    monkeypatch.setattr(discovery, "probe_host",
                        lambda ip, limiter: ({"ip": ip, "hostname": "", "mac": "", "ports": [445],
                                              **discovery.classify([445], [])}, True)
                        if ip == "192.168.1.165" else (None, True))

    wynik = discovery.scan(_config(cidrs=["192.168.1.160/29"], max_hosts=8))
    assert wynik["devices"][0]["mac"] == "84:5c:f3:51:17:fb"


def test_blad_odczytu_interfejsow_nie_przerywa_skanu(monkeypatch):
    """Skan bez adresow MAC jest gorszy, ale nadal uzyteczny."""
    from cmdb_agent import discovery

    def wybuch():
        raise OSError("brak dostepu do interfejsow")

    monkeypatch.setattr(discovery, "neighbors", lambda: {})
    monkeypatch.setattr(discovery, "own_addresses", wybuch)
    monkeypatch.setattr(discovery, "probe_host",
                        lambda ip, limiter: ({"ip": ip, "hostname": "", "mac": "", "ports": [445],
                                              **discovery.classify([445], [])}, True)
                        if ip == "192.168.1.165" else (None, True))

    wynik = discovery.scan(_config(cidrs=["192.168.1.160/29"], max_hosts=8))
    assert wynik["devices"][0]["ip"] == "192.168.1.165"
    assert wynik["devices"][0]["mac"] == ""
