#!/usr/bin/env bash
#
# Odinstalowuje agenta CMDB z maszyny linuksowej.
#
#   sudo ./uninstall-agent.sh              # zostawia konfiguracje i dane
#   sudo ./uninstall-agent.sh --wszystko   # kasuje takze konfiguracje i dane
#
# Domyslnie zostawiamy konfiguracje i stan: najczestszym powodem odinstalowania
# jest przeinstalowanie systemu albo naprawa, a nie pozegnanie z maszyna. Przy
# ponownej instalacji zachowana rejestracja oszczedza tokenu i nie tworzy
# drugiego wpisu w ewidencji.
#
# Odinstalowanie agenta NIE usuwa maszyny z panelu - to dwie rozne rzeczy.
# W panelu maszyne wycofuje sie osobno; jej historia raportow zostaje.
set -euo pipefail

KATALOG_PROGRAMU="/opt/cmdb-agent"
KATALOG_KONFIGURACJI="/etc/cmdb-agent"
KATALOG_DANYCH="/var/lib/cmdb-agent"
NAZWA_USLUGI="cmdb-agent"

WSZYSTKO=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wszystko|--all) WSZYSTKO=1; shift ;;
        -h|--help) sed -n '3,13p' "$0"; exit 0 ;;
        *) echo "nieznany parametr: $1" >&2; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "uruchom przez sudo - usluga systemd nalezy do roota" >&2; exit 1; }

krok() { echo "==> $1"; }

krok "Zatrzymuje usluge"
# Timer przed usluga: zatrzymana usluga bez zatrzymanego timera wstaje
# z powrotem przy najblizszym wyzwoleniu.
for jednostka in "$NAZWA_USLUGI.timer" "$NAZWA_USLUGI.service"; do
    systemctl stop "$jednostka" 2>/dev/null || true
    systemctl disable "$jednostka" 2>/dev/null || true
done

krok "Usuwam jednostki systemd"
rm -f "/etc/systemd/system/$NAZWA_USLUGI.timer" \
      "/etc/systemd/system/$NAZWA_USLUGI.service"
systemctl daemon-reload
systemctl reset-failed "$NAZWA_USLUGI.service" 2>/dev/null || true

krok "Usuwam program"
rm -rf "${KATALOG_PROGRAMU:?}"
rm -f /usr/local/bin/cmdb-agent

if [[ $WSZYSTKO -eq 1 ]]; then
    krok "Usuwam konfiguracje i dane"
    # Konfiguracja zawiera token firmowy, a dane - poswiadczenie tej maszyny.
    rm -rf "${KATALOG_KONFIGURACJI:?}" "${KATALOG_DANYCH:?}"
    echo
    echo "Agent usuniety w calosci."
    echo "Ponowna instalacja bedzie wymagala tokenu rejestracyjnego z panelu."
else
    echo
    echo "Agent usuniety. Zostawione:"
    echo "  konfiguracja : $KATALOG_KONFIGURACJI"
    echo "  dane         : $KATALOG_DANYCH"
    echo
    echo "Dzieki temu ponowna instalacja odtworzy te sama rejestracje - bez"
    echo "tokenu i bez drugiego wpisu w ewidencji. Aby skasowac takze je:"
    echo "  sudo $0 --wszystko"
fi

echo
echo "Maszyna zostaje w panelu razem z historia raportow. Jesli ma zniknac"
echo "z list, wycofaj ja w panelu - to osobna decyzja."
