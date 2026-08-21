"""Model danych CMDB.

Zasada: wszystko co przysyla agent ladujemy w JSON (payload snapshotu).
Do kolumn relacyjnych wyciagamy tylko to, po czym filtrujemy/sortujemy w UI.
Kazda tabela z danymi klienta niesie tenant_id - izolacja firm jest wymuszana
na poziomie zapytan (patrz services/scoping.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB na PostgreSQL, zwykly JSON na SQLite (dev/testy).
JSONType = JSON().with_variant(JSONB(), "postgresql")


# Cykl zycia zasobu. Wycofanie jest decyzja czlowieka i zostaje w bazie
# razem z cala historia - zasob znika z domyslnych list, ale nie z systemu.
LIFECYCLE_AKTYWNY = "aktywny"
LIFECYCLE_WYCOFANY = "wycofany"
LIFECYCLE_WSZYSTKIE = (LIFECYCLE_AKTYWNY, LIFECYCLE_WYCOFANY)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalizuje date odczytana z bazy do UTC-aware.

    PostgreSQL z DateTime(timezone=True) zwraca daty ze strefa, SQLite bez -
    porownanie jednych z drugimi rzuca TypeError. Wszystkie porownania w kodzie
    Pythona przepuszczamy przez ta funkcje.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """Firma / klient. Korzen izolacji danych."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Ustawienia firmowe. Puste znaczy "jak w konfiguracji serwera" - firmy
    # roznia sie charakterem: flota laptopow bywa offline tygodniami, a
    # serwerownia odzywa sie co godzine i tydzien ciszy to awaria.
    report_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    stale_after_hours: Mapped[int | None] = mapped_column(Integer)
    snapshot_retention: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    assets: Mapped[list["Asset"]] = relationship(back_populates="tenant")


class PortalUser(Base):
    """Uzytkownik panelu WWW. Zwykly user widzi tylko swoja firme."""

    __tablename__ = "portal_users"
    __table_args__ = (UniqueConstraint("email", name="uq_portal_user_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # admin  - moze zarzadzac tokenami, opiekunami i uzytkownikami swojej firmy
    # viewer - tylko odczyt
    role: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    # superadmin nie nalezy do zadnej firmy i widzi wszystkie
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant | None] = relationship()


class EnrollmentToken(Base):
    """Token rejestracyjny wydawany firmie.

    Agent uzywa go WYLACZNIE przy pierwszym uruchomieniu (enrollment), po czym
    dostaje wlasne, indywidualne poswiadczenie (AgentCredential). Dzieki temu
    wyciek z jednej maszyny nie kompromituje calej firmy.
    W bazie trzymamy wylacznie skrot SHA-256 - wartosc jawna pokazujemy raz.
    """

    __tablename__ = "enrollment_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255))

    tenant: Mapped[Tenant] = relationship()

    @property
    def is_usable(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires_at = as_utc(self.expires_at)
        if expires_at is not None and expires_at < utcnow():
            return False
        return True


class GlobalAgentTarget(Base):
    """Wersja agenta uznana za oficjalna - dla firm bez wlasnego ustawienia.

    Osobna tabela zamiast znacznika na wydaniu, bo warunek "tylko jedna
    oficjalna na system" musi byc niemozliwy do zlamania. Znacznik trzeba by
    pilnowac kodem, a blad w tym miejscu po cichu psulby rozstrzyganie dla
    wszystkich firm naraz. UNIQUE(os_family) zalatwia to strukturalnie.
    """

    __tablename__ = "global_agent_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    os_family: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    release_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_releases.id", ondelete="CASCADE"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255))


class TenantAgentTarget(Base):
    """Wersja agenta oczekiwana w danej firmie, osobno dla kazdego systemu.

    Pojedyncza maszyna moze miec wlasne ustawienie, ktore ma pierwszenstwo -
    pozwala to wypchnac nowa wersje na kilka maszyn testowych, nie ruszajac
    reszty floty.
    """

    __tablename__ = "tenant_agent_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "os_family", name="uq_target_tenant_os"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    os_family: Mapped[str] = mapped_column(String(32), nullable=False)
    release_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_releases.id", ondelete="CASCADE"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255))


class Owner(Base):
    """Opiekun / wlasciciel zasobu po stronie firmy."""

    __tablename__ = "owners"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_owner_tenant_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(64))
    department: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped[Tenant] = relationship()


class Asset(Base):
    """Maszyna (serwer/stacja). Kolumny = pola po ktorych filtrujemy w UI."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "machine_id", name="uq_asset_tenant_machine"),
        Index("ix_asset_tenant_lastseen", "tenant_id", "last_seen"),
        Index("ix_asset_tenant_hostname", "tenant_id", "hostname"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stabilny identyfikator maszyny liczony przez agenta (UUID plyty / machine-id).
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)

    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    fqdn: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))
    os_family: Mapped[str | None] = mapped_column(String(32), index=True)
    arch: Mapped[str | None] = mapped_column(String(16), index=True)
    os_name: Mapped[str | None] = mapped_column(String(200))
    os_version: Mapped[str | None] = mapped_column(String(100))
    manufacturer: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(200))
    serial_number: Mapped[str | None] = mapped_column(String(128), index=True)
    primary_ip: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32))

    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Rola maszyny wpisywana recznie w panelu (np. "serwer plikow", "laptop ksiegowosc").
    role_label: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list | None] = mapped_column(JSONType, default=list)

    # Skrocone podsumowanie do listy (cpu/ram/dyski) - zeby nie czytac calego payloadu.
    facts: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    # Aktualizacja agenta: ustawienie na maszynie ma pierwszenstwo przed firmowym.
    target_release_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_releases.id", ondelete="SET NULL")
    )
    upgrade_status: Mapped[str | None] = mapped_column(String(20))
    upgrade_detail: Mapped[str | None] = mapped_column(Text)
    upgrade_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Cykl zycia zasobu: decyzja czlowieka, a nie stan wyliczany.
    # "Bez kontaktu" celowo NIE jest tu zapisywane - wynika z last_seen
    # w chwili patrzenia. Zapisane po godzinie bylo by juz nieprawdziwe
    # i wymagaloby zadania, ktore je odswieza.
    lifecycle: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LIFECYCLE_AKTYWNY, index=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by: Mapped[str | None] = mapped_column(String(255))
    retired_reason: Mapped[str | None] = mapped_column(Text)

    # Zastapione przez lifecycle. Kolumna zostaje, bo istniejace bazy maja ja
    # jako NOT NULL bez wartosci domyslnej po stronie silnika - usuniecie
    # z modelu zepsulo by wstawianie nowych wierszy.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="assets")
    owner: Mapped[Owner | None] = relationship()
    snapshots: Mapped[list["InventorySnapshot"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="InventorySnapshot.collected_at.desc()"
    )


class AgentCredential(Base):
    """Indywidualne poswiadczenie agenta wydane po enrollmencie."""

    __tablename__ = "agent_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrollment_token_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("enrollment_tokens.id", ondelete="SET NULL")
    )
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    asset: Mapped[Asset] = relationship()

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None


class InventorySnapshot(Base):
    """Pelny raport agenta w postaci JSON - zrodlo prawdy o stanie maszyny."""

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        Index("ix_snapshot_asset_collected", "asset_id", "collected_at"),
        Index("ix_snapshot_tenant_collected", "tenant_id", "collected_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Skrot payloadu - identyczny raport nie tworzy nowego wiersza.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)

    asset: Mapped[Asset] = relationship(back_populates="snapshots")


class AgentRelease(Base):
    """Wersja agenta wgrana przez superadmina, gotowa do rozeslania.

    Sam plik lezy na dysku serwera (katalog z konfiguracji), w bazie trzymamy
    metadane i skrot. Skrot jest tu kluczowy: agent porownuje go po pobraniu
    i odmawia podmiany, jesli sie nie zgadza. Bez tego kanal aktualizacji
    bylby po prostu zdalnym uruchamianiem dowolnego kodu na kazdej maszynie.
    """

    __tablename__ = "agent_releases"
    __table_args__ = (
        UniqueConstraint("version", "os_family", "arch", name="uq_release_version_os_arch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Agent jest budowany osobno dla kazdego systemu - plik dla Windows nie
    # uruchomi sie na Linuksie i odwrotnie. Wersja bez tego rozroznienia
    # pozwalalaby wyslac maszynie plik, ktorego nie ma jak wykonac.
    os_family: Mapped[str] = mapped_column(String(32), nullable=False, default="windows", index=True)
    # Rodzina systemu nie wystarcza: ELF dla x86-64 i dla ARM64 to oba "linux",
    # a plik zbudowany dla jednej architektury nie uruchomi sie na drugiej.
    # Odczytywane z naglowka pliku, nie z deklaracji wgrywajacego.
    arch: Mapped[str] = mapped_column(String(16), nullable=False, default="x86_64", index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nazwa pliku w magazynie serwera (skrot + rozszerzenie).
    storage_name: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255))


class AgentUpgradeLog(Base):
    """Slad kazdej proby aktualizacji - kto zlecil, co sie stalo na maszynie."""

    __tablename__ = "agent_upgrade_log"
    __table_args__ = (Index("ix_upgrade_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    release_id: Mapped[str | None] = mapped_column(String(36))
    from_version: Mapped[str | None] = mapped_column(String(32))
    to_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # zlecona|pobrana|ok|blad
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetChange(Base):
    """Pojedyncza zmiana wykryta miedzy dwoma raportami maszyny.

    Roznice zapisujemy, zamiast liczyc je na zadanie. Inaczej pytanie
    "gdzie w zeszlym miesiacu doszlo konto administratora" wymagaloby
    wczytania i porownania kazdej pary raportow w calej flocie; tutaj jest
    to jedno zapytanie po indeksie.

    Wpisy powstaja tylko przy realnej zmianie - raport identyczny z poprzednim
    jest odrzucany wczesniej przez deduplikacje, wiec tabela rosnie w tempie
    zmian, a nie w tempie raportowania.
    """

    __tablename__ = "asset_changes"
    __table_args__ = (
        Index("ix_change_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_change_asset_time", "asset_id", "occurred_at"),
        Index("ix_change_kategoria", "tenant_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[str | None] = mapped_column(String(36))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # hardware | software | users | network | os
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    # dodano | usunieto | zmieniono
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    # Sciezka w raporcie, np. "software.packages" - do filtrowania.
    path: Mapped[str] = mapped_column(String(120), nullable=False)
    # Czytelny opis, np. "7-Zip 23.01" albo "NEWNODE\nowy_admin".
    label: Mapped[str] = mapped_column(String(400), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Slad audytowy operacji wrazliwych (tokeny, logowania, zmiany wlasciciela)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --- podatnosci -------------------------------------------------------------

class CveFeed(Base):
    """Stan kanalu danych o podatnosciach.

    Wiek danych jest czescia wyniku, dokladnie tak samo jak przy brakujacych
    aktualizacjach. "Brak znanych podatnosci" wedlug kanalu sprzed trzech
    miesiecy nie znaczy "maszyna bezpieczna" - znaczy "nie wiemy". Bez tego
    zapisu nie dalo by sie tego rozroznic.
    """

    __tablename__ = "cve_feeds"
    __table_args__ = (UniqueConstraint("source", "release", name="uq_cve_feed_source_release"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # "debian", "ubuntu" albo "msrc".
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Nazwa kodowa wydania ("bookworm", "jammy") albo "windows" dla MSRC.
    release: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "ok" albo "blad" - pobranie moglo sie nie udac, a stare dane zostaja.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    detail: Mapped[str | None] = mapped_column(Text)


class CveEntry(Base):
    """Pojedyncze "pakiet P w wydaniu W jest podatny na C ponizej wersji V".

    Klucz jest po pakiecie ZRODLOWYM, bo tak indeksuja dane dystrybucje.
    Zainstalowane sa pakiety binarne, wiec agent podaje jedno i drugie.
    """

    __tablename__ = "cve_entries"
    __table_args__ = (
        Index("ix_cve_lookup", "source", "release", "package"),
        UniqueConstraint("source", "release", "package", "cve", name="uq_cve_entry"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    release: Mapped[str] = mapped_column(String(64), nullable=False)
    package: Mapped[str] = mapped_column(String(255), nullable=False)
    cve: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Wersja, w ktorej luke naprawiono. Puste znaczy, ze poprawki jeszcze nie
    # ma - wtedy podatna jest kazda zainstalowana wersja.
    fixed_version: Mapped[str | None] = mapped_column(String(128))
    # "resolved" (jest poprawka) albo "open" (nie ma jeszcze poprawki).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="resolved")
    severity: Mapped[str | None] = mapped_column(String(32))
    # Powod, dla ktorego dystrybucja nie wyda poprawki - najczesciej
    # "Minor issue". Takie wpisy sa prawdziwe, ale nie sa do zrobienia:
    # zmieszane z reszta utopilyby to, na co administrator moze zareagowac.
    no_fix_reason: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
