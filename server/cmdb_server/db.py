"""Silnik bazy danych i sesje SQLAlchemy."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

log = logging.getLogger(__name__)

_settings = get_settings()

engine: Engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """Zaleznosc FastAPI: sesja per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sesja dla skryptow CLI - commit/rollback automatycznie."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Dowolna stala - liczy sie tylko to, ze wszystkie procesy uzywaja tej samej.
KLUCZ_BLOKADY_SCHEMATU = 0x434D4442  # "CMDB"


def init_db() -> None:
    """Tworzy schemat. Docelowo zastapione migracjami Alembic.

    Serwer produkcyjny dziala w kilku procesach roboczych i KAZDY z nich
    wykonuje te funkcje przy starcie. Bez blokady wszystkie sprawdzaja naraz,
    ze tabel nie ma, i wszystkie probuja je utworzyc - jeden proces wygrywa,
    reszta dostaje "duplicate key value violates unique constraint
    pg_type_typname_nsp_index" i nie wstaje. Kontener wowczas pada i wstaje
    ponownie, a przy drugim starcie tabele juz sa, wiec awaria wyglada na
    jednorazowa - mimo ze wroci przy kazdej zmianie schematu.

    Blokada doradcza Postgresa jest trzymana na poziomie polaczenia, wiec
    obejmuje caly przebieg tworzenia schematu, a nie pojedyncza transakcje.
    """
    from sqlalchemy import text

    from . import models  # noqa: F401  (rejestracja mapperow)

    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:klucz)"),
                     {"klucz": KLUCZ_BLOKADY_SCHEMATU})
        conn.commit()
        try:
            _utworz_schemat()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:klucz)"),
                         {"klucz": KLUCZ_BLOKADY_SCHEMATU})
            conn.commit()


def _utworz_schemat() -> None:
    from . import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)
    _dodaj_brakujace_kolumny()
    _utworz_indeksy_gin()


def _dodaj_brakujace_kolumny() -> None:
    """Doklada kolumny, ktorych nie ma w istniejacych tabelach.

    create_all tworzy brakujace TABELE, ale nie rusza tabel juz istniejacych -
    po dolozeniu pola do modelu baza z danymi zostawala ze starym ukladem
    i zapytania konczyly sie bledem o nieznana kolumne.

    Swiadomie obslugujemy tylko dokladanie kolumn dopuszczajacych NULL,
    czyli przypadek bezpieczny i odwracalny. Zmiany typow, usuwanie kolumn
    i przenoszenie danych wymagaja Alembica - to rozwiazanie pomostowe,
    ktore ma pozwolic zaktualizowac dzialajaca instalacje bez utraty danych.
    """
    from sqlalchemy import inspect, text

    from . import models

    inspector = inspect(engine)
    istniejace_tabele = set(inspector.get_table_names())
    dodane: set[tuple[str, str]] = set()

    for tabela in models.Base.metadata.sorted_tables:
        if tabela.name not in istniejace_tabele:
            continue
        obecne = {kolumna["name"] for kolumna in inspector.get_columns(tabela.name)}
        for kolumna in tabela.columns:
            if kolumna.name in obecne:
                continue
            typ = kolumna.type.compile(dialect=engine.dialect)
            definicja = f"{kolumna.name} {typ}"

            if not kolumna.nullable:
                # Kolumna wymagana da sie dolozyc do tabeli z danymi tylko
                # wtedy, gdy znamy wartosc dla juz istniejacych wierszy.
                wartosc = _domyslna_wartosc(kolumna)
                if wartosc is None:
                    log.warning(
                        "kolumna %s.%s jest wymagana i nie ma wartosci domyslnej - "
                        "dodaj ja migracja recznie",
                        tabela.name,
                        kolumna.name,
                    )
                    continue
                definicja += f" NOT NULL DEFAULT {wartosc}"

            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tabela.name} ADD COLUMN {definicja}"))
            log.info("dodano brakujaca kolumne %s.%s", tabela.name, kolumna.name)
            dodane.add((tabela.name, kolumna.name))

    if ("agent_releases", "arch") in dodane:
        _uzupelnij_architekture_wydan()

    _popraw_ograniczenie_czasu()
    _popraw_unikalnosc_wydan()
    _usun_stare_pola_slownikow()
    _przenies_osoby_do_slownika()
    _popraw_unikalnosc_schematow()
    _usun_pomiary_monitorow()
    _przenies_polaczenia_wirtualizacji()
    _poszerz_wektory_cvss()


def _popraw_ograniczenie_czasu() -> None:
    """Wymienia warunek czasu pracy: z "dodatni" na "niezerowy".

    Pierwotnie czas pracy mogl byc tylko dodatni. Skoro jednak wpisu nie da sie
    poprawic ani skasowac, jedyna droga do naprawienia pomylki jest wpis ujemny
    ("-15") - i baza musi go przepuscic. Zera nadal nie wpuszczamy: wpis o zerze
    minut niczego nie mowi.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "helpdesk_czas" not in set(inspector.get_table_names()):
        return
    nazwy = {w["name"] for w in inspector.get_check_constraints("helpdesk_czas")}
    if "ck_czas_niezerowy" in nazwy:
        return

    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE helpdesk_czas DROP CONSTRAINT IF EXISTS ck_czas_dodatni"
        ))
        conn.execute(text(
            "ALTER TABLE helpdesk_czas ADD CONSTRAINT ck_czas_niezerowy CHECK (minuty <> 0)"
        ))
    log.info("czas pracy: warunek 'minuty > 0' wymieniony na 'minuty <> 0'")


def _popraw_unikalnosc_wydan() -> None:
    """Wymienia stare ograniczenie unikalnosci wersji agenta.

    Pierwotnie wydanie bylo identyfikowane samym numerem wersji, bo agent byl
    tylko dla Windows. Dzis wydanie to numer PLUS system PLUS architektura -
    ta sama wersja 0.5.2 istnieje jako plik dla Windows i jako paczka zrodel
    dla Linuksa, a wydania dla ARM i x86 to dwa rozne pliki.

    Migracja pomostowa doklada kolumny, ale nie rusza ograniczen, wiec bazy
    zalozone wczesniej nadal odrzucaly druga wersje o tym samym numerze -
    i to bledem o naruszeniu unikalnosci, ktory wygladal jak awaria serwera.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "agent_releases" not in set(inspector.get_table_names()):
        return

    indeksy = {i["name"]: i for i in inspector.get_indexes("agent_releases")}
    stary = indeksy.get("ix_agent_releases_version")
    nowy = "uq_release_version_os_arch"

    if stary is not None and stary.get("unique"):
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_agent_releases_version"))
            conn.execute(text("CREATE INDEX ix_agent_releases_version ON agent_releases (version)"))
        log.info("wymieniono ograniczenie unikalnosci wersji agenta na wersje+system+architekture")
        indeksy.pop("ix_agent_releases_version", None)

    if nowy not in indeksy:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {nowy} "
                    "ON agent_releases (version, os_family, arch)"
                )
            )


def _uzupelnij_architekture_wydan() -> None:
    """Ustawia architekture wgranym wczesniej wydaniom, czytajac ich pliki.

    Kolumna dokladana jest z wartoscia domyslna, bo inaczej nie da sie jej
    dodac do tabeli z danymi. Domyslna wartosc jest jednak tylko zgadywaniem:
    wydanie dla ARM zostaloby oznaczone jako x86_64 i serwer zaproponowalby
    je Raspberry Pi jako plik nie do uruchomienia - czyli dokladnie ta awaria,
    ktorej rozroznianie architektur ma zapobiegac. Naglowek pliku jest faktem,
    wiec odczytujemy go i poprawiamy zapis.
    """
    from sqlalchemy import select, update

    from . import models
    from .config import get_settings
    from .services import architektura

    katalog = Path(get_settings().release_dir)
    poprawione = 0
    with Session(engine) as db:
        wydania = db.execute(
            select(models.AgentRelease.id, models.AgentRelease.storage_name,
                   models.AgentRelease.version, models.AgentRelease.arch)
        ).all()
        for identyfikator, nazwa_pliku, wersja, zapisana in wydania:
            wykryta = architektura.wykryj_z_pliku(katalog / nazwa_pliku)
            if wykryta is None:
                log.warning(
                    "nie moge odczytac architektury wydania %s - zostaje %s, "
                    "wgraj plik ponownie jesli to wydanie dla innej architektury",
                    wersja, zapisana,
                )
                continue
            if wykryta != zapisana:
                db.execute(
                    update(models.AgentRelease)
                    .where(models.AgentRelease.id == identyfikator)
                    .values(arch=wykryta)
                )
                log.info("wydanie %s: architektura %s -> %s", wersja, zapisana, wykryta)
                poprawione += 1
        db.commit()
    if poprawione:
        log.info("poprawiono architekture %d wydan", poprawione)


def _domyslna_wartosc(kolumna) -> str | None:
    """Wartosc domyslna kolumny w postaci gotowej do wstawienia w SQL.

    Obslugujemy wylacznie proste wartosci stale. Domyslne wyliczane funkcja
    (np. znacznik czasu) nie daja jednej wartosci dla istniejacych wierszy,
    wiec takie przypadki zostawiamy do recznej migracji.
    """
    zrodlo = kolumna.default or kolumna.server_default
    if zrodlo is None:
        return None
    wartosc = getattr(zrodlo, "arg", None)
    if wartosc is None or callable(wartosc):
        return None
    if isinstance(wartosc, bool):
        # PostgreSQL ma osobny typ boolean i nie przyjmie tu liczby - ALTER
        # TABLE z DEFAULT 0 konczy sie bledem "column is of type boolean but
        # default expression is of type integer", czyli serwer nie wstaje po
        # aktualizacji.
        return "TRUE" if wartosc else "FALSE"
    if isinstance(wartosc, (int, float)):
        return str(wartosc)
    if isinstance(wartosc, str):
        return "'" + wartosc.replace("'", "''") + "'"
    return None


def _utworz_indeksy_gin() -> None:
    """Indeksy GIN na kolumnach JSONB - po nich szukamy wewnatrz raportow."""
    from sqlalchemy import text

    statements = [
        "CREATE INDEX IF NOT EXISTS ix_snapshot_payload_gin "
        "ON inventory_snapshots USING gin (payload jsonb_path_ops)",
        "CREATE INDEX IF NOT EXISTS ix_asset_facts_gin "
        "ON assets USING gin (facts jsonb_path_ops)",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


# Kolumny tekstowe zastapione odwolaniem do wpisu slownika. Wartosci nie sa
# przenoszone swiadomie: to byly luzne napisy bez struktury, a przypisania
# powstaja od nowa jako relacje. Migracja pomostowa umie tylko DOKLADAC
# kolumny, wiec usuniecie musi byc wypisane wprost.
STARE_POLA_SLOWNIKOW = (
    ("owners", "department"),      # dzial nalezy do osoby, nie do maszyny
    ("assets", "lokalizacja"),
    ("assets", "vendor"),
)


def _usun_stare_pola_slownikow() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    istniejace = set(inspector.get_table_names())
    for tabela, kolumna in STARE_POLA_SLOWNIKOW:
        if tabela not in istniejace:
            continue
        if kolumna not in {k["name"] for k in inspector.get_columns(tabela)}:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {tabela} DROP COLUMN {kolumna}"))
        log.warning(
            "usunieto kolumne %s.%s wraz z zawartoscia - przypisania slownikowe "
            "wpisuje sie od nowa jako relacje", tabela, kolumna,
        )


def _przenies_osoby_do_slownika() -> None:
    """Przenosi tabele owners do slownika jako kategorie "osoba".

    Danych NIE tracimy: kazda osoba staje sie wpisem slownika, a wpis dostaje
    TEN SAM identyfikator co poprzednio. Dzieki temu assets.owner_id
    i assets.uzytkownik_id nadal wskazuja wlasciwa osobe i nie trzeba ich
    przepisywac - zmienia sie wylacznie tabela, do ktorej prowadza.

    Kolejnosc ma znaczenie: najpierw wpisy, potem przepiecie kluczy obcych,
    na koncu usuniecie tabeli. Odwrotna zostawilaby maszyny ze wskazaniem
    na nieistniejacy wiersz.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "owners" not in set(inspector.get_table_names()):
        return

    with engine.begin() as conn:
        osoby = conn.execute(text(
            "SELECT id, tenant_id, full_name, email, phone, dzial_id, notes FROM owners"
        )).mappings().all()

        uzyte: set[tuple[str, str]] = set()
        for osoba in osoby:
            nazwa = " ".join((osoba["full_name"] or "").split()) or osoba["email"]
            klucz = nazwa.lower()
            # Slownik ma unikalny klucz w obrebie firmy, a dwie osoby moga sie
            # nazywac tak samo. Rozroznia je wtedy adres - jedyne, co na pewno
            # bylo unikalne w starej tabeli.
            if (osoba["tenant_id"], klucz) in uzyte:
                nazwa = f"{nazwa} ({osoba['email']})"
                klucz = nazwa.lower()
            uzyte.add((osoba["tenant_id"], klucz))

            atrybuty = {"imie_nazwisko": nazwa, "email": osoba["email"]}
            if osoba["phone"]:
                atrybuty["telefon"] = osoba["phone"]
            if osoba["dzial_id"]:
                atrybuty["dzial"] = osoba["dzial_id"]
            if osoba["notes"]:
                atrybuty["notatki"] = osoba["notes"]

            conn.execute(
                text(
                    "INSERT INTO slowniki (id, tenant_id, kategoria, wartosc, klucz,"
                    " atrybuty, utworzony, utworzyl)"
                    " VALUES (:id, :tenant_id, 'osoba', :wartosc, :klucz,"
                    " CAST(:atrybuty AS jsonb), now(), 'migracja osob')"
                    " ON CONFLICT DO NOTHING"
                ),
                {"id": osoba["id"], "tenant_id": osoba["tenant_id"], "wartosc": nazwa,
                 "klucz": klucz, "atrybuty": json.dumps(atrybuty, ensure_ascii=False)},
            )

        # Klucze obce trzeba przepiac jawnie: create_all ich nie rusza, a bez
        # tego usuniecie tabeli owners by sie nie powiodlo.
        for kolumna in ("owner_id", "uzytkownik_id"):
            nazwy = conn.execute(text(
                "SELECT tc.constraint_name FROM information_schema.table_constraints tc"
                " JOIN information_schema.key_column_usage kcu"
                "   ON tc.constraint_name = kcu.constraint_name"
                " WHERE tc.table_name = 'assets' AND tc.constraint_type = 'FOREIGN KEY'"
                "   AND kcu.column_name = :kolumna"
            ), {"kolumna": kolumna}).scalars().all()
            for nazwa in nazwy:
                conn.execute(text(f'ALTER TABLE assets DROP CONSTRAINT "{nazwa}"'))
            conn.execute(text(
                f"ALTER TABLE assets ADD CONSTRAINT assets_{kolumna}_slownik_fkey"
                f" FOREIGN KEY ({kolumna}) REFERENCES slowniki (id) ON DELETE SET NULL"
            ))

        conn.execute(text("DROP TABLE owners CASCADE"))

    log.warning("przeniesiono %d osob do slownika i usunieto tabele owners", len(osoby))


def _popraw_unikalnosc_schematow() -> None:
    """Schemat rozroznia sie teraz takze rodzajem sprzetu.

    Wczesniej para (firma, kategoria) byla unikalna, bo schemat opisywal tylko
    slownik. Odkad kazdy rodzaj sprzetu ma wlasny zestaw pol, ta sama firma ma
    kilka schematow kategorii "sprzet" - po jednym na rodzaj.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "schematy_slownikow" not in set(inspector.get_table_names()):
        return
    nazwy = {o["name"] for o in inspector.get_unique_constraints("schematy_slownikow")}
    if "uq_schemat_kategoria" not in nazwy:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE schematy_slownikow "
                          "DROP CONSTRAINT uq_schemat_kategoria"))
        conn.execute(text("ALTER TABLE schematy_slownikow "
                          "ADD CONSTRAINT uq_schemat_kategoria_rodzaj "
                          "UNIQUE (tenant_id, kategoria, rodzaj)"))
    log.info("schematy rozrozniane takze rodzajem sprzetu")


def _usun_pomiary_monitorow() -> None:
    """Kasuje tabele pojedynczych pomiarow monitorowania.

    Pierwsza wersja monitorowania sondowala z serwera i zapisywala kazda
    sonde osobnym wierszem. Sonduje teraz agent i przysyla podsumowania
    okien wraz z samymi przerwami, wiec wiersz na sonde nie ma juz kogo
    opisywac - a przy sondowaniu co minute rosl o 1440 wierszy dziennie
    na kazdy cel.

    Danych nie przenosimy: pochodzily z sondowania, ktorego juz nie ma,
    i opisywaly widok z serwera, a nie z sieci uslugi. Zachowanie ich
    zawyzaloby albo zanizalo dostepnosc liczona teraz z okien agenta.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "pomiary_monitorow" not in set(inspector.get_table_names()):
        return
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE pomiary_monitorow CASCADE"))
    log.warning(
        "usunieto tabele pomiary_monitorow - dostepnosc liczy sie teraz "
        "z okien raportowanych przez agenta"
    )


def _przenies_polaczenia_wirtualizacji() -> None:
    """Jedno polaczenie na agenta -> dowolnie wiele (wirtualizacja_polaczenia).

    Wiersz dawnej tabeli staje sie polaczeniem z tym samym identyfikatorem
    rewizji - agent nie widzi zmiany konfiguracji, a jego wynik w locie
    nadal zostanie przyjety. Obiekty widziane przez tego agenta dostaja
    wskazanie na polaczenie, zeby znikniecie oceniane bylo jak dotad.
    Przeniesiony wiersz jest kasowany w tej samej transakcji, wiec usuniete
    pozniej polaczenie nie wraca przy kolejnym starcie.
    """
    from sqlalchemy import select, update

    from . import models

    with session_scope() as db:
        for dostawca, model in (("nutanix", models.NutanixUstawienia), ("vmware", models.VmwareUstawienia)):
            for stary in db.execute(select(model)).scalars().all():
                nowe = models.WirtualizacjaPolaczenie(
                    tenant_id=stary.tenant_id, asset_id=stary.asset_id, dostawca=dostawca,
                    **{k: getattr(stary, k) for k in (
                        "wlaczona", "adres", "uzytkownik", "haslo", "ca_pem", "interwal_minut",
                        "revision", "updated_by", "updated_at", "test_zlecony_o", "test_o",
                        "test_ok", "test_opis", "odczyt_zlecony_o", "odczyt_o", "odczyt_ok",
                        "odczyt_blad", "odczyt_liczby")},
                )
                db.add(nowe)
                db.flush()
                db.execute(update(models.NutanixObiekt).where(
                    models.NutanixObiekt.czytnik_id == stary.asset_id,
                    models.NutanixObiekt.dostawca == dostawca,
                    models.NutanixObiekt.polaczenie_id.is_(None),
                ).values(polaczenie_id=nowe.id))
                db.delete(stary)
                log.info("wirtualizacja: przeniesiono konfiguracje %s agenta %s", dostawca, stary.asset_id)


def _poszerz_wektory_cvss() -> None:
    """Wektory CVSS 4.0 bywaja dluzsze niz 128 znakow.

    Kolumny mialy 128 znakow, co wystarczalo na CVSS 3.1, ale ocena w wersji
    4.0 z NVD przerywala zapis calego przebiegu ("value too long"). Poszerzenie
    varchar w Postgresie nie przepisuje danych.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tabele = set(inspector.get_table_names())
    for tabela, kolumna in (("cve_scores", "vector"), ("cve_ubuntu", "vector"),
                            ("cve_entries", "cvss_vector")):
        if tabela not in tabele:
            continue
        for opis in inspector.get_columns(tabela):
            if opis["name"] == kolumna and (getattr(opis["type"], "length", None) or 0) < 512:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {tabela} ALTER COLUMN {kolumna} TYPE VARCHAR(512)"))
                log.info("poszerzono kolumne %s.%s do 512 znakow", tabela, kolumna)
