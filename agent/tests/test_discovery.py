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


def test_scan_schedule_and_errors_are_in_report(monkeypatch):
    scan = Mock(return_value={"errors": ["limit czasu"], "devices": []})
    monkeypatch.setattr(discovery, "scan", scan)
    config, state, report = AgentConfig(discovery_enabled=True), AgentState(), {}
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
    monkeypatch.setattr(discovery, "neighbors", lambda: {"10.1.1.1": "aa:bb:cc:dd:ee:ff"})
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
    assert config.discovery_enabled and config.discovery_rate == 8


@pytest.mark.parametrize("field,value", [("discovery_rate", 0), ("discovery_max_hosts", 1000000),
                                         ("discovery_cidrs", "10.0.0.0/24"), ("discovery_budget_seconds", 9999)])
def test_invalid_limits_rejected(field, value):
    config = AgentConfig(**{field: value})
    with pytest.raises(ValueError):
        discovery.validate_config(config)
