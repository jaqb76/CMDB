"""Aktualizacja agenta zainstalowanego ze zrodel.

Na Linuksie agent nie jest pojedynczym plikiem, tylko katalogiem - PyInstaller
nie kompiluje na inna architekture, wiec jedna paczka zrodel obsluguje i
Raspberry Pi, i serwer x86. Aktualizacja to wiec podmiana katalogu, a nie pliku.

Dwie rzeczy, ktore musza dzialac bezwzglednie: agent nie moze podmienic
katalogu, ktory nie jest instalacja (np. czyjejs kopii repozytorium), i nie
moze probowac instalowac paczki zrodel tam, gdzie dziala plik wykonywalny.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from cmdb_agent import upgrade


def _paczka(cel: Path, wersja: str = "9.9.9", zepsuta: bool = False) -> Path:
    """Buduje paczke zrodel z dzialajacym (albo nie) pakietem agenta."""
    korzen = cel / "budowa" / upgrade.KATALOG_W_ARCHIWUM
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / "packaging").mkdir(parents=True)

    tresc = f'__version__ = "{wersja}"\n'
    if zepsuta:
        tresc += "to nie jest poprawny Python (\n"
    (korzen / "cmdb_agent" / "__init__.py").write_text(tresc, encoding="utf-8")
    (korzen / "cmdb_agent" / "main.py").write_text(
        "from . import __version__\n"
        "import sys\n"
        "print(f'cmdb-agent {__version__}')\n",
        encoding="utf-8",
    )
    (korzen / "packaging" / "install-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    archiwum = cel / "paczka.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        tar.add(korzen, arcname=upgrade.KATALOG_W_ARCHIWUM)
    return archiwum


# --- ochrona katalogu roboczego ---------------------------------------------

def test_bez_znacznika_agent_nie_rusza_katalogu(tmp_path, monkeypatch):
    """Sedno zabezpieczenia: bez znacznika to nie jest instalacja, tylko
    czyjas kopia repozytorium - podmiana byla by utrata pracy."""
    korzen = tmp_path / "repozytorium"
    (korzen / "cmdb_agent").mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))

    assert upgrade.katalog_instalacji() is None


def test_ze_znacznikiem_katalog_jest_instalacja(tmp_path, monkeypatch):
    korzen = tmp_path / "opt-cmdb-agent"
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / upgrade.ZNACZNIK_INSTALACJI).write_text("2026-08-21", encoding="utf-8")
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))

    assert upgrade.katalog_instalacji() == korzen.resolve()


def test_agent_w_pliku_wykonywalnym_nie_jest_instalacja_ze_zrodel(monkeypatch):
    monkeypatch.setattr(upgrade.sys, "frozen", True, raising=False)
    assert upgrade.katalog_instalacji() is None


# --- rozpakowanie -----------------------------------------------------------

def test_rozpakowanie_daje_pakiet_agenta(tmp_path):
    archiwum = _paczka(tmp_path)
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")
    assert (rozpakowany / "cmdb_agent" / "__init__.py").is_file()


def test_archiwum_ze_sciezka_wychodzaca_poza_katalog_jest_odrzucane(tmp_path):
    """Sciezka z ".." pozwolilaby nadpisac dowolny plik na maszynie."""
    archiwum = tmp_path / "zlosliwa.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        dane = b"cokolwiek"
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = len(dane)
        tar.addfile(info, io.BytesIO(dane))

    with pytest.raises(upgrade.UpgradeError, match="podejrzana sciezke"):
        upgrade._rozpakuj(archiwum, tmp_path / "cel")


def test_archiwum_bez_pakietu_agenta_jest_odrzucane(tmp_path):
    archiwum = tmp_path / "obca.tar.gz"
    with tarfile.open(archiwum, "w:gz") as tar:
        dane = b"x"
        info = tarfile.TarInfo(f"{upgrade.KATALOG_W_ARCHIWUM}/cokolwiek.txt")
        info.size = len(dane)
        tar.addfile(info, io.BytesIO(dane))

    with pytest.raises(upgrade.UpgradeError, match="nie zawiera pakietu agenta"):
        upgrade._rozpakuj(archiwum, tmp_path / "cel")


# --- test dymny -------------------------------------------------------------

def test_dzialajaca_wersja_przechodzi_test(tmp_path):
    archiwum = _paczka(tmp_path, wersja="9.9.9")
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, opis = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert dziala, opis
    assert "9.9.9" in opis


def test_zepsuta_wersja_nie_przechodzi_testu(tmp_path):
    """Blad skladni wyszedlby dopiero przy nastepnym raporcie, gdy nie byloby
    juz do czego wracac."""
    archiwum = _paczka(tmp_path, wersja="9.9.9", zepsuta=True)
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, _ = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert not dziala


def test_inna_wersja_niz_obiecana_nie_przechodzi(tmp_path):
    archiwum = _paczka(tmp_path, wersja="1.0.0")
    rozpakowany = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    dziala, opis = upgrade._czy_zrodla_dzialaja(rozpakowany, "9.9.9")
    assert not dziala
    assert "oczekiwano" in opis


# --- podmiana i wycofanie ---------------------------------------------------

def _instalacja(tmp_path: Path) -> Path:
    korzen = tmp_path / "opt"
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / "cmdb_agent" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (korzen / "cmdb_agent" / "stary-plik.py").write_text("# stara wersja\n", encoding="utf-8")
    (korzen / upgrade.ZNACZNIK_INSTALACJI).write_text("2026-08-21", encoding="utf-8")
    return korzen


def test_podmiana_zostawia_poprzednia_wersje_do_wycofania(tmp_path):
    korzen = _instalacja(tmp_path)
    archiwum = _paczka(tmp_path, wersja="9.9.9")
    nowy = upgrade._rozpakuj(archiwum, tmp_path / "cel")

    zapasowy = upgrade._podmien_katalog(korzen, nowy)

    assert '9.9.9' in (korzen / "cmdb_agent" / "__init__.py").read_text(encoding="utf-8")
    assert (zapasowy / "stary-plik.py").is_file(), "poprzednia wersja musi zostac do wycofania"


def test_instalator_z_paczki_trafia_na_miejsce(tmp_path):
    """Pozwala odtworzyc usluge bez pobierania czegokolwiek."""
    korzen = _instalacja(tmp_path)
    nowy = upgrade._rozpakuj(_paczka(tmp_path), tmp_path / "cel")

    upgrade._podmien_katalog(korzen, nowy)
    assert (korzen / "packaging" / "install-agent.sh").is_file()


# --- zgodnosc rodzaju wydania -----------------------------------------------

def test_pobieranie_odrzuca_archiwum_ktore_nie_jest_archiwum(tmp_path, monkeypatch):
    import hashlib

    cel = tmp_path / "pobrane"

    class KlientAtrapa:
        def download(self, sciezka, token, plik, limit):
            dane = b"to nie jest gzip"
            plik.write_bytes(dane)
            return hashlib.sha256(dane).hexdigest()

    oferta = {
        "version": "9.9.9",
        "kind": upgrade.RODZAJ_ZRODLA,
        "sha256": hashlib.sha256(b"to nie jest gzip").hexdigest(),
    }
    with pytest.raises(upgrade.UpgradeError, match="nie jest archiwum"):
        upgrade._pobierz_i_sprawdz(KlientAtrapa(), "token", oferta, cel)


# --- widocznosc przyczyny ---------------------------------------------------
#
# Milczace pominiecie aktualizacji jest gorsze niz blad: w panelu widac tylko,
# ze maszyna uporczywie zostaje na starej wersji, i nie wiadomo dlaczego.

class _KlientAtrapa:
    def __init__(self, oferta):
        self.oferta = oferta
        self.zgloszenia = []

    def get_json(self, sciezka, token):
        return self.oferta

    def post_json(self, sciezka, token, dane):
        self.zgloszenia.append(dane)
        return {}


class _StanAtrapa:
    agent_token = "cmdb_agt_test"


def _zastosuj(monkeypatch, klient, frozen=False, korzen=None):
    monkeypatch.setattr(upgrade, "wlasny_plik", lambda: Path("/tmp/agent.exe") if frozen else None)
    # Powod odmowy jest czescia odpowiedzi - panel pokazuje go przy maszynie.
    monkeypatch.setattr(
        upgrade, "diagnoza_instalacji",
        lambda: (korzen, None) if korzen else (
            None, "brak instalacji ze zrodel - uruchom install-agent.sh"))
    monkeypatch.setattr(upgrade, "sprawdz_oferte", lambda c, t: klient.oferta)
    zgloszone = []
    monkeypatch.setattr(
        upgrade, "_zglos",
        lambda c, t, wersja, status, detal=None: zgloszone.append((wersja, status, detal)),
    )
    upgrade.zastosuj(None, _StanAtrapa(), klient)
    return zgloszone


def test_agent_bez_instalacji_zglasza_powod(monkeypatch):
    """Dokladnie przypadek agenta zainstalowanego starszym instalatorem,
    ktory nie zakladal jeszcze znacznika."""
    klient = _KlientAtrapa({"version": "0.5.2", "kind": upgrade.RODZAJ_ZRODLA})
    zgloszone = _zastosuj(monkeypatch, klient)

    assert len(zgloszone) == 1
    wersja, status, detal = zgloszone[0]
    assert (wersja, status) == ("0.5.2", "odrzucona")
    assert "install-agent.sh" in detal, "komunikat ma mowic, co zrobic"


def test_plik_wykonywalny_nie_bierze_paczki_zrodel(monkeypatch):
    klient = _KlientAtrapa({"version": "0.5.2", "kind": upgrade.RODZAJ_ZRODLA})
    zgloszone = _zastosuj(monkeypatch, klient, frozen=True)

    assert zgloszone[0][1] == "odrzucona"
    assert "paczke zrodel" in zgloszone[0][2]


def test_instalacja_ze_zrodel_nie_bierze_pliku(monkeypatch, tmp_path):
    klient = _KlientAtrapa({"version": "0.5.2", "kind": "plik"})
    zgloszone = _zastosuj(monkeypatch, klient, korzen=tmp_path)

    assert zgloszone[0][1] == "odrzucona"
    assert "plik wykonywalny" in zgloszone[0][2]


def test_brak_oferty_nie_generuje_szumu(monkeypatch):
    """Gdy serwer niczego nie oczekuje, nie ma czego zglaszac."""
    klient = _KlientAtrapa(None)
    assert _zastosuj(monkeypatch, klient) == []


# --- jednostka timera -------------------------------------------------------
#
# Cykliczne uruchamianie stoi wylacznie na tej jednostce. Blad w niej nie
# rzuca zadnym wyjatkiem - agent po prostu przestaje raportowac, i to cicho.

def _tresc_instalatora() -> str:
    korzen = Path(__file__).resolve().parent.parent
    return (korzen / "packaging" / "install-agent.sh").read_text(encoding="utf-8")


def test_timer_uzywa_harmonogramu_kalendarzowego():
    """Persistent= systemd honoruje WYLACZNIE razem z OnCalendar=.

    Przy timerze monotonicznym (OnUnitActiveSec) jest po cichu ignorowane,
    wiec przebiegi pominiete, gdy maszyna byla wylaczona, przepadaly - mimo ze
    jednostka deklarowala Persistent=true.
    """
    tresc = _tresc_instalatora()
    assert "OnCalendar=" in tresc
    assert "Persistent=true" in tresc
    # Szukamy dyrektywy, a nie slowa - w komentarzu wystepuje z wyjasnieniem,
    # dlaczego wlasnie jej nie uzywamy.
    dyrektywy = [w.strip() for w in tresc.splitlines() if not w.strip().startswith("#")]
    assert not any(w.startswith("OnUnitActiveSec=") for w in dyrektywy),         "monotoniczny odstep uniewaznia Persistent"


def test_timer_ma_przebieg_po_starcie_systemu():
    """Bez tego maszyna wlaczona miedzy terminami czekalaby do nastepnego."""
    assert "OnBootSec=" in _tresc_instalatora()


def test_timer_rozprasza_flote():
    assert "RandomizedDelaySec=" in _tresc_instalatora()


def test_usluga_jest_jednorazowa():
    """Agent zbiera dane i konczy prace - miedzy przebiegami nie zajmuje
    pamieci. Stan "inactive (dead)" jest wiec poprawny, a nie objawem awarii."""
    tresc = _tresc_instalatora()
    assert "Type=oneshot" in tresc
    assert "RemainAfterExit" not in tresc


# --- diagnoza instalacji ----------------------------------------------------

def test_kanoniczny_katalog_jest_instalacja_bez_znacznika(tmp_path, monkeypatch):
    """Znacznik pojawil sie pozniej niz instalator. Maszyny postawione
    wczesniej odrzucaly KAZDA aktualizacje - w panelu widac bylo 'agent nie
    dziala z instalacji zalozonej instalatorem' przy maszynie, ktora wlasnie
    z instalatora pochodzila, i nie bylo z tego wyjscia."""
    korzen = tmp_path / "opt-cmdb-agent"
    (korzen / "cmdb_agent").mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))
    monkeypatch.setattr(upgrade, "KATALOG_INSTALACJI", korzen.resolve())

    sciezka, powod = upgrade.diagnoza_instalacji()
    assert sciezka == korzen.resolve()
    assert powod is None
    # Znacznik zostaje odtworzony, zeby nie ustalac tego przy kazdym przebiegu.
    assert (korzen / upgrade.ZNACZNIK_INSTALACJI).is_file()


def test_powod_odmowy_mowi_czego_brakuje(tmp_path, monkeypatch):
    """Panel dostawal jedno zdanie na wszystkie przypadki, wiec nie dalo sie
    odroznic braku znacznika od braku prawa zapisu."""
    korzen = tmp_path / "gdzies-indziej"
    (korzen / "cmdb_agent").mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))

    sciezka, powod = upgrade.diagnoza_instalacji()
    assert sciezka is None
    assert str(korzen) in powod and upgrade.ZNACZNIK_INSTALACJI in powod


def test_powod_trafia_do_zgloszenia_dla_serwera(tmp_path, monkeypatch):
    """To ten napis widac w panelu przy maszynie - musi mowic, co naprawic."""
    korzen = tmp_path / "gdzies-indziej"
    (korzen / "cmdb_agent").mkdir(parents=True)
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))
    monkeypatch.setattr(upgrade, "wlasny_plik", lambda: None)
    monkeypatch.setattr(upgrade, "sprawdz_oferte",
                        lambda *a, **k: {"version": "0.6.40+1", "kind": upgrade.RODZAJ_ZRODLA})

    zgloszone = {}
    monkeypatch.setattr(upgrade, "_zglos",
                        lambda client, token, wersja, stan, detal=None: zgloszone.update(
                            {"wersja": wersja, "stan": stan, "detal": detal}))

    class _Stan:
        agent_token = "t"

    assert upgrade.zastosuj(object(), _Stan(), object()) is None
    assert zgloszone["stan"] == "odrzucona"
    assert str(korzen) in zgloszone["detal"]


def test_katalog_tylko_do_odczytu_jest_nazwany_po_imieniu(tmp_path, monkeypatch):
    """Prawdziwa przyczyna odmowy na Linuksie: ProtectSystem=strict montuje
    katalog programu tylko do odczytu, wiec agent nie moze sie podmienic.
    Spod powloki katalog wyglada normalnie - komunikat musi wiec wskazac
    jednostke systemd, a nie prawa pliku."""
    korzen = tmp_path / "opt-cmdb-agent"
    (korzen / "cmdb_agent").mkdir(parents=True)
    (korzen / upgrade.ZNACZNIK_INSTALACJI).write_text("2026-08-31", encoding="utf-8")
    monkeypatch.setattr(upgrade, "__file__", str(korzen / "cmdb_agent" / "upgrade.py"))
    monkeypatch.setattr(upgrade.os, "access", lambda *a, **k: False)

    sciezka, powod = upgrade.diagnoza_instalacji()
    assert sciezka is None
    assert "ReadWritePaths" in powod
    assert str(korzen) in powod


def test_instalator_daje_usludze_zapis_do_katalogu_programu():
    """Bez tego wpisu samoaktualizacja nie ma prawa dzialac - i nie dzialala."""
    skrypt = (Path(__file__).resolve().parents[1]
              / "packaging" / "install-agent.sh").read_text(encoding="utf-8")
    wiersz = [w for w in skrypt.splitlines() if w.startswith("ReadWritePaths=")]
    assert wiersz, "jednostka musi wymieniac katalogi zapisywalne"
    assert "$KATALOG_PROGRAMU" in wiersz[0]
