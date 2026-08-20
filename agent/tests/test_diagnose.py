"""Diagnostyka polaczenia i budzety czasowe rejestracji."""
from __future__ import annotations

import socket
import ssl
import threading

import pytest

from cmdb_agent import diagnose
from cmdb_agent.config import (
    INTERACTIVE_MAX_RETRIES,
    INTERACTIVE_PROCESS_TIMEOUT,
    INTERACTIVE_TIMEOUT_SECONDS,
    AgentConfig,
)
from cmdb_agent.transport import worst_case_seconds


# --- budzety czasowe --------------------------------------------------------

def test_interactive_budget_fits_in_process_timeout():
    """Regresja: okno ustawien ubijalo agenta 76 s zanim ten zdazyl zglosic
    brak lacznosci - uzytkownik dostawal surowy wyjatek Pythona zamiast
    zrozumialego komunikatu."""
    budget = worst_case_seconds(INTERACTIVE_TIMEOUT_SECONDS, INTERACTIVE_MAX_RETRIES)
    assert budget < INTERACTIVE_PROCESS_TIMEOUT, (
        f"agent moze potrzebowac {budget:.0f} s, a okno ubija go po "
        f"{INTERACTIVE_PROCESS_TIMEOUT} s"
    )


def test_interactive_budget_is_short_enough_for_a_human():
    """Ktos patrzy na okno - odpowiedz ma przyjsc w kilkanascie sekund."""
    assert worst_case_seconds(INTERACTIVE_TIMEOUT_SECONDS, INTERACTIVE_MAX_RETRIES) <= 45


def test_background_budget_is_patient():
    """W tle cierpliwosc jest zaleta - agent ma przetrwac chwilowa awarie sieci."""
    defaults = AgentConfig()
    assert worst_case_seconds(defaults.timeout_seconds, defaults.max_retries) > 120


def test_worst_case_grows_with_retries():
    assert worst_case_seconds(10, 1) < worst_case_seconds(10, 2) < worst_case_seconds(10, 4)


# --- diagnostyka ------------------------------------------------------------

def test_http_address_is_rejected_before_any_probe():
    steps, hints = diagnose.run_diagnostics("http://cmdb.firma.pl")
    assert steps[0].ok is False
    assert "https" in steps[0].detail
    assert hints


def test_unknown_host_reports_resolution_failure():
    steps, hints = diagnose.run_diagnostics("https://nie-ma-takiej-nazwy.invalid:8443")
    resolution = [s for s in steps if s.name == "Rozwiazanie nazwy"]
    assert resolution and resolution[0].ok is False
    assert any("DNS" in h for h in hints)


def test_closed_port_reports_refusal():
    # Port zajmowany i natychmiast zwalniany - polaczenie zostanie odrzucone.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    steps, hints = diagnose.run_diagnostics(f"https://127.0.0.1:{port}")
    tcp = [s for s in steps if s.name.startswith("Polaczenie TCP")]
    assert tcp and tcp[0].ok is False
    assert "odmowa" in tcp[0].detail
    assert any("serwer dziala" in h for h in hints)


def test_silent_drop_reports_firewall(monkeypatch):
    """Zapora odrzucajaca pakiety po cichu daje timeout, nie odmowe."""
    monkeypatch.setattr(diagnose, "PROBE_TIMEOUT", 0.4)
    steps, hints = diagnose.run_diagnostics("https://10.255.255.1:8443")
    tcp = [s for s in steps if s.name.startswith("Polaczenie TCP")]
    assert tcp and tcp[0].ok is False
    assert "zapora" in tcp[0].detail
    assert any("zapory" in h for h in hints)


def test_untrusted_certificate_is_distinguished_from_name_mismatch(tls_server):
    """Dwie rozne przyczyny, dwie rozne rady - mylenie ich wysyla uzytkownika
    po zly plik CA."""
    host, port, ca_path = tls_server

    steps, hints = diagnose.run_diagnostics(f"https://127.0.0.1:{port}")
    tls = [s for s in steps if s.name == "Uzgodnienie TLS"]
    assert tls and tls[0].ok is False
    assert "niezaufany" in tls[0].detail
    assert any("pliku CA" in h for h in hints)


def test_healthy_connection_passes_every_step(tls_server, monkeypatch):
    host, port, ca_path = tls_server
    # Serwer testowy konczy na TLS - krok HTTP pomijamy.
    monkeypatch.setattr(
        diagnose, "probe_health",
        lambda url, ca: diagnose.Step("Odpowiedz serwera CMDB", True, "schema_version=1", 0.01),
    )
    steps, hints = diagnose.run_diagnostics(f"https://127.0.0.1:{port}", ca_path)
    assert all(step.ok for step in steps), [s.render() for s in steps if not s.ok]
    assert hints == []


def test_report_renders_and_reports_health():
    report, healthy = diagnose.render_report("http://zle.firma.pl")
    assert healthy is False
    assert "BLAD" in report
    assert "Wynik" in report


# --- pomocniczy serwer TLS --------------------------------------------------

@pytest.fixture
def tls_server(tmp_path):
    """Minimalny serwer TLS z certyfikatem self-signed dla 127.0.0.1."""
    cryptography = pytest.importorskip("cryptography")
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve():
        listener.settimeout(0.3)
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            try:
                with context.wrap_socket(client, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
            finally:
                client.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port, str(cert_path)
    finally:
        stop.set()
        thread.join(timeout=3)
        listener.close()
