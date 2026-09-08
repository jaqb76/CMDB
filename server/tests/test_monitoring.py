"""Monitorowanie ciaglosci uslug i waznosci certyfikatow SSL.

Testy chodza po prawdziwym gniezdzie i prawdziwym TLS, a nie po zaslepce:
caly sens tego modulu polega na tym, co widac z drugiej strony polaczenia,
wiec zaslepiony ``socket`` sprawdzalby wylacznie zaslepke. Serwer testowy
stoi na petli zwrotnej, dlatego przypadki, ktore go uzywaja, jawnie znosza
blokade adresow petli zwrotnej - a osobny test pilnuje, ze bez tego zniesienia
blokada dziala.
"""
from __future__ import annotations

import datetime
import socket
import ssl
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import func, select

from cmdb_server.config import get_settings
from cmdb_server.db import SessionLocal
from cmdb_server.models import MonitorUslugi, PomiarMonitora, utcnow
from cmdb_server.security import issue_csrf_token
from cmdb_server.services import monitoring

PASSWORD = "bardzo-dlugie-haslo"


# --- zaplecze: certyfikat i serwer TLS --------------------------------------

def wystaw_certyfikat(dni_waznosci: int, nazwa: str = "cmdb-test.local"):
    """Certyfikat self-signed o zadanej pozostalej waznosci."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    klucz = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    podmiot = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nazwa)])
    teraz = datetime.datetime.now(datetime.timezone.utc)
    certyfikat = (
        x509.CertificateBuilder()
        .subject_name(podmiot).issuer_name(podmiot)
        .public_key(klucz.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(teraz - datetime.timedelta(days=30))
        .not_valid_after(teraz + datetime.timedelta(days=dni_waznosci))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(nazwa)]), critical=False)
        .sign(klucz, hashes.SHA256())
    )
    return (
        certyfikat.public_bytes(serialization.Encoding.PEM),
        klucz.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()),
    )


@contextmanager
def serwer_tls(tmp_path, dni_waznosci=90, odpowiedz=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
               nazwa="cmdb-test.local"):
    """Serwer TLS na losowym porcie petli zwrotnej. Zwraca (port, przelacznik)."""
    pem, klucz = wystaw_certyfikat(dni_waznosci, nazwa)
    plik_cert = tmp_path / f"{nazwa}-{dni_waznosci}.pem"
    plik_klucz = tmp_path / f"{nazwa}-{dni_waznosci}.key"
    plik_cert.write_bytes(pem)
    plik_klucz.write_bytes(klucz)

    kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontekst.load_cert_chain(plik_cert, plik_klucz)
    gniazdo = socket.socket()
    gniazdo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(8)
    dziala = threading.Event()
    dziala.set()

    def obsluga():
        while True:
            try:
                klient, _ = gniazdo.accept()
            except OSError:
                return
            if not dziala.is_set():
                klient.close()
                continue
            try:
                with kontekst.wrap_socket(klient, server_side=True) as tunel:
                    tunel.recv(4096)
                    tunel.sendall(odpowiedz)
            except OSError:
                pass

    watek = threading.Thread(target=obsluga, daemon=True)
    watek.start()
    try:
        yield gniazdo.getsockname()[1], dziala
    finally:
        gniazdo.close()
        watek.join(timeout=2)


@contextmanager
def serwer_tcp():
    """Nasluchujacy port bez TLS - do sprawdzen typu 'tcp'."""
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(8)
    try:
        yield gniazdo.getsockname()[1]
    finally:
        gniazdo.close()


def wolny_port() -> int:
    """Port, na ktorym na pewno nikt nie nasluchuje."""
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    port = gniazdo.getsockname()[1]
    gniazdo.close()
    return port


@pytest.fixture
def petla_zwrotna():
    """Znosi blokade adresow petli zwrotnej na czas testu.

    Serwer testowy nie ma innego adresu, a blokada jest domyslna i pilnowana
    osobnym testem - tu chodzi o zachowanie sond, nie o sama blokade.
    """
    settings = get_settings()
    poprzednio = settings.monitoring_allow_loopback
    settings.monitoring_allow_loopback = True
    yield
    settings.monitoring_allow_loopback = poprzednio


def cel(port, **nadpisania) -> monitoring.Cel:
    dane = dict(id="cel-1", tenant_id="firma", nazwa="test", host="127.0.0.1", port=port,
                protokol="https", sciezka="/", oczekiwany_kod=0, nazwa_tls="cmdb-test.local",
                weryfikuj_lancuch=False, limit_sekund=5)
    dane.update(nadpisania)
    return monitoring.Cel(**dane)


def dodaj_cel(tenant_id, **nadpisania) -> str:
    dane = dict(nazwa="Usluga", host="127.0.0.1", port=443, protokol="https",
                interwal_sekund=60, limit_sekund=5, liczba_prob=2,
                prog_ostrzezenia_dni=30, prog_alarmu_dni=7, weryfikuj_lancuch=False,
                powiadamiaj=False, aktywny=True)
    dane.update(nadpisania)
    with SessionLocal() as db:
        monitor = MonitorUslugi(tenant_id=tenant_id, **dane)
        db.add(monitor)
        db.commit()
        return monitor.id


def login(client, tenant, make_user, role="admin", email="monitor@example.com"):
    uid = make_user(tenant["id"], email, PASSWORD, role)
    assert client.post("/login", data={"email": email, "password": PASSWORD},
                       follow_redirects=False).status_code == 303
    return uid, issue_csrf_token(uid)


# --- sondy ------------------------------------------------------------------

def test_tcp_rozroznia_port_otwarty_od_zamknietego(petla_zwrotna):
    with serwer_tcp() as port:
        wynik = monitoring.sprawdz(cel(port, protokol="tcp"))
    assert wynik.dostepna is True
    assert wynik.certyfikat is None  # bez TLS nie ma czego zdejmowac
    assert wynik.adres == "127.0.0.1"

    zamkniety = monitoring.sprawdz(cel(wolny_port(), protokol="tcp"))
    assert zamkniety.dostepna is False
    assert zamkniety.blad


def test_tls_zdejmuje_certyfikat_takze_bez_zaufanego_lancucha(tmp_path, petla_zwrotna):
    """Certyfikat wlasnego urzedu firmy to najczestszy przypadek wewnetrzny.

    Gdyby dane certyfikatu brac ze slownika ``getpeercert()``, byloby tu
    pusto - a to wlasnie ten certyfikat trzeba pilnowac najbardziej, bo nikt
    go za nas nie odnowi.
    """
    with serwer_tls(tmp_path, dni_waznosci=42) as (port, _):
        wynik = monitoring.sprawdz(cel(port, protokol="tls", weryfikuj_lancuch=False))
    assert wynik.dostepna is True
    assert wynik.certyfikat is not None
    assert wynik.certyfikat.podmiot == "cmdb-test.local"
    assert wynik.certyfikat.nazwy == ["cmdb-test.local"]
    assert len(wynik.certyfikat.odcisk) == 64
    pozostalo = (wynik.certyfikat.wazny_do - utcnow()).days
    assert 40 <= pozostalo <= 42
    # Bez weryfikacji nie mowimy "zaufany" ani "niezaufany" - nie sprawdzalismy.
    assert wynik.cert_zaufany is None


def test_niezaufany_lancuch_nie_przykrywa_dostepnosci(tmp_path, petla_zwrotna):
    """Uslugа odpowiada, ale jej certyfikatu nie da sie zweryfikowac.

    To dwie osobne informacje i obie musza przetrwac: usluga dziala,
    a certyfikat jest do naprawy.
    """
    with serwer_tls(tmp_path, dni_waznosci=90) as (port, _):
        wynik = monitoring.sprawdz(cel(port, weryfikuj_lancuch=True))
    assert wynik.dostepna is True
    assert wynik.kod == 200
    assert wynik.cert_zaufany is False
    assert wynik.cert_blad
    assert wynik.certyfikat is not None  # mimo odmowy weryfikacji
    assert wynik.tls_zestawione is True  # uscisk dloni doszedl do skutku


def test_http_sprawdza_kod_odpowiedzi(tmp_path, petla_zwrotna):
    blad = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
    with serwer_tls(tmp_path, odpowiedz=blad) as (port, _):
        domyslny = monitoring.sprawdz(cel(port))
        oczekiwany = monitoring.sprawdz(cel(port, oczekiwany_kod=503))
    # Otwarty port to nie to samo co dzialajaca aplikacja.
    assert domyslny.dostepna is False and domyslny.kod == 503
    # ...chyba ze wlasnie tego kodu oczekujemy - np. od strony logowania.
    assert oczekiwany.dostepna is True and oczekiwany.kod == 503


def test_limit_czasu_konczy_sprawdzenie(petla_zwrotna):
    """Port, ktory przyjmuje polaczenie i milczy, nie moze zawiesic obiegu."""
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(1)
    try:
        wynik = monitoring.sprawdz(cel(gniazdo.getsockname()[1], protokol="tls", limit_sekund=1))
    finally:
        gniazdo.close()
    assert wynik.dostepna is False
    assert wynik.czas_ms < 5000


def test_nieistniejaca_nazwa_nie_wywraca_sprawdzenia():
    wynik = monitoring.sprawdz(cel(443, host="nie-ma-takiej-nazwy-cmdb.invalid"))
    assert wynik.dostepna is False
    assert "nazw" in wynik.blad


# --- blokada adresow --------------------------------------------------------

@pytest.mark.parametrize("adres", [
    "169.254.169.254",              # usluga metadanych chmury
    "::ffff:169.254.169.254",       # ten sam adres zapisany jako IPv6
    "127.0.0.1",
    "0.0.0.0",
    "224.0.0.1",
])
def test_zabronione_adresy_sa_odrzucane(adres):
    assert monitoring.powod_odmowy(adres) is not None


@pytest.mark.parametrize("adres", ["8.8.8.8", "10.0.0.5", "192.168.1.10", "2001:db8::1"])
def test_zwykle_adresy_sa_dozwolone(adres):
    assert monitoring.powod_odmowy(adres) is None


def test_sonda_nie_idzie_pod_adres_metadanych():
    """Sama sonda odmawia, niezaleznie od tego, co przepuscil formularz."""
    wynik = monitoring.sprawdz(cel(80, host="169.254.169.254", protokol="tcp"))
    assert wynik.dostepna is False
    assert "link-local" in wynik.blad


def test_formularz_odrzuca_zabroniony_adres(client, tenant_a, make_user):
    _, csrf = login(client, tenant_a, make_user)
    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Metadane", "host": "169.254.169.254",
        "protokol": "tcp", "port": "80", "interwal_sekund": "300", "limit_sekund": "10",
        "liczba_prob": "2", "prog_ostrzezenia_dni": "30", "prog_alarmu_dni": "7",
        "sciezka": "/", "oczekiwany_kod": "0",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MonitorUslugi.id))) == 0


# --- walidacja ustawien -----------------------------------------------------

def podstawa(**nadpisania) -> dict:
    dane = {"nazwa": "Cel", "host": "uslugi.firma.pl", "port": 443, "protokol": "https",
            "sciezka": "/", "oczekiwany_kod": 0, "interwal_sekund": 300, "limit_sekund": 10,
            "liczba_prob": 2, "prog_ostrzezenia_dni": 30, "prog_alarmu_dni": 7,
            "aktywny": True}
    dane.update(nadpisania)
    return dane


@pytest.mark.parametrize("nadpisania, fragment", [
    ({"host": "https://uslugi.firma.pl/health"}, "bez schematu"),
    ({"port": 0}, "1-65535"),
    ({"protokol": "gopher"}, "nieznany sposob"),
    ({"sciezka": "/health\r\nX-Ustawione: 1"}, "nowej linii"),
    ({"interwal_sekund": 5}, "odstep sprawdzen"),
    ({"limit_sekund": 600}, "limit czasu"),
    ({"limit_sekund": 60, "interwal_sekund": 60}, "krotszy niz odstep"),
    ({"prog_alarmu_dni": 60}, "mniejszy niz prog ostrzezenia"),
    ({"powiadamiaj": True, "adresaci": "to nie jest adres"}, "adresu e-mail"),
    ({"nazwa": "   "}, "podaj nazwe"),
])
def test_bledne_ustawienia_sa_odrzucane(nadpisania, fragment):
    with pytest.raises(ValueError) as blad:
        monitoring.sprawdz_ustawienia(podstawa(**nadpisania))
    assert fragment in str(blad.value)


def test_sciezka_i_nazwa_tls_znikaja_przy_protokole_bez_nich():
    """Ustawienie niewidoczne w formularzu nie moze zostac w bazie.

    Zapisana sciezka HTTP przy celu sprawdzanym po TCP wygladalaby jak
    obietnica sprawdzania, ktorego nie ma.
    """
    ustawienia = monitoring.sprawdz_ustawienia(
        podstawa(protokol="tcp", sciezka="/health", nazwa_tls="inna.firma.pl")
    )
    assert ustawienia["sciezka"] == "/"
    assert ustawienia["nazwa_tls"] is None


# --- stan i historia --------------------------------------------------------

def test_pojedynczy_blad_to_jeszcze_nie_awaria(tenant_a):
    """Jedna zgubiona odpowiedz zdarza sie w kazdej sieci."""
    monitor_id = dodaj_cel(tenant_a["id"], liczba_prob=2, protokol="tcp", port=wolny_port())
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=False, blad="odrzucone"))
        assert monitor.stan_dostepnosci == "ostrzezenie"
        assert monitor.kolejne_bledy == 1

        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=False, blad="odrzucone"))
        assert monitor.stan_dostepnosci == "awaria"
        assert monitor.stan == "awaria"

        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, czas_ms=12))
        assert monitor.stan_dostepnosci == "ok"
        assert monitor.kolejne_bledy == 0


def test_stan_celu_to_gorsza_ze_skladowych(tenant_a):
    """Dzialajaca usluga z certyfikatem na wyczerpaniu nie swieci na zielono."""
    monitor_id = dodaj_cel(tenant_a["id"], prog_ostrzezenia_dni=30, prog_alarmu_dni=7)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        certyfikat = monitoring.Certyfikat(
            podmiot="uslugi.firma.pl", wystawca="Wewnetrzne CA",
            wazny_od=utcnow() - datetime.timedelta(days=300),
            wazny_do=utcnow() + datetime.timedelta(days=20),
            odcisk="a" * 64, nazwy=["uslugi.firma.pl"])
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=15, certyfikat=certyfikat, cert_zaufany=None))
        assert monitor.stan_dostepnosci == "ok"
        assert monitor.stan_certyfikatu == "ostrzezenie"
        assert monitor.stan == "ostrzezenie"


def test_dostepnosc_liczy_sie_z_historii_a_nie_ze_stanu_teraz(tenant_a):
    monitor_id = dodaj_cel(tenant_a["id"])
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        for udana in (True, True, False, True):
            monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
                dostepna=udana, czas_ms=10, blad=None if udana else "brak odpowiedzi"))
        okno = monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)
    assert okno["pomiary"] == 4
    assert okno["udane"] == 3
    assert okno["procent"] == 75.0


def test_brak_pomiarow_to_nie_jest_stuprocentowa_dostepnosc(tenant_a):
    """Zero sprawdzen z wynikiem "wszystko dziala" byloby po prostu klamstwem."""
    monitor_id = dodaj_cel(tenant_a["id"])
    with SessionLocal() as db:
        okno = monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)
    assert okno["pomiary"] == 0
    assert okno["procent"] is None


def test_dostepnosc_nie_spada_przez_wygasajacy_certyfikat(tenant_a):
    """Do historii idzie stan dostepnosci, nie stan celu."""
    monitor_id = dodaj_cel(tenant_a["id"], prog_ostrzezenia_dni=30)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=10,
            certyfikat=monitoring.Certyfikat(
                podmiot="x", wystawca="y", wazny_od=utcnow(),
                wazny_do=utcnow() + datetime.timedelta(days=3), odcisk="b" * 64)))
        assert monitor.stan == "awaria"  # certyfikat ponizej progu alarmu
        assert monitoring.dostepnosc(db, monitor_id, tenant_a["id"], 24)["procent"] == 100.0


def test_stare_pomiary_sa_kasowane(tenant_a):
    monitor_id = dodaj_cel(tenant_a["id"])
    granica = utcnow() - datetime.timedelta(days=get_settings().monitoring_history_days + 2)
    with SessionLocal() as db:
        db.add(PomiarMonitora(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                              sprawdzono=granica, stan="ok", czas_odpowiedzi_ms=10))
        db.add(PomiarMonitora(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                              sprawdzono=utcnow(), stan="ok", czas_odpowiedzi_ms=10))
        db.commit()
        assert monitoring.usun_stare_pomiary(db) == 1
        assert db.scalar(select(func.count(PomiarMonitora.id))) == 1


# --- powiadomienia ----------------------------------------------------------

@pytest.fixture
def wyslane(monkeypatch):
    """Przechwytuje wysylke poczty - SMTP w tescie nie jest nam do niczego."""
    wiadomosci = []

    def zapisz(db, tenant_id, adresaci, temat, html, tekst):
        wiadomosci.append({"tenant_id": tenant_id, "adresaci": adresaci,
                           "temat": temat, "html": html, "tekst": tekst})

    from cmdb_server.services import poczta
    monkeypatch.setattr(poczta, "wyslij", zapisz)
    return wiadomosci


def test_powiadomienie_idzie_przy_zmianie_stanu_i_tylko_przy_zmianie(tenant_a, wyslane):
    """Awaria trwajaca tydzien nie moze zamienic sie w tysiac wiadomosci."""
    monitor_id = dodaj_cel(tenant_a["id"], liczba_prob=1, protokol="tcp",
                           powiadamiaj=True, adresaci="dyzur@firma.pl")
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        for _ in range(3):
            monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
                dostepna=False, blad="connection refused"))
        assert len(wyslane) == 1
        assert "niedostepna" in wyslane[0]["temat"]
        assert wyslane[0]["adresaci"] == ["dyzur@firma.pl"]
        assert "connection refused" in wyslane[0]["tekst"]

        # Powrot uslugi jest zdarzeniem tak samo jak awaria.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, czas_ms=8))
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, czas_ms=8))
        assert len(wyslane) == 2
        assert "dziala" in wyslane[1]["temat"]


def test_pierwsze_udane_sprawdzenie_nie_jest_powrotem_do_dzialania(tenant_a, wyslane):
    monitor_id = dodaj_cel(tenant_a["id"], protokol="tcp", powiadamiaj=True,
                           adresaci="dyzur@firma.pl")
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, czas_ms=5))
    assert wyslane == []


def test_wylaczone_powiadomienia_milcza(tenant_a, wyslane):
    monitor_id = dodaj_cel(tenant_a["id"], liczba_prob=1, protokol="tcp", powiadamiaj=False)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=False, blad="cisza"))
    assert wyslane == []


def _certyfikat(dni, odcisk="c" * 64):
    """Certyfikat o dokladnie ``dni`` pelnych dniach waznosci.

    Pol doby zapasu, bo waznosc liczymy w dol do pelnych dni: bez tego
    "dokladnie 12 dni" zamienialoby sie w 11 po kilku mikrosekundach
    wykonania testu.
    """
    return monitoring.Certyfikat(
        podmiot="uslugi.firma.pl", wystawca="Wewnetrzne CA",
        wazny_od=utcnow() - datetime.timedelta(days=100),
        wazny_do=utcnow() + datetime.timedelta(days=dni, hours=12),
        odcisk=odcisk, nazwy=["uslugi.firma.pl"])


def test_certyfikat_zglasza_sie_na_kazdym_progu_ale_raz(tenant_a, wyslane):
    """W "ostrzezeniu" certyfikat stoi tygodniami - sam stan by tego nie zlapal."""
    monitor_id = dodaj_cel(tenant_a["id"], powiadamiaj=True, adresaci="it@firma.pl",
                           prog_ostrzezenia_dni=30, prog_alarmu_dni=7)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        # jeszcze daleko - cisza
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(60)))
        assert wyslane == []

        # prog ostrzezenia, dwa sprawdzenia -> jedna wiadomosc
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(25)))
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(24)))
        assert len(wyslane) == 1
        assert "wygasa" in wyslane[-1]["temat"]

        # prog alarmu - kolejna wiadomosc
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(5)))
        assert len(wyslane) == 2

        # po terminie - trzecia i ostatnia
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(-1)))
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(-2)))
        assert len(wyslane) == 3
        assert "wygasl" in wyslane[-1]["temat"].lower()


def test_odnowiony_certyfikat_zeruje_progi_i_potwierdza_naprawe(tenant_a, wyslane):
    monitor_id = dodaj_cel(tenant_a["id"], powiadamiaj=True, adresaci="it@firma.pl",
                           prog_ostrzezenia_dni=30, prog_alarmu_dni=7)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=True, certyfikat=_certyfikat(3)))
        assert len(wyslane) == 1

        # Nowy certyfikat = nowy odcisk. Ktos poprosil o odnowienie i chce
        # wiedziec, ze zadzialalo.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, certyfikat=_certyfikat(365, odcisk="d" * 64)))
        assert len(wyslane) == 2
        assert "porzadku" in wyslane[-1]["temat"]
        assert monitor.stan_certyfikatu == "ok"
        assert monitor.powiadomiony_prog is None

        # ...a przed nastepnym koncem waznosci ostrzezenie przyjdzie ponownie.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, certyfikat=_certyfikat(20, odcisk="d" * 64)))
        assert len(wyslane) == 3
        assert "wygasa" in wyslane[-1]["temat"]


def test_niezaufany_lancuch_zglasza_sie_od_razu(tenant_a, wyslane):
    """Lancuch nie do zweryfikowania nie przekracza zadnego progu dni.

    Certyfikat wazny rok, ktorego przegladarka nie przyjmie, jest awaria od
    chwili, w ktorej go zobaczylismy - drabinka progow by go nie zlapala.
    """
    monitor_id = dodaj_cel(tenant_a["id"], powiadamiaj=True, adresaci="it@firma.pl",
                           weryfikuj_lancuch=True)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=12, tls_zestawione=True, certyfikat=_certyfikat(365),
            cert_zaufany=False, cert_blad="self-signed certificate"))
        assert monitor.stan_certyfikatu == "awaria"
        assert len(wyslane) == 1
        assert "niezaufany" in wyslane[0]["temat"]

        # Drugie takie samo sprawdzenie juz nie pisze.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=12, tls_zestawione=True, certyfikat=_certyfikat(365),
            cert_zaufany=False, cert_blad="self-signed certificate"))
        assert len(wyslane) == 1

        # Naprawiony lancuch to zdarzenie tak samo jak zepsuty.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=12, tls_zestawione=True, certyfikat=_certyfikat(365),
            cert_zaufany=True))
        assert len(wyslane) == 2
        assert "porzadku" in wyslane[1]["temat"]


def test_awaria_uslugi_nie_oglasza_naprawy_certyfikatu(tenant_a, wyslane):
    """Nieudane polaczenie nie moze skasowac tego, co wiemy o certyfikacie.

    Gdyby ocena lancucha zerowala sie przy kazdej nieudanej probie, padajaca
    usluga z popsutym certyfikatem wysylalaby "certyfikat w porzadku" -
    w chwili, w ktorej nikt niczego nie naprawial.
    """
    monitor_id = dodaj_cel(tenant_a["id"], liczba_prob=1, powiadamiaj=True,
                           adresaci="it@firma.pl", weryfikuj_lancuch=True)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=True, czas_ms=12, tls_zestawione=True, certyfikat=_certyfikat(365),
            cert_zaufany=False, cert_blad="self-signed certificate"))
        assert len(wyslane) == 1

        # Usluga przestaje odpowiadac - do TLS w ogole nie dochodzi.
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(
            dostepna=False, blad="connection refused"))
        assert monitor.cert_zaufany is False       # wiedza sprzed awarii zostaje
        assert monitor.stan_certyfikatu == "awaria"
        # Jedyna nowa wiadomosc dotyczy dostepnosci, nie certyfikatu.
        assert len(wyslane) == 2
        assert "niedostepna" in wyslane[1]["temat"]


def test_nieudana_wysylka_nie_gubi_powiadomienia(tenant_a, monkeypatch):
    """Powiadomienie, ktore nie doszlo, nie jest powiadomieniem."""
    from cmdb_server.services import poczta

    def odmow(*args, **kwargs):
        raise poczta.BladPoczty("firma nie ma skonfigurowanej poczty")

    monkeypatch.setattr(poczta, "wyslij", odmow)
    monitor_id = dodaj_cel(tenant_a["id"], liczba_prob=1, protokol="tcp",
                           powiadamiaj=True, adresaci="dyzur@firma.pl")
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitoring.zapisz_wynik(db, monitor, monitoring.Wynik(dostepna=False, blad="cisza"))
        assert monitor.blad_powiadomienia
        # Znacznik NIE zostal przesuniety - przy kolejnym obiegu sprobujemy znowu.
        assert monitor.powiadomiona_dostepnosc is None


# --- obieg w tle ------------------------------------------------------------

def test_obieg_sprawdza_tylko_cele_po_terminie(tenant_a, tmp_path, petla_zwrotna):
    with serwer_tls(tmp_path) as (port, _):
        swiezy = dodaj_cel(tenant_a["id"], nazwa="Swiezy", port=port, interwal_sekund=3600)
        zalegly = dodaj_cel(tenant_a["id"], nazwa="Zalegly", port=port, interwal_sekund=60)
        with SessionLocal() as db:
            db.get(MonitorUslugi, swiezy).ostatnie_sprawdzenie = utcnow()
            db.get(MonitorUslugi, zalegly).ostatnie_sprawdzenie = (
                utcnow() - datetime.timedelta(hours=1))
            db.commit()
            wynik = monitoring.przebieg_w_sesji(db)
    assert wynik["sprawdzone"] == 1
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, zalegly).stan_dostepnosci == "ok"
        assert db.get(MonitorUslugi, swiezy).stan_dostepnosci == "nieznany"


def test_obieg_pomija_cele_wylaczone(tenant_a, tmp_path, petla_zwrotna):
    with serwer_tls(tmp_path) as (port, _):
        dodaj_cel(tenant_a["id"], nazwa="Wylaczony", port=port, aktywny=False)
        with SessionLocal() as db:
            assert monitoring.przebieg_w_sesji(db)["sprawdzone"] == 0


def test_obieg_konczy_sie_mimo_jednego_niedzialajacego_celu(tenant_a, tmp_path, petla_zwrotna):
    """Zaden pojedynczy cel nie moze zatrzymac calego obiegu."""
    with serwer_tls(tmp_path) as (port, _):
        dobry = dodaj_cel(tenant_a["id"], nazwa="Dobry", port=port)
        zly = dodaj_cel(tenant_a["id"], nazwa="Zly", port=wolny_port(), protokol="tcp")
        with SessionLocal() as db:
            wynik = monitoring.przebieg_w_sesji(db)
    assert wynik["sprawdzone"] == 2
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, dobry).stan_dostepnosci == "ok"
        assert db.get(MonitorUslugi, zly).stan_dostepnosci == "ostrzezenie"


# --- panel i izolacja firm --------------------------------------------------

def test_panel_pokazuje_cel_i_pozwala_go_sprawdzic(client, tenant_a, make_user,
                                                   tmp_path, petla_zwrotna):
    _, csrf = login(client, tenant_a, make_user)
    with serwer_tls(tmp_path, dni_waznosci=10) as (port, _):
        odpowiedz = client.post("/monitoring", data={
            "csrf_token": csrf, "nazwa": "Portal", "host": "127.0.0.1", "port": str(port),
            "protokol": "https", "sciezka": "/", "oczekiwany_kod": "0",
            "nazwa_tls": "cmdb-test.local", "interwal_sekund": "300", "limit_sekund": "5",
            "liczba_prob": "2", "prog_ostrzezenia_dni": "30", "prog_alarmu_dni": "7",
        }, follow_redirects=False)
        assert odpowiedz.status_code == 303, odpowiedz.text

        with SessionLocal() as db:
            monitor = db.execute(select(MonitorUslugi)).scalar_one()
            monitor_id = monitor.id
            # Cel dodany od razu sie sprawdza - inaczej przez pierwsze piec
            # minut nie wiadomo, czy w ogole zostal wpisany poprawnie.
            assert monitor.ostatnie_sprawdzenie is not None
            assert monitor.cert_podmiot == "cmdb-test.local"
            assert monitor.stan == "ostrzezenie"  # certyfikat na 10 dni

        strona = client.get("/monitoring")
        assert "Portal" in strona.text
        szczegoly = client.get(f"/monitoring/{monitor_id}")
        assert "cmdb-test.local" in szczegoly.text

        assert client.post(f"/monitoring/{monitor_id}/sprawdz", data={"csrf_token": csrf},
                           follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count(PomiarMonitora.id))) == 2


def test_zmiana_adresu_zeruje_stan_i_historie_powiadomien(client, tenant_a, make_user):
    _, csrf = login(client, tenant_a, make_user)
    monitor_id = dodaj_cel(tenant_a["id"], host="stara.firma.pl", protokol="tcp", port=443)
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        monitor.stan = monitor.stan_dostepnosci = "awaria"
        monitor.powiadomiona_dostepnosc = "awaria"
        monitor.cert_odcisk = "e" * 64
        db.commit()

    odpowiedz = client.post(f"/monitoring/{monitor_id}", data={
        "csrf_token": csrf, "nazwa": "Usluga", "host": "nowa.firma.pl", "port": "443",
        "protokol": "tcp", "sciezka": "/", "oczekiwany_kod": "0",
        "interwal_sekund": "60", "limit_sekund": "5", "liczba_prob": "2",
        "prog_ostrzezenia_dni": "30", "prog_alarmu_dni": "7", "aktywny": "true",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 303, odpowiedz.text
    with SessionLocal() as db:
        monitor = db.get(MonitorUslugi, monitor_id)
        assert monitor.host == "nowa.firma.pl"
        # Pod ta sama nazwa stoi teraz inna usluga - alarm o awarii czegos,
        # czego juz nie monitorujemy, bylby falszywy.
        assert monitor.stan == "nieznany"
        assert monitor.powiadomiona_dostepnosc is None
        assert monitor.cert_odcisk is None


def test_konto_tylko_do_odczytu_nie_zmienia_niczego(client, tenant_a, make_user):
    monitor_id = dodaj_cel(tenant_a["id"], protokol="tcp")
    _, csrf = login(client, tenant_a, make_user, role="viewer", email="czytelnik@example.com")
    assert client.get("/monitoring").status_code == 200
    for adres in (f"/monitoring/{monitor_id}/sprawdz", f"/monitoring/{monitor_id}/usun",
                  f"/monitoring/{monitor_id}/przelacz"):
        assert client.post(adres, data={"csrf_token": csrf}, follow_redirects=False).status_code == 403
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, monitor_id) is not None


def test_cel_innej_firmy_jest_niewidoczny(client, tenant_a, tenant_b, make_user):
    """Identyfikator z cudzego panelu nie moze wystarczyc do niczego."""
    obcy = dodaj_cel(tenant_b["id"], nazwa="Cudza usluga", protokol="tcp")
    _, csrf = login(client, tenant_a, make_user)
    assert client.get(f"/monitoring/{obcy}").status_code == 404
    assert client.post(f"/monitoring/{obcy}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 404
    assert "Cudza usluga" not in client.get("/monitoring").text
    with SessionLocal() as db:
        assert db.get(MonitorUslugi, obcy) is not None


def test_bez_zalogowania_nie_ma_dostepu(client, tenant_a):
    monitor_id = dodaj_cel(tenant_a["id"], protokol="tcp")
    for adres in ("/monitoring", f"/monitoring/{monitor_id}"):
        assert client.get(adres, follow_redirects=False).status_code == 303


def test_usuniecie_celu_zabiera_jego_pomiary(client, tenant_a, make_user):
    monitor_id = dodaj_cel(tenant_a["id"], protokol="tcp")
    with SessionLocal() as db:
        db.add(PomiarMonitora(tenant_id=tenant_a["id"], monitor_id=monitor_id,
                              sprawdzono=utcnow(), stan="ok"))
        db.commit()
    _, csrf = login(client, tenant_a, make_user)
    assert client.post(f"/monitoring/{monitor_id}/usun", data={"csrf_token": csrf},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.scalar(select(func.count(MonitorUslugi.id))) == 0
        assert db.scalar(select(func.count(PomiarMonitora.id))) == 0


def test_limit_celow_na_firme(client, tenant_a, make_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "monitoring_max_targets", 1)
    dodaj_cel(tenant_a["id"], nazwa="Pierwszy", protokol="tcp")
    _, csrf = login(client, tenant_a, make_user)
    odpowiedz = client.post("/monitoring", data={
        "csrf_token": csrf, "nazwa": "Drugi", "host": "uslugi.firma.pl", "port": "443",
        "protokol": "tcp", "sciezka": "/", "oczekiwany_kod": "0", "interwal_sekund": "300",
        "limit_sekund": "10", "liczba_prob": "2", "prog_ostrzezenia_dni": "30",
        "prog_alarmu_dni": "7",
    }, follow_redirects=False)
    assert odpowiedz.status_code == 400
    assert "limit" in odpowiedz.json()["detail"]


# --- raport -----------------------------------------------------------------

def test_raport_uslug_widzi_awarie_i_certyfikaty(client, tenant_a, make_user):
    niedostepny = dodaj_cel(tenant_a["id"], nazwa="Sklep", protokol="tcp", liczba_prob=1)
    wygasajacy = dodaj_cel(tenant_a["id"], nazwa="Poczta", prog_ostrzezenia_dni=30)
    with SessionLocal() as db:
        monitoring.zapisz_wynik(db, db.get(MonitorUslugi, niedostepny),
                                monitoring.Wynik(dostepna=False, blad="connection refused"))
        monitoring.zapisz_wynik(db, db.get(MonitorUslugi, wygasajacy),
                                monitoring.Wynik(dostepna=True, czas_ms=9,
                                                 certyfikat=_certyfikat(12)))
    login(client, tenant_a, make_user)

    pocztowy = client.get("/raporty/podglad/uslugi")
    assert pocztowy.status_code == 200
    assert "Sklep" in pocztowy.text and "connection refused" in pocztowy.text
    assert "Poczta" in pocztowy.text and "za 12 dni" in pocztowy.text

    panel = client.get("/raporty/widok/uslugi")
    assert panel.status_code == 200
    assert "Sklep" in panel.text and "Poczta" in panel.text


def test_raport_uslug_dziala_bez_zadnego_celu(client, tenant_a, make_user):
    login(client, tenant_a, make_user)
    odpowiedz = client.get("/raporty/podglad/uslugi")
    assert odpowiedz.status_code == 200
    assert "nie monitoruje jeszcze" in odpowiedz.text


def test_raport_uslug_nie_widzi_celow_innej_firmy(client, tenant_a, tenant_b, make_user):
    dodaj_cel(tenant_b["id"], nazwa="Cudza usluga", protokol="tcp")
    login(client, tenant_a, make_user)
    assert "Cudza usluga" not in client.get("/raporty/podglad/uslugi").text
