"""Opt-in discovery of private IPv4 networks. No credentials or remote commands.

All network operations are bounded; no DNS/HTTP redirects can expand the scan.
Results are observations, never authoritative hardware/OS inventory.
"""
from __future__ import annotations

import errno
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace

log = logging.getLogger(__name__)
PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PORTS = (22, 80, 135, 139, 443, 445, 515, 631, 3389, 9100)
SOCKET_TIMEOUT = 0.35


def private_network(value):
    network = ipaddress.ip_network(value, strict=False)
    if network.version != 4 or not any(network.subnet_of(n) for n in PRIVATE):
        raise ValueError("Skanowanie obejmuje tylko prywatne IPv4: 10/8, 172.16/12, 192.168/16.")
    return network


def networks_for(cidrs, max_hosts):
    networks = list(ipaddress.collapse_addresses(private_network(c) for c in cidrs))
    if len(networks) > 32:
        raise ValueError("Maksymalnie 32 rozlaczne zakresy CIDR.")
    count = sum(n.num_addresses if n.prefixlen >= 31 else n.num_addresses - 2 for n in networks)
    if count > max_hosts:
        raise ValueError(f"Zakres zawiera {count} adresow; limit wynosi {max_hosts}. Podziel siec na mniejsze zakresy.")
    return networks


def validate_config(config):
    for field, lo, hi in (("discovery_max_hosts", 1, 4096), ("discovery_rate", 1, 128),
                          ("discovery_budget_seconds", 10, 900), ("discovery_interval_seconds", 3600, 604800)):
        value = getattr(config, field)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{field}: dozwolony zakres {lo}–{hi}.")
    if not isinstance(config.discovery_cidrs, list) or any(not isinstance(c, str) for c in config.discovery_cidrs):
        raise ValueError("discovery_cidrs musi byc lista zakresow CIDR.")
    if len(config.discovery_cidrs) > 32:
        raise ValueError("Maksymalnie 32 zakresy CIDR.")
    networks_for(config.discovery_cidrs, config.discovery_max_hosts)
    if config.discovery_enabled and not config.discovery_auto_subnets and not config.discovery_cidrs:
        raise ValueError("Podaj zakresy CIDR lub zaznacz automatyczne wykrywanie podsieci.")


def _command(args, timeout=8):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.stdout.decode("utf-8-sig", errors="replace")


def _powershell(script):
    import base64
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; " + script
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return json.loads(_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]) or "[]")


def local_subnets():
    if sys.platform == "win32":
        rows = _powershell("@(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | "
                           "Where-Object { (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex "
                           "-AddressFamily IPv4).ConnectionState -eq 'Connected' } | "
                           "Select-Object IPAddress,PrefixLength) | ConvertTo-Json -Compress")
        cidrs = [f"{r['IPAddress']}/{r['PrefixLength']}" for r in rows]
    elif sys.platform.startswith("linux"):
        rows = json.loads(_command(["ip", "-j", "-4", "address", "show", "up"]))
        cidrs = [f"{a['local']}/{a['prefixlen']}" for r in rows for a in r.get("addr_info", []) if a.get("scope") == "global"]
    else:
        raise ValueError("Automatyczne podsieci sa dostepne na Windows/Linux; podaj zakresy recznie.")
    result = []
    for cidr in cidrs:
        try:
            result.append(str(private_network(cidr)))
        except ValueError:
            continue
    return result


def neighbors():
    if sys.platform == "win32":
        rows = _powershell("@(Get-NetNeighbor -AddressFamily IPv4 | "
                           "Where-Object { $_.State -in @('Reachable','Stale','Delay','Probe','Permanent') } | "
                           "Select-Object IPAddress,LinkLayerAddress,@{Name='Reachable';Expression={$_.State -eq 'Reachable'}}) | ConvertTo-Json -Compress")
        pairs = ((r.get("IPAddress"), r.get("LinkLayerAddress"), r.get("Reachable", False)) for r in rows)
    elif sys.platform.startswith("linux"):
        rows = json.loads(_command(["ip", "-j", "-4", "neigh", "show"]))
        pairs = ((r.get("dst"), r.get("lladdr"), "REACHABLE" in r.get("state", [])) for r in rows)
    else:
        return {}
    result = {}
    for ip, mac, reachable in pairs:
        mac = (mac or "").replace("-", ":").lower()
        if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac) and mac not in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
            result[ip] = {"mac": mac, "reachable": bool(reachable)}
    return result


class RateLimit:
    def __init__(self, rate, deadline):
        self.interval, self.deadline = 1 / rate, deadline
        self.lock = threading.Lock()
        self.next_at = 0.0

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            when = max(now, self.next_at)
            if when >= self.deadline:
                return False
            self.next_at = when + self.interval
        time.sleep(max(0, when - now))
        return time.monotonic() < self.deadline


def clean_text(value, limit=240):
    value = "".join(c if c.isprintable() else " " for c in value)
    return " ".join(value.split())[:limit]


def dns_name(ip, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        # Separate, killable process: gethostbyaddr can block without a timeout.
        text = _command(["nslookup", "-timeout=1", "-retry=1", ip], timeout=min(2, remaining))
        match = re.search(r"(?:^\s*(?:Name|Nazwa)\s*:\s*|name\s*=\s*)([^\s]+)", text, re.I | re.M)
        return clean_text(match.group(1).rstrip("."), 255) if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def banner(ip, port, limiter):
    if not limiter.acquire():
        return ""
    try:
        with socket.create_connection((ip, port), timeout=SOCKET_TIMEOUT) as raw:
            if port == 443:
                # Public, unauthenticated banner only. CMDB transport keeps strict TLS.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                connection = context.wrap_socket(raw, server_hostname=ip)
            else:
                connection = raw
            with connection:
                if port in (80, 443, 631):
                    connection.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode("ascii"))
                text = connection.recv(4096).decode("utf-8", errors="replace")
                if port == 22:
                    return clean_text(text.splitlines()[0]) if text else ""
                server = re.search(r"^Server:\s*([^\r\n]+)", text, re.I | re.M)
                title = re.search(r"<title[^>]*>([^<]{0,200})</title>", text, re.I)
                return clean_text("; ".join(m.group(1) for m in (server, title) if m))
    except (OSError, ValueError):
        return ""


def classify(ports, banners):
    text = " ".join(banners).lower()
    kind, os_hint, confidence = "inne", "", "unknown"
    evidence = [f"TCP {p} otwarty" for p in ports] + banners
    printer = any(p in ports for p in (515, 631, 9100))
    if printer:
        kind, confidence = "drukarka", "low"
    if any(word in text for word in ("jetdirect", "laserjet", "brother", "epson", "kyocera", "xerox")):
        kind, confidence = "drukarka", "medium"
    elif any(word in text for word in ("routeros", "mikrotik", "cisco", "ubiquiti", "fortinet")):
        kind, confidence = "siec", "medium"
    elif not printer and any(p in ports for p in (135, 139, 445, 3389)):
        kind, confidence = "komputer", "low"
    # Ports alone do not establish an OS (Samba, xrdp, CUPS, appliances).
    if "openssh_for_windows" in text or "microsoft-iis" in text:
        os_hint = "Windows (prawdopodobny)"
    elif any(word in text for word in ("ubuntu", "debian", "linux")):
        os_hint = "Linux (prawdopodobny)"
        if kind == "inne":
            kind, confidence = "komputer", "low"
    elif 445 in ports or 3389 in ports:
        os_hint = "Windows / Samba / RDP — do weryfikacji"
    elif 22 in ports:
        os_hint = "SSH — system nierozpoznany"
    manufacturer = next((label for word, label in (("laserjet", "HP"), ("jetdirect", "HP"),
        ("brother", "Brother"), ("epson", "Epson"), ("kyocera", "Kyocera"), ("xerox", "Xerox"),
        ("mikrotik", "MikroTik"), ("cisco", "Cisco"), ("ubiquiti", "Ubiquiti")) if word in text), "")
    return {"device_type": kind, "os_hint": os_hint, "confidence": confidence,
            "manufacturer": manufacturer, "evidence": evidence}


def probe_host(ip, limiter):
    ports, alive, complete = [], False, True
    for port in PORTS:
        if not limiter.acquire():
            complete = False
            break
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(SOCKET_TIMEOUT)
                result = connection.connect_ex((ip, port))
                alive |= result in (0, errno.ECONNREFUSED, 10061)
                if result == 0:
                    ports.append(port)
        except OSError:
            continue
    if not alive:
        return None, complete
    banners = [value for p in (22, 80, 443, 631) if p in ports and (value := banner(ip, p, limiter))]
    return {"ip": ip, "hostname": dns_name(ip, limiter.deadline), "mac": "", "ports": ports,
            **classify(ports, banners)}, complete


def scan(config):
    validate_config(config)
    started = datetime.now(timezone.utc).isoformat()
    result = {"scanned_at": started, "ranges": [], "devices": [], "errors": [],
              "complete": False, "attempted_hosts": 0, "total_hosts": 0}
    try:
        cidrs = list(config.discovery_cidrs)
        if config.discovery_auto_subnets:
            cidrs += local_subnets()
        networks = networks_for(cidrs, config.discovery_max_hosts)
        if not networks:
            raise ValueError("Nie znaleziono prywatnej podsieci. Podaj zakresy CIDR recznie.")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result["errors"].append(clean_text(str(exc), 500))
        return result
    result["ranges"] = [str(n) for n in networks]
    hosts = [str(ip) for n in networks for ip in n.hosts()]
    result["total_hosts"] = len(hosts)
    limiter = RateLimit(config.discovery_rate, time.monotonic() + config.discovery_budget_seconds)
    complete = True
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="cmdb-discovery") as pool:
        for device, finished in pool.map(lambda ip: probe_host(ip, limiter), hosts):
            result["attempted_hosts"] += int(finished)
            complete &= finished
            if device:
                result["devices"].append(device)
    try:
        cache = neighbors()
        found = {d["ip"] for d in result["devices"]}
        targets = set(hosts)
        for device in result["devices"]:
            device["mac"] = cache.get(device["ip"], {}).get("mac", "")
        # A local host may answer ARP but filter every TCP probe. Only a
        # Reachable entry is evidence of activity; Stale/Permanent is not.
        for ip, entry in cache.items():
            if ip in targets and ip not in found and entry["reachable"]:
                result["devices"].append({"ip": ip, "mac": entry["mac"], "hostname": "", "ports": [],
                    **classify([], ["Aktywny wpis ARP (Reachable); brak odpowiedzi TCP w badanym zestawie."])})
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result["errors"].append("Nie odczytano MAC: " + clean_text(str(exc), 400))
    result["complete"] = complete
    if not complete:
        result["errors"].append("Osiagnieto limit czasu; wynik czesciowy. Podziel zakres lub zwieksz budzet.")
    return result


def limit_result(result, max_bytes=1024 * 1024):
    """Reserve room for normal inventory; never let banners grow without bound."""
    devices = result["devices"]
    result["devices"] = []
    # Include space for the explanatory error and JSON separators.
    used = len(json.dumps(result, ensure_ascii=False).encode("utf-8")) + 1024
    for device in devices:
        size = len(json.dumps(device, ensure_ascii=False).encode("utf-8")) + 2
        if used + size > max_bytes:
            result["complete"] = False
            result["errors"].append("Limit rozmiaru wynikow (1 MiB); wynik czesciowy. Podziel zakres skanowania.")
            break
        result["devices"].append(device)
        used += size
    return result


def authorized_config(config, state, client):
    """Fresh, nonce-bound policy only. Persisted state is NEVER authorization."""
    if client is None or not state.is_enrolled:
        raise ValueError("brak uwierzytelnionego polaczenia z CMDB")
    nonce = secrets.token_hex(16)
    payload = client.get("/api/v1/agent/discovery-policy?nonce=" + nonce, state.agent_token)
    if (not isinstance(payload, dict) or type(payload.get("protocol")) is not int or payload.get("protocol") != 1 or
            payload.get("nonce") != nonce or payload.get("asset_id") != state.asset_id or
            payload.get("machine_id") != state.machine_id):
        raise ValueError("niezgodna odpowiedz polityki CMDB")
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None or not 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 65:
        raise ValueError("wygasla lub niepoprawna waznosc polityki CMDB")
    revision = payload["revision"]
    policy = payload["policy"]
    expected = {"enabled", "auto_subnets", "cidrs", "interval_seconds", "max_hosts", "rate", "budget_seconds"}
    if (not isinstance(revision, str) or not 1 <= len(revision) <= 36 or
            not isinstance(policy, dict) or set(policy) != expected or
            type(policy["enabled"]) is not bool or type(policy["auto_subnets"]) is not bool):
        raise ValueError("niepoprawny format polityki CMDB")
    effective = replace(config, **{"discovery_" + name: value for name, value in policy.items()})
    validate_config(effective)
    return effective, revision


def attach_discovery(config, state, report, client=None):
    now = datetime.now(timezone.utc)
    try:
        effective, revision = authorized_config(config, state, client)
    except Exception as exc:
        # A failed policy fetch must not stop normal inventory or use a cached grant.
        log.warning("skanowanie zablokowane: %s", exc)
        state.discovery_status = {"state": "unavailable", "enabled": False, "checked_at": now.isoformat()}
        return
    state.discovery_status = {"state": "waiting" if effective.discovery_enabled else "disabled",
                              "enabled": effective.discovery_enabled, "checked_at": now.isoformat(),
                              "revision": revision, "last_scan_at": state.last_discovery_at}
    if revision != state.discovery_policy_revision:
        state.last_discovery_at = ""
        state.discovery_policy_revision = revision
    if not effective.discovery_enabled:
        return
    try:
        last = datetime.fromisoformat(state.last_discovery_at)
        last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last.astimezone(timezone.utc)
        if 0 <= (now - last).total_seconds() < effective.discovery_interval_seconds:
            return
    except (ValueError, TypeError):
        pass
    try:
        from . import status as public_status
        state.discovery_status["state"] = "running"
        public_status.publish(config, state)
        observation = scan(effective)
    except Exception as exc:
        log.exception("blad wykrywania sieci")
        observation = {"scanned_at": now.isoformat(), "ranges": [], "devices": [],
                       "errors": [clean_text(str(exc), 500)], "complete": False,
                       "attempted_hosts": 0, "total_hosts": 0}
    observation = limit_result(observation)
    report["network_discovery"] = observation
    for error in observation["errors"]:
        report.setdefault("errors", []).append({"section": "network_discovery", "message": error})
    state.last_discovery_at = now.isoformat()
    state.discovery_status.update(state="completed" if observation.get("complete") else "partial",
                                  last_scan_at=state.last_discovery_at, ranges=observation.get("ranges", []))
