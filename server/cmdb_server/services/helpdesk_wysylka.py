"""Wysylka helpdesku: odpowiedzi do klienta i potwierdzenia przyjecia.

Wychodzi wszystko z jednej skrzynki - tej samej, ktora odbiera - wiec
odpowiedz klienta wraca tam, skad wyszla, i trafia do swojego zgloszenia.
Za watkowanie odpowiadaja naglowki: zapisujemy Message-ID kazdej wyslanej
wiadomosci, bo klient przysle go z powrotem w In-Reply-To.

Modul rozni sie od services/poczta.py adresatem i wlascicielem skrzynki:
tamten wysyla raporty firmy jej wlasnym serwerem, ten pisze do klientow
z adresu helpdesku. Wspolne jest tylko szyfrowanie hasla (services/sekrety).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    WPIS_DO_KLIENTA,
    HelpdeskUstawienia,
    PortalUser,
    WpisZgloszenia,
    Zgloszenie,
    utcnow,
)
from . import helpdesk, sekrety

log = logging.getLogger(__name__)

LIMIT_SEKUND = 30

# Te teksty czyta klient, wiec z ogonkami. Operator moze je nadpisac w ustawieniach.
DOMYSLNE_POTWIERDZENIE = (
    "Dzień dobry,\n\n"
    "przyjęliśmy zgłoszenie {numer} - „{temat}”.\n"
    "Odpowiadając na tego maila, dopisze Pan/Pani wiadomość do tego samego zgłoszenia.\n"
)

DOMYSLNE_ZAMKNIECIE = (
    "Dzień dobry,\n\n"
    "zgłoszenie {numer} - „{temat}” zostało zakończone.\n"
    "Jeśli sprawa nie jest dla Pana/Pani załatwiona, wystarczy odpowiedzieć "
    "na tego maila - zgłoszenie wróci do realizacji pod tym samym numerem.\n"
)


class BladWysylki(RuntimeError):
    """Nie udalo sie wyslac wiadomosci."""


def ustawienia(db: Session) -> HelpdeskUstawienia | None:
    wpis = db.get(HelpdeskUstawienia, "helpdesk")
    return wpis if wpis is not None and wpis.aktywne else None


def _polaczenie(konfiguracja: HelpdeskUstawienia):
    kontekst = ssl.create_default_context()
    if konfiguracja.smtp_szyfrowanie == "ssl":
        return smtplib.SMTP_SSL(
            konfiguracja.smtp_host, konfiguracja.smtp_port,
            timeout=LIMIT_SEKUND, context=kontekst,
        )
    polaczenie = smtplib.SMTP(
        konfiguracja.smtp_host, konfiguracja.smtp_port, timeout=LIMIT_SEKUND
    )
    if konfiguracja.smtp_szyfrowanie == "starttls":
        polaczenie.starttls(context=kontekst)
    return polaczenie


def temat_odpowiedzi(zgloszenie: Zgloszenie) -> str:
    """Temat z numerem zgloszenia - to po nim wraca odpowiedz bez naglowkow.

    Znacznika nie dublujemy: temat, w ktorym numer juz jest, przechodzi bez
    zmian, inaczej po kilku turach wygladalby jak "Re: [BON-1] Re: [BON-1] ...".
    """
    if helpdesk.rozpoznaj_numer(zgloszenie.temat) == zgloszenie.numer_pelny:
        podstawa = zgloszenie.temat
    else:
        podstawa = f"[{zgloszenie.numer_pelny}] {zgloszenie.temat}"
    return podstawa if podstawa.lower().startswith("re:") else f"Re: {podstawa}"


def adresaci(db: Session, zgloszenie: Zgloszenie, konfiguracja: HelpdeskUstawienia):
    """Do kogo idzie odpowiedz: zglaszajacy plus kopia z jego ostatniego maila.

    Klient czesto pisze z przelozonym w DW i odpowiedz ma trafic do tych samych
    osob - polowa rozmowy prowadzona bez nich konczy sie pytaniem "a co z tym
    zgloszeniem". Bierzemy kopie z OSTATNIEJ wiadomosci klienta, bo lista
    zmienia sie w trakcie sprawy.

    Z kopii wypada adres helpdesku (wroci do nas wlasna wiadomosc) i sam
    zglaszajacy (dostalby ja dwa razy).
    """
    do = [zgloszenie.zglaszajacy_email]
    ostatnia = helpdesk.ostatnia_od_klienta(db, zgloszenie.id)
    kopia = list(ostatnia.dw or []) if ostatnia is not None else []

    wykluczone = {zgloszenie.zglaszajacy_email.lower()}
    if konfiguracja.nadawca:
        wykluczone.add(konfiguracja.nadawca.lower())
    dw = [adres for adres in dict.fromkeys(kopia) if adres.lower() not in wykluczone]
    return do, dw


def _stopka(konfiguracja: HelpdeskUstawienia, tresc: str) -> str:
    if not konfiguracja.stopka:
        return tresc
    return f"{tresc.rstrip()}\n\n--\n{konfiguracja.stopka}\n"


def _wyslij(konfiguracja: HelpdeskUstawienia, wiadomosc: EmailMessage) -> None:
    haslo = sekrety.odszyfruj(konfiguracja.smtp_haslo_szyfr)
    # Sprawdzamy PRZED polaczeniem: niedostepny serwer przykrylby prawdziwa
    # przyczyne bledem sieci, a nieczytelne haslo znaczy zwykle, ze zmienil
    # sie CMDB_SECRET_KEY.
    if konfiguracja.smtp_uzytkownik and konfiguracja.smtp_haslo_szyfr and not haslo:
        raise BladWysylki("nie moge odczytac zapisanego hasla SMTP - wpisz je ponownie")

    try:
        with _polaczenie(konfiguracja) as polaczenie:
            if konfiguracja.smtp_uzytkownik and haslo:
                polaczenie.login(konfiguracja.smtp_uzytkownik, haslo)
            polaczenie.send_message(wiadomosc)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as blad:
        raise BladWysylki(f"{type(blad).__name__}: {blad}") from blad


def wyslij_wpis(db: Session, zgloszenie: Zgloszenie, wpis: WpisZgloszenia) -> None:
    """Wysyla wpis rodzaju "do klienta" i odnotowuje wynik przy nim.

    Komentarz wewnetrzny nie ma prawa tu trafic - i nie polegamy na tym, ze
    kazdy widok o tym pamieta: rodzaj wpisu sprawdzamy tutaj, w jedynym
    miejscu, przez ktore tresc wychodzi na zewnatrz.
    """
    if wpis.rodzaj != WPIS_DO_KLIENTA:
        raise BladWysylki(f"wpis rodzaju '{wpis.rodzaj}' nie wychodzi do klienta")

    konfiguracja = ustawienia(db)
    if konfiguracja is None or not konfiguracja.smtp_host or not konfiguracja.nadawca:
        raise BladWysylki("skrzynka helpdesku nie jest skonfigurowana")

    do, dw = adresaci(db, zgloszenie, konfiguracja)
    ostatnia = helpdesk.ostatnia_od_klienta(db, zgloszenie.id)

    wiadomosc = EmailMessage()
    wiadomosc["Subject"] = temat_odpowiedzi(zgloszenie)
    wiadomosc["From"] = formataddr((konfiguracja.nazwa_nadawcy or "Helpdesk", konfiguracja.nadawca))
    wiadomosc["To"] = ", ".join(do)
    if dw:
        wiadomosc["Cc"] = ", ".join(dw)
    # Wlasny Message-ID zapisujemy przy wpisie: klient odpowie na te wiadomosc
    # i przysle go z powrotem w In-Reply-To - to jest najpewniejsza droga
    # powrotu do tego zgloszenia.
    wiadomosc["Message-ID"] = make_msgid(domain=helpdesk.domena_adresu(konfiguracja.nadawca) or None)
    if ostatnia is not None and ostatnia.message_id:
        wiadomosc["In-Reply-To"] = ostatnia.message_id
        wiadomosc["References"] = ostatnia.message_id
    wiadomosc.set_content(_stopka(konfiguracja, wpis.tresc))

    try:
        _wyslij(konfiguracja, wiadomosc)
    except BladWysylki as blad:
        # Blad zostaje przy wpisie, a nie tylko w dzienniku: technik ma
        # zobaczyc w watku, ze jego odpowiedz nie wyszla.
        wpis.blad_wysylki = str(blad)
        konfiguracja.ostatni_blad = str(blad)
        raise

    wpis.message_id = str(wiadomosc["Message-ID"])
    wpis.dw = dw or None
    wpis.wyslano_o = utcnow()
    wpis.blad_wysylki = None
    konfiguracja.ostatni_blad = None
    log.info("helpdesk: odpowiedz w %s poszla do %d adresatow", zgloszenie.numer_pelny, len(do) + len(dw))


def odpowiedz_klientowi(
    db: Session, zgloszenie: Zgloszenie, tresc: str, autor: PortalUser
) -> WpisZgloszenia:
    """Zapisuje odpowiedz w watku i wysyla ja. Zapis jest pierwszy.

    Gdyby najpierw szla wysylka, awaria SMTP kasowalaby napisana odpowiedz -
    a tresc, ktora technik wlasnie sformulowal, jest cenniejsza od jednej
    proby polaczenia. Nieudana wysylka zostaje w watku z bledem i da sie ja
    powtorzyc.
    """
    wpis = helpdesk.dopisz_wiadomosc(
        db, zgloszenie, rodzaj=WPIS_DO_KLIENTA, tresc=tresc, autor=autor,
    )
    wyslij_wpis(db, zgloszenie, wpis)
    return wpis


def potwierdzenie_nalezne(
    db: Session, zgloszenie: Zgloszenie, *, automat: bool, tresc_maila: str
) -> bool:
    """Czy nowemu zgloszeniu nalezy sie automatyczne potwierdzenie.

    Warunki sa te uzgodnione przy projekcie: wlaczone w ustawieniach, nowe
    zgloszenie z dozwolonej domeny (skoro zgloszenie powstalo, domena byla
    znana) i wiadomosc, ktora jest zgloszeniem - czyli ma tresc. Autoodpowiedz
    nie dostaje potwierdzenia, bo dwa automaty pisza wtedy do siebie w kolko.
    """
    konfiguracja = ustawienia(db)
    if konfiguracja is None or not konfiguracja.potwierdzenie_wlaczone:
        return False
    if automat:
        return False
    return bool((tresc_maila or "").strip())


def _z_szablonu(zgloszenie: Zgloszenie, szablon: str, dopisek: str = "") -> str:
    """Szablon z podstawionym numerem i tematem, opcjonalnie z dopiskiem technika."""
    tresc = szablon.replace("{numer}", zgloszenie.numer_pelny).replace("{temat}", zgloszenie.temat)
    if dopisek.strip():
        tresc = f"{tresc.rstrip()}\n\n{dopisek.strip()}\n"
    return tresc


def _wiadomosc_automatyczna(
    db: Session, zgloszenie: Zgloszenie, tresc: str, autor_nazwa: str, co: str
) -> WpisZgloszenia:
    """Wiadomosc wychodzaca sama, zapisana w watku jak kazda inna.

    Klient ma zobaczyc numer, a technik - co dokladnie klient dostal. Wpis
    systemowy nie wystarczy: to jest wiadomosc, ktora wyszla na zewnatrz.

    Blad wysylki zostaje przy wpisie (robi to wyslij_wpis) i w dzienniku, ale
    nie leci wyzej: te maile towarzysza czynnosci, ktora wlasnie sie udala -
    zgloszenie zostalo zalozone albo zamkniete - i nie moga jej przewrocic.
    """
    wpis = helpdesk.dopisz_wiadomosc(
        db, zgloszenie, rodzaj=WPIS_DO_KLIENTA, tresc=tresc, autor_nazwa=autor_nazwa,
    )
    try:
        wyslij_wpis(db, zgloszenie, wpis)
    except BladWysylki as blad:
        log.warning("helpdesk: %s %s nie poszlo: %s", co, zgloszenie.numer_pelny, blad)
    return wpis


def wyslij_potwierdzenie(db: Session, zgloszenie: Zgloszenie) -> WpisZgloszenia | None:
    """Potwierdzenie przyjecia zgloszenia."""
    konfiguracja = ustawienia(db)
    if konfiguracja is None:
        return None

    return _wiadomosc_automatyczna(
        db, zgloszenie,
        _z_szablonu(zgloszenie, konfiguracja.potwierdzenie_tresc or DOMYSLNE_POTWIERDZENIE),
        autor_nazwa="Helpdesk (potwierdzenie automatyczne)",
        co="potwierdzenie",
    )


def zamkniecie_nalezne(db: Session) -> bool:
    """Czy zamkniecie sprawy ma pojsc mailem do klienta."""
    konfiguracja = ustawienia(db)
    return konfiguracja is not None and konfiguracja.zamkniecie_wlaczone


def wyslij_zamkniecie(
    db: Session, zgloszenie: Zgloszenie, podsumowanie: str = ""
) -> WpisZgloszenia | None:
    """Wiadomosc o zakonczeniu sprawy, z opcjonalnym podsumowaniem technika.

    Zamkniete zgloszenie znika z tablicy, wiec bez tego maila klient dowiaduje
    sie o koncu sprawy dopiero wtedy, gdy sam zapyta. Podsumowanie jest
    dopisywane pod szablonem: szablon zalatwia forme, technik - tresc.
    """
    konfiguracja = ustawienia(db)
    if konfiguracja is None or not konfiguracja.zamkniecie_wlaczone:
        return None

    return _wiadomosc_automatyczna(
        db, zgloszenie,
        _z_szablonu(
            zgloszenie, konfiguracja.zamkniecie_tresc or DOMYSLNE_ZAMKNIECIE, podsumowanie
        ),
        autor_nazwa="Helpdesk (zakończenie zgłoszenia)",
        co="zamkniecie",
    )


def sprawdz(db: Session, adres: str) -> None:
    """Wiadomosc probna - jedyny pewny sposob sprawdzenia ustawien SMTP."""
    konfiguracja = ustawienia(db)
    if konfiguracja is None or not konfiguracja.smtp_host or not konfiguracja.nadawca:
        raise BladWysylki("skrzynka helpdesku nie jest skonfigurowana")

    wiadomosc = EmailMessage()
    wiadomosc["Subject"] = "Helpdesk: wiadomość próbna"
    wiadomosc["From"] = formataddr((konfiguracja.nazwa_nadawcy or "Helpdesk", konfiguracja.nadawca))
    wiadomosc["To"] = adres
    wiadomosc["Message-ID"] = make_msgid(domain=helpdesk.domena_adresu(konfiguracja.nadawca) or None)
    wiadomosc.set_content(
        "Ustawienia wysyłki helpdesku działają. Ta wiadomość została wysłana z panelu CMDB."
    )
    _wyslij(konfiguracja, wiadomosc)


def niewyslane(db: Session, zgloszenie_id: str) -> list[WpisZgloszenia]:
    """Wpisy do klienta, ktore nie wyszly - do powtorzenia proby."""
    return list(db.execute(
        select(WpisZgloszenia).where(
            WpisZgloszenia.zgloszenie_id == zgloszenie_id,
            WpisZgloszenia.rodzaj == WPIS_DO_KLIENTA,
            WpisZgloszenia.wyslano_o.is_(None),
        ).order_by(WpisZgloszenia.utworzono)
    ).scalars())
