"""Wiazanie wykrytych urzadzen z maszynami, ktore juz sa w ewidencji.

Maszyna z agentem sama zglasza swoje interfejsy, wiec ewidencja wie, jakie ma
adresy sprzetowe i sieciowe. Skaner widzi to samo z drugiej strony. Jesli oba
opisy wskazuja na jedno, przypisanie jest wnioskiem, a nie zgadywaniem - i nie
ma powodu prosic o nie czlowieka.

Sila dowodu nie jest jednak jednakowa:

* **Adres MAC** przypisuje automatycznie. Jest unikalny w segmencie, a maszyna
  zglosila go o sobie sama.
* **Adres IP** przypisuje tylko wtedy, gdy MAC-a nie znamy - czyli gdy
  urzadzenie jest poza segmentem skanera i nie ma go w tablicy ARP.
* **Znany MAC, ktory do niczego nie pasuje**, zatrzymuje wiazanie po IP. To nie
  jest brak informacji, tylko informacja przeciwna: widzimy sprzet, ktorego
  zadna maszyna o sobie nie zglosila.

Wszedzie obowiazuje warunek **dokladnie jednej** pasujacej maszyny. Przypadki,
w ktorych jeden adres wskazuje kilka maszyn, zdarzaja sie naprawde: stacja
dokujaca uzycza MAC-a kolejnym laptopom, wirtualny MAC VRRP stoi na dwoch
routerach, adres bywa sklonowany. Wtedy decyduje czlowiek, bo automat nie ma
z czego wybrac.

Dlaczego IP nie wystarcza samo: DHCP przenosi adresy. Adres, ktory wczoraj
nalezal do serwera, dzis moze nalezec do drukarki. Zle przypisanie po IP nie
zglasza sie samo - od tego momentu podatnosci i raporty ladowalyby pod
niewlasciwa maszyna, a nikt nie mialby powodu tego sprawdzic.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset

# Skad wzielo sie przypisanie - do pokazania w panelu i w dzienniku audytu.
# Automat, ktorego nie widac, jest gorszy od reki: nie da sie go sprawdzic.
PO_MAC = "mac"
PO_IP = "ip"
RECZNIE = "reczne"


def indeks(db: Session, tenant_id: str) -> dict:
    """Adresy zgloszone przez same maszyny: MAC -> zasoby, IP -> zasoby.

    Zrodlem jest skrot raportu (``facts``), a nie pole ``primary_ip``: maszyna
    z kilkoma interfejsami ma kilka adresow i kazdy z nich jest jej wlasny.
    Wycofane zasoby pomijamy - wiazanie z czyms, co przestalo istniec,
    tworzyloby falszywy obraz.
    """
    wg_mac: dict[str, set[str]] = {}
    wg_ip: dict[str, set[str]] = {}
    wiersze = db.execute(
        select(Asset.id, Asset.facts).where(
            Asset.tenant_id == tenant_id, Asset.lifecycle == "aktywny"
        )
    ).all()
    for asset_id, facts in wiersze:
        dane = facts or {}
        for mac in dane.get("mac_addresses") or []:
            klucz = str(mac).strip().upper()
            if klucz:
                wg_mac.setdefault(klucz, set()).add(asset_id)
        for adres in dane.get("ip_addresses") or []:
            klucz = str(adres).strip()
            if klucz:
                wg_ip.setdefault(klucz, set()).add(asset_id)
    return {"mac": wg_mac, "ip": wg_ip}


def dopasuj(katalog: dict, mac: str, ip: str) -> tuple[str | None, str, str]:
    """Zwraca (asset_id, sposob, uzasadnienie).

    Uzasadnienie wraca takze przy odmowie - czlowiek, ktory oglada liste
    oczekujacych, ma widziec, dlaczego akurat to urzadzenie na niego czeka.
    """
    klucz_mac = (mac or "").strip().upper()
    if klucz_mac:
        kandydaci = katalog["mac"].get(klucz_mac) or set()
        if len(kandydaci) == 1:
            return next(iter(kandydaci)), PO_MAC, f"zgodny adres MAC {klucz_mac}"
        if len(kandydaci) > 1:
            return None, "", (
                f"adres MAC {klucz_mac} wskazuje {len(kandydaci)} maszyny - "
                "wybierz wlasciwa"
            )
        # Znamy MAC i nie nalezy do zadnej maszyny w ewidencji. To przeslanka
        # przeciwko wiazaniu po adresie IP, a nie brak informacji.
        return None, "", f"adres MAC {klucz_mac} nieznany w ewidencji"

    klucz_ip = (ip or "").strip()
    kandydaci = katalog["ip"].get(klucz_ip) or set()
    if len(kandydaci) == 1:
        return next(iter(kandydaci)), PO_IP, f"adres {klucz_ip} zgloszony przez sama maszyne"
    if len(kandydaci) > 1:
        return None, "", f"adres {klucz_ip} wskazuje {len(kandydaci)} maszyny - wybierz wlasciwa"
    return None, "", "brak maszyny o tym adresie"


def podpowiedzi(katalog: dict, mac: str, ip: str) -> set[str]:
    """Zasoby warte pokazania czlowiekowi, gdy automat sie wstrzymal."""
    wynik: set[str] = set()
    klucz_mac = (mac or "").strip().upper()
    if klucz_mac:
        wynik |= katalog["mac"].get(klucz_mac) or set()
    wynik |= katalog["ip"].get((ip or "").strip()) or set()
    return wynik
