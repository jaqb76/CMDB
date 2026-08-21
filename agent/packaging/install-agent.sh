#!/usr/bin/env bash
# Instalacja agenta CMDB na Linuksie (Ubuntu, Debian, Raspberry Pi OS).
#
# Agent nie ma zaleznosci poza biblioteka standardowa Pythona, wiec na
# Linuksie instalujemy go wprost ze zrodel - nie trzeba nic budowac. Ma to
# znaczenie na Raspberry Pi: PyInstaller nie potrafi budowac na inna
# architekture, wiec plik zbudowany na maszynie x86 i tak by sie tam nie
# uruchomil.
#
# Cykliczne uruchamianie zapewnia timer systemd, tak samo jak na Windows
# robi to Harmonogram zadan - agent jest jednorazowy, a nie rezydentny.
#
#   sudo ./install-agent.sh --server https://cmdb.firma.pl --token cmdb_ent_...
#
set -euo pipefail

KATALOG_PROGRAMU="/opt/cmdb-agent"
KATALOG_KONFIGURACJI="/etc/cmdb-agent"
KATALOG_DANYCH="/var/lib/cmdb-agent"
NAZWA_USLUGI="cmdb-agent"

SERWER=""
TOKEN=""
CA_BUNDLE=""
INTERWAL_GODZIN=4
ZBIERAJ_PROCESY="true"

blad() { echo "BLAD: $*" >&2; exit 1; }
krok() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)        SERWER="$2"; shift 2 ;;
        --token)         TOKEN="$2"; shift 2 ;;
        --ca-bundle)     CA_BUNDLE="$2"; shift 2 ;;
        --interval)      INTERWAL_GODZIN="$2"; shift 2 ;;
        --no-processes)  ZBIERAJ_PROCESY="false"; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) blad "nieznany parametr: $1" ;;
    esac
done

# --- warunki wstepne --------------------------------------------------------
[[ $EUID -eq 0 ]] || blad "skrypt wymaga uprawnien roota (uzyj sudo) - agent czyta dane systemowe i zaklada usluge"
[[ -n "$SERWER" ]] || blad "podaj --server https://..."
[[ -n "$TOKEN" ]] || blad "podaj --token cmdb_ent_..."
[[ "$SERWER" == https://* ]] || blad "adres serwera musi zaczynac sie od https:// - agent nie wysyla danych po nieszyfrowanym polaczeniu"
[[ "$TOKEN" == cmdb_ent_* ]] || blad "to nie wyglada na token rejestracyjny - powinien zaczynac sie od 'cmdb_ent_'"

command -v systemctl >/dev/null || blad "brak systemd - uruchamiaj agenta z crona: cmdb-agent run"
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || blad "brak python3 - zainstaluj: apt install python3"

WERSJA_PYTHONA="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || blad "wymagany Python 3.9 lub nowszy (jest $WERSJA_PYTHONA)"

ZRODLA="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -d "$ZRODLA/cmdb_agent" ]] || blad "nie znajduje pakietu cmdb_agent obok skryptu"

krok "Instalacja agenta CMDB (Python $WERSJA_PYTHONA, $(uname -m))"

# --- 1. pliki programu ------------------------------------------------------
systemctl stop "$NAZWA_USLUGI.timer" 2>/dev/null || true
install -d -m 0755 "$KATALOG_PROGRAMU"
rm -rf "${KATALOG_PROGRAMU:?}/cmdb_agent"
cp -r "$ZRODLA/cmdb_agent" "$KATALOG_PROGRAMU/"
find "$KATALOG_PROGRAMU" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "    program        : $KATALOG_PROGRAMU"

cat > /usr/local/bin/cmdb-agent <<LAUNCHER
#!/usr/bin/env bash
# Uruchamia agenta CMDB zainstalowanego ze zrodel.
exec "$PYTHON" -m cmdb_agent.main "\$@"
LAUNCHER
chmod 0755 /usr/local/bin/cmdb-agent
sed -i "1a export PYTHONPATH=\"$KATALOG_PROGRAMU\"" /usr/local/bin/cmdb-agent

# --- 2. konfiguracja --------------------------------------------------------
install -d -m 0700 "$KATALOG_KONFIGURACJI"
install -d -m 0700 "$KATALOG_DANYCH"
# Status czyta zwykly uzytkownik, wiec trafia do osobnego podkatalogu -
# w konfiguracji jest token firmowy, ktorego nikt poza rootem widziec nie ma.
install -d -m 0755 "$KATALOG_DANYCH/public"

KONFIGURACJA="$KATALOG_KONFIGURACJI/agent.conf"
{
    echo "{"
    echo "  \"server_url\": \"${SERWER%/}\","
    echo "  \"enrollment_token\": \"$TOKEN\","
    echo "  \"report_interval_seconds\": $((INTERWAL_GODZIN * 3600)),"
    echo "  \"collect_processes\": $ZBIERAJ_PROCESY,"
    [[ -n "$CA_BUNDLE" ]] && echo "  \"ca_bundle\": \"$CA_BUNDLE\","
    echo "  \"data_dir\": \"$KATALOG_DANYCH\","
    echo "  \"log_level\": \"INFO\""
    echo "}"
} > "$KONFIGURACJA"
chmod 0600 "$KONFIGURACJA"
echo "    konfiguracja   : $KONFIGURACJA (dostep: tylko root)"

# --- 3. rejestracja ---------------------------------------------------------
krok "Rejestruje maszyne w serwerze..."
if ! cmdb-agent --config "$KONFIGURACJA" enroll; then
    blad "rejestracja nie powiodla sie - sprawdz adres, token i zaufanie do certyfikatu (cmdb-agent doctor)"
fi

# --- 4. usluga i timer ------------------------------------------------------
cat > "/etc/systemd/system/$NAZWA_USLUGI.service" <<UNIT
[Unit]
Description=Agent inwentaryzacyjny CMDB
Documentation=https://github.com/jaqb76/CMDB
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cmdb-agent --config $KONFIGURACJA run
# Agent jest jednorazowy - zbiera dane, wysyla i konczy prace. Cykl zapewnia
# timer, dzieki czemu nic nie zajmuje pamieci miedzy przebiegami.
TimeoutStartSec=1800

# Agent czyta dane systemowe, ale niczego nie modyfikuje poza wlasnym katalogiem.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$KATALOG_DANYCH

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/$NAZWA_USLUGI.timer" <<TIMER
[Unit]
Description=Cykliczna inwentaryzacja CMDB

[Timer]
OnBootSec=3min
OnUnitActiveSec=${INTERWAL_GODZIN}h
# Losowe rozproszenie - przy wiekszej flocie maszyny nie uderzaja w serwer
# w tej samej sekundzie.
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now "$NAZWA_USLUGI.timer" >/dev/null
echo "    usluga         : $NAZWA_USLUGI.timer (co ${INTERWAL_GODZIN} h, przy starcie + 3 min)"

# --- 5. pierwszy przebieg ---------------------------------------------------
krok "Wysylam pierwszy raport..."
if ! cmdb-agent --config "$KONFIGURACJA" run; then
    echo "    UWAGA: pierwszy raport sie nie powiodl - timer sprobuje ponownie"
fi

echo
krok "Gotowe."
cmdb-agent --config "$KONFIGURACJA" status || true
echo
echo "Diagnostyka        : cmdb-agent doctor"
echo "Stan timera        : systemctl list-timers $NAZWA_USLUGI.timer"
echo "Dziennik przebiegow: journalctl -u $NAZWA_USLUGI.service"
