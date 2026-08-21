#!/usr/bin/env bash
# Instalacja agenta CMDB jednym poleceniem, bez klonowania repozytorium.
#
#   curl -fsSL https://serwer/download/install.sh | sudo bash -s -- --token cmdb_ent_...
#
# Skrypt pobiera z serwera paczke zrodel agenta, sprawdza jej skrot i uruchamia
# wlasciwy instalator. Adres serwera jest wpisywany przez serwer w chwili
# wydania tego pliku, wiec nie trzeba go podawac drugi raz.
#
set -euo pipefail

SERWER="@@ADRES_SERWERA@@"
TOKEN=""
CA_BUNDLE=""
POZOSTALE=()

blad() { echo "BLAD: $*" >&2; exit 1; }
krok() { echo "==> $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)     TOKEN="$2"; shift 2 ;;
        --server)    SERWER="$2"; shift 2 ;;
        --ca-bundle) CA_BUNDLE="$2"; POZOSTALE+=(--ca-bundle "$2"); shift 2 ;;
        # Reszte przekazujemy dalej, zeby --interval czy --no-processes
        # dzialaly tak samo jak przy instalacji z repozytorium.
        *) POZOSTALE+=("$1"); shift ;;
    esac
done

[[ -n "$TOKEN" ]] || blad "podaj --token cmdb_ent_... (token firmowy z panelu)"
[[ "$TOKEN" == cmdb_ent_* ]] || blad "to nie wyglada na token rejestracyjny - powinien zaczynac sie od 'cmdb_ent_'"
[[ "$SERWER" == https://* ]] || blad "adres serwera musi zaczynac sie od https://"
[[ $EUID -eq 0 ]] || blad "uruchom przez sudo - instalator zaklada usluge systemd"
[[ -z "$CA_BUNDLE" || -f "$CA_BUNDLE" ]] || blad "nie znajduje pliku CA: $CA_BUNDLE"

SERWER="${SERWER%/}"
ADRES_PACZKI="$SERWER/download/agent-linux.tar.gz"

# --- narzedzie do pobierania -------------------------------------------------
# Raspberry Pi OS ma curl, minimalne obrazy Debiana bywaja tylko z wget.
if command -v curl >/dev/null; then
    POBIERACZ="curl"
elif command -v wget >/dev/null; then
    POBIERACZ="wget"
else
    blad "brak curl i wget - zainstaluj jedno z nich: apt install curl"
fi

ROBOCZY="$(mktemp -d)"
trap 'rm -rf "$ROBOCZY"' EXIT

pobierz() {
    local adres="$1" cel="$2" naglowki="$3"
    if [[ "$POBIERACZ" == "curl" ]]; then
        local opcje=(-fsSL -H "Authorization: Bearer $TOKEN" -D "$naglowki" -o "$cel")
        [[ -n "$CA_BUNDLE" ]] && opcje+=(--cacert "$CA_BUNDLE")
        curl "${opcje[@]}" "$adres"
    else
        # Bez -q: ta opcja wycisza takze --server-response, a stamtad
        # bierzemy naglowek ze skrotem.
        local opcje=(--header="Authorization: Bearer $TOKEN" --server-response -O "$cel")
        [[ -n "$CA_BUNDLE" ]] && opcje+=(--ca-certificate="$CA_BUNDLE")
        wget "${opcje[@]}" "$adres" 2>"$naglowki"
    fi
}

krok "Pobieram agenta z $SERWER"
PACZKA="$ROBOCZY/agent.tar.gz"
NAGLOWKI="$ROBOCZY/naglowki.txt"
if ! pobierz "$ADRES_PACZKI" "$PACZKA" "$NAGLOWKI"; then
    echo >&2
    echo "Nie udalo sie pobrac paczki. Najczestsze przyczyny:" >&2
    echo "  - certyfikat serwera nie jest zaufany na tej maszynie" >&2
    echo "    (self-signed: dograj certyfikat i podaj --ca-bundle /sciezka/ca.pem)" >&2
    echo "  - token jest nieprawidlowy, wycofany albo wygasl" >&2
    echo "  - zapora po stronie serwera nie przepuszcza ruchu na ten port" >&2
    exit 1
fi

# --- weryfikacja skrotu ------------------------------------------------------
# Polaczenie jest po TLS, wiec to zabezpieczenie dodatkowe - wychwytuje
# uszkodzenie pliku w transporcie albo pomylke po stronie magazynu wydan.
OCZEKIWANY="$(grep -i '^x-cmdb-sha256:' "$NAGLOWKI" | tail -1 | tr -d '\r' | awk '{print $2}')"
if [[ -n "$OCZEKIWANY" ]] && command -v sha256sum >/dev/null; then
    FAKTYCZNY="$(sha256sum "$PACZKA" | awk '{print $1}')"
    [[ "$FAKTYCZNY" == "$OCZEKIWANY" ]] \
        || blad "skrot pobranej paczki sie nie zgadza (oczekiwano $OCZEKIWANY, jest $FAKTYCZNY)"
    echo "    skrot SHA-256 zgodny"
else
    echo "    UWAGA: nie moge sprawdzic skrotu paczki (brak naglowka lub sha256sum)"
fi

krok "Rozpakowuje"
tar -xzf "$PACZKA" -C "$ROBOCZY"
INSTALATOR="$ROBOCZY/cmdb-agent/packaging/install-agent.sh"
[[ -f "$INSTALATOR" ]] || blad "paczka nie zawiera instalatora - zglos to administratorowi serwera"
chmod +x "$INSTALATOR"

exec "$INSTALATOR" --server "$SERWER" --token "$TOKEN" ${POZOSTALE[@]+"${POZOSTALE[@]}"}
