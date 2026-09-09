"""Monitorowanie uslug i certyfikatow - strona serwera.

Sonduje agent, wiec nie ma tu ani jednego gniazda: serwer wydaje polityke,
przyjmuje raporty, ocenia stan i powiadamia. Sonda ma wlasne testy po stronie
agenta (agent/tests/test_monitoring.py), gdzie chodzi po prawdziwym TLS.

Raport agenta udajemy dokladnie tak, jak wyglada na drucie - przez publiczne
API z tokenem maszyny. Dzieki temu testy pilnuja takze kontraktu, a nie tylko
funkcji wewnetrznych.
"""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import (Asset, MonitorUslugi, OknoMonitorowania,
                                PrzerwaDostepnosci, utcnow)
from cmdb_server.security import issue_csrf_token
from cmdb_server.services import monitoring

from .factories import build_report
from .test_review_improvements import enroll, send

PASSWORD = "bardzo-dlugie-haslo"


# --- zaplecze ---------------------------------------------------------------

def zaloguj(client, tenant, make_user, role="admin", email="monitor@example.com"):
    uid = make_user(tenant["id"], email, PASSWORD, role)
    assert client.post("/login", data={"email": email, "password": PASSWORD},
                       follow_redirects=False).status_code == 303
    return uid, issue_csrf_token(uid)


def zarejestruj_agenta(client, tenant, machine="maszyna-monitor-01"):
    """Rejestruje agenta i wysyla raport, zeby powstal zasob z agentem."""
    enrolled, report = enroll(client, tenant, machine=machine)
    assert send(client, enrolled, report).status_code == 200
    return enrolled


def dodaj_cel(tenant_id, wykonawca_id, **nadpisania) -> str:
    dane = dict(nazwa="Usluga", host="uslugi.firma.pl", port=443, protokol="https",
                interwal_sekund=60, interwal_certyfikatu=86400, limit_sekund=10,
                liczba_prob=2, prog_ostrzezenia_dni=30, prog_alarmu_dni=7,
                weryfikuj_lancuch=False, powiadamiaj=False, aktywny=True)
    dane.update(nadpisania)
    with SessionLocal() as db:
        monitor = MonitorUslugi(tenant_id=tenant_id, wykonawca_id=wykonawca_id, **dane)
        db.add(monitor)
        db.commit()
        return monitor.id


def wyslij_raport(client, enrolled, cele, powod="okresowy"):
    return client.post(
        "/api/v1/agent/monitoring",
        headers={"Authorization": "Bearer " + enrolled["agent_token"]},
        json={"protocol": 1, "wyslano": utcnow().isoformat(), "powod": powod, "cele": cele},
    )


def wpis(monitor_id, *, sond=15, udanych=15, przerwy=None, dostepna=True,
         potwierdzona=False, blad=None, tls=None, minuty=15):
    """Wpis raportu o jednym celu - taki, jaki sklada agent."""
    koniec = utcnow()
    poczatek = koniec - datetime.timedelta(minutes=minuty)
    return {
        "id": monitor_id,
        "okno": {"od": poczatek.isoformat(), "do": koniec.isoformat(),
                 "sond": sond, "udanych": udanych},
        "przerwy": przerwy or [],
        "ostatnia_sonda": {"kiedy": koniec.isoformat(), "dostepna": dostepna,
                           "potwierdzona_awaria": potwierdzona, "czas_ms": 12,
                           "kod": 200 if dostepna else None, "adres": "10.0.0.5",
                           "blad": blad},
        "tls": tls,
    }


def certyfikat_pem(dni_waznosci: int, nazwa: str = "uslugi.firma.pl") -> str:
    """Certyfikat self-signed - agent przysyla dokladnie taki tekst."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    klucz = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    podmiot = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nazwa)])
    teraz = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(podmiot).issuer_name(podmiot)
            .public_key(klucz.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(teraz - datetime.timedelta(days=30))
            # Pol doby zapasu: waznosc liczymy w dol do pelnych dni, wiec
            # "dokladnie N dni" zamienialoby sie w N-1 po kilku mikrosekundach.
            .not_valid_after(teraz + datetime.timedelta(days=dni_waznosci, hours=12))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(nazwa)]), False)
            .sign(klucz, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def stan_tls(pem: str | None = None, odcisk: str = "a" * 64, zaufany=None, blad=None) -> dict:
    if pem is not None:
        import hashlib
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_pem_x509_certificate(pem.encode())
        odcisk = hashlib.sha256(
            cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return {"odcisk": odcisk, "zaufany": zaufany, "blad": blad, "pem": pem}


# --- polityka wydawana agentowi ---------------------------------------------

def test_agent_dostaje_tylko_swoje_cele(client, tenant_a, tenant_b):
    """Agent nie widzi celow innych maszyn ani niczego z cudzej firmy."""
    moj = zarejestruj_agenta(client, tenant_a, "maszyna-a")
    obcy = zarejestruj_agenta(client, tenant_b, "maszyna-b")
    with SessionLocal() as db:
        moje_id = db.execute(select(Asset.id).where(Asset.tenant_id == tenant_a["id"])).scalar_one()
        obce_id = db.execute(select(Asset.id).where(Asset.tenant_id == tenant_b["id"])).scalar_one()
    dodaj_cel(tenant_a["id"], moje_id, nazwa="Moja usluga")
    dodaj_cel(tenant_b["id"], obce_id, nazwa="Cudza usluga")

    odpowiedz = client.get("/api/v1/agent/monitoring-policy?nonce=" + "0" * 32,
                           headers={"Authorization": "Bearer " + moj["agent_token"]})
    assert odpowiedz.status_code == 200
    dane = odpowiedz.json()
    assert dane["asset_id"] == moje_id
    nazwy = {c["host"] for c in dane["policy"]["cele"]}
    assert len(dane["policy"]["cele"]) == 1
    # Cel drugiej firmy nie moze pojawic sie nawet jako identyfikator.
    assert obcy["agent_token"] != moj["agent_token"]
    assert "Cudza" not in odpowiedz.text


def test_polityka_niesie_nazwe_celu(client, tenant_a):
    """Nazwa jedzie do agenta, zeby "cmdb-agent status" pokazal CO sprawdza.

    Sam adres z portem nie odpowiada operatorowi stojacemu przy maszynie na
    pytanie, ktora to usluga. Zakres nadal wyznacza wykonawca_id - patrz
    test_agent_dostaje_tylko_swoje_cele, ktory pilnuje, ze cudza nazwa nie
    pojawia sie nawet w tresci odpowiedzi.
    """
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id).where(Asset.tenant_id == tenant_a["id"])).scalar_one()
    dodaj_cel(tenant_a["id"], asset_id, nazwa="Portal firmowy")

    dane = client.get("/api/v1/agent/monitoring-policy?nonce=" + "f" * 32,
                      headers={"Authorization": "Bearer " + enrolled["agent_token"]}).json()
    assert [c["nazwa"] for c in dane["policy"]["cele"]] == ["Portal firmowy"]


def test_polityka_wymaga_swiezej_wartosci_jednorazowej(client, tenant_a):
    """Nonce wraca w odpowiedzi - to on wiaze ja z tym jednym pytaniem."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    naglowki = {"Authorization": "Bearer " + enrolled["agent_token"]}
    assert client.get("/api/v1/agent/monitoring-policy?nonce=" + "a" * 32,
                      headers=naglowki).json()["nonce"] == "a" * 32
    # Wartosc spoza formatu jest odrzucana przez sam kontrakt trasy.
    assert client.get("/api/v1/agent/monitoring-policy?nonce=krotka",
                      headers=naglowki).status_code == 422


def test_wylaczony_cel_znika_z_polityki(client, tenant_a):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    naglowki = {"Authorization": "Bearer " + enrolled["agent_token"]}

    assert len(client.get("/api/v1/agent/monitoring-policy?nonce=" + "b" * 32,
                          headers=naglowki).json()["policy"]["cele"]) == 1
    with SessionLocal() as db:
        db.get(MonitorUslugi, monitor_id).aktywny = False
        db.commit()
    # Odebranie celu w panelu ma go odebrac NAPRAWDE - inaczej agent chodzilby
    # dalej po ostatniej znanej liscie.
    assert client.get("/api/v1/agent/monitoring-policy?nonce=" + "c" * 32,
                      headers=naglowki).json()["policy"]["cele"] == []


def test_polityka_niesie_zadanie_sprawdzenia_i_gasi_je_wynikiem(client, tenant_a):
    """Wymuszenie gasi sie samo, gdy przyjdzie wynik nowszy niz zadanie."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    naglowki = {"Authorization": "Bearer " + enrolled["agent_token"]}

    with SessionLocal() as db:
        db.get(MonitorUslugi, monitor_id).wymuszone_o = utcnow()
        db.commit()
    cele = client.get("/api/v1/agent/monitoring-policy?nonce=" + "d" * 32,
                      headers=naglowki).json()["policy"]["cele"]
    assert cele[0]["wymus"] is True

    assert wyslij_raport(client, enrolled, [wpis(monitor_id)]).status_code == 200
    cele = client.get("/api/v1/agent/monitoring-policy?nonce=" + "e" * 32,
                      headers=naglowki).json()["policy"]["cele"]
    assert cele[0]["wymus"] is False


def test_polityka_podaje_znany_odcisk_certyfikatu(client, tenant_a):
    """Po tym agent poznaje, czy jest sens wysylac caly certyfikat."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    naglowki = {"Authorization": "Bearer " + enrolled["agent_token"]}

    pem = certyfikat_pem(200)
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(pem))])
    cele = client.get("/api/v1/agent/monitoring-policy?nonce=" + "f" * 32,
                      headers=naglowki).json()["policy"]["cele"]
    with SessionLocal() as db:
        assert cele[0]["znany_odcisk"] == db.get(MonitorUslugi, monitor_id).cert_odcisk
        assert len(cele[0]["znany_odcisk"]) == 64


# --- przyjmowanie raportu ---------------------------------------------------

def test_raport_zapisuje_okno_i_przerwy(client, tenant_a):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    od = utcnow() - datetime.timedelta(minutes=10)
    do = od + datetime.timedelta(seconds=348)   # 12:34:34 - 12:40:22
    odpowiedz = wyslij_raport(client, enrolled, [wpis(
        monitor_id, sond=15, udanych=9,
        przerwy=[{"od": od.isoformat(), "do": do.isoformat(),
                  "blad": "connection refused", "sond": 6}])])
    assert odpowiedz.status_code == 200, odpowiedz.text
    assert odpowiedz.json()["przyjeto"] == 1

    with SessionLocal() as db:
        okno = db.execute(select(OknoMonitorowania)).scalar_one()
        assert (okno.sond, okno.udanych) == (15, 9)
        przerwa = db.execute(select(PrzerwaDostepnosci)).scalar_one()
        assert przerwa.trwanie_sekund == 348
        assert przerwa.blad == "connection refused"
        assert monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)["procent"] == 60.0


def test_otwarta_przerwa_domyka_sie_kolejnym_raportem(client, tenant_a):
    """Poczatek przerwy jest jej tozsamoscia - drugi raport ma ja domknac,
    a nie rozbic jednej awarii na dwie."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    poczatek = utcnow() - datetime.timedelta(minutes=20)
    wyslij_raport(client, enrolled, [wpis(
        monitor_id, sond=15, udanych=0, dostepna=False, potwierdzona=True,
        blad="timeout",
        przerwy=[{"od": poczatek.isoformat(), "do": None, "blad": "timeout", "sond": 15}])])
    with SessionLocal() as db:
        przerwa = db.execute(select(PrzerwaDostepnosci)).scalar_one()
        assert przerwa.koniec is None
        assert przerwa.trwanie_sekund is None   # trwa, a nie "zero sekund"

    koniec = poczatek + datetime.timedelta(minutes=6)
    wyslij_raport(client, enrolled, [wpis(
        monitor_id, sond=15, udanych=9,
        przerwy=[{"od": poczatek.isoformat(), "do": koniec.isoformat(),
                  "blad": "timeout", "sond": 6}])])
    with SessionLocal() as db:
        assert db.scalar(select(func.count(PrzerwaDostepnosci.id))) == 1
        assert db.execute(select(PrzerwaDostepnosci)).scalar_one().trwanie_sekund == 360


def test_powtorzony_raport_nie_dubluje_okna(client, tenant_a):
    """Agent moze wyslac to samo okno ponownie po nieudanej probie wysylki."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    wpis_celu = wpis(monitor_id, sond=15, udanych=15)
    wyslij_raport(client, enrolled, [wpis_celu])
    wyslij_raport(client, enrolled, [wpis_celu])
    with SessionLocal() as db:
        assert db.scalar(select(func.count(OknoMonitorowania.id))) == 1
        assert monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)["sond"] == 15


def test_agent_nie_zglosi_cudzego_celu(client, tenant_a, tenant_b):
    """Identyfikator celu przychodzi z zewnatrz - sam nie moze wystarczyc."""
    moj = zarejestruj_agenta(client, tenant_a, "maszyna-a")
    zarejestruj_agenta(client, tenant_b, "maszyna-b")
    with SessionLocal() as db:
        obce_id = db.execute(select(Asset.id).where(Asset.tenant_id == tenant_b["id"])).scalar_one()
    cudzy = dodaj_cel(tenant_b["id"], obce_id, nazwa="Cudza usluga")

    odpowiedz = wyslij_raport(client, moj, [wpis(cudzy, sond=5, udanych=0, dostepna=False)])
    assert odpowiedz.status_code == 200
    assert odpowiedz.json() == {**odpowiedz.json(), "przyjeto": 0, "pominieto": 1}
    with SessionLocal() as db:
        assert db.scalar(select(func.count(OknoMonitorowania.id))) == 0
        assert db.get(MonitorUslugi, cudzy).stan == "nieznany"


def test_agent_nie_zglosi_celu_przypisanego_innej_maszynie(client, tenant_a):
    """Ta sama firma, ale inny wykonawca - to nadal nie jego cel."""
    pierwszy = zarejestruj_agenta(client, tenant_a, "maszyna-1")
    drugi = zarejestruj_agenta(client, tenant_a, "maszyna-2")
    with SessionLocal() as db:
        drugi_id = db.execute(select(Asset.id).where(
            Asset.machine_id == "maszyna-2")).scalar_one()
    cel_drugiego = dodaj_cel(tenant_a["id"], drugi_id)

    assert wyslij_raport(client, pierwszy, [wpis(cel_drugiego)]).json()["pominieto"] == 1
    assert wyslij_raport(client, drugi, [wpis(cel_drugiego)]).json()["przyjeto"] == 1


@pytest.mark.parametrize("uszkodzony, powod", [
    ({"okno": {"od": "2026-01-02T00:00:00Z", "do": "2026-01-01T00:00:00Z",
               "sond": 5, "udanych": 5}}, "okno konczy sie przed poczatkiem"),
    ({"okno": {"od": "2026-01-01T00:00:00Z", "do": "2026-01-01T01:00:00Z",
               "sond": 5, "udanych": 9}}, "wiecej udanych niz wszystkich"),
    ({"przerwy": [{"od": "2026-01-02T00:00:00Z", "do": "2026-01-01T00:00:00Z"}]},
     "przerwa konczy sie przed poczatkiem"),
])
def test_niespojny_raport_jest_odrzucany(client, tenant_a, uszkodzony, powod):
    """Raport przychodzi z maszyny klienta i jest danymi, a nie prawda o swiecie."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    assert wyslij_raport(client, enrolled,
                         [{"id": monitor_id, **uszkodzony}]).status_code == 422, powod


def test_nieznana_wersja_protokolu_jest_odrzucana(client, tenant_a):
    enrolled = zarejestruj_agenta(client, tenant_a)
    odpowiedz = client.post(
        "/api/v1/agent/monitoring",
        headers={"Authorization": "Bearer " + enrolled["agent_token"]},
        json={"protocol": 99, "wyslano": utcnow().isoformat(), "cele": []})
    assert odpowiedz.status_code == 422


def test_raport_bez_tokenu_maszyny_nie_przechodzi(client, tenant_a):
    assert client.post("/api/v1/agent/monitoring",
                       json={"protocol": 1, "wyslano": utcnow().isoformat(),
                             "cele": []}).status_code == 401


# --- ocena stanu ------------------------------------------------------------

def test_stan_celu_to_gorsza_ze_skladowych(client, tenant_a):
    """Dzialajaca usluga z certyfikatem na wyczerpaniu nie swieci na zielono."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, prog_ostrzezenia_dni=30,
                           prog_alarmu_dni=7)

    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(certyfikat_pem(20)))])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.stan_dostepnosci == "ok"
        assert monitor.stan_certyfikatu == "ostrzezenie"
        assert monitor.stan == "ostrzezenie"


def test_dostepnosc_nie_spada_przez_wygasajacy_certyfikat(client, tenant_a):
    """Do okien idzie stan dostepnosci, nie stan celu."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    wyslij_raport(client, enrolled, [wpis(monitor_id, sond=15, udanych=15,
                                          tls=stan_tls(certyfikat_pem(3)))])
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, monitor_id).stan == "awaria"  # certyfikat
        assert monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)["procent"] == 100.0


def test_brak_sond_to_nie_jest_stuprocentowa_dostepnosc(client, tenant_a):
    """Zero sond z wynikiem "wszystko dziala" byloby po prostu klamstwem."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    with SessionLocal() as db:
        assert monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)["procent"] is None


def test_cisza_agenta_nie_jest_sprawna_usluga(client, tenant_a):
    """Cel bez swiezych raportow dostaje znacznik - to brak wiedzy, nie stan."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    wyslij_raport(client, enrolled, [wpis(monitor_id)])

    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitoring.milczy(monitor) is False
        # Cofamy ostatni raport poza tolerancje.
        prog = get_settings().monitoring_report_seconds * monitoring.TOLERANCJA_RAPORTU
        monitor.ostatni_raport = utcnow() - datetime.timedelta(seconds=prog + 60)
        db.commit()
        assert monitoring.milczy(monitor) is True
        assert monitoring.podsumowanie(db, tenant_a["id"])["milczace"] == 1


# --- certyfikat -------------------------------------------------------------

def test_certyfikat_jedzie_tylko_przy_zmianie_odcisku(client, tenant_a):
    """Certyfikat wazny rok nie ma po co jechac przez siec co dobe."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    pem = certyfikat_pem(200)
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(pem))])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        odcisk, waznosc = monitor.cert_odcisk, monitor.cert_do
        assert monitor.cert_podmiot == "uslugi.firma.pl"

    # Kolejny raport bez PEM - sam odcisk. Zapisana waznosc ma przetrwac.
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(odcisk=odcisk))])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.cert_odcisk == odcisk
        assert monitor.cert_do == waznosc


def test_brak_tls_w_raporcie_nie_kasuje_wiedzy_o_certyfikacie(client, tenant_a):
    """Nieudane polaczenie nie moze oglosic, ze certyfikat naprawil sie sam."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, weryfikuj_lancuch=True)

    pem = certyfikat_pem(200)
    wyslij_raport(client, enrolled, [wpis(
        monitor_id, tls=stan_tls(pem, zaufany=False, blad="self-signed certificate"))])
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, monitor_id).stan_certyfikatu == "awaria"

    # Usluga przestaje odpowiadac - do TLS w ogole nie dochodzi (tls=None).
    wyslij_raport(client, enrolled, [wpis(monitor_id, sond=15, udanych=0,
                                          dostepna=False, potwierdzona=True,
                                          blad="connection refused")])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.cert_zaufany is False        # wiedza sprzed awarii zostaje
        assert monitor.stan_certyfikatu == "awaria"
        assert monitor.stan_dostepnosci == "awaria"


def test_uszkodzony_certyfikat_nie_wywraca_raportu(client, tenant_a):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    # Skladamy wpis wprost, a nie przez stan_tls: ten liczylby odcisk,
    # a tu chodzi wlasnie o tresc, ktorej nie da sie rozebrac.
    odpowiedz = wyslij_raport(client, enrolled, [wpis(monitor_id, tls={
        "odcisk": "f" * 64, "zaufany": None, "blad": None,
        "pem": "-----BEGIN CERTIFICATE-----\nnie certyfikat\n"
               "-----END CERTIFICATE-----\n"})])
    assert odpowiedz.status_code == 200
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.cert_do is None
        assert "nie moge odczytac certyfikatu" in monitor.cert_blad
        assert monitor.stan_dostepnosci == "ok"     # usluga sama dziala


# --- powiadomienia ----------------------------------------------------------

@pytest.fixture
def wyslane(monkeypatch):
    """Przechwytuje wysylke poczty - SMTP w tescie nie jest nam do niczego."""
    wiadomosci = []

    def zapisz(db, tenant_id, adresaci, temat, html, tekst):
        wiadomosci.append({"adresaci": adresaci, "temat": temat, "tekst": tekst})

    from cmdb_server.services import poczta
    monkeypatch.setattr(poczta, "wyslij", zapisz)
    return wiadomosci


def test_powiadomienie_idzie_przy_zmianie_i_tylko_przy_zmianie(client, tenant_a, wyslane):
    """Awaria trwajaca tydzien nie moze zamienic sie w tysiac wiadomosci."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="dyzur@firma.pl", protokol="tcp")

    # Pierwsze udane sprawdzenie nie jest powrotem do dzialania.
    wyslij_raport(client, enrolled, [wpis(monitor_id)])
    assert wyslane == []

    for _ in range(3):
        wyslij_raport(client, enrolled, [wpis(monitor_id, sond=15, udanych=0,
                                              dostepna=False, potwierdzona=True,
                                              blad="connection refused")],
                      powod="zdarzenie")
    assert len(wyslane) == 1
    assert "niedostepna" in wyslane[0]["temat"]
    assert wyslane[0]["adresaci"] == ["dyzur@firma.pl"]
    assert "connection refused" in wyslane[0]["tekst"]

    wyslij_raport(client, enrolled, [wpis(monitor_id)], powod="zdarzenie")
    wyslij_raport(client, enrolled, [wpis(monitor_id)])
    assert len(wyslane) == 2
    assert "dziala" in wyslane[1]["temat"]


def test_jedna_zgubiona_odpowiedz_to_jeszcze_nie_awaria(client, tenant_a, wyslane):
    """Agent zna licznik prob i to on mowi, czy awaria jest potwierdzona."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="dyzur@firma.pl", protokol="tcp")

    wyslij_raport(client, enrolled, [wpis(monitor_id, sond=15, udanych=14,
                                          dostepna=False, potwierdzona=False,
                                          blad="chwilowy blad")])
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, monitor_id).stan_dostepnosci == "ostrzezenie"
    assert wyslane == []


def test_certyfikat_zglasza_sie_na_kazdym_progu_ale_raz(client, tenant_a, wyslane):
    """W "ostrzezeniu" certyfikat stoi tygodniami - sam stan by tego nie zlapal."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="it@firma.pl", prog_ostrzezenia_dni=30,
                           prog_alarmu_dni=7)

    daleki = certyfikat_pem(60)
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(daleki))])
    assert wyslane == []

    blisko = certyfikat_pem(25)
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(blisko))])
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(blisko))])
    assert len(wyslane) == 1 and "wygasa" in wyslane[-1]["temat"]

    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(certyfikat_pem(5)))])
    assert len(wyslane) == 2

    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(certyfikat_pem(-2)))])
    assert len(wyslane) == 3 and "wygasl" in wyslane[-1]["temat"].lower()


def test_odnowiony_certyfikat_zeruje_progi_i_potwierdza_naprawe(client, tenant_a, wyslane):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="it@firma.pl")

    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(certyfikat_pem(3)))])
    assert len(wyslane) == 1

    # Nowy certyfikat = nowy odcisk. Ktos poprosil o odnowienie i chce wiedziec,
    # ze zadzialalo.
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(certyfikat_pem(365)))])
    assert len(wyslane) == 2 and "porzadku" in wyslane[-1]["temat"]
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.stan_certyfikatu == "ok"
        assert monitor.powiadomiony_prog is None


def test_niezaufany_lancuch_zglasza_sie_od_razu(client, tenant_a, wyslane):
    """Lancuch nie do zweryfikowania nie przekracza zadnego progu dni."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="it@firma.pl", weryfikuj_lancuch=True)

    pem = certyfikat_pem(365)
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(
        pem, zaufany=False, blad="self-signed certificate"))])
    assert len(wyslane) == 1 and "niezaufany" in wyslane[0]["temat"]

    odcisk = stan_tls(pem)["odcisk"]
    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(
        odcisk=odcisk, zaufany=False, blad="self-signed certificate"))])
    assert len(wyslane) == 1  # drugie takie samo sprawdzenie juz nie pisze

    wyslij_raport(client, enrolled, [wpis(monitor_id, tls=stan_tls(
        odcisk=odcisk, zaufany=True))])
    assert len(wyslane) == 2 and "porzadku" in wyslane[-1]["temat"]


def test_nieudana_wysylka_nie_gubi_powiadomienia(client, tenant_a, monkeypatch):
    """Powiadomienie, ktore nie doszlo, nie jest powiadomieniem."""
    from cmdb_server.services import poczta

    def odmow(*args, **kwargs):
        raise poczta.BladPoczty("firma nie ma skonfigurowanej poczty")

    monkeypatch.setattr(poczta, "wyslij", odmow)
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, powiadamiaj=True,
                           adresaci="dyzur@firma.pl", protokol="tcp")

    wyslij_raport(client, enrolled, [wpis(monitor_id, sond=5, udanych=0,
                                          dostepna=False, potwierdzona=True,
                                          blad="cisza")])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.blad_powiadomienia
        # Znacznik NIE zostal przesuniety - przy kolejnym raporcie sprobujemy znowu.
        assert monitor.powiadomiona_dostepnosc is None


# --- historia ---------------------------------------------------------------

def test_stara_historia_jest_kasowana(client, tenant_a):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    granica = utcnow() - datetime.timedelta(
        days=get_settings().monitoring_history_days + 2)

    with SessionLocal() as db:
        db.add(OknoMonitorowania(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                                 poczatek=granica, koniec=granica, sond=1, udanych=1))
        db.add(OknoMonitorowania(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                                 poczatek=utcnow(), koniec=utcnow(), sond=1, udanych=1))
        db.add(PrzerwaDostepnosci(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                                  poczatek=granica, koniec=granica, sond=1))
        # Przerwa nadal trwajaca nie moze zniknac tylko dlatego, ze zaczela sie
        # dawno - to wtedy najdluzsza awaria, jaka mamy.
        db.add(PrzerwaDostepnosci(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                                  poczatek=granica - datetime.timedelta(days=1), sond=9))
        db.commit()
        wynik = monitoring.usun_stara_historie(db)
        assert wynik == {"okna": 1, "przerwy": 1}
        assert db.scalar(select(func.count(PrzerwaDostepnosci.id))) == 1


# --- panel ------------------------------------------------------------------

def test_panel_dodaje_cel_z_wykonawca(client, tenant_a, make_user):
    zarejestruj_agenta(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()

    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Portal", "host": "portal.firma.pl",
        "wykonawca_id": asset_id, "port": "443", "protokol": "https", "sciezka": "/",
        "oczekiwany_kod": "0", "interwal_sekund": "60", "interwal_certyfikatu": "86400",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text
    with SessionLocal() as db:
        monitor = db.execute(select(MonitorUslugi)).scalar_one()
        assert monitor.wykonawca_id == asset_id
        # Cel dodany i milczacy do konca pierwszego odstepu nie mowi, czy
        # w ogole zostal wpisany poprawnie - stad zadanie sprawdzenia od razu.
        assert monitor.wymuszone_o is not None
    assert "Portal" in client.get("/monitoring").text


def test_wykonawca_musi_byc_maszyna_z_agentem(client, tenant_a, make_user):
    """Wpis reczny agenta nie ma i niczego nie sprawdzi."""
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        reczny = Asset(tenant_id=tenant_a["id"], machine_id="reczne:drukarka",
                       hostname="Drukarka", typ="drukarka", zrodlo="reczne")
        db.add(reczny)
        db.commit()
        reczny_id = reczny.id

    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Portal", "host": "portal.firma.pl",
        "wykonawca_id": reczny_id, "port": "443", "protokol": "https", "sciezka": "/",
        "oczekiwany_kod": "0", "interwal_sekund": "60", "interwal_certyfikatu": "86400",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    assert "agentem" in odpowiedz.json()["detail"]


def test_wykonawca_z_cudzej_firmy_jest_odrzucany(client, tenant_a, tenant_b, make_user):
    zarejestruj_agenta(client, tenant_b, "maszyna-b")
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        obcy_id = db.execute(select(Asset.id).where(
            Asset.tenant_id == tenant_b["id"])).scalar_one()

    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Portal", "host": "portal.firma.pl",
        "wykonawca_id": obcy_id, "port": "443", "protokol": "https", "sciezka": "/",
        "oczekiwany_kod": "0", "interwal_sekund": "60", "interwal_certyfikatu": "86400",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 404


def test_zlecenie_sprawdzenia_jest_zleceniem_a_nie_odpowiedzia(client, tenant_a, make_user):
    """Panel nie sonduje - udawanie natychmiastowego wyniku byloby klamstwem."""
    zarejestruj_agenta(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)

    odpowiedz = client.post(f"/monitoring/{monitor_id}/sprawdz",
                            data={"csrf_token": csrf}, follow_redirects=True)
    assert odpowiedz.status_code == 200
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.wymuszone_o is not None
        assert monitor.ostatnie_sprawdzenie is None   # nikt jeszcze nie sondowal


def test_zmiana_wykonawcy_zeruje_stan_ale_zostawia_przerwy(client, tenant_a, make_user):
    pierwszy = zarejestruj_agenta(client, tenant_a, "maszyna-1")
    zarejestruj_agenta(client, tenant_a, "maszyna-2")
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        pierwszy_id = db.execute(select(Asset.id).where(
            Asset.machine_id == "maszyna-1")).scalar_one()
        drugi_id = db.execute(select(Asset.id).where(
            Asset.machine_id == "maszyna-2")).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], pierwszy_id, nazwa="Usluga", protokol="tcp")

    poczatek = utcnow() - datetime.timedelta(hours=1)
    wyslij_raport(client, pierwszy, [wpis(
        monitor_id, sond=15, udanych=0, dostepna=False, potwierdzona=True, blad="cisza",
        przerwy=[{"od": poczatek.isoformat(),
                  "do": (poczatek + datetime.timedelta(minutes=5)).isoformat(),
                  "blad": "cisza", "sond": 5}])])

    odpowiedz = client.post(f"/monitoring/{monitor_id}", data={
        "csrf_token": csrf, "nazwa": "Usluga", "host": "uslugi.firma.pl",
        "wykonawca_id": drugi_id, "port": "443", "protokol": "tcp", "sciezka": "/",
        "oczekiwany_kod": "0", "interwal_sekund": "60", "interwal_certyfikatu": "86400",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7", "aktywny": "true",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.wykonawca_id == drugi_id
        # Inny wykonawca to inny pomiar - alarm o awarii sprzed przepiecia
        # bylby fałszywy.
        assert monitor.stan == "nieznany"
        assert monitor.powiadomiona_dostepnosc is None
        # ...ale przerwa opisuje to, co bylo naprawde, i zostaje.
        assert db.scalar(select(func.count(PrzerwaDostepnosci.id))) == 1


def test_konto_tylko_do_odczytu_nie_zmienia_niczego(client, tenant_a, make_user):
    zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    _, csrf = zaloguj(client, tenant_a, make_user, role="viewer",
                      email="czytelnik@example.com")
    assert client.get("/monitoring").status_code == 200
    for adres in (f"/monitoring/{monitor_id}/sprawdz", f"/monitoring/{monitor_id}/usun",
                  f"/monitoring/{monitor_id}/przelacz"):
        assert client.post(adres, data={"csrf_token": csrf},
                           follow_redirects=False).status_code == 403


def test_cel_innej_firmy_jest_niewidoczny(client, tenant_a, tenant_b, make_user):
    zarejestruj_agenta(client, tenant_b, "maszyna-b")
    with SessionLocal() as db:
        obcy_id = db.execute(select(Asset.id).where(
            Asset.tenant_id == tenant_b["id"])).scalar_one()
    obcy = dodaj_cel(tenant_b["id"], obcy_id, nazwa="Cudza usluga")
    _, csrf = zaloguj(client, tenant_a, make_user)

    assert client.get(f"/monitoring/{obcy}").status_code == 404
    assert client.post(f"/monitoring/{obcy}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 404
    assert "Cudza usluga" not in client.get("/monitoring").text
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, obcy) is not None


def test_usuniecie_celu_zabiera_jego_historie(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    wyslij_raport(client, enrolled, [wpis(monitor_id)])
    _, csrf = zaloguj(client, tenant_a, make_user)

    assert client.post(f"/monitoring/{monitor_id}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MonitorUslugi.id))) == 0
        assert db.scalar(select(func.count(OknoMonitorowania.id))) == 0


def test_strona_celu_pokazuje_przerwy(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    poczatek = utcnow() - datetime.timedelta(minutes=30)
    wyslij_raport(client, enrolled, [wpis(
        monitor_id, sond=15, udanych=9,
        przerwy=[{"od": poczatek.isoformat(),
                  "do": (poczatek + datetime.timedelta(seconds=348)).isoformat(),
                  "blad": "connection refused", "sond": 6}])])
    zaloguj(client, tenant_a, make_user)

    strona = client.get(f"/monitoring/{monitor_id}")
    assert strona.status_code == 200
    assert "connection refused" in strona.text
    assert "Przerwy w działaniu" in strona.text
    assert "5 min 48 s" in strona.text


# --- walidacja ustawien -----------------------------------------------------

def podstawa(**nadpisania) -> dict:
    dane = {"nazwa": "Cel", "host": "uslugi.firma.pl", "port": 443, "protokol": "https",
            "sciezka": "/", "oczekiwany_kod": 0, "interwal_sekund": 60,
            "interwal_certyfikatu": 86400, "limit_sekund": 10, "liczba_prob": 2,
            "prog_ostrzezenia_dni": 30, "prog_alarmu_dni": 7, "aktywny": True}
    dane.update(nadpisania)
    return dane


@pytest.mark.parametrize("nadpisania, fragment", [
    ({"host": "https://uslugi.firma.pl/health"}, "bez schematu"),
    ({"host": "169.254.169.254"}, "zabroniony"),
    ({"port": 0}, "1-65535"),
    ({"protokol": "gopher"}, "nieznany sposob"),
    ({"sciezka": "/health\r\nX-Ustawione: 1"}, "nowej linii"),
    ({"interwal_sekund": 5}, "odstep sprawdzen"),
    ({"interwal_certyfikatu": 60}, "odstep sprawdzania certyfikatu"),
    ({"limit_sekund": 60, "interwal_sekund": 60}, "krotszy niz odstep"),
    ({"prog_alarmu_dni": 60}, "mniejszy niz prog ostrzezenia"),
    ({"powiadamiaj": True, "adresaci": "to nie jest adres"}, "adresu e-mail"),
    ({"nazwa": "   "}, "podaj nazwe"),
])
def test_bledne_ustawienia_sa_odrzucane(nadpisania, fragment):
    with pytest.raises(ValueError) as blad:
        monitoring.sprawdz_ustawienia(podstawa(**nadpisania))
    assert fragment in str(blad.value)


@pytest.mark.parametrize("adres", [
    "169.254.169.254",              # usluga metadanych chmury
    "::ffff:169.254.169.254",       # ten sam adres zapisany jako IPv6
    "0.0.0.0",
    "224.0.0.1",
])
def test_zabronione_adresy_sa_odrzucane(adres):
    assert monitoring.powod_odmowy(adres) is not None


@pytest.mark.parametrize("adres", ["8.8.8.8", "10.0.0.5", "192.168.1.10", "2001:db8::1"])
def test_zwykle_adresy_sa_dozwolone(adres):
    assert monitoring.powod_odmowy(adres) is None


def test_sciezka_i_nazwa_tls_znikaja_przy_protokole_bez_nich():
    """Zapisana sciezka HTTP przy celu sprawdzanym po TCP wygladalaby jak
    obietnica sprawdzania, ktorego nie ma."""
    ustawienia = monitoring.sprawdz_ustawienia(
        podstawa(protokol="tcp", sciezka="/health", nazwa_tls="inna.firma.pl"))
    assert ustawienia["sciezka"] == "/"
    assert ustawienia["nazwa_tls"] is None


# --- raport -----------------------------------------------------------------

def test_raport_uslug_widzi_awarie_certyfikaty_i_cisze(client, tenant_a, make_user):
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    niedostepny = dodaj_cel(tenant_a["id"], asset_id, nazwa="Sklep", protokol="tcp")
    wygasajacy = dodaj_cel(tenant_a["id"], asset_id, nazwa="Poczta")

    wyslij_raport(client, enrolled, [
        wpis(niedostepny, sond=15, udanych=0, dostepna=False, potwierdzona=True,
             blad="connection refused"),
        wpis(wygasajacy, tls=stan_tls(certyfikat_pem(12))),
    ])
    zaloguj(client, tenant_a, make_user)

    pocztowy = client.get("/raporty/podglad/uslugi")
    assert pocztowy.status_code == 200
    assert "Sklep" in pocztowy.text and "connection refused" in pocztowy.text
    assert "Poczta" in pocztowy.text and "za 12 dni" in pocztowy.text

    panel = client.get("/raporty/widok/uslugi")
    assert panel.status_code == 200
    assert "Sklep" in panel.text and "Poczta" in panel.text


def test_raport_uslug_dziala_bez_zadnego_celu(client, tenant_a, make_user):
    zaloguj(client, tenant_a, make_user)
    odpowiedz = client.get("/raporty/podglad/uslugi")
    assert odpowiedz.status_code == 200
    assert "nie monitoruje jeszcze" in odpowiedz.text


def test_raport_uslug_nie_widzi_celow_innej_firmy(client, tenant_a, tenant_b, make_user):
    zarejestruj_agenta(client, tenant_b, "maszyna-b")
    with SessionLocal() as db:
        obcy_id = db.execute(select(Asset.id).where(
            Asset.tenant_id == tenant_b["id"])).scalar_one()
    dodaj_cel(tenant_b["id"], obcy_id, nazwa="Cudza usluga")
    zaloguj(client, tenant_a, make_user)
    assert "Cudza usluga" not in client.get("/raporty/podglad/uslugi").text


# --- agent jako jedyny wykonawca --------------------------------------------

def test_serwer_nie_ma_czym_sondowac():
    """Sonduje agent i tylko agent - w module serwera nie ma gniazda.

    To nie jest test kosmetyczny: sonda po stronie serwera znaczylaby jedno
    miejsce z wgladem w sieci wszystkich firm, a dwie implementacje sondy
    (serwer + agent stdlib-only) rozjezdzalyby sie po cichu.
    """
    import inspect

    zrodlo = inspect.getsource(monitoring)
    for zakazane in ("import socket", "import ssl", "socket.socket",
                     "wrap_socket", "getaddrinfo", "create_connection"):
        assert zakazane not in zrodlo, f"serwer nie moze sondowac: {zakazane}"
    # ...i nie ma funkcji, ktora by o to prosila.
    for nieistniejaca in ("sprawdz_teraz", "przebieg", "petla", "sonduj"):
        assert not hasattr(monitoring, nieistniejaca), nieistniejaca


def test_cel_bez_wykonawcy_jest_widocznym_problemem(client, tenant_a, make_user):
    """Cel bez czynnego agenta nie jest sprawdzany WCALE.

    Musi to byc widac od razu, a nie dopiero po dwoch pominietych raportach:
    "brak raportow" mowi, ze cos sie zepsulo, a tu nic sie nie psulo -
    po prostu nikomu tego nie zlecono.
    """
    zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, nazwa="Osierocony")
    zaloguj(client, tenant_a, make_user)

    with SessionLocal() as db:
        assert monitoring.bez_wykonawcy(db.get(MonitorUslugi, monitor_id)) is None
        # Maszyna zostaje wycofana - agent przestaje chodzic.
        db.get(Asset, asset_id).lifecycle = "wycofany"
        db.commit()
        monitor = db.get(MonitorUslugi, monitor_id)
        assert "wycofana" in monitoring.bez_wykonawcy(monitor)
        assert monitoring.podsumowanie(db, tenant_a["id"])["bez_wykonawcy"] == 1

    assert "nikt nie sprawdza" in client.get("/monitoring").text
    assert "Tego celu nikt nie sprawdza" in client.get(f"/monitoring/{monitor_id}").text
    assert "Nikt ich nie sprawdza" in client.get("/raporty/podglad/uslugi").text


def test_usuniecie_maszyny_nie_kasuje_celu_ani_historii(client, tenant_a):
    """SET NULL, a nie CASCADE: znikniecie maszyny nie moze po cichu zabrac
    konfiguracji celu razem z historia jego awarii."""
    enrolled = zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id)
    poczatek = utcnow() - datetime.timedelta(hours=1)
    wyslij_raport(client, enrolled, [wpis(
        monitor_id, sond=15, udanych=9,
        przerwy=[{"od": poczatek.isoformat(),
                  "do": (poczatek + datetime.timedelta(minutes=5)).isoformat(),
                  "blad": "cisza", "sond": 5}])])

    with SessionLocal() as db:
        db.delete(db.get(Asset, asset_id))
        db.commit()
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor is not None                    # cel zyje
        assert monitor.wykonawca_id is None           # ...ale bez wykonawcy
        assert monitoring.bez_wykonawcy(monitor)      # i widac dlaczego
        assert db.scalar(select(func.count(PrzerwaDostepnosci.id))) == 1


def test_wycofana_maszyna_nie_moze_zostac_wykonawca(client, tenant_a, make_user):
    """Lista rozwijana jej nie pokaze, ale zadanie da sie zlozyc wprost."""
    zarejestruj_agenta(client, tenant_a)
    _, csrf = zaloguj(client, tenant_a, make_user)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
        db.get(Asset, asset_id).lifecycle = "wycofany"
        db.commit()

    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Portal", "host": "portal.firma.pl",
        "wykonawca_id": asset_id, "port": "443", "protokol": "https", "sciezka": "/",
        "oczekiwany_kod": "0", "interwal_sekund": "60", "interwal_certyfikatu": "86400",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    assert "aktywna maszyna z agentem" in odpowiedz.json()["detail"]


def test_wylaczony_cel_nie_liczy_sie_jako_osierocony(client, tenant_a):
    """Cel wylaczony swiadomie to inna sprawa niz cel bez wykonawcy."""
    zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id)).scalar_one()
    monitor_id = dodaj_cel(tenant_a["id"], asset_id, aktywny=False)
    with SessionLocal() as db:
        db.delete(db.get(Asset, asset_id))
        db.commit()
        assert monitoring.bez_wykonawcy(db.get(MonitorUslugi, monitor_id)) is None
        assert monitoring.podsumowanie(db, tenant_a["id"])["bez_wykonawcy"] == 0


# --- "nie wiem" to nie to samo co "nie ma czego oceniac" --------------------

from datetime import timedelta
from types import SimpleNamespace

from cmdb_server.models import STAN_AWARIA, STAN_NIEZNANY, STAN_OK


def test_sprawny_cel_tcp_nie_swieci_nieznany():
    """Cel bez TLS nie ma certyfikatu i miec go nie bedzie.

    Gdy brak oceny certyfikatu znaczyl to samo co "nie wiem", dzialajacy cel
    TCP pokazywal w panelu stan "nieznany" obok dostepnosci "dziala" - czego
    nie dalo sie wytlumaczyc nikomu, kto na to patrzyl.
    """
    from cmdb_server.models import STAN_NIE_DOTYCZY
    from cmdb_server.services.monitoring import stan_celu, stan_certyfikatu

    cel = SimpleNamespace(protokol="tcp", cert_do=None, cert_zaufany=None,
                          weryfikuj_lancuch=True, prog_ostrzezenia_dni=30,
                          prog_alarmu_dni=7)
    certyfikat = stan_certyfikatu(cel)
    assert certyfikat == STAN_NIE_DOTYCZY
    assert stan_celu(STAN_OK, certyfikat) == STAN_OK
    # Brak sondy nadal musi byc widoczny - to JEST luka w wiedzy.
    assert stan_celu(STAN_NIEZNANY, certyfikat) == STAN_NIEZNANY


def test_certyfikat_nadal_psuje_stan_celu_tam_gdzie_istnieje():
    """Poprawka nie moze uciszyc certyfikatu na celach, ktore go maja."""
    from cmdb_server.services.monitoring import stan_celu, stan_certyfikatu

    blisko = SimpleNamespace(
        protokol="https", cert_do=utcnow() + timedelta(days=5, hours=12),
        cert_zaufany=True, weryfikuj_lancuch=True,
        prog_ostrzezenia_dni=30, prog_alarmu_dni=7)
    assert stan_celu(STAN_OK, stan_certyfikatu(blisko)) == STAN_AWARIA


def test_cel_bez_tls_nie_generuje_powiadomien_o_certyfikacie(client, tenant_a):
    """Bez tego cel TCP zaczalby slac wiadomosci o certyfikacie, ktorego nie ma."""
    from cmdb_server.models import STAN_NIE_DOTYCZY
    from cmdb_server.services.monitoring import powiadom

    zarejestruj_agenta(client, tenant_a)
    with SessionLocal() as db:
        asset_id = db.execute(select(Asset.id).where(Asset.tenant_id == tenant_a["id"])).scalar_one()
        cel = dodaj_cel(tenant_a["id"], asset_id, nazwa="Router",
                        protokol="tcp", port=443, powiadamiaj=True)
        monitor = db.get(MonitorUslugi, cel)
        monitor.stan_certyfikatu = STAN_NIE_DOTYCZY
        monitor.stan_dostepnosci = STAN_OK
        monitor.stan = STAN_OK
        db.commit()
        assert powiadom(db, monitor) == []
