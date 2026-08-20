"""Diagnostyka polaczenia z serwerem: 'cmdb-agent doctor'.

Rejestracja, ktora sie zawiesza, nie mowi nic o przyczynie - agent czeka do
konca limitu i dopiero wtedy zglasza blad. Ten modul rozklada polaczenie na
etapy i mierzy kazdy z osobna, wiec od razu widac, ktory zawodzi:

    nazwa -> adresy -> TCP -> TLS -> HTTP

Najczestsze przypadki, ktore to wychwytuje:
  * 'localhost' rozwiazuje sie na ::1, a serwer nasluchuje tylko na IPv4,
  * zapora odrzuca pakiety po cichu (TCP wisi do konca limitu),
  * certyfikat serwera nie jest podpisany przez wskazany plik CA.
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from .transport import build_ssl_context

# Krotki limit - chodzi o szybka odpowiedz, nie o doprowadzenie polaczenia.
PROBE_TIMEOUT = 5.0


OK = "ok"
WARN = "warn"
ERROR = "error"

_MARKS = {OK: "OK   ", WARN: "UWAGA", ERROR: "BLAD "}


@dataclass
class Step:
    name: str
    status: str
    detail: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def is_error(self) -> bool:
        """Tylko to przesadza o werdykcie.

        Nieudana proba na jednym z kilku adresow nie jest bledem, jesli inny
        adres dziala - klient i tak przechodzi do kolejnego. Traktowanie jej
        jak awarii dawalo werdykt "polaczenie nie dziala" mimo dzialajacego
        polaczenia."""
        return self.status == ERROR

    def render(self) -> str:
        return f"  [{_MARKS[self.status]}] {self.name:<34} {self.seconds:6.2f} s  {self.detail}"


def _timed(func):
    started = time.monotonic()
    try:
        return func(), None, time.monotonic() - started
    except Exception as exc:
        return None, exc, time.monotonic() - started


def resolve(host: str, port: int) -> tuple[list[tuple], Step]:
    def do():
        return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)

    result, error, seconds = _timed(do)
    if error is not None:
        return [], Step("Rozwiazanie nazwy", ERROR, f"{type(error).__name__}: {error}", seconds)

    families = []
    for family, _, _, _, sockaddr in result:
        label = "IPv6" if family == socket.AF_INET6 else "IPv4"
        families.append(f"{sockaddr[0]} ({label})")
    return result, Step("Rozwiazanie nazwy", OK, ", ".join(families), seconds)


def probe_tcp(addrinfo: list[tuple]) -> tuple[tuple | None, list[Step]]:
    """Sprawdza KAZDY zwrocony adres osobno - to tu wychodzi problem z IPv6."""
    steps: list[Step] = []
    working = None
    for family, socktype, proto, _, sockaddr in addrinfo:
        label = "IPv6" if family == socket.AF_INET6 else "IPv4"
        name = f"Polaczenie TCP {sockaddr[0]} ({label})"

        def do(family=family, socktype=socktype, proto=proto, sockaddr=sockaddr):
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(PROBE_TIMEOUT)
            try:
                sock.connect(sockaddr)
            finally:
                sock.close()
            return True

        _, error, seconds = _timed(do)
        if error is None:
            steps.append(Step(name, OK, "polaczono", seconds))
            if working is None:
                working = (family, socktype, proto, sockaddr)
        elif isinstance(error, socket.timeout):
            steps.append(
                Step(name, ERROR, "brak odpowiedzi - zapora odrzuca pakiety po cichu", seconds)
            )
        elif isinstance(error, ConnectionRefusedError):
            steps.append(Step(name, ERROR, "odmowa - nikt nie nasluchuje na tym adresie", seconds))
        else:
            steps.append(Step(name, ERROR, f"{type(error).__name__}: {error}", seconds))
    return working, steps


def probe_tls(host: str, sockaddr, family, ca_bundle: str | None) -> Step:
    def do():
        context = build_ssl_context(ca_bundle)
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(PROBE_TIMEOUT)
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.connect(sockaddr)
            return tls.version(), tls.getpeercert()

    result, error, seconds = _timed(do)
    if error is None:
        version, cert = result
        subject = dict(x[0] for x in cert.get("subject", ())) if cert else {}
        return Step("Uzgodnienie TLS", OK, f"{version}, CN={subject.get('commonName', '?')}", seconds)

    if isinstance(error, ssl.SSLCertVerificationError):
        # Rozrozniamy przyczyny - inaczej odsylamy uzytkownika po zly plik CA,
        # podczas gdy problemem jest nazwa hosta albo data waznosci.
        reason = (getattr(error, "verify_message", "") or str(error)).lower()
        if "hostname mismatch" in reason or "doesn't match" in reason:
            detail = f"certyfikat nie obejmuje nazwy '{host}' - uzyj nazwy lub adresu z certyfikatu"
        elif "expired" in reason:
            detail = "certyfikat serwera wygasl"
        else:
            detail = "certyfikat niezaufany - wskaz plik CA serwera (--ca-bundle)"
        return Step("Uzgodnienie TLS", ERROR, detail, seconds)
    return Step("Uzgodnienie TLS", ERROR, f"{type(error).__name__}: {error}", seconds)


def probe_health(base_url: str, ca_bundle: str | None) -> Step:
    def do():
        context = build_ssl_context(ca_bundle)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/v1/health", headers={"User-Agent": "cmdb-agent/doctor"}
        )
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    result, error, seconds = _timed(do)
    if error is None:
        return Step("Odpowiedz serwera CMDB", OK, f"schema_version={result.get('schema_version')}", seconds)
    if isinstance(error, urllib.error.HTTPError):
        return Step("Odpowiedz serwera CMDB", ERROR, f"HTTP {error.code}", seconds)
    return Step("Odpowiedz serwera CMDB", ERROR, f"{type(error).__name__}: {error}", seconds)


def run_diagnostics(server_url: str, ca_bundle: str | None = None) -> tuple[list[Step], list[str]]:
    """Zwraca (kroki, podpowiedzi)."""
    parsed = urlsplit(server_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    steps: list[Step] = []
    hints: list[str] = []

    if parsed.scheme != "https":
        steps.append(Step("Adres serwera", ERROR, "agent wymaga https://", 0.0))
        return steps, ["Popraw adres serwera - agent nie wysyla danych po http."]
    steps.append(Step("Adres serwera", OK, f"{host}:{port}", 0.0))

    addrinfo, resolve_step = resolve(host, port)
    steps.append(resolve_step)
    if not addrinfo:
        hints.append("Sprawdz pisownie nazwy serwera i ustawienia DNS.")
        return steps, hints

    working, tcp_steps = probe_tcp(addrinfo)

    # Jesli ktorykolwiek adres dziala, nieudane proby na pozostale nie sa
    # awaria - klient przechodzi do kolejnego adresu z listy. Kosztuja jednak
    # czas przy KAZDYM zadaniu, wiec zglaszamy je jako ostrzezenie.
    wasted = 0.0
    if working is not None:
        for step in tcp_steps:
            if step.status == ERROR:
                step.status = WARN
                wasted = max(wasted, step.seconds)
    steps.extend(tcp_steps)

    skipped = [s for s in tcp_steps if s.status == WARN]
    if skipped:
        families = ", ".join(s.name.replace("Polaczenie TCP ", "") for s in skipped)
        hint = (
            f"Nazwa rozwiazuje sie takze na {families}, gdzie serwer nie nasluchuje. "
            "Polaczenie dziala, ale agent traci czas na nieudana probe"
        )
        if wasted >= 0.5:
            hint += f" - okolo {wasted:.1f} s przy kazdym zadaniu"
        hint += ". Wpisz adres IPv4 wprost (np. https://127.0.0.1:8443), zeby to pominac."
        hints.append(hint)

    if any(s.status == ERROR and "zapora" in s.detail for s in tcp_steps):
        hints.append(
            "Brak odpowiedzi na TCP zwykle oznacza regule zapory odrzucajaca pakiety. "
            "Sprawdz Zapore Windows Defender po stronie serwera."
        )

    if working is None:
        hints.append("Zaden adres nie przyjal polaczenia - upewnij sie, ze serwer dziala.")
        return steps, hints

    family, _, _, sockaddr = working
    tls_step = probe_tls(host, sockaddr, family, ca_bundle)
    steps.append(tls_step)
    if not tls_step.ok:
        if "niezaufany" in tls_step.detail:
            hints.append(
                "Certyfikat self-signed wymaga jawnego wskazania pliku CA - "
                "pole 'Certyfikat CA' w oknie ustawien albo --ca-bundle."
            )
        elif "nie obejmuje nazwy" in tls_step.detail:
            hints.append(
                "Wpisz w agencie dokladnie te nazwe lub adres, na ktory wystawiono "
                "certyfikat. Skrypt quickstart.py wystawia go dla 'localhost', "
                "nazwy maszyny i adresow lokalnych."
            )
        return steps, hints
    if not tls_step.ok:
        return steps, hints

    steps.append(probe_health(server_url, ca_bundle))
    return steps, hints


def render_report(server_url: str, ca_bundle: str | None = None) -> tuple[str, bool]:
    steps, hints = run_diagnostics(server_url, ca_bundle)
    lines = [f"\nDiagnostyka polaczenia z {server_url}\n"]
    lines += [step.render() for step in steps]

    healthy = not any(step.is_error for step in steps)
    warned = any(step.status == WARN for step in steps)

    lines.append("")
    if not healthy:
        lines.append("  Wynik: polaczenie NIE dziala.")
    elif warned:
        lines.append("  Wynik: polaczenie dziala, ale da sie je przyspieszyc.")
    else:
        lines.append("  Wynik: polaczenie sprawne.")
    for hint in hints:
        lines.append(f"\n  Podpowiedz: {hint}")
    lines.append("")
    return "\n".join(lines), healthy
