"""Model danych CMDB.

Zasada: wszystko co przysyla agent ladujemy w JSON (payload snapshotu).
Do kolumn relacyjnych wyciagamy tylko to, po czym filtrujemy/sortujemy w UI.
Kazda tabela z danymi klienta niesie tenant_id - izolacja firm jest wymuszana
na poziomie zapytan (patrz services/scoping.py).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Raporty agentow trzymamy w JSONB - binarnej postaci Postgresa, po ktorej da
# sie szukac i ktora da sie zaindeksowac (indeksy GIN zaklada db.py).
JSONType = JSONB()


# Cykl zycia zasobu. Wycofanie jest decyzja czlowieka i zostaje w bazie
# razem z cala historia - zasob znika z domyslnych list, ale nie z systemu.
LIFECYCLE_AKTYWNY = "aktywny"
LIFECYCLE_WYCOFANY = "wycofany"
LIFECYCLE_WSZYSTKIE = (LIFECYCLE_AKTYWNY, LIFECYCLE_WYCOFANY)


# Skad wziely sie dane zasobu. Agent nie zaloguje sie do drukarki ani do
# switcha, wiec taki sprzet wpisuje czlowiek - i te wpisy trzeba odroznic,
# bo nie wolno ich oceniac miara "od kiedy nie bylo kontaktu".
ZRODLO_AGENT = "agent"
ZRODLO_RECZNE = "reczne"

# Rodzaj sprzetu. Maszyny z agentem sa komputerami; reszte wybiera czlowiek.
TYP_KOMPUTER = "komputer"
TYPY_SPRZETU: dict[str, str] = {
    TYP_KOMPUTER: "Komputer / serwer",
    "siec": "Sprzet sieciowy",
    "drukarka": "Drukarka / skaner",
    "monitor": "Monitor",
    "telefon": "Telefon / tablet",
    "inne": "Inne",
    "vm": "Maszyna wirtualna",
    "host": "Host wirtualizacji",
    "klaster": "Klaster",
    "aplikacja": "Aplikacja",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalizuje date do UTC-aware.

    Baza oddaje daty ze strefa, ale do porownan trafiaja tez daty z raportow
    agenta - a te sa napisami ISO i bywaja bez strefy. Porownanie daty ze
    strefa z data bez strefy rzuca TypeError, wiec wszystkie porownania
    w kodzie Pythona przepuszczamy przez ta funkcje.
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
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # admin  - moze zarzadzac tokenami, opiekunami i uzytkownikami swojej firmy
    # viewer - tylko odczyt
    role: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    # superadmin nie nalezy do zadnej firmy i widzi wszystkie
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Audytor: widzi wszystkie firmy, ale nie moze zmienic w nich niczego.
    # Osobna flaga, a nie rola "viewer" bez tenanta - rola opisuje uprawnienia
    # WEWNATRZ firmy, a to jest uprawnienie do przekraczania jej granicy.
    is_global_viewer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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


# Slowniki firmowe. Kategoria jest zamknieta, wartosci otwarte - dzialow
# i lokalizacji nie da sie przewidziec, ale ich lista musi byc skonczona,
# zeby "Ksiegowosc", "ksiegowosc" i "Księgowość" nie byly trzema dzialami.
KATEGORIE_SLOWNIKA: dict[str, str] = {
    # Osoby sa slownikiem jak kazdy inny: ta sama karta, te same pola opisane
    # schematem, ta sama zasada uzupelniania. Wczesniej mialy wlasna tabele
    # i wlasny formularz, wiec te same pojecia dzialaly w dwoch miejscach
    # inaczej - a dodanie osobie pola wymagalo migracji.
    "osoba": "Osoby",
    "lokalizacja": "Lokalizacje",
    "dzial": "Działy",
    "dostawca": "Dostawcy",
    # Rodzaje sprzetu tez sa slownikiem: firma dokłada "Projektor" albo "UPS"
    # bez czekania na wydanie serwera. Kazdy rodzaj niesie przy tym wlasny
    # zestaw pol - inne dla monitora, inne dla przelacznika.
    "rodzaj": "Rodzaje sprzętu",
}


class WpisSlownika(Base):
    """Jedna wartosc slownika firmowego (dzial, lokalizacja, dostawca).

    Slownik nie ogranicza tego, co mozna wpisac - kazda nowa wartosc wpisana
    w formularzu od razu do niego trafia. Sluzy do podpowiadania juz uzywanych
    nazw, zeby literowka nie tworzyla drugiego "magazynu".

    Klucz unikalnosci liczymy z wartosci znormalizowanej (male litery, bez
    zbednych spacji), a pokazujemy wersje wpisana przez czlowieka.
    """

    __tablename__ = "slowniki"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kategoria", "klucz", name="uq_slownik_wartosc"),
        Index("ix_slownik_tenant_kategoria", "tenant_id", "kategoria"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kategoria: Mapped[str] = mapped_column(String(32), nullable=False)
    wartosc: Mapped[str] = mapped_column(String(200), nullable=False)
    klucz: Mapped[str] = mapped_column(String(200), nullable=False)
    # Wartosci pol opisanych schematem firmy. Kolumna zamiast osobnej tabeli
    # wartosci: wzorzec encja-atrybut-wartosc wyglada elegancko i zamienia
    # kazde pytanie w lancuch zlaczen. Tu wystarczy zwykly WHERE po indeksie.
    atrybuty: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    utworzony: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    utworzyl: Mapped[str | None] = mapped_column(String(255))


class SchematSlownika(Base):
    """Jakie pola ma wpis slownika w TEJ firmie.

    Schemat jest danymi, nie kolumnami - dodanie atrybutu to zapis wiersza,
    a nie migracja i wydanie serwera. Firma dostaje kopie wzorca przy pierwszym
    uzyciu i od tej chwili schemat nalezy wylacznie do niej.
    """

    __tablename__ = "schematy_slownikow"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kategoria", "rodzaj", name="uq_schemat_kategoria_rodzaj"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kategoria: Mapped[str] = mapped_column(String(32), nullable=False)
    # Dla kategorii "sprzet" wskazuje rodzaj, ktorego dotyczy zestaw pol.
    # Pusty ciag dla schematow slownikowych - kolumna wchodzi w klucz
    # unikalnosci, a NULL nie porownuje sie sam ze soba.
    rodzaj: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    definicja: Mapped[dict] = mapped_column(JSONType, nullable=False)
    zmieniony: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    zmienil: Mapped[str | None] = mapped_column(String(255))

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
    enrollment_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Rodzaj sprzetu i sposob, w jaki trafil do bazy. Zasob wpisany recznie
    # (drukarka, switch) nie ma agenta i nigdy sie nie odezwie - bez tego
    # rozroznienia kazdy taki wpis trafialby na liste "bez kontaktu".
    typ: Mapped[str] = mapped_column(String(32), nullable=False, default=TYP_KOMPUTER, index=True)
    zrodlo: Mapped[str] = mapped_column(String(16), nullable=False, default=ZRODLO_AGENT, index=True)

    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("slowniki.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Opiekun odpowiada za sprzet, uzytkownik przy nim siedzi - to czesto dwie
    # rozne osoby (laptop prezesa ma opiekuna w IT). Obie wskazuja na te sama
    # liste osob, bo to ten sam katalog ludzi w firmie.
    uzytkownik_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("slowniki.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Gdzie sprzet fizycznie stoi. Agent tego nie wie - wpisuje czlowiek,
    # a podpowiedzi biora sie ze slownika lokalizacji firmy.
    lokalizacja_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("slowniki.id", ondelete="SET NULL"), index=True
    )
    # Miejsce WEWNATRZ lokalizacji nalezy do sprzetu, nie do adresu: dwie
    # maszyny pod tym samym adresem stoja w innych pomieszczeniach.
    miejsce: Mapped[str | None] = mapped_column(String(200))
    # Rola maszyny wpisywana recznie w panelu (np. "serwer plikow", "laptop ksiegowosc").
    role_label: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list | None] = mapped_column(JSONType, default=list)

    # Skrocone podsumowanie do listy (cpu/ram/dyski) - zeby nie czytac calego payloadu.
    facts: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    # Pola wlasciwe dla RODZAJU sprzetu: przekatna monitora, liczba portow
    # przelacznika, licznik wydrukow. Opisuje je schemat przypiety do rodzaju,
    # wiec dolozenie pola jest zapisem, a nie migracja.
    atrybuty: Mapped[dict | None] = mapped_column(JSONType, default=dict)

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
    # --- zakup i gwarancja ---
    # Wpisywane recznie: agent nie ma skad znac daty zakupu ani warunkow
    # umowy. Data konca gwarancji jest tu najwazniejsza - to na niej opiera
    # sie raport o wygasajacym wsparciu.
    purchase_date: Mapped[date | None] = mapped_column(Date)
    warranty_until: Mapped[date | None] = mapped_column(Date, index=True)
    dostawca_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("slowniki.id", ondelete="SET NULL"), index=True
    )
    purchase_price: Mapped[float | None] = mapped_column(Float)
    purchase_currency: Mapped[str | None] = mapped_column(String(8))
    invoice_number: Mapped[str | None] = mapped_column(String(120))
    support_contract: Mapped[str | None] = mapped_column(String(200))
    purchase_notes: Mapped[str | None] = mapped_column(Text)

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
    # Dwa klucze obce do tej samej tabeli - SQLAlchemy nie zgadnie, ktory
    # nalezy do ktorej relacji, wiec wskazujemy je jawnie.
    owner: Mapped["WpisSlownika | None"] = relationship(foreign_keys=[owner_id])
    uzytkownik: Mapped["WpisSlownika | None"] = relationship(foreign_keys=[uzytkownik_id])
    # Wpisy slownika jako obiekty, nie napisy: dopiero przez nie widac telefon
    # dostawcy czy adres lokalizacji.
    lokalizacja: Mapped["WpisSlownika | None"] = relationship(foreign_keys=[lokalizacja_id])
    dostawca: Mapped["WpisSlownika | None"] = relationship(foreign_keys=[dostawca_id])
    snapshots: Mapped[list["InventorySnapshot"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="InventorySnapshot.collected_at.desc()"
    )


class AssetCurrentReport(Base):
    """Ostatni pelny odczyt, niezalezny od historii zmian konfiguracji."""
    __tablename__ = "asset_current_reports"
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ReportReceipt(Base):
    """Idempotencja takze dla raportow bez zmiany i po retencji snapshotow."""
    __tablename__ = "report_receipts"
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    report_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


RELATION_KINDS = {
    "vm_host": "VM → host",
    "host_cluster": "Host → klaster",
    "application_server": "Aplikacja → serwer",
}


class AssetRelation(Base):
    __tablename__ = "asset_relations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "target_id", "kind", name="uq_asset_relation"),
        CheckConstraint("source_id <> target_id", name="ck_relation_not_self"),
        CheckConstraint("kind IN ('vm_host', 'host_cluster', 'application_server')", name="ck_relation_kind"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[Asset] = relationship(foreign_keys=[source_id])
    target: Mapped[Asset] = relationship(foreign_keys=[target_id])


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
    # Podpisany, uporzadkowany opis zmian z manifestu CI. Nie jest generowany
    # z przypadkowej listy wszystkich commitow serwera.
    changelog: Mapped[list | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255))


    provenance: Mapped[ReleaseProvenance | None] = relationship(
        "ReleaseProvenance", lazy="joined", uselist=False, passive_deletes="all")


class ReleaseProvenance(Base):
    """Signed evidence and import tombstone. Never just a trusted boolean."""
    __tablename__ = "release_provenance"
    __table_args__ = (UniqueConstraint("repository", "tag", "kind", name="uq_release_import_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    release_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_releases.id", ondelete="SET NULL"), unique=True)
    repository: Mapped[str] = mapped_column(String(255))
    tag: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))
    envelope: Mapped[dict] = mapped_column(JSONType)
    setup_storage_name: Mapped[str | None] = mapped_column(String(128))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseImportStatus(Base):
    __tablename__ = "release_import_status"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default="waiting")
    detail: Mapped[str | None] = mapped_column(Text)
    next_page: Mapped[int] = mapped_column(Integer, default=2)


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


class CveScore(Base):
    """Ocena CVSS pojedynczej podatnosci.

    Dane dystrybucji mowia, czy pakiet jest podatny, ale nie podaja wagi w skali
    CVSS - Debian ma wlasna skale "urgency", Ubuntu nie podaje zadnej. Ocena
    pochodzi wiec z NVD i jest tu buforowana na stale: dla wydanego CVE zmienia
    sie rzadko, a limit zapytan do NVD wynosi 5 na 30 sekund.

    Pobieramy oceny wylacznie dla podatnosci faktycznie dopasowanych do maszyn.
    Sciaganie ich dla calego kanalu oznaczaloby dziesiatki tysiecy zapytan po to,
    by opisac luki, ktorych u nikogo nie ma.
    """

    __tablename__ = "cve_scores"

    cve: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_score: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str | None] = mapped_column(String(16), index=True)
    vector: Mapped[str | None] = mapped_column(String(128))
    published: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Pusty wynik tez zapamietujemy - inaczej przy kazdym odswiezeniu pytalibysmy
    # o te same CVE, ktorych NVD nie zna (np. swieze, jeszcze nieopisane).
    found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BlokadaLogowania(Base):
    """Nieudane proby logowania do panelu i wynikajaca z nich blokada.

    Trzymana w bazie, a nie w pamieci procesu, z dwoch powodow. Serwer
    produkcyjny dziala w kilku procesach roboczych, wiec licznik w pamieci
    dawalby tylokrotnie wiecej prob, ile jest procesow. Po drugie restart
    serwera zerowalby go doszczetnie, a atakujacy nie musi czekac na restart -
    wystarczy, ze poczeka na wdrozenie.

    Klucz opisuje, czego dotyczy blokada: "konto:<email>" albo "ip:<adres>".
    Konto chroni haslo konkretnego uzytkownika, adres - probe rozproszona
    po wielu kontach.
    """

    __tablename__ = "blokady_logowania"

    klucz: Mapped[str] = mapped_column(String(320), primary_key=True)
    licznik: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blokada_do: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ostatnia_proba: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


# --- raporty i poczta -------------------------------------------------------

class UstawieniaPoczty(Base):
    """Poswiadczenia SMTP jednej firmy.

    Kazda firma wpisuje wlasne: raporty maja wychodzic z jej domeny i przez
    jej serwer, a nie przez wspolna skrzynke operatora - inaczej wiadomosci
    o jednej organizacji szlyby infrastruktura, do ktorej ma dostep inna.

    Haslo jest szyfrowane kluczem serwera. Musi byc odwracalne, bo SMTP
    wymaga podania go przy kazdym polaczeniu - skrot tu nie wystarczy.
    """

    __tablename__ = "ustawienia_poczty"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    uzytkownik: Mapped[str | None] = mapped_column(String(255))
    haslo_szyfr: Mapped[str | None] = mapped_column(Text)
    # "starttls" (587), "ssl" (465) albo "brak" - serwery wewnetrzne bywaja
    # bez szyfrowania, ale wtedy to swiadomy wybor, a nie domysl.
    szyfrowanie: Mapped[str] = mapped_column(String(16), nullable=False, default="starttls")
    nadawca: Mapped[str] = mapped_column(String(320), nullable=False)
    nazwa_nadawcy: Mapped[str | None] = mapped_column(String(200))
    aktywne: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ostatni_blad: Mapped[str | None] = mapped_column(Text)
    zaktualizowano: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DefinicjaRaportu(Base):
    """Raport wysylany cyklicznie do wskazanych adresatow.

    Firm moze byc kilkanascie, a kazda chce czego innego - stad definicja
    zamiast jednego raportu wbudowanego. Rodzaj okresla, co raport zawiera,
    czestotliwosc - jak czesto ma przyjsc.
    """

    __tablename__ = "definicje_raportow"
    __table_args__ = (Index("ix_raport_tenant", "tenant_id", "aktywny"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nazwa: Mapped[str] = mapped_column(String(200), nullable=False)
    # "podatnosci", "sprzet" albo "gwarancje".
    rodzaj: Mapped[str] = mapped_column(String(32), nullable=False)
    # "dziennie", "tygodniowo" albo "miesiecznie".
    czestotliwosc: Mapped[str] = mapped_column(String(16), nullable=False, default="tygodniowo")
    # Adresaci rozdzieleni przecinkiem albo srednikiem - rozbierane przy wysylce.
    adresaci: Mapped[str] = mapped_column(Text, nullable=False)
    # Wysylka poza firme nie moze byc niespodzianka - wlacza sie ja jawnie
    # przy definicji raportu.
    do_dostawcow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    aktywny: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Ktore atrybuty pokazac w tabeli. Puste = zestaw domyslny, zeby raport
    # dodany bez zastanowienia i tak byl czytelny.
    kolumny: Mapped[list | None] = mapped_column(JSONType)
    # Kiedy raport ostatnio poszedl i z jakim skutkiem. Bez tego nie da sie
    # odroznic "jeszcze nie byl wysylany" od "wysylka sie nie udaje".
    ostatnia_wysylka: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ostatni_status: Mapped[str | None] = mapped_column(String(16))
    ostatni_blad: Mapped[str | None] = mapped_column(Text)
    utworzony: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    utworzyl: Mapped[str | None] = mapped_column(String(255))


class DiscoveryPolicy(Base):
    """Desired scan settings, writable only by CMDB administrators."""
    __tablename__ = "discovery_policies"
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    config: Mapped[dict] = mapped_column(JSONType, nullable=False)
    revision: Mapped[str] = mapped_column(String(36), nullable=False, default=new_id)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DiscoveryScanner(Base):
    """Last scan status, including empty, failed and partial scans."""
    __tablename__ = "discovery_scanners"
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict] = mapped_column(JSONType, nullable=False)


class DiscoveryDevice(Base):
    """Candidate, not an asset. IP is scoped to scanner to separate sites/VLANs."""
    __tablename__ = "discovery_devices"
    __table_args__ = (UniqueConstraint("scanner_id", "ip", name="uq_discovery_scanner_ip"),
                      Index("ix_discovery_tenant_seen", "tenant_id", "last_seen"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    scanner_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    mac: Mapped[str] = mapped_column(String(17), nullable=False, default="")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    device_type: Mapped[str] = mapped_column(String(32), nullable=False, default="inne")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation: Mapped[dict] = mapped_column(JSONType, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("assets.id", ondelete="SET NULL"))
    # Skad wzielo sie powiazanie: "mac", "ip" albo "reczne". Automat, ktorego
    # nie widac, jest gorszy od reki - tego pola uzywa panel i dziennik audytu.
    link_mode: Mapped[str | None] = mapped_column(String(16))
    link_reason: Mapped[str | None] = mapped_column(String(200))


# --- monitorowanie uslug i certyfikatow -------------------------------------

# Stan celu. "nieznany" celowo NIE jest tym samym co "ok": cel dopiero dodany
# albo wylaczony nie zostal jeszcze sprawdzony, a zielona kropka przy czyms,
# czego nikt nie zmierzyl, jest gorsza niz jawne przyznanie sie do niewiedzy.
STAN_OK = "ok"
STAN_OSTRZEZENIE = "ostrzezenie"
STAN_AWARIA = "awaria"
STAN_NIEZNANY = "nieznany"
# "Nie dotyczy" to co innego niz "nie wiem". Cel sprawdzany po TCP nie ma
# certyfikatu i miec go nie bedzie - wiec brak oceny certyfikatu nie jest tu
# luka w wiedzy i nie moze psuc stanu calego celu.
STAN_NIE_DOTYCZY = "nie_dotyczy"
STANY_MONITORA = (STAN_OK, STAN_OSTRZEZENIE, STAN_AWARIA, STAN_NIEZNANY,
                  STAN_NIE_DOTYCZY)

# Kolejnosc wagi stanow - stan celu to najgorszy ze skladowych (dostepnosc,
# certyfikat). Dzieki temu dzialajaca usluga z certyfikatem wygasajacym za
# tydzien nie swieci na zielono.
# "Nie dotyczy" wazy tyle co "ok", czyli nic: przy rownej wadze max() zwraca
# pierwszy skladnik, a pierwsza jest dostepnosc - dzialajacy cel TCP swieci
# wiec na zielono, zamiast na zawsze zostawac "nieznany" przez certyfikat,
# ktorego nigdy nie bedzie mial.
WAGA_STANU = {STAN_OK: 0, STAN_NIE_DOTYCZY: 0, STAN_NIEZNANY: 1,
              STAN_OSTRZEZENIE: 2, STAN_AWARIA: 3}

# Co sprawdzamy pod adresem. TCP odpowiada na pytanie "czy port przyjmuje
# polaczenia", TLS dodatkowo zdejmuje certyfikat - takze z uslug, ktore nie
# mowia po HTTP (SMTPS, IMAPS, LDAPS, RDP), a HTTP(S) pyta jeszcze aplikacje
# o kod odpowiedzi. Otwarty port nie znaczy, ze aplikacja za nim zyje.
PROTOKOL_TCP = "tcp"
PROTOKOL_TLS = "tls"
PROTOKOL_HTTP = "http"
PROTOKOL_HTTPS = "https"
PROTOKOLY_MONITORA: dict[str, str] = {
    PROTOKOL_TCP: "TCP - port przyjmuje polaczenia",
    PROTOKOL_TLS: "TLS - polaczenie szyfrowane i certyfikat",
    PROTOKOL_HTTP: "HTTP - kod odpowiedzi serwera WWW",
    PROTOKOL_HTTPS: "HTTPS - kod odpowiedzi i certyfikat",
}
# Protokoly, przy ktorych zdejmujemy certyfikat.
PROTOKOLY_Z_TLS = (PROTOKOL_TLS, PROTOKOL_HTTPS)
# Protokoly, przy ktorych pytamy aplikacje o kod odpowiedzi.
PROTOKOLY_Z_HTTP = (PROTOKOL_HTTP, PROTOKOL_HTTPS)
PORTY_DOMYSLNE = {PROTOKOL_TCP: 443, PROTOKOL_TLS: 443, PROTOKOL_HTTP: 80, PROTOKOL_HTTPS: 443}


class MonitorUslugi(Base):
    """Cel monitorowania: adres IP albo nazwa, port i sposob sprawdzenia.

    Celowo NIE jest to zasob (Asset). Usluga bywa czyms, co nie ma sprzetu
    w ewidencji - certyfikat domeny u zewnetrznego dostawcy, adres VIP na
    load balancerze, ten sam serwer widziany pod czterema nazwami. Wiazanie
    z zasobem jest mozliwe (asset_id), ale nie wymagane.

    Sprawdza AGENT, nie serwer - patrz services/monitoring.py. Ta tabela jest
    wiec zapisem ZAMIARU (co, jak czesto, z ktorej maszyny) plus ostatnim
    znanym stanem; sama niczego nie odpytuje.

    Stan jest tu ZAPISANY, w odroznieniu od "bez kontaktu" przy maszynie,
    ktore wynika z last_seen w chwili patrzenia. Roznica jest istotna:
    o zmianie stanu uslugi trzeba powiadomic w momencie, w ktorym nastapila,
    a nie wtedy, gdy ktos akurat otworzy strone.
    """

    __tablename__ = "monitory_uslug"
    __table_args__ = (
        UniqueConstraint("tenant_id", "nazwa", name="uq_monitor_nazwa"),
        Index("ix_monitor_tenant_aktywny", "tenant_id", "aktywny"),
        Index("ix_monitor_wykonawca", "wykonawca_id", "aktywny"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nazwa: Mapped[str] = mapped_column(String(200), nullable=False)

    # Maszyna z agentem, ktora wykonuje sprawdzenia. To ona stoi w sieci,
    # w ktorej usluga zyje - serwer CMDB czesto tej sieci nie widzi wcale,
    # a gdyby widzial wszystkie naraz, bylby jednym miejscem z wgladem
    # w sieci wszystkich firm. Agent jest JEDYNYM wykonawca: serwer nie ma
    # czym sondowac i nie ma trybu, w ktorym by to robil.
    #
    # SET NULL, a nie CASCADE: znikniecie maszyny nie moze po cichu zabrac
    # konfiguracji celu razem z historia jego awarii. Cel bez wykonawcy jest
    # widocznym problemem do naprawienia - trzeba go przepisac innemu
    # agentowi - a nie danymi do wyrzucenia.
    wykonawca_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )

    # Adres IP albo nazwa DNS. Nazwa jest rozwiazywana przy KAZDYM sprawdzeniu,
    # bo zmiana adresu pod nazwa jest sama w sobie informacja.
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    protokol: Mapped[str] = mapped_column(String(16), nullable=False, default=PROTOKOL_HTTPS)
    # Sciezka pytana po HTTP(S). Strona glowna bywa przekierowaniem, a
    # /health mowi o aplikacji wiecej.
    sciezka: Mapped[str] = mapped_column(String(500), nullable=False, default="/")
    # Kod uznawany za poprawny. 0 znaczy "kazdy kod ponizej 400".
    oczekiwany_kod: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Nazwa wysylana w SNI i sprawdzana w certyfikacie, gdy rozni sie od
    # adresu - przy monitorowaniu po IP inaczej nie da sie trafic we wlasciwy
    # certyfikat serwera obslugujacego kilka domen.
    nazwa_tls: Mapped[str | None] = mapped_column(String(255))
    # Czy lancuch certyfikatu ma byc uznany za wymagany. Wewnetrzne uslugi
    # czesto maja certyfikat wlasnego urzedu, ktorego agent nie zna - wtedy
    # niezaufany lancuch jest stanem normalnym, a nie awaria. Data waznosci
    # jest sprawdzana zawsze, niezaleznie od tego ustawienia.
    weryfikuj_lancuch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Dwa osobne rytmy, bo to dwa rozne pytania. "Czy odpowiada" ma sens co
    # minute; "do kiedy wazny certyfikat" zmienia odpowiedz raz na kilka
    # miesiecy i pytanie o to co minute jest wylacznie halasem.
    interwal_sekund: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    interwal_certyfikatu: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    limit_sekund: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # Po ilu kolejnych nieudanych probach uznajemy usluge za niedostepna.
    # Jedna zgubiona odpowiedz zdarza sie w kazdej sieci; powiadomienie o niej
    # uczy ludzi ignorowania powiadomien.
    liczba_prob: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    # Ile dni przed koncem waznosci certyfikatu zaczynamy ostrzegac i alarmowac.
    prog_ostrzezenia_dni: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    prog_alarmu_dni: Mapped[int] = mapped_column(Integer, nullable=False, default=7)

    # Powiazanie z zasobem jest opcjonalne - patrz dokumentacja klasy.
    asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )

    powiadamiaj: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Adresy rozdzielone przecinkiem, srednikiem albo nowa linia.
    adresaci: Mapped[str | None] = mapped_column(Text)
    aktywny: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Zadanie sprawdzenia poza kolejnoscia, zlozone z panelu. Nie ma tu flagi
    # do wyzerowania: agent dostaje zadanie, dopoki ostatnie sprawdzenie jest
    # starsze niz zadanie, wiec przychodzacy wynik gasi je sam.
    wymuszone_o: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- stan z ostatniego raportu agenta ---
    stan: Mapped[str] = mapped_column(String(16), nullable=False, default=STAN_NIEZNANY)
    # Dostepnosc i certyfikat sa oceniane osobno, bo osobno sie psuja
    # i osobno o nich powiadamiamy. Widoczny stan celu to gorszy z tych dwoch.
    stan_dostepnosci: Mapped[str] = mapped_column(String(16), nullable=False, default=STAN_NIEZNANY)
    stan_certyfikatu: Mapped[str] = mapped_column(String(16), nullable=False, default=STAN_NIEZNANY)
    ostatnie_sprawdzenie: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Kiedy agent ostatnio sie odezwal w sprawie tego celu. Rozne od
    # ostatniego sprawdzenia: agent moze sondowac co minute, a raportowac
    # co kwadrans - i to cisza AGENTA, a nie uslugi, znaczy "nie wiem".
    ostatni_raport: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ostatnia_zmiana_stanu: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ostatni_blad: Mapped[str | None] = mapped_column(Text)
    czas_odpowiedzi_ms: Mapped[int | None] = mapped_column(Integer)
    ostatni_kod: Mapped[int | None] = mapped_column(Integer)
    ostatni_adres: Mapped[str | None] = mapped_column(String(64))

    # --- ostatnio zdjety certyfikat ---
    cert_podmiot: Mapped[str | None] = mapped_column(String(500))
    cert_wystawca: Mapped[str | None] = mapped_column(String(500))
    cert_od: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cert_do: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Odcisk rozroznia certyfikaty. Po odnowieniu zmienia sie, a wraz z nim
    # zeruja sie progi powiadomien - inaczej odnowiony certyfikat nie
    # doczekalby sie zadnego ostrzezenia przed nastepnym koncem waznosci.
    # Agent przysyla sam odcisk, a caly certyfikat dopiero gdy odcisk sie
    # zmienil - certyfikat wazny rok nie ma po co jechac przez siec co dobe.
    cert_odcisk: Mapped[str | None] = mapped_column(String(95))
    cert_nazwy: Mapped[list | None] = mapped_column(JSONType)
    cert_zaufany: Mapped[bool | None] = mapped_column(Boolean)
    cert_blad: Mapped[str | None] = mapped_column(Text)

    # --- co juz zostalo zgloszone ---
    # Zapisujemy stan, o ktorym powiadomilismy, a nie sam fakt wyslania:
    # dzieki temu wiadomosc idzie przy KAZDEJ zmianie i tylko przy zmianie.
    powiadomiona_dostepnosc: Mapped[str | None] = mapped_column(String(16))
    # Ostatni ogloszony prog certyfikatu w dniach (0 = juz wygasl). Sam stan
    # nie wystarczy: certyfikat stoi w "ostrzezeniu" przez trzy tygodnie,
    # a kolejne progi maja sie odezwac po drodze.
    powiadomiony_prog: Mapped[int | None] = mapped_column(Integer)
    # Stan certyfikatu, o ktorym juz zglosilismy - lapie to, czego prog nie
    # widzi: niezaufany lancuch i powrot do porzadku po odnowieniu.
    powiadomiony_stan_cert: Mapped[str | None] = mapped_column(String(16))
    powiadomiony_odcisk: Mapped[str | None] = mapped_column(String(95))
    ostatnie_powiadomienie: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blad_powiadomienia: Mapped[str | None] = mapped_column(Text)

    utworzony: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    utworzyl: Mapped[str | None] = mapped_column(String(255))

    asset: Mapped["Asset | None"] = relationship(foreign_keys=[asset_id])
    wykonawca: Mapped["Asset | None"] = relationship(foreign_keys=[wykonawca_id])


class OknoMonitorowania(Base):
    """Podsumowanie jednego okresu raportowania jednego celu.

    Tu mieszka odpowiedz na "ile z tego dzialalo". Agent sonduje co minute,
    ale nie przysyla tysiaca wierszy "ok" - przysyla jeden wiersz: "od 12:30
    do 12:45 wykonalem 15 sond, 9 sie udalo". Doba monitorowania co minute to
    96 wierszy zamiast 1440, a procent dostepnosci wychodzi z nich dokladnie
    ten sam.

    Okno mowi TAKZE, ze agent w ogole monitorowal. Bez tego nie da sie
    odroznic "usluga dzialala" od "nikt nie patrzyl" - a to dwie zupelnie
    rozne rzeczy, ktore w procencie dostepnosci wygladalyby identycznie.
    """

    __tablename__ = "okna_monitorowania"
    __table_args__ = (
        UniqueConstraint("monitor_id", "poczatek", name="uq_okno_monitor_poczatek"),
        Index("ix_okno_monitor_czas", "monitor_id", "koniec"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    monitor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("monitory_uslug.id", ondelete="CASCADE"), nullable=False
    )
    poczatek: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    koniec: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sond: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    udanych: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    przyjeto: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PrzerwaDostepnosci(Base):
    """Jedna przerwa w dzialaniu uslugi: od kiedy do kiedy nie odpowiadala.

    To jest tresc raportu agenta. Nie wysylamy tysiaca potwierdzen, ze wszystko
    gra - wysylamy to, czego brakowalo: "12:34:34 - 12:40:22, connection
    refused". Przerwa jeszcze trwajaca ma pusty ``koniec`` i zostaje domknieta
    kolejnym raportem; rozpoznajemy ja po (monitor_id, poczatek), bo poczatek
    przerwy jest jej tozsamoscia.
    """

    __tablename__ = "przerwy_dostepnosci"
    __table_args__ = (
        UniqueConstraint("monitor_id", "poczatek", name="uq_przerwa_monitor_poczatek"),
        Index("ix_przerwa_monitor_czas", "monitor_id", "poczatek"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    monitor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("monitory_uslug.id", ondelete="CASCADE"), nullable=False
    )
    poczatek: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Puste znaczy "trwa nadal" - nie "trwala zero sekund".
    koniec: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blad: Mapped[str | None] = mapped_column(String(500))
    # Ile sond nie powiodlo sie w tej przerwie. Rozroznia jedno zgubione
    # pakiety od godziny ciszy.
    sond: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    monitor: Mapped["MonitorUslugi"] = relationship()

    @property
    def trwanie_sekund(self) -> int | None:
        """Ile trwala. None, gdy trwa nadal - a nie zero."""
        if self.koniec is None:
            return None
        return int((as_utc(self.koniec) - as_utc(self.poczatek)).total_seconds())


# ============================================================================
# HELPDESK
# ============================================================================
#
# Zgloszenia klientow obslugiwane z jednej skrzynki pocztowej. Firma nie jest
# tu nowym bytem - to ten sam Tenant, co w reszcie CMDB, bo technik obslugujacy
# zgloszenia firmy ma widziec takze jej kartoteke sprzetu. Drugi rejestr firm
# rozjechalby sie z pierwszym w tygodniu.

# Statusy zgloszenia. Kolejnosc jest kolejnoscia kolumn na tablicy.
STATUS_NOWE = "nowe"
STATUS_W_TRAKCIE = "w_trakcie"
STATUS_OCZEKUJE = "oczekuje"
STATUS_ZAMKNIETE = "zamkniete"
STATUSY_ZGLOSZENIA: dict[str, str] = {
    STATUS_NOWE: "Nowe",
    STATUS_W_TRAKCIE: "W trakcie",
    STATUS_OCZEKUJE: "Oczekuje",
    STATUS_ZAMKNIETE: "Zamkniete",
}
STATUSY_OTWARTE = (STATUS_NOWE, STATUS_W_TRAKCIE, STATUS_OCZEKUJE)

# Typ zgloszenia jest pomocniczy i moze zostac pusty - projekt zaklada, ze
# helpdesk ma dzialac bez kategoryzowania niczego.
TYPY_ZGLOSZENIA: dict[str, str] = {
    "incydent": "Incydent",
    "prosba": "Prosba / zmiana",
    "inne": "Inne",
}

# Rodzaje wpisow w watku. Wszystkie leza w jednej tabeli, bo zgloszenie ma byc
# jedna chronologiczna rozmowa - trzy osobne listy trzeba by scalac przy
# kazdym wyswietleniu i przy kazdym scalaniu mozna sie pomylic o kolejnosc.
WPIS_OD_KLIENTA = "od_klienta"
WPIS_DO_KLIENTA = "do_klienta"
WPIS_WEWNETRZNY = "wewnetrzny"
WPIS_SYSTEM = "system"
RODZAJE_WPISOW = (WPIS_OD_KLIENTA, WPIS_DO_KLIENTA, WPIS_WEWNETRZNY, WPIS_SYSTEM)

# Stan wiadomosci, ktorej nadawcy nie dalo sie przypisac do firmy.
NIEROZPOZNANA_CZEKA = "czeka"
NIEROZPOZNANA_ZIGNOROWANA = "zignorowana"
NIEROZPOZNANA_PRZYPISANA = "przypisana"


class HelpdeskUstawienia(Base):
    """Jedna skrzynka helpdesku dla calego systemu.

    Klienci wszystkich firm pisza pod jeden adres, wiec ustawien jest dokladnie
    jeden komplet - stad sztywny klucz glowny. Tabela z jednym wierszem jest
    brzydsza od pliku konfiguracyjnego, ale haslo skrzynki musi dac sie zmienic
    z panelu bez restartu procesu, a plik tego nie da.

    Hasla sa szyfrowane kluczem serwera (services/sekrety.py): IMAP i SMTP
    wymagaja ich w postaci jawnej przy kazdym polaczeniu, wiec skrot nie
    wystarczy.
    """

    __tablename__ = "helpdesk_ustawienia"
    __table_args__ = (
        CheckConstraint("klucz = 'helpdesk'", name="ck_helpdesk_ustawienia_jeden_wiersz"),
    )

    klucz: Mapped[str] = mapped_column(String(16), primary_key=True, default="helpdesk")

    # --- odbior ---
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993)
    imap_szyfrowanie: Mapped[str] = mapped_column(String(16), nullable=False, default="ssl")
    imap_uzytkownik: Mapped[str | None] = mapped_column(String(255))
    imap_haslo_szyfr: Mapped[str | None] = mapped_column(Text)
    imap_folder: Mapped[str] = mapped_column(String(120), nullable=False, default="INBOX")
    # Co robimy z wiadomoscia po pobraniu: "przeczytana" (domyslnie),
    # "przenies" do folderu albo "nic". Kasowania nie ma i nie bedzie -
    # helpdesk czyta cudza skrzynke i nie jest od sprzatania w niej.
    imap_po_pobraniu: Mapped[str] = mapped_column(String(16), nullable=False, default="przeczytana")
    imap_folder_docelowy: Mapped[str | None] = mapped_column(String(120))
    imap_interwal_sekund: Mapped[int] = mapped_column(Integer, nullable=False, default=120)

    # --- wysylka ---
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    smtp_szyfrowanie: Mapped[str] = mapped_column(String(16), nullable=False, default="starttls")
    smtp_uzytkownik: Mapped[str | None] = mapped_column(String(255))
    smtp_haslo_szyfr: Mapped[str | None] = mapped_column(Text)
    nadawca: Mapped[str | None] = mapped_column(String(320))
    nazwa_nadawcy: Mapped[str | None] = mapped_column(String(200))
    stopka: Mapped[str | None] = mapped_column(Text)

    # --- zachowanie ---
    potwierdzenie_wlaczone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    potwierdzenie_tresc: Mapped[str | None] = mapped_column(Text)
    # Puste znaczy "nie usuwaj nigdy" i taka jest wartosc domyslna: nierozpoznana
    # poczta bywa jedynym sladem po zgubionym zgloszeniu.
    usun_zignorowane_po_dniach: Mapped[int | None] = mapped_column(Integer)
    usun_spam_po_dniach: Mapped[int | None] = mapped_column(Integer)

    aktywne: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ostatnie_pobranie: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ostatni_blad: Mapped[str | None] = mapped_column(Text)
    zaktualizowano: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class HelpdeskFirma(Base):
    """Ustawienia helpdeskowe firmy: skrot numeru i licznik zgloszen.

    Numeracja idzie osobno dla kazdej firmy, wiec skrot jest czescia numeru -
    bez niego "123" dwoch firm byloby tym samym napisem w temacie maila
    i odpowiedz klienta trafialaby do cudzego zgloszenia.
    """

    __tablename__ = "helpdesk_firmy"

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    skrot: Mapped[str] = mapped_column(String(8), nullable=False, unique=True, index=True)
    # Ostatni nadany numer. Nie liczba zgloszen: skasowanie zgloszenia nie moze
    # spowodowac, ze nastepne dostanie numer juz uzyty w korespondencji.
    licznik: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aktywna: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    tenant: Mapped[Tenant] = relationship()


class HelpdeskDomena(Base):
    """Domena nadawcy wskazujaca firme.

    Domena nalezy do jednej firmy - stad unikalnosc. Odwrotnie wolno: firma
    moze miec ich kilka, bo po konsolidacji poczta przychodzi z dwoch adresow
    i obie drogi maja prowadzic do tej samej numeracji.
    """

    __tablename__ = "helpdesk_domeny"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Zawsze malymi literami - porownanie domen jest bez wzgledu na wielkosc,
    # a normalizacja przy zapisie jest tansza niz lower() w kazdym zapytaniu.
    domena: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    dodal: Mapped[str | None] = mapped_column(String(255))
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    tenant: Mapped[Tenant] = relationship()


class HelpdeskDostep(Base):
    """Technik dopuszczony do firmy.

    Jeden wpis otwiera i zgloszenia firmy, i jej kartoteke w CMDB: kto obsluguje
    zgloszenie o niedzialajacej drukarce, ten musi zobaczyc te drukarke.
    Rozdzielenie tych dwoch praw dawaloby technika, ktory czyta zgloszenie
    o sprzecie, ktorego nie moze otworzyc.
    """

    __tablename__ = "helpdesk_dostepy"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_helpdesk_dostep"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nadal: Mapped[str | None] = mapped_column(String(255))
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    user: Mapped[PortalUser] = relationship()
    tenant: Mapped[Tenant] = relationship()


class Zgloszenie(Base):
    """Jedno zgloszenie klienta."""

    __tablename__ = "helpdesk_zgloszenia"
    __table_args__ = (
        UniqueConstraint("tenant_id", "numer", name="uq_zgloszenie_numer_firmy"),
        CheckConstraint(
            "status IN ('nowe', 'w_trakcie', 'oczekuje', 'zamkniete')",
            name="ck_zgloszenie_status",
        ),
        Index("ix_zgloszenie_firma_status", "tenant_id", "status"),
        Index("ix_zgloszenie_aktywnosc", "ostatnia_aktywnosc"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Numer w obrebie firmy i ten sam numer w postaci, ktora idzie w temacie
    # maila. Trzymamy oba, bo po napisie z tematu ("BON-123") szukamy przy
    # kazdej przychodzacej odpowiedzi - rozkladanie go na czesci w zapytaniu
    # kosztowaloby indeks.
    numer: Mapped[int] = mapped_column(Integer, nullable=False)
    numer_pelny: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    temat: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_NOWE)
    typ: Mapped[str | None] = mapped_column(String(20))

    zglaszajacy_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    zglaszajacy_nazwa: Mapped[str | None] = mapped_column(String(200))

    technik_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("portal_users.id", ondelete="SET NULL"), index=True
    )
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ostatnia_aktywnosc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    zamkniete_o: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship()
    technik: Mapped[PortalUser | None] = relationship()


class ZgloszenieSprzet(Base):
    """Sprzet, ktorego dotyczy zgloszenie.

    Tabela laczaca, a nie pole przy zgloszeniu: jedna awaria potrafi dotyczyc
    kilku urzadzen (drukarka i przelacznik, do ktorego jest wpieta), a jedno
    urzadzenie zbiera zgloszenia przez cale zycie - i to jest jego karta
    napraw. Pole w zgloszeniu dawaloby pierwsze, ale nie drugie.

    ``zrodlo`` mowi, skad wzielo sie powiazanie: "automat" to sprzet
    zglaszajacego rozpoznany po jego adresie, "reczne" to wybor technika.
    Bez tego rozroznienia nie da sie pozniej ocenic, czy automat trafia.
    """

    __tablename__ = "helpdesk_zgloszenie_sprzet"
    __table_args__ = (
        UniqueConstraint("zgloszenie_id", "asset_id", name="uq_zgloszenie_sprzet"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    zgloszenie_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("helpdesk_zgloszenia.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    zrodlo: Mapped[str] = mapped_column(String(16), nullable=False, default="reczne")
    dodal: Mapped[str | None] = mapped_column(String(255))
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    zgloszenie: Mapped[Zgloszenie] = relationship()
    asset: Mapped["Asset"] = relationship()


class WpisZgloszenia(Base):
    """Wpis w watku: mail od klienta, odpowiedz, komentarz albo zdarzenie.

    Komentarz wewnetrzny lezy w tej samej tabeli co odpowiedz do klienta, ale
    rozni sie jednym polem - i to pole decyduje, czy tresc wyjdzie na zewnatrz.
    Dlatego wysylka pyta o rodzaj wpisu w jednym miejscu (services/helpdesk),
    zamiast ufac, ze kazdy widok pamieta o filtrze.
    """

    __tablename__ = "helpdesk_wpisy"
    __table_args__ = (
        CheckConstraint(
            "rodzaj IN ('od_klienta', 'do_klienta', 'wewnetrzny', 'system')",
            name="ck_wpis_rodzaj",
        ),
        Index("ix_wpis_zgloszenie_czas", "zgloszenie_id", "utworzono"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    zgloszenie_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("helpdesk_zgloszenia.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rodzaj: Mapped[str] = mapped_column(String(20), nullable=False)
    # Autor jest technikiem (konto panelu) albo klientem (sam adres e-mail).
    autor_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("portal_users.id", ondelete="SET NULL")
    )
    autor_email: Mapped[str | None] = mapped_column(String(320))
    autor_nazwa: Mapped[str | None] = mapped_column(String(200))
    tresc: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Naglowki potrzebne do watkowania. Message-ID wiadomosci wychodzacych
    # zapisujemy, bo klient odpowie na nia i przysle je w In-Reply-To.
    message_id: Mapped[str | None] = mapped_column(String(500), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    # Adresy z kopii. Klient czesto pisze z przelozonym w DW i odpowiedz ma
    # trafic do tych samych osob - inaczej polowa rozmowy dzieje sie bez nich.
    dw: Mapped[list | None] = mapped_column(JSONType)

    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    wyslano_o: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blad_wysylki: Mapped[str | None] = mapped_column(Text)

    zgloszenie: Mapped[Zgloszenie] = relationship()
    autor: Mapped[PortalUser | None] = relationship()


class ZalacznikWpisu(Base):
    """Plik przyslany przez klienta albo doklejony do odpowiedzi.

    Sama tresc pliku lezy na dysku, w bazie zostaje opis i sciezka wzgledna.
    Zrzut ekranu bywa calym zgloszeniem ("nie dziala, zalaczam") i bez niego
    technik pyta o to, co juz dostal.

    Nazwe pliku na dysku nadajemy sami z identyfikatora wpisu i licznika -
    nazwa z maila trafia wylacznie do kolumny ``nazwa``. Klient moze przyslac
    "..\..\etc\passwd" i taka nazwa nie ma prawa dotknac systemu plikow.
    """

    __tablename__ = "helpdesk_zalaczniki"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wpis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("helpdesk_wpisy.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    zgloszenie_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("helpdesk_zgloszenia.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    nazwa: Mapped[str] = mapped_column(String(255), nullable=False)
    typ_mime: Mapped[str | None] = mapped_column(String(120))
    rozmiar: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Sciezka WZGLEDEM katalogu zalacznikow z konfiguracji. Katalog bywa
    # przenoszony miedzy wdrozeniami, a sciezka bezwzgledna w bazie zamienia
    # przenosiny w reczna migracje.
    sciezka: Mapped[str] = mapped_column(String(500), nullable=False)
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    wpis: Mapped["WpisZgloszenia"] = relationship()


class CzasPracy(Base):
    """Minuty dopisane do zgloszenia przez jednego technika.

    Wpisu nie da sie zmienic ani skasowac - pomylke prostuje kolejny wpis.
    Czas pracy trafia na faktury klientow, wiec musi byc rejestrem zdarzen,
    a nie polem, ktore ktos poprawi przed koncem miesiaca.

    tenant_id jest tu powtorzony za zgloszeniem swiadomie: raport za miesiac
    sumuje setki tysiecy wierszy per firma i nie ma powodu, zeby robil to
    zlaczeniem.
    """

    __tablename__ = "helpdesk_czas"
    __table_args__ = (
        CheckConstraint("minuty > 0", name="ck_czas_dodatni"),
        Index("ix_czas_firma_data", "tenant_id", "utworzono"),
        Index("ix_czas_technik_data", "technik_id", "utworzono"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    zgloszenie_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("helpdesk_zgloszenia.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    technik_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("portal_users.id", ondelete="RESTRICT"), nullable=False
    )
    minuty: Mapped[int] = mapped_column(Integer, nullable=False)
    opis: Mapped[str | None] = mapped_column(String(500))
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    zgloszenie: Mapped[Zgloszenie] = relationship()
    technik: Mapped[PortalUser] = relationship()


class NierozpoznanaWiadomosc(Base):
    """Poczta, ktorej domena nadawcy nie wskazuje zadnej firmy.

    Nie kasujemy jej i nie odpowiadamy na nia: skrzynka sluzy wylacznie
    helpdeskowi, wiec obca domena to zwykle spam - ale "zwykle" nie znaczy
    "zawsze", a skasowane zgloszenie klienta nie zostawia po sobie sladu.
    """

    __tablename__ = "helpdesk_nierozpoznane"
    __table_args__ = (
        CheckConstraint(
            "stan IN ('czeka', 'zignorowana', 'przypisana')", name="ck_nierozpoznana_stan"
        ),
        Index("ix_nierozpoznana_stan_czas", "stan", "otrzymano"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    nadawca_email: Mapped[str] = mapped_column(String(320), nullable=False)
    nadawca_nazwa: Mapped[str | None] = mapped_column(String(200))
    domena: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    temat: Mapped[str | None] = mapped_column(String(500))
    tresc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_id: Mapped[str | None] = mapped_column(String(500), index=True)
    naglowki: Mapped[dict | None] = mapped_column(JSONType)
    otrzymano: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    stan: Mapped[str] = mapped_column(String(20), nullable=False, default=NIEROZPOZNANA_CZEKA)
    decyzje_podjal: Mapped[str | None] = mapped_column(String(255))
    decyzja_o: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zgloszenie_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("helpdesk_zgloszenia.id", ondelete="SET NULL")
    )

    # Miejsce na ocene AI (etap 2). Puste pola nie zmieniaja niczego w dzialaniu
    # helpdesku - lista bez ocen jest zwykla skrzynka do przejrzenia.
    ocena_ai: Mapped[str | None] = mapped_column(String(32))
    ocena_pewnosc: Mapped[float | None] = mapped_column(Float)


class IgnorowanaDomena(Base):
    """Regula: poczta z tej domeny nie trafia na liste do przejrzenia.

    Regula odsuwa wiadomosci od oczu operatora, ale ich nie kasuje - leza
    w nierozpoznanych ze stanem "zignorowana". Licznik pokazuje, ile poczty
    regula przechwycila, zeby pomylke dalo sie zauwazyc i cofnac.
    """

    __tablename__ = "helpdesk_ignorowane_domeny"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    domena: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    zalozyl: Mapped[str | None] = mapped_column(String(255))
    utworzono: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    przechwycone: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ostatnia_wiadomosc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
