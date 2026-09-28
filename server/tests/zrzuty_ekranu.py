"""Zrzuty ekranu portalu do dokumentacji (docs/zrzuty).

Nie jest zwyklym testem - nazwa nie zaczyna sie od "test_", wiec pytest go
nie zbiera. Uruchamia sie go recznie, gdy zmieni sie wyglad:

    cd server
    ZRZUTY=1 python -m pytest tests/zrzuty_ekranu.py -q

Wymaga bazy testowej (jak pozostale testy) i Playwrighta z Chromium
(pip install playwright; przegladarke wskazuje CHROMIUM, domyslnie ta
z playwright install). Dane sa przykladowe - generowane tutaj, bez zadnej
prawdziwej maszyny.
"""
from __future__ import annotations

import os
import random
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from cmdb_server.db import SessionLocal
from cmdb_server.models import (
    Asset, AssetCurrentReport, BlokadaLogowania, InventorySnapshot, Tenant, utcnow,
)
from cmdb_server.services import ruch

from .factories import build_report
from .test_agent_api import enroll
from .test_cve import _pakiet, _raport as _raport_debian, kanal_debian  # noqa: F401
from .test_cve_windows import _pobierz_kanal
from .test_tenant_isolation import _login

pytestmark = pytest.mark.skipif(not os.environ.get("ZRZUTY"), reason="tylko na zadanie: ZRZUTY=1")

KATALOG = Path(__file__).resolve().parents[2] / "docs" / "zrzuty"
STATIC = Path(__file__).resolve().parents[1] / "cmdb_server" / "static"

EKRANY = {
    "komputer": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1},
    "przegladarka-telefon": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2,
                "is_mobile": True, "has_touch": True},
}


def _wyslij_raport(client, token_rejestracji, machine_id, hostname, raport):
    token = enroll(client, token_rejestracji, machine_id=machine_id, hostname=hostname).json()["agent_token"]
    raport.update(machine_id=machine_id)
    raport["identity"]["hostname"] = hostname
    raport["identity"]["fqdn"] = f"{hostname.lower()}.przyklad.local"
    raport["identity"]["domain"] = "przyklad.local"
    odpowiedz = client.post("/api/v1/inventory", headers={"Authorization": f"Bearer {token}"}, json=raport)
    assert odpowiedz.status_code == 200, odpowiedz.text
    return odpowiedz.json()["asset_id"]


def _windows_serwer(poprawka="2600"):
    raport = build_report()
    raport["agent"]["version"] = "0.6.149+1"
    raport["os"].update(name="Microsoft Windows Server 2022 Standard", version="10.0.20348",
                        build="20348", build_revision=poprawka, display_version="21H2")
    raport["hardware"]["system"].update(manufacturer="HPE", model="ProLiant DL360 Gen10")
    raport["software"]["updates"] = [
        {"id": "KB5042881", "description": "Security Update", "installed_on": "2026-08-14"},
        {"id": "KB5041160", "description": "Security Update", "installed_on": "2026-07-10"},
    ]
    raport["software"]["updates_pending"] = {
        "status": "ok", "source": "windows-update", "checked_at": utcnow().isoformat(),
        "count": 2, "security_count": 2, "index_age_hours": None, "index_refresh": None,
        "detail": None,
        "entries": [
            {"id": "KB5043050", "title": "2026-09 Cumulative Update for Microsoft server operating system version 21H2 for x64-based Systems (KB5043050)",
             "security": True, "severity": "Critical"},
            {"id": "KB5043126", "title": "2026-09 Cumulative Update for .NET Framework 3.5, 4.8 and 4.8.1 (KB5043126)",
             "security": True, "severity": "Important"},
        ],
    }
    return raport


def _debian_serwer():
    raport = build_report(packages=[_pakiet("curl", "7.88.1-10+deb12u14"), _pakiet("openssl", "3.0.15-1~deb12u1")])
    raport["agent"]["collector"] = "linux"
    raport["agent"]["version"] = "0.6.149+1"
    raport["identity"]["os_family"] = "linux"
    raport["os"] = {"name": "Debian GNU/Linux 12 (bookworm)", "version": "12", "distro_id": "debian",
                    "codename": "bookworm", "kernel": "6.1.0-25-amd64"}
    raport["hardware"]["system"].update(manufacturer="QEMU", model="Standard PC (Q35)")
    return raport


def _flota(tenant_id: str, ile: int, przedrostek: str) -> None:
    """Pozostale zasoby firmy: stacje, serwery, drukarki, sprzet sieciowy."""
    teraz = utcnow()
    with SessionLocal() as db:
        for i in range(ile):
            typ = random.choice(["komputer"] * 6 + ["laptop"] * 3 + ["siec", "drukarka", "monitor"])
            agent = typ in ("komputer", "laptop") and random.random() < 0.75
            system = random.choice(["windows"] * 5 + ["linux"] * 2) if agent else None
            nazwa = {"komputer": "WS", "laptop": "NB", "siec": "SW", "drukarka": "PRN", "monitor": "MON"}[typ]
            maszyna = Asset(
                tenant_id=tenant_id, machine_id=f"{przedrostek}-{i}",
                hostname=f"{przedrostek}-{nazwa}-{i:03d}", typ=typ,
                zrodlo="agent" if agent else "reczne", os_family=system,
                os_name={"windows": "Microsoft Windows 11 Pro", "linux": "Debian GNU/Linux 12 (bookworm)"}.get(system),
                agent_version=random.choice(["0.6.149+1"] * 5 + ["0.6.131+1"] * 2 + ["0.6.120+1"]) if agent else None,
                last_seen=teraz - timedelta(hours=random.choice([1, 1, 2, 3, 6, 20, 70])),
                first_seen=teraz - timedelta(days=random.randint(0, 200)),
            )
            db.add(maszyna)
            db.flush()
            raport = None
            if system == "linux":
                raport = _raport_debian([_pakiet("curl", random.choice(["7.88.1-10+deb12u14", "7.88.1-10+deb12u16"]))])
            elif system == "windows" and random.random() < 0.5:
                maszyna.os_name = "Microsoft Windows Server 2022 Standard"
                raport = {"os": {"name": maszyna.os_name, "version": "10.0.20348", "build": "20348",
                                 "build_revision": random.choice(["2600", "2655", "2700"])},
                          "software": {"packages": []}}
            if raport:
                db.add(AssetCurrentReport(asset_id=maszyna.id, tenant_id=tenant_id, collected_at=teraz,
                                          payload=raport, payload_hash="x"))
                db.add(InventorySnapshot(tenant_id=tenant_id, asset_id=maszyna.id, collected_at=teraz,
                                         payload_hash="0" * 64, payload=raport))
        db.commit()


def _ruch() -> None:
    teraz = utcnow()
    for godzina in range(24):
        for _ in range(random.randint(120, 380)):
            sciezka = random.choice(["/assets"] * 3 + ["/api/v1/inventory"] * 5 + ["/admin", "/api/v1/mobile/a", "/login"])
            ms = random.choice([25, 40, 60, 90, 140, 320]) if "inventory" not in sciezka else random.choice([60, 110, 240, 480])
            ruch.zanotuj(sciezka, 500 if random.random() < 0.002 else 200, ms,
                         teraz - timedelta(hours=godzina, minutes=random.randint(0, 59)))
    with SessionLocal() as db:
        ruch.zrzuc(db)
        db.add(BlokadaLogowania(klucz="konto:jan.kowalski@przyklad.pl", licznik=5,
                                ostatnia_proba=teraz - timedelta(minutes=12), blokada_do=teraz + timedelta(minutes=48)))
        db.add(BlokadaLogowania(klucz="ip:203.0.113.45", licznik=19,
                                ostatnia_proba=teraz - timedelta(minutes=3), blokada_do=teraz + timedelta(hours=11)))
        db.commit()


def _html(client, adres: str) -> str:
    html = client.get(adres).text
    for nazwa in ("app.css", "app.js"):
        html = re.sub(rf'/static/{re.escape(nazwa)}\?v=[^"]*', (STATIC / nazwa).as_uri(), html)
    # Pasek "DEVELOPMENT" oznacza instancje testowa - na zrzucie tylko by mylil.
    html = re.sub(r'<div class="pasek-instancji[^"]*">[^<]*</div>', "", html)
    return html.replace('src="/static/', f'src="{STATIC.as_uri()}/').replace('href="/static/', f'href="{STATIC.as_uri()}/')


def _przyklad(client, tenant_a, tenant_b) -> str:
    """Przykladowa instalacja: dwie firmy, flota, podatnosci, ruch. Zwraca id SRV-FS01."""
    random.seed(7)
    _pobierz_kanal()
    with SessionLocal() as db:
        db.execute(update(Tenant).where(Tenant.id == tenant_a["id"]).values(
            name="Przykładowa Sp. z o.o.", slug="przyklad"))
        db.execute(update(Tenant).where(Tenant.id == tenant_b["id"]).values(
            name="Biuro Rachunkowe Nowak", slug="nowak"))
        db.commit()

    windows = _wyslij_raport(client, tenant_a["token"], "przyklad-srv-fs01", "SRV-FS01", _windows_serwer())
    _wyslij_raport(client, tenant_a["token"], "przyklad-srv-www01", "SRV-WWW01", _debian_serwer())
    _flota(tenant_a["id"], 46, "PRZ")
    _flota(tenant_b["id"], 18, "BRN")
    _ruch()
    return windows


def test_zrzuty_ekranu(client, tenant_a, tenant_b, make_user, kanal_debian, tmp_path):  # noqa: F811
    from playwright.sync_api import sync_playwright

    windows = _przyklad(client, tenant_a, tenant_b)
    make_user(tenant_a["id"], "admin@przyklad.pl", "haslo-do-zrzutow-123")
    make_user(None, "root@cmdb.local", "haslo-do-zrzutow-123")

    firma = [
        ("01-pulpit", "/", None),
        ("02-zasoby", "/assets", None),
        ("03-karta-maszyny", f"/assets/{windows}", None),
        ("04-podatnosci-maszyny", f"/assets/{windows}#podatnosci", None),
        ("05-pulpit-jasny", "/", "jasny"),
    ]
    administracja = [
        ("10-przeglad-administratora", "/admin", None),
        ("11-stan-portalu", "/admin/portal", None),
        ("12-dane-o-podatnosciach", "/admin/podatnosci", None),
    ]

    strony = {}
    for email, lista in (("admin@przyklad.pl", firma), ("root@cmdb.local", administracja)):
        client.cookies.clear()
        _login(client, email, "haslo-do-zrzutow-123")
        client.cookies.set("cmdb_sidebar", "rozwiniete")
        for nazwa, adres, motyw in lista:
            client.cookies.set("cmdb_motyw", motyw or "ciemny")
            if adres.startswith("/admin"):
                client.get(adres + ("&" if "?" in adres else "?") + "odswiez=1")
            sciezka, _, kotwica = adres.partition("#")
            plik = tmp_path / f"{nazwa}.html"
            plik.write_text(_html(client, sciezka), encoding="utf-8")
            strony[nazwa] = plik.as_uri() + (f"#{kotwica}" if kotwica else "")

    with sync_playwright() as p:
        przegladarka = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        for ekran, ustawienia in EKRANY.items():
            cel = KATALOG / ekran
            cel.mkdir(parents=True, exist_ok=True)
            for stary in cel.glob("*.jpg"):
                stary.unlink()
            strona = przegladarka.new_page(locale="pl-PL", timezone_id="Europe/Warsaw", **ustawienia)
            for nazwa, adres in strony.items():
                strona.goto(adres)
                strona.wait_for_timeout(200)
                strona.screenshot(path=str(cel / f"{nazwa}.jpg"), full_page=True, type="jpeg", quality=80)
            strona.close()
        przegladarka.close()


# --- dane dla zrzutow aplikacji Android -----------------------------------------
#
# Aplikacja ma wlasne, natywne ekrany. Zrzuty rysuje test Paparazzi w CI
# (android/app/src/test/.../ZrzutyEkranow.kt) - bez telefonu i bez serwera,
# z odpowiedzi API mobilnego zapisanych tutaj. Dzieki temu ekrany pokazuja
# dokladnie to, co zwraca serwer, a nie dane zmyslone w Kotlinie.

DANE_APLIKACJI = (Path(__file__).resolve().parents[2] / "android" / "app" / "src" / "test"
                  / "resources" / "zrzuty")


def test_dane_dla_aplikacji(client, tenant_a, tenant_b, kanal_debian):  # noqa: F811
    import json

    from .test_mobile_helpdesk import HASLO, _firma, _naglowki, _technik

    windows = _przyklad(client, tenant_a, tenant_b)
    _firma(tenant_a["id"], "PRZ", ("przyklad.pl",))
    from cmdb_server.services import helpdesk

    with SessionLocal() as db:
        for temat, email, tresc in (
            ("Drukarka w księgowości nie drukuje", "anna.wisniewska@przyklad.pl",
             "Od rana drukarka PRN-007 pokazuje błąd papieru, choć papier jest."),
            ("Brak dostępu do udziału \\\\SRV-FS01\\kadry", "piotr.zielinski@przyklad.pl",
             "Po zmianie hasła nie otwiera się folder kadr."),
            ("Nowy laptop dla pracownika", "kadry@przyklad.pl",
             "Od poniedziałku zaczyna nowa osoba w dziale handlowym."),
            ("VPN rozłącza się co kilka minut", "tomasz.lewandowski@przyklad.pl",
             "Pracuję zdalnie, połączenie zrywa się średnio co 5 minut."),
        ):
            helpdesk.utworz_zgloszenie(db, tenant_id=tenant_a["id"], temat=temat, tresc=tresc,
                                       zglaszajacy_email=email, message_id=f"<{abs(hash(temat))}@przyklad.pl>")
        db.commit()
    _technik("technik@przyklad.pl", [tenant_a["id"]], nazwa="Marek Technik")
    naglowki = _naglowki(client, "technik@przyklad.pl", HASLO)
    naglowki["X-CMDB-Tenant"] = "przyklad"

    def pobierz(adres):
        odpowiedz = client.get(adres, headers=naglowki)
        assert odpowiedz.status_code == 200, (adres, odpowiedz.text)
        return odpowiedz.json()

    zgloszenia = pobierz("/api/v1/mobile/helpdesk/tickets")
    dane = {
        "me": pobierz("/api/v1/mobile/me"),
        "dashboard": pobierz("/api/v1/mobile/dashboard"),
        "assets": pobierz("/api/v1/mobile/assets"),
        "asset": pobierz(f"/api/v1/mobile/assets/{windows}"),
        "changes": pobierz("/api/v1/mobile/changes"),
        "helpdesk_catalog": pobierz("/api/v1/mobile/helpdesk/catalog"),
        "tickets": zgloszenia,
        "ticket": pobierz(f"/api/v1/mobile/helpdesk/tickets/{zgloszenia['items'][0]['id']}"),
    }
    DANE_APLIKACJI.mkdir(parents=True, exist_ok=True)
    for nazwa, tresc in dane.items():
        (DANE_APLIKACJI / f"{nazwa}.json").write_text(
            json.dumps(tresc, ensure_ascii=False, indent=1), encoding="utf-8")
