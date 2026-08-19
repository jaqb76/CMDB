"""Narzedzie administracyjne CMDB.

    python -m cmdb_server.cli init-db
    python -m cmdb_server.cli tenant-create --name "Firma ABC" --slug abc
    python -m cmdb_server.cli user-create --email admin@abc.pl --tenant abc --role admin
    python -m cmdb_server.cli token-issue --tenant abc --name "stacje robocze"
"""
from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from datetime import timedelta

from sqlalchemy import select

from .db import init_db, session_scope
from .models import Asset, EnrollmentToken, Owner, PortalUser, Tenant, utcnow
from .security import generate_token, hash_password

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _tenant_by_slug(db, slug: str) -> Tenant:
    tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
    if tenant is None:
        sys.exit(f"blad: nie ma firmy o identyfikatorze '{slug}'")
    return tenant


def cmd_init_db(_args: argparse.Namespace) -> None:
    init_db()
    print("schemat bazy utworzony/zaktualizowany")


def cmd_tenant_create(args: argparse.Namespace) -> None:
    slug = args.slug.strip().lower()
    if not SLUG_RE.match(slug):
        sys.exit("blad: slug moze zawierac tylko male litery, cyfry i myslnik (2-63 znaki)")
    with session_scope() as db:
        if db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none():
            sys.exit(f"blad: firma '{slug}' juz istnieje")
        tenant = Tenant(name=args.name.strip(), slug=slug)
        db.add(tenant)
        db.flush()
        print(f"utworzono firme: {tenant.name} (slug={tenant.slug}, id={tenant.id})")


def cmd_tenant_list(_args: argparse.Namespace) -> None:
    with session_scope() as db:
        tenants = db.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
        if not tenants:
            print("brak zdefiniowanych firm")
            return
        print(f"{'SLUG':<20} {'NAZWA':<32} {'MASZYN':>7}  STATUS")
        for t in tenants:
            count = len(
                db.execute(select(Asset.id).where(Asset.tenant_id == t.id)).scalars().all()
            )
            status = "aktywna" if t.is_active else "wylaczona"
            print(f"{t.slug:<20} {t.name:<32} {count:>7}  {status}")


def cmd_user_create(args: argparse.Namespace) -> None:
    email = args.email.strip().lower()
    password = args.password or getpass.getpass("haslo: ")
    if len(password) < 12:
        sys.exit("blad: haslo musi miec co najmniej 12 znakow")

    with session_scope() as db:
        if db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none():
            sys.exit(f"blad: uzytkownik {email} juz istnieje")

        tenant_id = None
        if args.superadmin:
            if args.tenant:
                sys.exit("blad: superadmin nie moze byc przypisany do firmy")
        else:
            if not args.tenant:
                sys.exit("blad: podaj --tenant albo --superadmin")
            tenant_id = _tenant_by_slug(db, args.tenant).id

        db.add(
            PortalUser(
                tenant_id=tenant_id,
                email=email,
                full_name=args.full_name,
                password_hash=hash_password(password),
                role="admin" if args.superadmin else args.role,
                is_superadmin=args.superadmin,
            )
        )
        scope = "superadmin (wszystkie firmy)" if args.superadmin else f"firma {args.tenant}"
        print(f"utworzono uzytkownika {email} - {scope}")


def cmd_token_issue(args: argparse.Namespace) -> None:
    with session_scope() as db:
        tenant = _tenant_by_slug(db, args.tenant)
        token = generate_token("ent")
        db.add(
            EnrollmentToken(
                tenant_id=tenant.id,
                name=args.name,
                prefix=token.prefix,
                token_hash=token.token_hash,
                expires_at=utcnow() + timedelta(days=args.days) if args.days else None,
                created_by="cli",
            )
        )
        print(f"token rejestracyjny dla firmy '{tenant.slug}' ({args.name}):\n")
        print(f"  {token.plaintext}\n")
        print("Zapisz go teraz - w bazie zostaje wylacznie skrot SHA-256.")
        print("Instalacja agenta:")
        print(f"  cmdb-agent enroll --server https://cmdb.example.com --token {token.plaintext}")


def cmd_owner_add(args: argparse.Namespace) -> None:
    with session_scope() as db:
        tenant = _tenant_by_slug(db, args.tenant)
        email = args.email.strip().lower()
        existing = db.execute(
            select(Owner).where(Owner.tenant_id == tenant.id, Owner.email == email)
        ).scalar_one_or_none()
        if existing:
            sys.exit(f"blad: opiekun {email} juz istnieje w firmie {tenant.slug}")
        db.add(
            Owner(
                tenant_id=tenant.id,
                full_name=args.name,
                email=email,
                phone=args.phone,
                department=args.department,
            )
        )
        print(f"dodano opiekuna {args.name} <{email}> do firmy {tenant.slug}")


def cmd_demo_secret(_args: argparse.Namespace) -> None:
    print(secrets.token_urlsafe(48))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmdb-admin", description="Administracja serwerem CMDB")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="tworzy schemat bazy").set_defaults(func=cmd_init_db)

    p = sub.add_parser("tenant-create", help="dodaje firme (tenant)")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True, help="krotki identyfikator, np. 'abc'")
    p.set_defaults(func=cmd_tenant_create)

    sub.add_parser("tenant-list", help="lista firm").set_defaults(func=cmd_tenant_list)

    p = sub.add_parser("user-create", help="dodaje uzytkownika panelu")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="pominiete = zapyta interaktywnie")
    p.add_argument("--full-name", dest="full_name")
    p.add_argument("--tenant", help="slug firmy")
    p.add_argument("--role", choices=["admin", "viewer"], default="viewer")
    p.add_argument("--superadmin", action="store_true", help="dostep do wszystkich firm")
    p.set_defaults(func=cmd_user_create)

    p = sub.add_parser("token-issue", help="wydaje token rejestracyjny dla firmy")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", default="token rejestracyjny")
    p.add_argument("--days", type=int, default=0, help="waznosc w dniach (0 = bezterminowo)")
    p.set_defaults(func=cmd_token_issue)

    p = sub.add_parser("owner-add", help="dodaje opiekuna zasobow")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--phone")
    p.add_argument("--department")
    p.set_defaults(func=cmd_owner_add)

    sub.add_parser("gen-secret", help="generuje losowy CMDB_SECRET_KEY").set_defaults(
        func=cmd_demo_secret
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
