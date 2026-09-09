"""Monitorowanie uslug - strona agenta.

Sondy chodza po prawdziwym gniezdzie i prawdziwym TLS, a nie po zaslepce:
caly sens tego modulu polega na tym, co widac z drugiej strony polaczenia,
wiec zaslepiony ``socket`` sprawdzalby wylacznie zaslepke.

Osobno sprawdzamy skladanie przerw i redukcje ruchu - to one decyduja o tym,
czy doba monitorowania co minute to 96 wierszy, czy 1440.
"""
from __future__ import annotations

import datetime
import socket
import ssl
import threading
from contextlib import contextmanager

import pytest

from cmdb_agent import monitoring


# --- zaplecze: certyfikat i serwer TLS --------------------------------------

def wystaw_certyfikat(dni_waznosci: int, nazwa: str = "cmdb-test.local"):
    """Certyfikat self-signed o zadanej pozostalej waznosci.

    Sam agent certyfikatow nie wystawia ani nie rozbiera - w bibliotece
    standardowej nie ma parsera X.509. Tu korzystamy z cryptography, bo jest
    ono zaleznoscia TESTOW, a nie agenta.
    """
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
            .not_valid_after(teraz + datetime.timedelta(days=dni_waznosci))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(nazwa)]), False)
            .sign(klucz, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            klucz.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()))


@contextmanager
def serwer_tls(tmp_path, dni_waznosci=90,
               odpowiedz=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
    pem, klucz = wystaw_certyfikat(dni_waznosci)
    plik_cert = tmp_path / f"cert-{dni_waznosci}.pem"
    plik_klucz = tmp_path / f"cert-{dni_waznosci}.key"
    plik_cert.write_bytes(pem)
    plik_klucz.write_bytes(klucz)

    kontekst = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontekst.load_cert_chain(plik_cert, plik_klucz)
    gniazdo = socket.socket()
    gniazdo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(8)

    def obsluga():
        while True:
            try:
                klient, _ = gniazdo.accept()
            except OSError:
                return
            try:
                with kontekst.wrap_socket(klient, server_side=True) as tunel:
                    tunel.recv(4096)
                    tunel.sendall(odpowiedz)
            except OSError:
                pass

    watek = threading.Thread(target=obsluga, daemon=True)
    watek.start()
    try:
        yield gniazdo.getsockname()[1]
    finally:
        gniazdo.close()
        watek.join(timeout=2)


@contextmanager
def serwer_tcp():
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(8)
    try:
        yield gniazdo.getsockname()[1]
    finally:
        gniazdo.close()


def wolny_port() -> int:
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    port = gniazdo.getsockname()[1]
    gniazdo.close()
    return port


def cel(port, **nadpisania) -> dict:
    dane = {"id": "cel-1", "host": "127.0.0.1", "port": port, "protokol": "https",
            "sciezka": "/", "oczekiwany_kod": 0, "nazwa_tls": "cmdb-test.local",
            "weryfikuj_lancuch": False, "interwal_sekund": 60,
            "interwal_certyfikatu": 86400, "limit_sekund": 5, "liczba_prob": 2,
            "znany_odcisk": "", "wymus": False}
    dane.update(nadpisania)
    return dane


# --- sondy ------------------------------------------------------------------

def test_tcp_rozroznia_port_otwarty_od_zamknietego():
    with serwer_tcp() as port:
        wynik = monitoring.sonduj(cel(port, protokol="tcp"))
    assert wynik.dostepna is True
    assert wynik.odcisk is None  # bez TLS nie ma czego zdejmowac
    assert wynik.adres == "127.0.0.1"

    zamkniety = monitoring.sonduj(cel(wolny_port(), protokol="tcp"))
    assert zamkniety.dostepna is False
    assert zamkniety.blad


def test_tls_liczy_odcisk_takze_bez_zaufanego_lancucha(tmp_path):
    """Certyfikat wlasnego urzedu firmy to najczestszy przypadek wewnetrzny."""
    with serwer_tls(tmp_path, dni_waznosci=42) as port:
        wynik = monitoring.sonduj(cel(port, protokol="tls"), zdejmij_cert=True)
    assert wynik.dostepna is True
    assert wynik.tls_zestawione is True
    assert len(wynik.odcisk) == 64
    assert wynik.pem.startswith("-----BEGIN CERTIFICATE-----")
    # Bez weryfikacji nie mowimy "zaufany" ani "niezaufany" - nie sprawdzalismy.
    assert wynik.cert_zaufany is None


def test_niezaufany_lancuch_nie_przykrywa_dostepnosci(tmp_path):
    """Usluga odpowiada, ale jej certyfikatu nie da sie zweryfikowac.

    To dwie osobne informacje i obie musza przetrwac: usluga dziala,
    a certyfikat jest do naprawy.
    """
    with serwer_tls(tmp_path) as port:
        wynik = monitoring.sonduj(cel(port, weryfikuj_lancuch=True), zdejmij_cert=True)
    assert wynik.dostepna is True
    assert wynik.kod == 200
    assert wynik.cert_zaufany is False
    assert wynik.cert_blad
    assert wynik.odcisk is not None  # mimo odmowy weryfikacji


def test_certyfikat_jedzie_tylko_gdy_o_niego_poprosimy(tmp_path):
    """Odcisk liczymy zawsze, caly certyfikat wylacznie przy zmianie."""
    with serwer_tls(tmp_path) as port:
        bez = monitoring.sonduj(cel(port), zdejmij_cert=False)
        z_certyfikatem = monitoring.sonduj(cel(port), zdejmij_cert=True)
    assert bez.odcisk == z_certyfikatem.odcisk
    assert bez.pem is None
    assert z_certyfikatem.pem is not None


def test_http_sprawdza_kod_odpowiedzi(tmp_path):
    blad = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
    with serwer_tls(tmp_path, odpowiedz=blad) as port:
        domyslny = monitoring.sonduj(cel(port))
        oczekiwany = monitoring.sonduj(cel(port, oczekiwany_kod=503))
    # Otwarty port to nie to samo co dzialajaca aplikacja.
    assert domyslny.dostepna is False and domyslny.kod == 503
    # ...chyba ze wlasnie tego kodu oczekujemy - np. od strony logowania.
    assert oczekiwany.dostepna is True and oczekiwany.kod == 503


def test_limit_czasu_konczy_sonde():
    """Port, ktory przyjmuje polaczenie i milczy, nie moze zawiesic petli."""
    gniazdo = socket.socket()
    gniazdo.bind(("127.0.0.1", 0))
    gniazdo.listen(1)
    try:
        wynik = monitoring.sonduj(cel(gniazdo.getsockname()[1], protokol="tls",
                                      limit_sekund=1))
    finally:
        gniazdo.close()
    assert wynik.dostepna is False
    assert wynik.czas_ms < 5000


def test_nieistniejaca_nazwa_nie_wywraca_sondy():
    wynik = monitoring.sonduj(cel(443, host="nie-ma-takiej-nazwy-cmdb.invalid"))
    assert wynik.dostepna is False
    assert "nazw" in wynik.blad


def test_agent_nie_idzie_pod_adres_metadanych():
    """Agent bywa maszyna wirtualna u dostawcy - 169.254.169.254 odpowiada tam
    usluga metadanych instancji, a nie usluga firmy."""
    wynik = monitoring.sonduj(cel(80, host="169.254.169.254", protokol="tcp"))
    assert wynik.dostepna is False
    assert "link-local" in wynik.blad


@pytest.mark.parametrize("adres", ["169.254.169.254", "::ffff:169.254.169.254",
                                   "0.0.0.0", "224.0.0.1"])
def test_zabronione_adresy(adres):
    assert monitoring.powod_odmowy(adres) is not None


def test_petla_zwrotna_jest_dozwolona_dla_agenta():
    """Agent stoi na maszynie klienta - usluga na tej samej maszynie to
    calkiem zwyczajny cel. Zabrania jej dopiero serwer, przy zapisie celu."""
    assert monitoring.powod_odmowy("127.0.0.1") is None


# --- skladanie przerw -------------------------------------------------------

def chwila(sekundy: int) -> datetime.datetime:
    baza = datetime.datetime(2026, 9, 8, 12, 30, tzinfo=datetime.timezone.utc)
    return baza + datetime.timedelta(seconds=sekundy)


def test_przerwa_zaczyna_sie_przy_pierwszym_bledzie_nie_przy_potwierdzeniu():
    """Inaczej z opisu przerwy znikalaby ta jej czesc, w ktorej usluga juz nie
    dzialala, a agent jeszcze nie mial pewnosci."""
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0))
    assert stan.zapisz(monitoring.Wynik(dostepna=False, blad="refused"), 2, chwila(10)) is None
    assert stan.otwarta["od"] == chwila(10).isoformat()
    # Dopiero druga nieudana proba jest zdarzeniem wartym natychmiastowej wysylki.
    assert stan.zapisz(monitoring.Wynik(dostepna=False, blad="refused"), 2, chwila(70)) == "awaria"
    assert stan.otwarta["od"] == chwila(10).isoformat()
    assert stan.otwarta["sond"] == 2


def test_koniec_przerwy_to_ostatnia_nieudana_sonda():
    """Miedzy ostatnim bledem a pierwsza udana sonda uslugi nie bylo -
    liczenie konca od tej udanej skracaloby kazda awarie o jeden odstep."""
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0))
    stan.zapisz(monitoring.Wynik(dostepna=False, blad="refused"), 1, chwila(34))
    stan.zapisz(monitoring.Wynik(dostepna=False, blad="refused"), 1, chwila(322))
    assert stan.zapisz(monitoring.Wynik(dostepna=True, czas_ms=9), 1, chwila(382)) == "powrot"
    assert stan.otwarta is None
    assert len(stan.zamkniete) == 1
    przerwa = stan.zamkniete[0]
    assert przerwa["od"] == chwila(34).isoformat()
    assert przerwa["do"] == chwila(322).isoformat()
    assert przerwa["sond"] == 2


def test_raport_niesie_podsumowanie_zamiast_kazdej_sondy():
    """To jest cala redukcja ruchu: 15 sond zamienia sie w jeden wiersz."""
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0))
    for numer in range(15):
        stan.zapisz(monitoring.Wynik(dostepna=True, czas_ms=8), 2, chwila(numer * 60))
    wpis = stan.do_wyslania(chwila(900))

    assert wpis["okno"] == {"od": chwila(0).isoformat(), "do": chwila(900).isoformat(),
                            "sond": 15, "udanych": 15}
    assert wpis["przerwy"] == []
    # Po wyslaniu liczniki zeruja sie, a nowe okno zaczyna sie tam, gdzie
    # skonczylo poprzednie - bez dziury i bez nakladania sie.
    assert stan.sond == 0 and stan.okno_od == chwila(900)


def test_trwajaca_przerwa_jedzie_w_raporcie_bez_konca_i_wraca_w_nastepnym():
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0))
    stan.zapisz(monitoring.Wynik(dostepna=False, blad="timeout"), 1, chwila(60))
    pierwszy = stan.do_wyslania(chwila(900))
    assert pierwszy["przerwy"][0]["do"] is None
    # Poczatek przerwy jest jej tozsamoscia - serwer domknie ten sam wiersz.
    assert pierwszy["przerwy"][0]["od"] == chwila(60).isoformat()

    stan.zapisz(monitoring.Wynik(dostepna=True, czas_ms=7), 1, chwila(1000))
    drugi = stan.do_wyslania(chwila(1800))
    assert drugi["przerwy"][0]["od"] == chwila(60).isoformat()
    assert drugi["przerwy"][0]["do"] == chwila(60).isoformat()


def test_cel_bez_sond_nie_trafia_do_raportu():
    """Nie ma o czym mowic - a pusty wpis kosztowalby tyle samo co pelny."""
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0))
    assert stan.do_wyslania(chwila(900)) is None


def test_certyfikat_w_raporcie_tylko_przy_zmianie_odcisku():
    stan = monitoring.StanCelu(id="c1", okno_od=chwila(0), znany_odcisk="a" * 64)
    znany = monitoring.Wynik(dostepna=True, tls_zestawione=True,
                             odcisk="a" * 64, pem="PEM-STARY")
    stan.zapisz(znany, 2, chwila(60))
    assert "pem" not in stan.tls          # serwer juz go zna
    assert stan.tls["odcisk"] == "a" * 64

    nowy = monitoring.Wynik(dostepna=True, tls_zestawione=True,
                            odcisk="b" * 64, pem="PEM-NOWY")
    stan.zapisz(nowy, 2, chwila(120))
    assert stan.tls["pem"] == "PEM-NOWY"  # odnowiony - trzeba go przeslac

    wpis = stan.do_wyslania(chwila(900))
    stan.potwierdz_wyslanie(wpis)
    assert stan.znany_odcisk == "b" * 64
    # Po potwierdzeniu ten sam certyfikat juz nie jedzie.
    stan.zapisz(monitoring.Wynik(dostepna=True, tls_zestawione=True,
                                 odcisk="b" * 64, pem="PEM-NOWY"), 2, chwila(960))
    assert "pem" not in stan.tls


# --- polityka ---------------------------------------------------------------

def test_polityka_odrzuca_cel_z_zabojczym_odstepem():
    """Serwer sprawdza to u siebie, ale agent nie moze na tym polegac:
    to jego maszyne obciazy odstep ustawiony na sekunde."""
    with pytest.raises(ValueError, match="odstep sond"):
        monitoring.sprawdz_cel({"id": "c1", "host": "a.pl", "port": 443,
                                "protokol": "https", "interwal_sekund": 1,
                                "limit_sekund": 1})


def test_polityka_odrzuca_wstrzykniecie_w_sciezke():
    with pytest.raises(ValueError, match="sciezka"):
        monitoring.sprawdz_cel({"id": "c1", "host": "a.pl", "port": 443,
                                "protokol": "https", "interwal_sekund": 60,
                                "limit_sekund": 10,
                                "sciezka": "/health\r\nX-Ustawione: 1"})


def test_polityka_odrzuca_nieznany_protokol():
    with pytest.raises(ValueError, match="protokol"):
        monitoring.sprawdz_cel({"id": "c1", "host": "a.pl", "port": 443,
                                "protokol": "gopher", "interwal_sekund": 60,
                                "limit_sekund": 10})


class KlientZaslepka:
    """Minimalny klient CMDB - tylko tyle, ile widzi z niego monitorowanie."""

    def __init__(self, polityka, wyslane=None):
        self.polityka = polityka
        self.wyslane = wyslane if wyslane is not None else []
        self.odmawiaj = False

    def get(self, sciezka, token):
        nonce = sciezka.split("nonce=")[1]
        wygasa = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(seconds=60))
        return {"protocol": 1, "nonce": nonce, "asset_id": "asset-1",
                "machine_id": "maszyna-1", "expires_at": wygasa.isoformat(),
                "policy": self.polityka}

    def post(self, sciezka, token, payload):
        if self.odmawiaj:
            raise RuntimeError("brak lacznosci")
        self.wyslane.append(payload)
        return {"przyjeto": len(payload["cele"]), "pominieto": 0,
                "interwal_raportu": 900}


class StanZaslepka:
    agent_token = "cmdb_agt_x"
    asset_id = "asset-1"
    machine_id = "maszyna-1"
    is_enrolled = True


def polityka(cele=None, enabled=True) -> dict:
    return {"enabled": enabled, "interwal_raportu": 900,
            "cele": cele if cele is not None else [
                {"id": "c1", "host": "uslugi.firma.pl", "port": 443,
                 "protokol": "tcp", "interwal_sekund": 60, "limit_sekund": 5,
                 "liczba_prob": 2, "sciezka": "/", "oczekiwany_kod": 0,
                 "znany_odcisk": "", "wymus": False}]}


def test_pobrana_polityka_musi_zgadzac_sie_z_maszyna():
    """Zapisany stan NIGDY nie jest upowaznieniem - odpowiedz musi wracac
    zwiazana z tym pytaniem i z ta maszyna."""
    klient = KlientZaslepka(polityka())

    def obca_odpowiedz(sciezka, token):
        odpowiedz = KlientZaslepka.get(klient, sciezka, token)
        odpowiedz["asset_id"] = "cudzy-asset"
        return odpowiedz

    klient.get = obca_odpowiedz
    with pytest.raises(ValueError, match="niezgodna odpowiedz"):
        monitoring.pobierz_polityke(klient, StanZaslepka())


def test_jeden_bledny_cel_nie_wylacza_pozostalych():
    klient = KlientZaslepka(polityka(cele=[
        {"id": "zly", "host": "a.pl", "port": 443, "protokol": "gopher",
         "interwal_sekund": 60, "limit_sekund": 5},
        {"id": "dobry", "host": "b.pl", "port": 443, "protokol": "tcp",
         "interwal_sekund": 60, "limit_sekund": 5, "sciezka": "/"},
    ]))
    wynik = monitoring.pobierz_polityke(klient, StanZaslepka())
    assert [c["id"] for c in wynik["cele"]] == ["dobry"]


def test_odebrany_cel_przestaje_byc_sondowany():
    """Odebranie celu w panelu ma go odebrac naprawde, razem z jego stanem."""
    klient = KlientZaslepka(polityka())
    monitor = monitoring.Monitor(klient, StanZaslepka())
    assert monitor.odswiez_polityke() is True
    assert set(monitor.stany) == {"c1"}

    klient.polityka = polityka(cele=[])
    monitor.odswiez_polityke()
    assert monitor.cele == {} and monitor.stany == {}


def test_nieudane_pobranie_polityki_nie_wylacza_monitorowania():
    """Sondujemy dalej to, co juz mamy - cisza monitorowania wyglada tak samo
    jak sprawna usluga i jest gorsza niz nieaktualna lista celow."""
    klient = KlientZaslepka(polityka())
    monitor = monitoring.Monitor(klient, StanZaslepka())
    monitor.odswiez_polityke()

    def odmow(sciezka, token):
        raise RuntimeError("serwer niedostepny")

    klient.get = odmow
    assert monitor.odswiez_polityke() is False
    assert set(monitor.cele) == {"c1"}
    assert monitor.ostatni_blad


# --- petla i wysylka --------------------------------------------------------

def test_awaria_idzie_natychmiast_a_potwierdzenia_czekaja(tmp_path, monkeypatch):
    """Redukcja ruchu dotyczy potwierdzen, ze wszystko dziala - alarm, ktory
    czeka kwadrans, przestaje byc alarmem."""
    klient = KlientZaslepka(polityka(cele=[
        {"id": "c1", "host": "127.0.0.1", "port": wolny_port(), "protokol": "tcp",
         "interwal_sekund": 60, "limit_sekund": 1, "liczba_prob": 2,
         "sciezka": "/", "oczekiwany_kod": 0, "znany_odcisk": "", "wymus": False}]))
    monitor = monitoring.Monitor(klient, StanZaslepka(),
                                 sciezka_stanu=tmp_path / "stan.json")
    monitor.nastepny_raport = float("inf")  # okresowa wysylka jeszcze nie teraz

    monitor.krok()                       # pierwszy blad - jeszcze nie zdarzenie
    assert klient.wyslane == []
    monitor.stany["c1"].nastepna_sonda = 0
    monitor.nastepna_polityka = float("inf")
    monitor.krok()                       # drugi blad - potwierdzona awaria
    assert len(klient.wyslane) == 1
    assert klient.wyslane[0]["powod"] == "zdarzenie"
    assert klient.wyslane[0]["cele"][0]["ostatnia_sonda"]["potwierdzona_awaria"] is True


def test_nieudana_wysylka_nie_gubi_trwajacej_przerwy(tmp_path):
    """Okres przepada swiadomie - kolejka bez ograniczen zjadlaby dysk klienta.
    Ale otwarta przerwa zyje w stanie celu i pojdzie z nastepnym raportem."""
    klient = KlientZaslepka(polityka())
    monitor = monitoring.Monitor(klient, StanZaslepka(),
                                 sciezka_stanu=tmp_path / "stan.json")
    monitor.odswiez_polityke()
    stan = monitor.stany["c1"]
    stan.zapisz(monitoring.Wynik(dostepna=False, blad="timeout"), 1, chwila(60))

    klient.odmawiaj = True
    assert monitor.wyslij() is False
    klient.odmawiaj = False
    stan.zapisz(monitoring.Wynik(dostepna=False, blad="timeout"), 1, chwila(120))
    assert monitor.wyslij() is True
    przerwy = klient.wyslane[0]["cele"][0]["przerwy"]
    assert przerwy[0]["od"] == chwila(60).isoformat()   # ta sama przerwa
    assert przerwy[0]["do"] is None


def test_stan_przezywa_restart(tmp_path):
    """Bez tego restart maszyny gubilby trwajaca awarie i wysylal ponownie
    wszystkie certyfikaty."""
    sciezka = tmp_path / "stan.json"
    klient = KlientZaslepka(polityka())
    pierwszy = monitoring.Monitor(klient, StanZaslepka(), sciezka_stanu=sciezka)
    pierwszy.odswiez_polityke()
    pierwszy.stany["c1"].zapisz(
        monitoring.Wynik(dostepna=False, blad="timeout"), 1, chwila(60))
    pierwszy.stany["c1"].znany_odcisk = "c" * 64
    pierwszy.wyslij()

    drugi = monitoring.Monitor(klient, StanZaslepka(), sciezka_stanu=sciezka)
    assert drugi.stany["c1"].otwarta["od"] == chwila(60).isoformat()
    assert drugi.stany["c1"].znany_odcisk == "c" * 64


def test_blad_jednego_obrotu_nie_konczy_monitorowania(tmp_path):
    """Petla monitorowania nie moze umrzec: jej cisza wyglada tak samo jak
    sprawna usluga."""
    klient = KlientZaslepka(polityka())
    monitor = monitoring.Monitor(klient, StanZaslepka(),
                                 sciezka_stanu=tmp_path / "stan.json")
    obroty = []

    def wybuchowy_krok():
        obroty.append(1)
        if len(obroty) == 1:
            raise RuntimeError("cos poszlo nie tak")
        monitor.zatrzymaj.set()

    monitor.krok = wybuchowy_krok
    monitor.petla(odstep_petli=0)
    assert len(obroty) == 2   # po bledzie petla poszla dalej


# --- zgodnosc aktualizacji --------------------------------------------------

def test_sonda_workera_nie_oglasza_monitorowania():
    """Odpowiedz sondy jest uzgodnieniem z POPRZEDNIA wersja agenta.

    To zainstalowany agent sprawdza nia kandydata przed podmiana pliku.
    Dolozenie tam pola sprawiloby, ze kazdy juz wdrozony agent odmawia
    aktualizacji do tej wersji - czyli psuje dokladnie ten mechanizm,
    ktorym zmiana mialaby dojechac na maszyny.
    """
    import json

    from cmdb_agent import main
    from cmdb_agent import __version__

    class Przechwyt:
        def __init__(self):
            self.tresc = ""

        def write(self, tekst):
            self.tresc += tekst

        def flush(self):
            pass

    import contextlib
    przechwyt = Przechwyt()
    with contextlib.redirect_stdout(przechwyt):
        assert main.main(["worker-probe", "--nonce", "a" * 32]) == 0
    assert json.loads(przechwyt.tresc) == {
        "protocol": 1, "version": __version__, "nonce": "a" * 32,
        "commands": ["run", "enroll", "status"], "discovery_control": "cmdb-policy-v1"}


def test_kandydat_z_nowa_umiejetnoscia_przechodzi_sprawdzenie(monkeypatch, tmp_path):
    """Kandydat jest nowszy z zalozenia i wolno mu oglosic wiecej.

    Wymaganie rownosci calego slownika znaczyloby, ze kazde rozszerzenie
    agenta blokuje aktualizacje do niego samego.
    """
    import json
    import sys
    from unittest.mock import Mock

    from cmdb_agent import upgrade

    monkeypatch.setattr(sys, "platform", "win32")
    wywolania = []

    def udawany_run(args, **kwargs):
        wywolania.append(args)
        if "--version" in args:
            return Mock(returncode=0, stdout=b"cmdb-agent 0.9.9", stderr=b"")
        nonce = args[args.index("--nonce") + 1]
        return Mock(returncode=0, stdout=json.dumps({
            "protocol": 1, "version": "0.9.9", "nonce": nonce,
            "commands": ["run", "enroll", "status", "cos-nowego"],
            "discovery_control": "cmdb-policy-v1",
            "przyszle_pole": "nieznane tej wersji"}).encode(), stderr=b"")

    monkeypatch.setattr(upgrade.subprocess, "run", udawany_run)
    dziala, opis = upgrade._czy_dziala(tmp_path / "agent.exe", "0.9.9")
    assert dziala is True, opis

    # ...ale zla wersja albo cudza wartosc jednorazowa nadal odpadaja.
    def podszywajacy_sie(args, **kwargs):
        if "--version" in args:
            return Mock(returncode=0, stdout=b"cmdb-agent 0.9.9", stderr=b"")
        return Mock(returncode=0, stdout=json.dumps({
            "protocol": 1, "version": "0.9.9", "nonce": "b" * 32,
            "commands": ["run", "enroll", "status"],
            "discovery_control": "cmdb-policy-v1"}).encode(), stderr=b"")

    monkeypatch.setattr(upgrade.subprocess, "run", podszywajacy_sie)
    assert upgrade._czy_dziala(tmp_path / "agent.exe", "0.9.9")[0] is False


# --- co widac na maszynie ----------------------------------------------------
#
# Cisza monitorowania wyglada dokladnie tak samo jak sprawna usluga. Jedyna
# rzecz, ktora je rozroznia, to status publikowany przez sama petle - wiec on
# sam musi byc sprawdzony rownie dokladnie jak sonda.

import json
import types
from datetime import timedelta
from pathlib import Path

from cmdb_agent import status as status_mod


def _konfiguracja(tmp_path: Path):
    return types.SimpleNamespace(
        data_dir=tmp_path, server_url="https://cmdb.example",
        report_interval_seconds=3600, spool_dir=tmp_path / "spool",
    )


def _monitor(tmp_path: Path, cele: dict | None = None) -> monitoring.Monitor:
    stan = types.SimpleNamespace(is_enrolled=True, agent_token="t",
                                 asset_id="A1", machine_id="M1")
    monitor = monitoring.Monitor(
        client=None, state=stan,
        sciezka_stanu=tmp_path / "monitoring-state.json",
        sciezka_statusu=status_mod.monitoring_path(_konfiguracja(tmp_path)),
    )
    if cele:
        monitor.cele = cele
        for identyfikator in cele:
            monitor.stany[identyfikator] = monitoring.StanCelu(id=identyfikator)
    return monitor


CEL_TESTOWY = {
    "c1": {"id": "c1", "nazwa": "Portal firmowy", "host": "10.0.0.5", "port": 443,
           "protokol": "https", "sciezka": "/", "oczekiwany_kod": 0, "nazwa_tls": "",
           "weryfikuj_lancuch": True, "interwal_sekund": 60, "interwal_certyfikatu": 86400,
           "limit_sekund": 5, "liczba_prob": 2, "znany_odcisk": "", "wymus": False},
}


def test_status_wymienia_co_jest_sprawdzane(tmp_path):
    """Operator przy maszynie ma zobaczyc CO ona sprawdza, a nie samo "dziala"."""
    monitor = _monitor(tmp_path, CEL_TESTOWY)
    monitor.opublikuj_status()

    zapis = json.loads(status_mod.monitoring_path(_konfiguracja(tmp_path)).read_text())
    assert zapis["cele"] == 1
    wpis = zapis["lista"][0]
    assert wpis["nazwa"] == "Portal firmowy"
    assert wpis["adres"] == "10.0.0.5:443"
    assert wpis["protokol"] == "https"
    assert wpis["interwal_sekund"] == 60


def test_status_nie_publikuje_certyfikatu(tmp_path):
    """Plik czyta kazdy zalogowany uzytkownik - nie ma tam po co trafiac PEM."""
    monitor = _monitor(tmp_path, CEL_TESTOWY)
    monitor.stany["c1"].tls = {"odcisk": "ab", "zaufany": True, "pem": "-----BEGIN CERT..."}
    monitor.opublikuj_status()

    tresc = status_mod.monitoring_path(_konfiguracja(tmp_path)).read_text()
    assert "BEGIN CERT" not in tresc
    assert "agent_token" not in tresc


def test_brak_pliku_to_nie_jest_to_samo_co_brak_celow(tmp_path):
    """Dwie rozne awarie z dwoma roznymi naprawami - nie wolno ich zlac."""
    config = _konfiguracja(tmp_path)
    (tmp_path / "public").mkdir(parents=True, exist_ok=True)

    assert status_mod.read_monitoring(config)["stan"] == "brak"

    _monitor(tmp_path).opublikuj_status()          # zyje, ale panel nic nie przypisal
    assert status_mod.read_monitoring(config)["stan"] == "bez_celow"

    _monitor(tmp_path, CEL_TESTOWY).opublikuj_status()
    assert status_mod.read_monitoring(config)["stan"] == "dziala"


def test_zatrzymany_proces_nie_udaje_dzialajacego(tmp_path):
    """Status sprzed godziny znaczy, ze petla nie zyje - a nie ze wszystko gra."""
    config = _konfiguracja(tmp_path)
    monitor = _monitor(tmp_path, CEL_TESTOWY)
    monitor.opublikuj_status()

    zapis = json.loads(status_mod.monitoring_path(config).read_text())
    stary = monitoring._teraz() - timedelta(seconds=status_mod.MONITORING_STALE_SECONDS + 60)
    zapis["opublikowano"] = stary.isoformat()
    status_mod.monitoring_path(config).write_text(json.dumps(zapis), encoding="utf-8")

    assert status_mod.read_monitoring(config)["stan"] == "zatrzymane"


def test_cisza_monitorowania_trafia_do_ostrzezen(tmp_path):
    """Bez ostrzezenia nikt sie nie dowie, ze nic nie jest sprawdzane."""
    config = _konfiguracja(tmp_path)
    (tmp_path / "public").mkdir(parents=True, exist_ok=True)
    stan = types.SimpleNamespace(
        last_status="ok", last_sync_at="", last_attempt_at="", last_error="",
        last_sync_changed=False, tenant_slug="firma", asset_id="A1",
        is_enrolled=True, discovery_status={},
    )
    migawka = status_mod.build_status(config, stan)
    assert any("cmdb-agent-monitor" in u for u in migawka.warnings)

    _monitor(tmp_path, CEL_TESTOWY).opublikuj_status()
    migawka = status_mod.build_status(config, stan)
    assert not any("monitorowani" in u for u in migawka.warnings)
    assert migawka.monitoring["stan"] == "dziala"


def test_odczyt_gui_bierze_zywy_plik_a_nie_migawke(tmp_path):
    """status.json zapisuje inwentaryzacja raz na godzine.

    Gdyby ikona ufala zapisanej tam migawce, pokazywalaby "dziala" godzine po
    smierci monitora. Regresja: to bylo zle w pierwszym podejsciu.
    """
    config = _konfiguracja(tmp_path)
    (tmp_path / "public").mkdir(parents=True, exist_ok=True)
    status_mod.status_path(config).write_text(json.dumps({
        "hostname": "maszyna", "last_status": "ok",
        "monitoring": {"stan": "dziala", "cele": 7},
    }), encoding="utf-8")

    # Monitor nigdy nie wystartowal - mimo optymistycznej migawki w status.json.
    assert status_mod.read(config).monitoring["stan"] == "brak"


def test_pojedyncza_zgubiona_sonda_to_nie_awaria(tmp_path):
    """Przerwa otwiera sie przy pierwszym bledzie, ale awaria wymaga potwierdzenia."""
    monitor = _monitor(tmp_path, CEL_TESTOWY)
    wynik = monitoring.Wynik(dostepna=False, blad="timed out")
    monitor.stany["c1"].zapisz(wynik, liczba_prob=2, teraz=monitoring._teraz())

    assert monitor.stany["c1"].otwarta is not None       # poczatek zapamietany
    assert monitor.status()["otwarte_przerwy"] == 0      # ale to jeszcze nie awaria

    monitor.stany["c1"].zapisz(wynik, liczba_prob=2, teraz=monitoring._teraz())
    assert monitor.status()["otwarte_przerwy"] == 1


def test_starszy_serwer_bez_nazwy_celu_dziala_dalej(tmp_path):
    """Nazwa jest dodatkiem do wyswietlania - jej brak nie moze wywrocic sondy."""
    surowy = dict(CEL_TESTOWY["c1"])
    surowy.pop("nazwa")
    cel = monitoring.sprawdz_cel(surowy)
    assert cel["nazwa"] == ""
    assert cel["host"] == "10.0.0.5"

    cel = monitoring.sprawdz_cel({**surowy, "nazwa": "x" * 500})
    assert len(cel["nazwa"]) == 200


# --- droga wyniku do panelu --------------------------------------------------
#
# Agent moze poprawnie sondowac i mimo to nie dolozyc do panelu ani jednego
# wiersza. Oba przypadki nizej wygladaly u niego identycznie jak sukces.

class _KlientRaportu:
    """Serwer, ktory odpowiada tak, jak kaze mu test."""

    def __init__(self, odpowiedz=None):
        self.odpowiedz = odpowiedz if odpowiedz is not None else {"przyjeto": 1, "pominieto": 0}
        self.wyslane = []

    def post(self, sciezka, token, dane):
        self.wyslane.append(dane)
        return self.odpowiedz


def _monitor_z_celem(tmp_path, klient) -> monitoring.Monitor:
    stan = types.SimpleNamespace(is_enrolled=True, agent_token="t",
                                 asset_id="A1", machine_id="M1")
    monitor = monitoring.Monitor(klient, stan, sciezka_stanu=tmp_path / "stan.json")
    monitor.cele = dict(CEL_TESTOWY)
    monitor.stany["c1"] = monitoring.StanCelu(id="c1")
    return monitor


def test_serwer_pominal_wszystko_a_agent_tego_nie_widzial(tmp_path):
    """HTTP 200 nie znaczy, ze cokolwiek wyladowalo w panelu.

    Serwer pomija cele, ktore dostaly innego wykonawce albo zostaly wylaczone.
    Bez tego agent pokazywal "Dziala", a w panelu byla cisza nie do
    wytlumaczenia z zadnej strony.
    """
    klient = _KlientRaportu({"przyjeto": 0, "pominieto": 1})
    monitor = _monitor_z_celem(tmp_path, klient)
    monitor.stany["c1"].zapisz(monitoring.Wynik(dostepna=True, czas_ms=5), 2, monitoring._teraz())

    assert monitor.wyslij() is True          # serwer przyjal polaczenie...
    assert monitor.ostatni_blad               # ...ale agent wie, ze nic nie wpisano
    assert "pominal" in monitor.ostatni_blad
    assert monitor.status()["pominieto"] == 1


def test_czesciowe_pominiecie_nie_jest_bledem(tmp_path):
    """Jeden cel przepisany innemu agentowi to nie awaria reszty raportu."""
    klient = _KlientRaportu({"przyjeto": 3, "pominieto": 1})
    monitor = _monitor_z_celem(tmp_path, klient)
    monitor.stany["c1"].zapisz(monitoring.Wynik(dostepna=True, czas_ms=5), 2, monitoring._teraz())

    monitor.wyslij()
    assert monitor.ostatni_blad == ""
    assert monitor.status()["przyjeto"] == 3


def test_zlecone_sprawdzenie_wraca_bez_czekania_na_kwadrans(tmp_path, monkeypatch):
    """Panel obiecuje wynik, a nie miejsce w kolejce.

    Wymuszenie kazalo sondowac od razu, ale raport i tak czekal do konca
    okresu - czyli "sprawdz teraz" na udanej usludze milczalo do 15 minut.
    Natychmiastowe bylo TYLKO zle: awarie jada jako zdarzenie.
    """
    klient = _KlientRaportu()
    monitor = _monitor_z_celem(tmp_path, klient)
    monitor.pilne.add("c1")
    monitor.stany["c1"].nastepna_sonda = 0
    monkeypatch.setattr(monitoring, "sonduj",
                        lambda cel, zdejmij_cert=False: monitoring.Wynik(dostepna=True, czas_ms=7))

    zdarzenia = monitor.sonduj_zalegle()
    assert zdarzenia == ["c1:na zadanie"]     # powod natychmiastowej wysylki
    assert monitor.pilne == set()             # i tylko raz, nie w kolko


def test_wymuszenie_z_polityki_trafia_na_liste_pilnych(tmp_path, monkeypatch):
    """Przechodzimy prawdziwa odswiez_polityke - to ona laczy flage z lista."""
    stan = types.SimpleNamespace(is_enrolled=True, agent_token="t",
                                 asset_id="A1", machine_id="M1")
    monitor = monitoring.Monitor(None, stan, sciezka_stanu=tmp_path / "stan.json")

    def polityka(_client, _state, wymus):
        return {"enabled": True, "interwal_raportu": 900,
                "cele": [{**CEL_TESTOWY["c1"], "wymus": wymus}]}

    monkeypatch.setattr(monitoring, "pobierz_polityke",
                        lambda c, s: polityka(c, s, True))
    assert monitor.odswiez_polityke() is True
    assert monitor.pilne == {"c1"}

    # Kolejne pobranie BEZ wymuszenia nie moze dokladac celu w kolko - inaczej
    # kazda odswiezona polityka wysylalaby raport poza kolejnoscia.
    monitor.pilne.clear()
    monkeypatch.setattr(monitoring, "pobierz_polityke",
                        lambda c, s: polityka(c, s, False))
    monitor.odswiez_polityke()
    assert monitor.pilne == set()


def test_pierwszy_raport_nie_kaze_czekac_kwadransa(tmp_path, monkeypatch):
    """Po starcie panel ma sie odezwac w minute, a nie po pelnym okresie.

    Przez te 15 minut dzialajacy monitor wygladal w panelu dokladnie tak samo
    jak martwy: zero sond i stan "nieznany".
    """
    klient = _KlientRaportu()
    monitor = _monitor_z_celem(tmp_path, klient)
    monitor.interwal_raportu = 900
    monitor.zatrzymaj.set()          # jeden obrot i wyjscie
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: 1000.0)

    monitor.petla(odstep_petli=0)
    assert monitor.nastepny_raport == 1000.0 + monitoring.PIERWSZY_RAPORT
    assert monitoring.PIERWSZY_RAPORT < monitor.interwal_raportu


def test_etykieta_mowi_o_sondach_a_nie_o_dlugosci_listy(tmp_path):
    """Napis w oknie agenta byl twierdzeniem o pomiarze, a liczyl przydzial.

    Agent, ktory pobral polityke i nie wykonal ani jednej sondy, mowil
    dokladnie to samo co pracujacy poprawnie - i wlasnie na tym mozna sie
    przejechac przy szukaniu przyczyny ciszy w panelu.
    """
    monitor = _monitor_z_celem(tmp_path, _KlientRaportu())
    monitor.cele["c2"] = {**CEL_TESTOWY["c1"], "id": "c2", "nazwa": "Druga"}
    monitor.stany["c2"] = monitoring.StanCelu(id="c2")

    stan = monitor.status()
    stan["stan"] = "dziala"
    assert status_mod.sprawdzone_cele(stan) == 0
    assert "jeszcze bez sondy" in status_mod.monitoring_label(stan)

    monitor.stany["c1"].zapisz(monitoring.Wynik(dostepna=True, czas_ms=4), 2, monitoring._teraz())
    stan = monitor.status(); stan["stan"] = "dziala"
    assert status_mod.monitoring_label(stan).endswith("sprawdza 1 z 2 usł.")

    monitor.stany["c2"].zapisz(monitoring.Wynik(dostepna=True, czas_ms=6), 2, monitoring._teraz())
    stan = monitor.status(); stan["stan"] = "dziala"
    assert status_mod.monitoring_label(stan).endswith("sprawdza 2 usł.")
