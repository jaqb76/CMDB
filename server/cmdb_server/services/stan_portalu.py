"""Stan samego portalu: ruch, wydajnosc, zasoby serwera, baza i bezpieczenstwo.

Wszystko czytane na zadanie, bez osobnego zbierania w tle - poza ruchem,
ktory liczy services.ruch. Zasoby serwera czytamy wprost z /proc i cgroup,
bez dodatkowej biblioteki: portal chodzi w kontenerze Linuksa, a tam te
pliki sa zawsze. Gdzie ich nie ma (testy na innym systemie), pozycja jest
pusta zamiast wywracac strone.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import (
    AgentCredential, Asset, AuditLog, BlokadaLogowania, PortalUser, ReportReceipt,
    StatystykaRuchu, as_utc, utcnow,
)
from . import ruch

START_PROCESU = time.time()


# --- ruch i wydajnosc -------------------------------------------------------

def _zsumuj(wiersze) -> dict:
    wynik = {"liczba": 0, "bledy_4xx": 0, "bledy_5xx": 0, "suma_ms": 0.0, "maks_ms": 0.0,
             **{k: 0 for k in ruch.KOLUMNY}}
    for w in wiersze:
        for klucz in ("liczba", "bledy_4xx", "bledy_5xx", "suma_ms", *ruch.KOLUMNY):
            wynik[klucz] += getattr(w, klucz) or 0
        wynik["maks_ms"] = max(wynik["maks_ms"], w.maks_ms or 0)
    wynik["srednia_ms"] = round(wynik["suma_ms"] / wynik["liczba"], 1) if wynik["liczba"] else None
    wynik["p50_ms"] = ruch.percentyl(wynik, 0.5)
    wynik["p95_ms"] = ruch.percentyl(wynik, 0.95)
    wynik["bledy_proc"] = (round(wynik["bledy_5xx"] * 100 / wynik["liczba"], 2)
                           if wynik["liczba"] else None)
    return wynik


def statystyki_ruchu(db: Session, godzin: int = 24) -> dict:
    teraz = utcnow()
    od = (teraz - timedelta(hours=godzin)).replace(second=0, microsecond=0)
    wiersze = db.execute(select(StatystykaRuchu).where(StatystykaRuchu.minuta >= od)).scalars().all()

    wedlug_grupy: dict[str, list] = {}
    for w in wiersze:
        wedlug_grupy.setdefault(w.grupa, []).append(w)
    grupy = {}
    for nazwa in ruch.NAZWY_GRUP:
        grupy[nazwa] = _zsumuj(wedlug_grupy.get(nazwa, []))
        grupy[nazwa]["nazwa"] = ruch.NAZWY_GRUP[nazwa]
    # Bez plikow statycznych - ich czas to czas dysku, nie aplikacji.
    aplikacja = [w for w in wiersze if w.grupa != "statyczne"]

    # Jeden przebieg po wierszach zamiast przegladania wszystkich dla kazdej
    # godziny - przy 30 dniach to byly setki milionow porownan.
    wedlug_godziny: dict = {}
    for w in aplikacja:
        wedlug_godziny.setdefault(as_utc(w.minuta).replace(minute=0), []).append(w)

    godziny = []
    for krok in range(godzin - 1, -1, -1):
        poczatek = (teraz - timedelta(hours=krok)).replace(minute=0, second=0, microsecond=0)
        suma = _zsumuj(wedlug_godziny.get(as_utc(poczatek), []))
        godziny.append({"godzina": poczatek, "liczba": suma["liczba"],
                        "bledy": suma["bledy_5xx"], "p95_ms": suma["p95_ms"]})
    najwiecej = max((g["liczba"] for g in godziny), default=0)
    for g in godziny:
        g["wysokosc"] = round(g["liczba"] * 80 / najwiecej, 2) if najwiecej else 0

    ostatnia_godzina = _zsumuj([w for w in aplikacja if as_utc(w.minuta) >= teraz - timedelta(hours=1)])
    ostatnie_5 = _zsumuj([w for w in wiersze if as_utc(w.minuta) >= teraz - timedelta(minutes=5)])
    return {
        "razem": _zsumuj(aplikacja),
        "ostatnia_godzina": ostatnia_godzina,
        "na_minute": round(ostatnie_5["liczba"] / 5, 1),
        "grupy": grupy,
        "godziny": godziny,
        "najwiecej": najwiecej,
        "godzin": godzin,
    }


# --- zasoby serwera -----------------------------------------------------------

def _czytaj(sciezka: str) -> str | None:
    try:
        return Path(sciezka).read_text().strip()
    except OSError:
        return None


def _pamiec() -> dict | None:
    """Pamiec kontenera z cgroup v2, a gdy jej nie ma - calej maszyny."""
    uzyte, limit = _czytaj("/sys/fs/cgroup/memory.current"), _czytaj("/sys/fs/cgroup/memory.max")
    info = _czytaj("/proc/meminfo")
    calosc = dostepne = None
    if info:
        pola = {w.split(":")[0]: int(w.split()[1]) * 1024 for w in info.splitlines() if w.split()}
        calosc, dostepne = pola.get("MemTotal"), pola.get("MemAvailable")
    if uzyte and uzyte.isdigit():
        granica = int(limit) if limit and limit.isdigit() else calosc
        if granica:
            return {"uzyte": int(uzyte), "calosc": granica, "zrodlo": "kontener",
                    "procent": round(int(uzyte) * 100 / granica, 1)}
    if calosc and dostepne is not None:
        return {"uzyte": calosc - dostepne, "calosc": calosc, "zrodlo": "serwer",
                "procent": round((calosc - dostepne) * 100 / calosc, 1)}
    return None


def zasoby() -> dict:
    procesory = os.cpu_count() or 1
    try:
        obciazenie = os.getloadavg()
    except OSError:
        obciazenie = None
    try:
        dysk = shutil.disk_usage("/")
        dysk = {"uzyte": dysk.used, "calosc": dysk.total,
                "procent": round(dysk.used * 100 / dysk.total, 1)}
    except OSError:
        dysk = None
    czas_pracy = None
    uptime = _czytaj("/proc/uptime")
    if uptime:
        czas_pracy = float(uptime.split()[0])
    return {
        "procesory": procesory,
        "obciazenie": [round(o, 2) for o in obciazenie] if obciazenie else None,
        # Obciazenie z minuty w stosunku do liczby rdzeni - 100% to pelne.
        "obciazenie_proc": round(obciazenie[0] * 100 / procesory) if obciazenie else None,
        "pamiec": _pamiec(),
        "dysk": dysk,
        "czas_pracy_s": czas_pracy,
        "proces_s": time.time() - START_PROCESU,
    }


# --- baza ---------------------------------------------------------------------

def baza(db: Session) -> dict:
    wynik: dict = {}
    wynik["rozmiar"] = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    polaczenia = db.execute(text(
        "SELECT count(*) AS razem, count(*) FILTER (WHERE state = 'active') AS aktywne "
        "FROM pg_stat_activity WHERE datname = current_database()")).one()
    wynik["polaczenia"], wynik["aktywne"] = polaczenia.razem, polaczenia.aktywne
    wynik["maks_polaczen"] = int(db.execute(text("SHOW max_connections")).scalar())
    trafienia = db.execute(text(
        "SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname = current_database()")).one()
    suma = (trafienia.blks_hit or 0) + (trafienia.blks_read or 0)
    wynik["trafienia_proc"] = round(trafienia.blks_hit * 100 / suma, 2) if suma else None
    wynik["tabele"] = [
        {"nazwa": w.relname, "rozmiar": w.rozmiar, "wiersze": w.n_live_tup}
        for w in db.execute(text(
            "SELECT relname, pg_total_relation_size(relid) AS rozmiar, n_live_tup "
            "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 6"))
    ]
    najwieksza = max((t["rozmiar"] for t in wynik["tabele"]), default=0)
    for t in wynik["tabele"]:
        t["szerokosc"] = round(t["rozmiar"] * 100 / najwieksza, 2) if najwieksza else 0
    return wynik


# --- bezpieczenstwo -------------------------------------------------------------

def bezpieczenstwo(db: Session) -> dict:
    teraz = utcnow()
    doba = teraz - timedelta(hours=24)
    blokady = db.execute(select(BlokadaLogowania).order_by(
        BlokadaLogowania.ostatnia_proba.desc())).scalars().all()
    aktywne = [b for b in blokady if b.blokada_do and as_utc(b.blokada_do) > teraz]
    proby = [b for b in blokady if b.ostatnia_proba and as_utc(b.ostatnia_proba) >= doba]

    def wiersz(b):
        rodzaj, _, co = b.klucz.partition(":")
        return {"klucz": b.klucz, "rodzaj": rodzaj, "co": co, "licznik": b.licznik,
                "do": b.blokada_do, "ostatnia": b.ostatnia_proba}

    konta = db.execute(select(
        func.count(PortalUser.id),
        func.count(PortalUser.id).filter(PortalUser.is_active.is_(True)),
        func.count(PortalUser.id).filter(PortalUser.is_superadmin.is_(True)),
        func.count(PortalUser.id).filter(PortalUser.totp_wlaczone.is_(True)),
        func.count(PortalUser.id).filter(PortalUser.last_login_at >= doba),
    )).one()
    return {
        "blokady": [wiersz(b) for b in aktywne],
        "zablokowane_konta": sum(1 for b in aktywne if b.klucz.startswith("konto:")),
        "zablokowane_adresy": sum(1 for b in aktywne if b.klucz.startswith("ip:")),
        # Nieudane proby z doby liczone po adresach - konto i adres to te
        # same proby widziane z dwoch stron, wiec suma bylaby podwojna.
        "nieudane_proby": sum(b.licznik for b in proby if b.klucz.startswith("ip:")),
        "podejrzane_adresy": [wiersz(b) for b in proby
                              if b.klucz.startswith("ip:") and b not in aktywne][:10],
        "konta": {"razem": konta[0], "aktywne": konta[1], "wylaczone": konta[0] - konta[1],
                  "superadmini": konta[2], "z_2fa": konta[3], "zalogowani_doba": konta[4]},
        "logowania_udane": _zdarzenia(db, doba, ("login.ok", "mobile.login.ok")),
        "logowania_nieudane": _zdarzenia(db, doba, ("login.failed", "mobile.login.failed",
                                                    "login.blocked", "mobile.login.blocked")),
        "zdarzenia_audytu_doba": db.execute(select(func.count(AuditLog.id)).where(
            AuditLog.created_at >= doba)).scalar_one(),
        "odwolane_klucze_agentow": db.execute(select(func.count(AgentCredential.id)).where(
            AgentCredential.revoked_at.is_not(None))).scalar_one(),
        "zablokowana_rejestracja": db.execute(select(func.count(Asset.id)).where(
            Asset.enrollment_blocked.is_(True))).scalar_one(),
        "raporty_doba": db.execute(select(func.count()).select_from(ReportReceipt).where(
            ReportReceipt.received_at >= doba)).scalar_one(),
    }


def _zdarzenia(db: Session, od, akcje: tuple[str, ...]) -> int:
    return db.execute(select(func.count(AuditLog.id)).where(
        AuditLog.created_at >= od, AuditLog.action.in_(akcje))).scalar_one()
