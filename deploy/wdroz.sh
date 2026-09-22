#!/usr/bin/env bash
#
# Wdrozenie nowej wersji portalu na serwerze.
#
#   cd ~/CMDB && ./deploy/wdroz.sh
#
# Rowniez "docker compose up -d --build" postawi nowy obraz, ale zbuduje go
# BEZ numeru wersji - a wtedy stopka mowi "nieznana" i wracamy do zgadywania,
# co wlasciwie chodzi. Ten skrypt wpisuje numer commita do obrazu i na koncu
# sprawdza, co serwer o sobie mowi.
set -euo pipefail

KORZEN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KORZEN"

echo "==> Pobieram zmiany"
git pull --ff-only

# Numer bierzemy z repozytorium TERAZ, bo w obrazie katalogu .git nie ma.
CMDB_WERSJA="$(git log -1 --format='%h (%cs)')"
export CMDB_WERSJA
echo "==> Wersja do wpisania w obraz: $CMDB_WERSJA"

echo "==> Buduje i uruchamiam"
docker compose -f deploy/docker-compose.yml up -d --build

# Konfiguracja nginx jest zamontowana z repozytorium, a "up -d" nie rusza
# kontenera, ktorego definicja sie nie zmienila - nowe limity i adresy
# (np. wgrywanie APK) nie weszlyby w zycie. Najpierw sprawdzamy skladnie:
# blad w pliku nie moze polozyc jedynego wejscia z zewnatrz.
echo "==> Przeladowuje konfiguracje nginx"
if docker compose -f deploy/docker-compose.yml exec -T proxy nginx -t -q; then
    docker compose -f deploy/docker-compose.yml exec -T proxy nginx -s reload
else
    echo "==> UWAGA: bledna konfiguracja nginx - zostaje poprzednia." >&2
fi

echo "==> Sprawdzam, co serwer o sobie mowi"
# Pytamy pod adresem wewnetrznym: to samo, co widzi swiat, ale bez zaleznosci
# od DNS i certyfikatu.
for _ in $(seq 1 30); do
    odpowiedz="$(docker compose -f deploy/docker-compose.yml exec -T server \
        python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=4).read().decode())" \
        2>/dev/null || true)"
    if [[ -n "$odpowiedz" ]]; then
        echo "$odpowiedz"
        case "$odpowiedz" in
            *"$CMDB_WERSJA"*)
                echo "==> Wdrozone: serwer podaje te wersje, ktora zbudowalismy."
                exit 0 ;;
        esac
        echo "==> UWAGA: serwer podaje inna wersje niz zbudowana." >&2
        exit 1
    fi
    sleep 2
done

echo "==> Serwer nie odpowiedzial w 60 s - sprawdz 'docker compose logs server'." >&2
exit 1
