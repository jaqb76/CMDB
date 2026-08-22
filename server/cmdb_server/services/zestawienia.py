"""Zestawienia "pierwsza dziesiatka" - maszyny wymagajace uwagi.

Raport odpowiada na pytanie "od czego zaczac", wiec kazde zestawienie jest
krotkie i uszeregowane. Dziesiec pozycji to swiadomy limit: lista, ktora nie
miesci sie na ekranie, przestaje byc lista rzeczy do zrobienia, a staje sie
kolejnym raportem do przejrzenia kiedys.

Rozroznienie, ktore przewija sie przez caly modul: **stan** kontra **tempo**.
Zajetosc pamieci i dysku to stan - pomiar z dowolnej chwili jest o nich
prawdziwy. Obciazenie procesora to tempo, a agent raportuje raz na kilka
godzin, wiec jego odczyt opisuje jedna chwile, nie caly okres. Na Linuksie
lagodzi to srednia z pietnastu minut; na Windows zostaje probka. Kazde
zestawienie mowi wprost, na czym sie opiera.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from ..models import as_utc, utcnow
from . import cve, kolumny

log = logging.getLogger(__name__)

ILE = 10

# Progi, powyzej ktorych wartosc jest wyrozniana. Nie sa regula sztuki,
# tylko poziomem, przy ktorym warto spojrzec.
PROG_UWAGI = 75.0
PROG_ALARMU = 90.0

# Po ilu dniach bez restartu maszyna trafia na liste. Czesc poprawek jadra
# i bibliotek systemowych zaczyna dzialac dopiero po ponownym uruchomieniu,
# wiec dlugi czas pracy jest sygnalem, a nie powodem do dumy.
DNI_BEZ_RESTARTU = 60


def _procent_pamieci(raport: dict) -> float | None:
    calosc = kolumny.sciezka(raport, "hardware", "memory", "total_bytes")
    dostepna = kolumny.sciezka(raport, "hardware", "memory", "available_bytes")
    if not calosc or dostepna is None:
        return None
    return round(100 * (1 - dostepna / calosc), 1)


def _najpelniejszy_dysk(raport: dict) -> dict | None:
    """Najbardziej zapelniony wolumen maszyny.

    Bierzemy najgorszy, a nie srednia: maszyna z zapelnionym dyskiem
    systemowym i pustym dyskiem danych ma problem, ktorego srednia nie widzi.
    """
    najgorszy = None
    for wolumen in kolumny.sciezka(raport, "hardware", "storage", "logical_disks") or []:
        procent = wolumen.get("used_percent")
        if procent is None:
            continue
        if najgorszy is None or procent > najgorszy["procent"]:
            najgorszy = {
                "procent": float(procent),
                "mount": wolumen.get("mount"),
                "wolne": kolumny.bajty(wolumen.get("free_bytes")),
                "rozmiar": kolumny.bajty(wolumen.get("size_bytes")),
            }
    return najgorszy


def _dni_pracy(maszyna, raport: dict) -> float | None:
    sekundy = kolumny.sciezka(raport, "os", "uptime_seconds")
    if sekundy:
        return round(sekundy / 86400, 1)
    # Windows nie zawsze podaje czas pracy, ale podaje date startu.
    start = kolumny.sciezka(raport, "os", "last_boot")
    if not start:
        return None
    try:
        from datetime import datetime

        chwila = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((utcnow() - as_utc(chwila)).total_seconds() / 86400, 1)


def _wpis(maszyna, wartosc, opis: str, jednostka: str = "%") -> dict:
    return {
        "maszyna": maszyna,
        "wartosc": wartosc,
        "opis": opis,
        "jednostka": jednostka,
        "alarm": isinstance(wartosc, (int, float)) and jednostka == "%" and wartosc >= PROG_ALARMU,
        "uwaga": isinstance(wartosc, (int, float)) and jednostka == "%" and wartosc >= PROG_UWAGI,
    }


def zbierz(db: Session, maszyny: list, ostatni_raport) -> dict:
    """Wszystkie zestawienia naraz.

    Raporty agenta czytamy RAZ na maszyne i liczymy z nich wszystko - kazde
    zestawienie osobno oznaczaloby kilkukrotne czytanie tych samych danych.
    """
    pamiec, dyski, procesor, restarty = [], [], [], []
    podatnosci, aktualizacje, administratorzy, bez_kontaktu = [], [], [], []
    teraz = utcnow()

    for maszyna in maszyny:
        raport = ostatni_raport(db, maszyna.id) or {}

        procent = _procent_pamieci(raport)
        if procent is not None:
            pamiec.append(_wpis(maszyna, procent, _opis_pamieci(raport)))

        dysk = _najpelniejszy_dysk(raport)
        if dysk is not None:
            dyski.append(_wpis(
                maszyna, dysk["procent"],
                f"{dysk['mount']} - wolne {dysk['wolne']} z {dysk['rozmiar']}",
            ))

        obciazenie = kolumny.sciezka(raport, "hardware", "load") or {}
        if obciazenie.get("percent") is not None:
            procesor.append(_wpis(
                maszyna, obciazenie["percent"], _opis_obciazenia(obciazenie)
            ))

        dni = _dni_pracy(maszyna, raport)
        if dni is not None and dni >= DNI_BEZ_RESTARTU:
            restarty.append(_wpis(maszyna, dni, "bez ponownego uruchomienia", "dni"))

        czekajace = kolumny.sciezka(raport, "software", "updates_pending", "count")
        if czekajace:
            bezpieczenstwa = kolumny.sciezka(
                raport, "software", "updates_pending", "security_count"
            ) or 0
            aktualizacje.append(_wpis(
                maszyna, czekajace,
                f"w tym {bezpieczenstwa} z poprawkami bezpieczenstwa", "szt.",
            ))

        admini = kolumny.sciezka(raport, "users", "administrators") or []
        if len(admini) > 2:
            administratorzy.append(_wpis(
                maszyna, len(admini), ", ".join(str(a) for a in admini[:4]), "kont"
            ))

        if raport:
            wynik = cve.dopasuj(db, raport)
            if wynik["status"] == cve.STATUS_OK and wynik["fixable_count"]:
                podatnosci.append(_wpis(
                    maszyna, wynik["fixable_count"],
                    f"w tym {wynik['critical_count']} powaznych (CVSS >= 7)", "szt.",
                ))

        ostatni = as_utc(maszyna.last_seen)
        if ostatni is None or teraz - ostatni > timedelta(hours=48):
            dni_ciszy = round((teraz - ostatni).total_seconds() / 86400, 1) if ostatni else None
            bez_kontaktu.append(_wpis(
                maszyna, dni_ciszy if dni_ciszy is not None else "—",
                "od ostatniego raportu", "dni",
            ))

    def dziesiec(pozycje):
        liczbowe = [p for p in pozycje if isinstance(p["wartosc"], (int, float))]
        return sorted(liczbowe, key=lambda p: p["wartosc"], reverse=True)[:ILE]

    return {
        "pamiec": dziesiec(pamiec),
        "dyski": dziesiec(dyski),
        "procesor": dziesiec(procesor),
        "podatnosci": dziesiec(podatnosci),
        "aktualizacje": dziesiec(aktualizacje),
        "restarty": dziesiec(restarty),
        "administratorzy": dziesiec(administratorzy),
        "bez_kontaktu": bez_kontaktu[:ILE],
        "zbadanych": len(maszyny),
        "prog_uwagi": PROG_UWAGI,
        "prog_alarmu": PROG_ALARMU,
        "dni_bez_restartu": DNI_BEZ_RESTARTU,
    }


def _opis_pamieci(raport: dict) -> str:
    calosc = kolumny.sciezka(raport, "hardware", "memory", "total_bytes")
    dostepna = kolumny.sciezka(raport, "hardware", "memory", "available_bytes")
    return f"wolne {kolumny.bajty(dostepna)} z {kolumny.bajty(calosc)}"


def _opis_obciazenia(obciazenie: dict) -> str:
    """Opis mowi, na czym oparta jest liczba - to nie jest ozdoba.

    Srednia z pietnastu minut i trzysekundowa probka to dwie rozne rzeczy,
    a w jednej kolumnie wygladaja tak samo.
    """
    if obciazenie.get("source") == "loadavg-15min":
        rdzenie = obciazenie.get("cores")
        return f"srednia z 15 min, {obciazenie.get('load_15')} na {rdzenie} rdzeni"
    return "probka z chwili raportu"
