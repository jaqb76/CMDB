"""Rozmowa z katalogiem AD/LDAP.

Ten modul wie, jak zapytac katalog, i nic poza tym: nie zaklada kont, nie
nadaje rol. Wynikiem jest ``WpisKatalogu`` - kim ktos jest i w jakich grupach.
Co z tego wynika w CMDB, decyduje services/tozsamosc.

Klienta tworzy ``fabryka_klienta``. Testy podstawiaja tam wlasna atrape, bo
prawdziwego AD w testach nie ma - a atrapa na poziomie calego klienta sprawdza
logike CMDB, nie biblioteke ldap3.

Zasady, ktorych nie wolno poluzowac:
  * puste haslo nigdy nie trafia do AD - bind z pustym haslem to w LDAP
    bind anonimowy i serwer odpowiada na niego SUKCESEM,
  * login wstawiany do filtra jest escapowany (wstrzykniecie do filtra),
  * polaczenie jest zawsze szyfrowane (LDAPS albo StartTLS) i certyfikat jest
    sprawdzany - haslo uzytkownika idzie tym polaczeniem jawnie.
"""
from __future__ import annotations

import logging
import ssl
import struct
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from ..models import KatalogTozsamosci
from . import sekrety

log = logging.getLogger(__name__)

LIMIT_POLACZENIA = 5
LIMIT_ODPOWIEDZI = 10

# Domyslny filtr dla Active Directory. {login} to czesc przed @ albo po "\\",
# {upn} - pelny adres. Szukamy po UPN, nazwie logowania i adresie e-mail, bo
# ludzie wpisuja kazda z tych postaci.
FILTR_DOMYSLNY = (
    "(&(objectClass=user)(objectCategory=person)"
    "(|(userPrincipalName={upn})(sAMAccountName={login})(mail={upn})))"
)

# userAccountControl: bit 2 = konto wylaczone.
UAC_WYLACZONE = 0x2


class BladKatalogu(RuntimeError):
    """Katalog nie odpowiada albo odrzuca konto serwisowe.

    To co innego niz zle haslo uzytkownika: przy awarii katalogu nie wolno
    wylaczac kont ani mowic uzytkownikowi, ze pomylil haslo.
    """


@dataclass(frozen=True)
class Grupa:
    identyfikator: str  # SID albo DN
    nazwa: str
    dn: str | None = None


@dataclass
class WpisKatalogu:
    guid: str
    dn: str
    login: str
    email: str | None
    nazwa: str | None
    aktywne: bool
    grupy: list[Grupa] = field(default_factory=list)

    def identyfikatory_grup(self) -> set[str]:
        """SID-y i DN-y (DN bez rozrozniania wielkosci liter)."""
        wynik: set[str] = set()
        for grupa in self.grupy:
            wynik.add(normalizuj_identyfikator(grupa.identyfikator))
            if grupa.dn:
                wynik.add(normalizuj_identyfikator(grupa.dn))
        return wynik


def normalizuj_identyfikator(wartosc: str) -> str:
    wartosc = (wartosc or "").strip()
    return wartosc.upper() if wartosc.upper().startswith("S-1-") else wartosc.lower()


class KlientKatalogu(Protocol):
    def sprawdz_haslo(self, login: str, haslo: str) -> WpisKatalogu | None: ...
    def znajdz(self, login: str) -> WpisKatalogu | None: ...
    def znajdz_po_guid(self, guid: str) -> WpisKatalogu | None: ...
    def szukaj_grup(self, fraza: str) -> list[Grupa]: ...
    def sprawdz_polaczenie(self) -> str: ...


# --- rozpoznawanie loginu ---------------------------------------------------

def rozbierz_login(login: str) -> tuple[str, str]:
    """(domena, nazwa) z "jan@abc.pl" albo "ABC\\jan". Domena malymi literami.

    Domena NetBIOS dostaje na koncu odwrotny ukosnik ("abc\\"), zeby nie
    mylila sie z domena pocztowa o tej samej nazwie.
    """
    login = (login or "").strip()
    if "\\" in login:
        domena, _, nazwa = login.partition("\\")
        return domena.strip().lower() + "\\", nazwa.strip()
    if "@" in login:
        nazwa, _, domena = login.rpartition("@")
        return domena.strip().lower(), nazwa.strip()
    return "", login


def normalizuj_domene(wpis: str) -> str:
    wpis = (wpis or "").strip().lower()
    if wpis.endswith("\\\\"):
        wpis = wpis[:-1]
    return wpis.lstrip("@")


# --- konwersje atrybutow AD -------------------------------------------------

def sid_z_bajtow(dane: bytes) -> str:
    """Binarny objectSid -> "S-1-5-21-...". Format opisany w MS-DTYP 2.4.2."""
    if len(dane) < 8:
        raise ValueError("za krotki SID")
    rewizja = dane[0]
    ile = dane[1]
    autorytet = int.from_bytes(dane[2:8], "big")
    if len(dane) < 8 + 4 * ile:
        raise ValueError("uciety SID")
    podrzedne = struct.unpack("<" + "I" * ile, dane[8:8 + 4 * ile])
    return "S-" + "-".join([str(rewizja), str(autorytet), *map(str, podrzedne)])


def sid_do_filtra(sid: str) -> str:
    """"S-1-5-..." -> postac bajtowa do filtra LDAP (\\01\\05...)."""
    czesci = sid.split("-")
    if len(czesci) < 3 or czesci[0].upper() != "S":
        raise ValueError("to nie jest SID")
    rewizja, autorytet, *podrzedne = (int(c) for c in czesci[1:])
    dane = bytes([rewizja, len(podrzedne)]) + autorytet.to_bytes(6, "big")
    dane += struct.pack("<" + "I" * len(podrzedne), *podrzedne)
    return "".join(f"\\{b:02x}" for b in dane)


def guid_z_bajtow(dane: bytes) -> str:
    return str(uuid.UUID(bytes_le=dane))


def cn_z_dn(dn: str) -> str:
    pierwszy = (dn or "").split(",", 1)[0]
    return pierwszy.split("=", 1)[1] if "=" in pierwszy else dn


# --- klient ldap3 -----------------------------------------------------------

def _escape(wartosc: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(wartosc)


class KlientLdap3:
    """Prawdziwy klient - biblioteka ldap3, czysty Python."""

    def __init__(self, katalog: KatalogTozsamosci):
        self.katalog = katalog
        self.haslo_serwisowe = sekrety.odszyfruj(katalog.bind_haslo_szyfr) or ""
        domeny = [normalizuj_domene(d) for d in (katalog.domeny or [])]
        self.domena_pocztowa = next((d for d in domeny if not d.endswith("\\")), "")

    # -- polaczenie --

    def _pula(self):
        from ldap3 import FIRST, NONE, Server, ServerPool, Tls

        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            version=ssl.PROTOCOL_TLS_CLIENT,
            ca_certs_data=self.katalog.certyfikat_ca or None,
        )
        serwery = []
        for adres in sprawdz_adresy(self.katalog.serwery, self.katalog.starttls):
            czesci = urlsplit(adres)
            ldaps = czesci.scheme == "ldaps"
            serwery.append(Server(
                czesci.hostname, port=czesci.port or (636 if ldaps else 389),
                use_ssl=ldaps, tls=tls, get_info=NONE, connect_timeout=LIMIT_POLACZENIA,
            ))
        return ServerPool(serwery, FIRST, active=1, exhaust=True)

    def _polacz(self, uzytkownik: str, haslo: str):
        """Otwarte i zwiazane polaczenie albo None, gdy dane sa zle.

        BladKatalogu, gdy serwer nie odpowiada - to nie jest zle haslo.
        """
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException

        if not haslo:
            # Bind z pustym haslem to bind anonimowy - AD odpowiada sukcesem.
            return None
        try:
            polaczenie = Connection(
                self._pula(), user=uzytkownik, password=haslo, read_only=True,
                receive_timeout=LIMIT_ODPOWIEDZI, raise_exceptions=False,
            )
            polaczenie.open()
            if self.katalog.starttls and not polaczenie.server.ssl:
                if not polaczenie.start_tls():
                    raise BladKatalogu("serwer odrzucil StartTLS")
            if not polaczenie.bind():
                wynik = polaczenie.result or {}
                # 49 = invalidCredentials; wszystko inne to problem katalogu.
                if wynik.get("result") == 49:
                    polaczenie.unbind()
                    return None
                raise BladKatalogu(f"bind nieudany: {wynik.get('description')} {wynik.get('message')}")
            return polaczenie
        except LDAPException as blad:
            raise BladKatalogu(f"{type(blad).__name__}: {blad}") from blad

    def _serwisowe(self):
        polaczenie = self._polacz(self.katalog.bind_dn, self.haslo_serwisowe)
        if polaczenie is None:
            raise BladKatalogu("katalog odrzucil konto serwisowe - sprawdz login i haslo")
        return polaczenie

    # -- odczyt --

    ATRYBUTY = ["objectGUID", "sAMAccountName", "userPrincipalName", "mail",
                "displayName", "cn", "userAccountControl", "memberOf", "uid"]

    def _filtr(self, login: str) -> str:
        domena, nazwa = rozbierz_login(login)
        if domena and not domena.endswith("\\"):
            upn = f"{nazwa}@{domena}"
        else:
            upn = f"{nazwa}@{self.domena_pocztowa}" if self.domena_pocztowa else nazwa
        wzor = self.katalog.filtr_uzytkownika or FILTR_DOMYSLNY
        return wzor.replace("{login}", _escape(nazwa)).replace("{upn}", _escape(upn))

    def _szukaj_jednego(self, polaczenie, filtr: str) -> WpisKatalogu | None:
        from ldap3 import SUBTREE

        polaczenie.search(self.katalog.base_dn, filtr, SUBTREE,
                          attributes=self.ATRYBUTY, size_limit=2)
        wyniki = [w for w in polaczenie.response or [] if w.get("type") == "searchResEntry"]
        if len(wyniki) != 1:
            # Dwa trafienia to niejednoznaczny login - nie zgadujemy, kto to.
            return None
        return self._wpis(polaczenie, wyniki[0])

    def _wpis(self, polaczenie, wynik: dict) -> WpisKatalogu:
        from ldap3 import BASE

        surowe = wynik.get("raw_attributes", {})
        atr = wynik.get("attributes", {})

        def pierwszy(nazwa):
            wartosc = atr.get(nazwa)
            if isinstance(wartosc, list):
                wartosc = wartosc[0] if wartosc else None
            if isinstance(wartosc, bytes):
                wartosc = wartosc.decode("utf-8", "replace")
            return str(wartosc) if wartosc not in (None, "") else None

        guid_surowy = (surowe.get("objectGUID") or [b""])[0]
        guid = guid_z_bajtow(guid_surowy) if len(guid_surowy) == 16 else wynik["dn"].lower()
        uac = int(pierwszy("userAccountControl") or 0)

        grupy: list[Grupa] = []
        widziane: set[str] = set()
        # tokenGroups: wszystkie grupy, takze zagniezdzone, jako SID-y. To
        # atrybut wyliczany - AD podaje go tylko przy zapytaniu BASE o obiekt.
        polaczenie.search(wynik["dn"], "(objectClass=*)", BASE, attributes=["tokenGroups"])
        for odp in polaczenie.response or []:
            for surowy in odp.get("raw_attributes", {}).get("tokenGroups", []) or []:
                try:
                    sid = sid_z_bajtow(surowy)
                except ValueError:
                    continue
                if sid not in widziane:
                    widziane.add(sid)
                    grupy.append(Grupa(identyfikator=sid, nazwa=sid))
        # memberOf: bezposrednie grupy po DN - jedyne zrodlo w LDAP innym niz AD.
        for dn in atr.get("memberOf") or []:
            dn = dn.decode() if isinstance(dn, bytes) else str(dn)
            grupy.append(Grupa(identyfikator=dn, nazwa=cn_z_dn(dn), dn=dn))

        return WpisKatalogu(
            guid=guid,
            dn=wynik["dn"],
            login=pierwszy("sAMAccountName") or pierwszy("uid") or pierwszy("userPrincipalName") or "",
            email=(pierwszy("mail") or pierwszy("userPrincipalName") or "").lower() or None,
            nazwa=pierwszy("displayName") or pierwszy("cn"),
            aktywne=not (uac & UAC_WYLACZONE),
            grupy=grupy,
        )

    # -- interfejs --

    def sprawdz_haslo(self, login: str, haslo: str) -> WpisKatalogu | None:
        if not haslo:
            return None
        polaczenie = self._serwisowe()
        try:
            wpis = self._szukaj_jednego(polaczenie, self._filtr(login))
        finally:
            polaczenie.unbind()
        if wpis is None:
            return None
        jako_uzytkownik = self._polacz(wpis.dn, haslo)
        if jako_uzytkownik is None:
            return None
        jako_uzytkownik.unbind()
        return wpis

    def znajdz(self, login: str) -> WpisKatalogu | None:
        polaczenie = self._serwisowe()
        try:
            return self._szukaj_jednego(polaczenie, self._filtr(login))
        finally:
            polaczenie.unbind()

    def znajdz_po_guid(self, guid: str) -> WpisKatalogu | None:
        try:
            bajty = uuid.UUID(guid).bytes_le
        except ValueError:
            return None
        filtr = "(objectGUID=" + "".join(f"\\{b:02x}" for b in bajty) + ")"
        polaczenie = self._serwisowe()
        try:
            return self._szukaj_jednego(polaczenie, filtr)
        finally:
            polaczenie.unbind()

    def nazwy_grup(self, sidy: list[str]) -> dict[str, tuple[str, str]]:
        """SID -> (nazwa, DN). Tylko do pokazania w panelu."""
        from ldap3 import SUBTREE

        if not sidy:
            return {}
        polaczenie = self._serwisowe()
        wynik = {}
        try:
            for poczatek in range(0, len(sidy), 50):
                czesc = sidy[poczatek:poczatek + 50]
                filtr = "(|" + "".join(f"(objectSid={sid_do_filtra(s)})" for s in czesc) + ")"
                polaczenie.search(self.katalog.base_dn, filtr, SUBTREE,
                                  attributes=["cn", "objectSid"])
                for odp in polaczenie.response or []:
                    if odp.get("type") != "searchResEntry":
                        continue
                    surowy = (odp.get("raw_attributes", {}).get("objectSid") or [b""])[0]
                    try:
                        sid = sid_z_bajtow(surowy)
                    except ValueError:
                        continue
                    wynik[sid] = (cn_z_dn(odp["dn"]), odp["dn"])
        finally:
            polaczenie.unbind()
        return wynik

    def szukaj_grup(self, fraza: str) -> list[Grupa]:
        from ldap3 import SUBTREE

        fraza = _escape(fraza.strip())
        filtr = (f"(&(|(objectClass=group)(objectClass=groupOfNames)(objectClass=posixGroup))"
                 f"(|(cn=*{fraza}*)(sAMAccountName=*{fraza}*)))")
        polaczenie = self._serwisowe()
        try:
            polaczenie.search(self.katalog.base_dn, filtr, SUBTREE,
                              attributes=["cn", "objectSid"], size_limit=50)
            grupy = []
            for odp in polaczenie.response or []:
                if odp.get("type") != "searchResEntry":
                    continue
                surowy = (odp.get("raw_attributes", {}).get("objectSid") or [b""])[0]
                try:
                    identyfikator = sid_z_bajtow(surowy)
                except ValueError:
                    identyfikator = odp["dn"]
                grupy.append(Grupa(identyfikator=identyfikator, nazwa=cn_z_dn(odp["dn"]),
                                   dn=odp["dn"]))
            return sorted(grupy, key=lambda g: g.nazwa.lower())
        finally:
            polaczenie.unbind()

    def sprawdz_polaczenie(self) -> str:
        from ldap3 import SUBTREE

        polaczenie = self._serwisowe()
        try:
            serwer = polaczenie.server.host
            polaczenie.search(self.katalog.base_dn, "(objectClass=user)", SUBTREE,
                              attributes=["cn"], size_limit=1)
            return f"Połączono z {serwer}, konto serwisowe przyjęte, baza wyszukiwania dostępna."
        finally:
            polaczenie.unbind()


def sprawdz_adresy(serwery: str, starttls: bool) -> list[str]:
    """Lista adresow serwerow - albo ValueError, gdy ktorys nie jest szyfrowany."""
    adresy = [a.strip() for a in (serwery or "").replace("\n", ",").split(",") if a.strip()]
    if not adresy:
        raise ValueError("podaj co najmniej jeden serwer")
    for adres in adresy:
        czesci = urlsplit(adres)
        if czesci.scheme not in ("ldap", "ldaps") or not czesci.hostname:
            raise ValueError(f"adres {adres!r} musi mieć postać ldaps://serwer:636")
        if czesci.scheme == "ldap" and not starttls:
            raise ValueError(
                f"{adres}: połączenie bez szyfrowania - użyj ldaps:// albo włącz StartTLS. "
                "Hasła użytkowników szłyby siecią jawnym tekstem."
            )
    return adresy


def _klient_domyslny(katalog: KatalogTozsamosci) -> KlientKatalogu:
    return KlientLdap3(katalog)


# Podmieniane w testach.
fabryka_klienta = _klient_domyslny


def klient(katalog: KatalogTozsamosci) -> KlientKatalogu:
    return fabryka_klienta(katalog)
