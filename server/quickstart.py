"""Uruchomienie serwera CMDB jednym poleceniem - do testow i pierwszego kontaktu.

    python quickstart.py

Skrypt jest idempotentny: przy kolejnych uruchomieniach uzywa juz istniejacego
certyfikatu, bazy i tokenu, zamiast generowac nowe.

Co robi:
  1. wystawia certyfikat self-signed dla localhost, nazwy maszyny i adresow
     lokalnych - agent nie wysyla danych po nieszyfrowanym polaczeniu, wiec
     TLS jest wymagany takze w tescie,
  2. tworzy baze SQLite, firme, konto administratora i token rejestracyjny,
  3. wypisuje gotowe dane do wklejenia w oknie ustawien agenta,
  4. startuje serwer.

Do produkcji uzyj deploy/docker-compose.yml - tam jest PostgreSQL, nginx
i certyfikat publicznego urzedu.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKDIR = HERE / ".quickstart"
CERT_PATH = WORKDIR / "server.crt"
KEY_PATH = WORKDIR / "server.key"
DB_PATH = WORKDIR / "cmdb.db"
SECRET_PATH = WORKDIR / "secret.key"
TOKEN_PATH = WORKDIR / "token.txt"

TENANT_SLUG = "moja-firma"
TENANT_NAME = "Moja Firma"
ADMIN_EMAIL = "admin@moja-firma.pl"
ADMIN_PASSWORD = "cmdb-haslo-testowe-2026"

CERT_DAYS = 825  # maksimum akceptowane przez wspolczesne klienty TLS


def fail(message: str, hint: str = "") -> None:
    print(f"\n  BLAD: {message}")
    if hint:
        print(f"  {hint}")
    sys.exit(1)


def check_dependencies() -> None:
    missing = []
    for module, package in [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("sqlalchemy", "sqlalchemy"),
        ("pydantic_settings", "pydantic-settings"),
        ("jinja2", "jinja2"),
        ("argon2", "argon2-cffi"),
        ("multipart", "python-multipart"),
        ("itsdangerous", "itsdangerous"),
        ("cryptography", "cryptography"),
    ]:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        fail(
            "brakuje bibliotek: " + ", ".join(missing),
            f"Zainstaluj je poleceniem:\n    {sys.executable} -m pip install "
            + " ".join(f'"{m}"' for m in missing),
        )


def local_addresses() -> list[str]:
    """Adresy, pod ktorymi serwer bedzie osiagalny - trafiaja do SAN certyfikatu."""
    addresses = {"127.0.0.1"}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Gniazdo UDP nie wysyla pakietu; jadro przypisuje adres wyjsciowy.
            sock.connect(("192.0.2.1", 53))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(addresses)


def make_certificate(hostnames: list[str], addresses: list[str]) -> None:
    """Certyfikat self-signed. Uzywamy biblioteki cryptography, bo na Windows
    zwykle nie ma polecenia openssl."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CMDB quickstart"),
    ])
    alt_names: list[x509.GeneralName] = [x509.DNSName(name) for name in hostnames]
    alt_names += [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]

    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_PATH.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    if os.name == "posix":
        os.chmod(KEY_PATH, 0o600)


def ensure_certificate() -> list[str]:
    hostname = socket.gethostname()
    hostnames = ["localhost", hostname]
    if "." in hostname:
        hostnames.append(hostname.split(".")[0])
    addresses = local_addresses()

    if CERT_PATH.is_file() and KEY_PATH.is_file():
        print(f"  certyfikat        : {CERT_PATH} (istniejacy)")
    else:
        make_certificate(sorted(set(hostnames)), addresses)
        print(f"  certyfikat        : {CERT_PATH} (nowy, wazny {CERT_DAYS} dni)")
    return addresses


def ensure_secret() -> str:
    if not SECRET_PATH.is_file():
        SECRET_PATH.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        if os.name == "posix":
            os.chmod(SECRET_PATH, 0o600)
    return SECRET_PATH.read_text(encoding="utf-8").strip()


def bootstrap_database() -> str:
    """Tworzy schemat, firme, konto panelu i token. Zwraca token rejestracyjny."""
    from sqlalchemy import select

    from cmdb_server.db import init_db, session_scope
    from cmdb_server.models import EnrollmentToken, PortalUser, Tenant
    from cmdb_server.security import generate_token, hash_password

    init_db()

    with session_scope() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG)).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name=TENANT_NAME, slug=TENANT_SLUG)
            db.add(tenant)
            db.flush()
            print(f"  firma             : {TENANT_NAME} ({TENANT_SLUG})")
        else:
            print(f"  firma             : {tenant.name} ({tenant.slug}) (istniejaca)")

        user = db.execute(
            select(PortalUser).where(PortalUser.email == ADMIN_EMAIL)
        ).scalar_one_or_none()
        if user is None:
            db.add(
                PortalUser(
                    tenant_id=tenant.id,
                    email=ADMIN_EMAIL,
                    full_name="Administrator",
                    password_hash=hash_password(ADMIN_PASSWORD),
                    role="admin",
                )
            )
            print(f"  konto panelu      : {ADMIN_EMAIL}")
        else:
            print(f"  konto panelu      : {ADMIN_EMAIL} (istniejace)")

        # Token pokazujemy tylko raz, wiec zapisujemy go obok bazy.
        if TOKEN_PATH.is_file():
            token_value = TOKEN_PATH.read_text(encoding="utf-8").strip()
            still_valid = db.execute(
                select(EnrollmentToken).where(
                    EnrollmentToken.tenant_id == tenant.id,
                    EnrollmentToken.revoked_at.is_(None),
                )
            ).scalars().first()
            if token_value and still_valid is not None:
                print("  token agenta      : (istniejacy)")
                return token_value

        token = generate_token("ent")
        db.add(
            EnrollmentToken(
                tenant_id=tenant.id,
                name="quickstart",
                prefix=token.prefix,
                token_hash=token.token_hash,
                created_by="quickstart",
            )
        )
        TOKEN_PATH.write_text(token.plaintext, encoding="utf-8")
        if os.name == "posix":
            os.chmod(TOKEN_PATH, 0o600)
        print("  token agenta      : (nowy)")
        return token.plaintext


def print_instructions(token: str, port: int, addresses: list[str]) -> None:
    lan = [a for a in addresses if a != "127.0.0.1"]
    line = "=" * 74

    print("\n" + line)
    print("  DANE DO WKLEJENIA W AGENCIE")
    print(line)
    print(f"\n  Adres serwera : https://localhost:{port}")
    if lan:
        print(f"  (z innej maszyny w sieci: https://{lan[0]}:{port})")
    print(f"\n  Token         : {token}")
    print(f"\n  Certyfikat CA : {CERT_PATH}")
    print("\n  Certyfikat jest self-signed, wiec agent musi go dostac jawnie -")
    print("  wskaz powyzszy plik w polu 'Certyfikat CA' okna ustawien.")

    print("\n" + line)
    print("  AGENT - z wiersza polecen")
    print(line)
    print("\n  cmdb-agent.exe --server https://localhost:%d ^" % port)
    print(f"      --token {token} ^")
    print(f'      --ca-bundle "{CERT_PATH}" enroll')
    print("  cmdb-agent.exe run")

    print("\n" + line)
    print("  PANEL WWW")
    print(line)
    print(f"\n  https://localhost:{port}/")
    print(f"  login : {ADMIN_EMAIL}")
    print(f"  haslo : {ADMIN_PASSWORD}")
    print("\n  Przegladarka ostrzeze o certyfikacie self-signed - to oczekiwane.")
    print(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Uruchamia serwer CMDB do testow")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="0.0.0.0 = dostepny takze dla innych maszyn w sieci lokalnej",
    )
    parser.add_argument(
        "--print-only", action="store_true",
        help="tylko wypisz dane dostepowe, bez uruchamiania serwera",
    )
    args = parser.parse_args()

    print("\n== CMDB - uruchomienie testowe ==\n")
    check_dependencies()

    WORKDIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))

    # Konfiguracje ustawiamy PRZED importem serwera - ustawienia i silnik bazy
    # tworza sie na poziomie modulu.
    os.environ.setdefault("CMDB_DATABASE_URL", f"sqlite:///{DB_PATH}")
    os.environ.setdefault("CMDB_SECRET_KEY", ensure_secret())
    os.environ.setdefault("CMDB_ENV", "dev")
    os.environ.setdefault("CMDB_REQUIRE_HTTPS", "true")
    # Zrodla agenta leza obok - dzieki temu serwer sam odswieza paczke
    # do pobrania i instalacja jednym poleceniem dziala od razu.
    zrodla_agenta = Path(__file__).resolve().parent.parent / "agent"
    if (zrodla_agenta / "cmdb_agent").is_dir():
        os.environ.setdefault("CMDB_AGENT_SOURCE_DIR", str(zrodla_agenta))

    addresses = ensure_certificate()
    print(f"  baza danych       : {DB_PATH}")
    token = bootstrap_database()
    print_instructions(token, args.port, addresses)

    if args.print_only:
        return 0

    print(f"  Serwer startuje na https://{args.host}:{args.port}  (Ctrl+C konczy)\n")
    command = [
        sys.executable, "-m", "uvicorn", "cmdb_server.main:app",
        "--host", args.host, "--port", str(args.port),
        "--ssl-keyfile", str(KEY_PATH), "--ssl-certfile", str(CERT_PATH),
    ]
    try:
        return subprocess.call(command, cwd=str(HERE))
    except KeyboardInterrupt:
        print("\n  Serwer zatrzymany.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
