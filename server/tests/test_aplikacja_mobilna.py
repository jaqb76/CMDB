"""Wydawanie aplikacji Android przez portal: wgranie, kod QR i pobranie.

Kod QR jest wygodą, ale adres w nim zawarty jest uprawnieniem - i to jego
pilnujemy tutaj: ma dzialac krotko, tylko dla konta, ktore ten kod ogladalo,
i nie ma byc publicznym odsylaczem do pliku.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from cmdb_server.db import SessionLocal
from cmdb_server.models import PortalUser
from cmdb_server.security import sign_download_key
from cmdb_server.services import mobilna

from .test_tenant_isolation import _extract_csrf, _login

HASLO = "haslo-portalu-2026"


@pytest.fixture
def magazyn(tmp_path, monkeypatch):
    """Wgrane pliki ladują w katalogu tymczasowym testu, nie w repozytorium."""
    from cmdb_server.config import get_settings

    monkeypatch.setattr(get_settings(), "release_dir", str(tmp_path))
    return tmp_path


def _apk(wersja_kodu: bytes = b"kod aplikacji") -> bytes:
    """Najmniejszy plik, ktory przechodzi sprawdzenie zawartosci APK."""
    bufor = io.BytesIO()
    with zipfile.ZipFile(bufor, "w") as archiwum:
        archiwum.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binarny manifest")
        archiwum.writestr("classes.dex", wersja_kodu)
        archiwum.writestr("resources.arsc", b"zasoby")
    return bufor.getvalue()


def _superadmin(email: str = "szef@cmdb.pl") -> str:
    from cmdb_server.security import hash_password

    with SessionLocal() as db:
        user = PortalUser(
            tenant_id=None, email=email, password_hash=hash_password(HASLO),
            role="admin", is_superadmin=True,
        )
        db.add(user)
        db.commit()
        return user.id


def _wgraj(client, nazwa: str = "CMDB-Mobile-0.5.0-debug.apk", wersja: str = "",
           tresc: bytes | None = None):
    csrf = _extract_csrf(client.get("/admin/wersje").text)
    return client.post(
        "/admin/mobilna",
        data={"csrf_token": csrf, "version": wersja},
        files={"plik": (nazwa, tresc if tresc is not None else _apk(),
                        "application/vnd.android.package-archive")},
        follow_redirects=False,
    )


# --- wgrywanie --------------------------------------------------------------

def test_wgrany_apk_wydaje_sie_z_wersja_z_nazwy_pliku(client, tenant_a, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)

    odpowiedz = _wgraj(client)

    assert odpowiedz.status_code == 303
    opis = mobilna.opis()
    assert opis is not None
    # Numeru nie da sie odczytac z bajtow APK, wiec bierzemy go z nazwy pliku.
    assert opis.wersja == "0.5.0"
    assert opis.nazwa_pobrania == "CMDB-Mobile-0.5.0.apk"
    assert opis.wgral == "szef@cmdb.pl"
    assert mobilna.plik().read_bytes().startswith(b"PK")


def test_plik_ktory_nie_jest_apk_nie_przechodzi(client, tenant_a, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)

    odpowiedz = _wgraj(client, tresc=b"to jest zwykly plik, a nie APK")

    assert odpowiedz.status_code == 400
    # Portal nie zostaje z plikiem, ktorego telefon i tak nie zainstaluje.
    assert mobilna.opis() is None
    assert not mobilna.plik().exists()


def test_apk_bez_numeru_w_nazwie_wymaga_podania_wersji(client, tenant_a, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)

    bez_numeru = _wgraj(client, nazwa="aplikacja.apk")
    z_polem = _wgraj(client, nazwa="aplikacja.apk", wersja="1.2.3")

    assert bez_numeru.status_code == 400
    assert z_polem.status_code == 303
    assert mobilna.opis().wersja == "1.2.3"


def test_zwykle_konto_nie_wgrywa_aplikacji(client, tenant_a, make_user, magazyn):
    make_user(tenant_a["id"], "admin@a.pl", HASLO)
    _login(client, "admin@a.pl", HASLO)

    odpowiedz = client.post(
        "/admin/mobilna",
        data={"csrf_token": "cokolwiek"},
        files={"plik": ("CMDB-Mobile-0.5.0.apk", _apk(), "application/octet-stream")},
        follow_redirects=False,
    )

    # Administrator firmy nie jest superadminem - panel odmawia wprost.
    assert odpowiedz.status_code == 403
    assert mobilna.opis() is None


# --- strona instalacji i kod QR --------------------------------------------

def test_strona_instalacji_pokazuje_kod_qr_z_kluczem(client, tenant_a, make_user, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    _wgraj(client)

    strona = client.get("/pobierz")

    assert strona.status_code == 200
    # Kod jest rysowany w kodzie strony, a nie dociagany osobnym zapytaniem.
    assert "<svg" in strona.text
    assert "/pobierz/aplikacja.apk?klucz=" in strona.text
    assert "0.5.0" in strona.text


def test_bez_wgranego_pliku_strona_nie_pokazuje_kodu(client, tenant_a, make_user, magazyn):
    make_user(tenant_a["id"], "admin@a.pl", HASLO)
    _login(client, "admin@a.pl", HASLO)

    strona = client.get("/pobierz")

    assert strona.status_code == 200
    assert "aplikacja.apk" not in strona.text
    assert "nie ma pliku aplikacji" in strona.text


# --- pobieranie -------------------------------------------------------------

def test_klucz_z_kodu_qr_wydaje_plik_bez_sesji_portalu(client, tenant_a, make_user, magazyn):
    user_id = make_user(tenant_a["id"], "admin@a.pl", HASLO)
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    _wgraj(client)
    client.cookies.clear()

    with SessionLocal() as db:
        klucz = sign_download_key(user_id, db.get(PortalUser, user_id).session_version)
    odpowiedz = client.get(f"/pobierz/aplikacja.apk?klucz={klucz}")

    assert odpowiedz.status_code == 200
    assert odpowiedz.headers["content-type"] == "application/vnd.android.package-archive"
    assert "CMDB-Mobile-0.5.0.apk" in odpowiedz.headers["content-disposition"]
    assert odpowiedz.content.startswith(b"PK")


def test_bez_klucza_i_bez_sesji_plik_jest_niedostepny(client, tenant_a, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    _wgraj(client)
    client.cookies.clear()

    bez_niczego = client.get("/pobierz/aplikacja.apk", follow_redirects=False)
    podrobiony = client.get("/pobierz/aplikacja.apk?klucz=zmyslony", follow_redirects=False)

    # Brak uprawnienia konczy sie logowaniem, a nie plikiem.
    assert bez_niczego.status_code == 303
    assert podrobiony.status_code == 303


def test_klucz_traci_waznosc_razem_z_sesjami_konta(client, tenant_a, make_user, magazyn):
    """Zmiana hasla podbija session_version - stary kod QR przestaje dzialac."""
    user_id = make_user(tenant_a["id"], "admin@a.pl", HASLO)
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    _wgraj(client)
    client.cookies.clear()

    with SessionLocal() as db:
        konto = db.get(PortalUser, user_id)
        klucz = sign_download_key(user_id, konto.session_version)
        konto.session_version += 1
        db.commit()

    odpowiedz = client.get(f"/pobierz/aplikacja.apk?klucz={klucz}", follow_redirects=False)

    assert odpowiedz.status_code == 303


def test_zdjecie_aplikacji_zabiera_plik_i_kod(client, tenant_a, magazyn):
    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    _wgraj(client)

    csrf = _extract_csrf(client.get("/admin/wersje").text)
    odpowiedz = client.post(
        "/admin/mobilna/usun", data={"csrf_token": csrf}, follow_redirects=False
    )

    assert odpowiedz.status_code == 303
    assert mobilna.opis() is None
    assert client.get("/pobierz/aplikacja.apk", follow_redirects=False).status_code == 404


# --- limit rozmiaru -----------------------------------------------------------
#
# APK wersji debug ma kilkadziesiat MB. Adres wgrywania nie mial wyjatku od
# limitu raportu (8 MB w aplikacji, 12 MB w nginx), wiec plik odbijal sie
# kodem 413 Request Entity Too Large, zanim portal go w ogole zobaczyl.

def test_apk_wiekszy_niz_limit_raportu_przechodzi(client, tenant_a, magazyn):
    import os

    from cmdb_server.config import get_settings

    _superadmin()
    _login(client, "szef@cmdb.pl", HASLO)
    duzy = _apk(os.urandom(get_settings().max_report_bytes + 2 * 1024 * 1024))
    assert len(duzy) > get_settings().max_report_bytes

    odpowiedz = _wgraj(client, tresc=duzy)

    assert odpowiedz.status_code == 303, odpowiedz.text
    assert mobilna.plik().stat().st_size == len(duzy)


@pytest.mark.parametrize("plik", ["cmdb.conf", "cmdb-dev.conf"])
def test_nginx_przepuszcza_duze_apk(plik):
    import re
    from pathlib import Path

    tresc = (Path(__file__).resolve().parents[2] / "deploy" / "nginx" / plik).read_text(encoding="utf-8")
    blok = tresc.split("location = /admin/mobilna {")[1].split("}")[0]
    assert int(re.search(r"client_max_body_size\s+(\d+)m", blok).group(1)) >= 64


# --- wyglad kodu QR -----------------------------------------------------------
#
# SVG mial sztywne width/height bez viewBox, a CSS dokladal padding przy
# box-sizing: border-box - przegladarka ucinala prawy i dolny brzeg kodu
# i zaden telefon go nie odczytal.

def test_kod_qr_skaluje_sie_i_ma_pelny_margines():
    import re
    from pathlib import Path

    from cmdb_server.services import qr

    kod = qr.svg("https://cmdb.example.pl/pobierz/aplikacja.apk?klucz=abc")
    otwarcie = kod.split(">", 1)[0]
    assert "viewBox=" in otwarcie
    assert not re.search(r'\s(width|height)="', otwarcie), "rozmiar ma ustalac CSS, nie atrybut"
    assert qr.MARGINES >= 4, "specyfikacja QR wymaga marginesu co najmniej 4 modulow"

    styl = (Path(__file__).resolve().parents[1] / "cmdb_server" / "static" / "app.css").read_text(encoding="utf-8")
    regula = styl.split(".apk-kod svg {")[1].split("}")[0]
    assert "padding" not in regula
    assert "height: auto" in regula
