# Obraz serwera CMDB.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Klient PostgreSQL do kopii zapasowych i przywracania. Musi byc w TEJ SAMEJ
# wersji glownej co serwer bazy (16): pg_dump nie rozmawia z serwerem nowszym
# od siebie, a Debian bookworm ma w swoich repozytoriach dopiero 15 - stad
# repozytorium PGDG.
#
# Alternatywa byloby wolanie "docker exec db pg_dump" z kontenera aplikacji,
# co wymaga podmontowania gniazda dockera - czyli dania aplikacji webowej
# roota na hoscie. Kilkanascie megabajtow w obrazie jest tanszym rozwiazaniem.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client-16 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Zaleznosci osobna warstwa - zmiana kodu nie uniewaznia cache pip.
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ /app/

# Paczka zrodel agenta dla Linuksa. Wbudowujemy ja w obraz, bo maszyny
# docelowe pobieraja agenta z serwera - inaczej kazda z nich musialaby
# klonowac repozytorium. Zrodla agenta sa potrzebne wylacznie w tym kroku
# i nie zostaja w obrazie.
# Katalog jest osobny od magazynu wydan: wydania wgrywa administrator i zyja
# w wolumenie, a paczka jest czescia obrazu i zmienia sie razem z kodem.
# Wspolny katalog oznaczalby, ze pusty wolumen przyslania paczke z obrazu.
ENV CMDB_AGENT_BUNDLE_DIR=/app/agent-pakiet
COPY agent/ /tmp/agent/
# Skrypty, ktore serwer wydaje po HTTPS (instalacja dla Windows oraz
# odinstalowanie dla obu systemow), musza zostac w obrazie - reszta zrodel
# agenta jest potrzebna wylacznie do zbudowania paczki i po tym kroku znika.
# Deinstalator bywa potrzebny wtedy, gdy maszyna nie ma juz nic poza
# adresem serwera.
RUN python -m cmdb_server.pakiet /tmp/agent --do "$CMDB_AGENT_BUNDLE_DIR"     && cp /tmp/agent/packaging/install.ps1 /tmp/agent/packaging/install-agent.ps1           /tmp/agent/packaging/uninstall-agent.ps1 /tmp/agent/packaging/uninstall-agent.sh           /app/cmdb_server/bootstrap/     && rm -rf /tmp/agent

# Ktora wersja portalu siedzi w tym obrazie. Katalog .git do obrazu nie
# trafia, wiec serwer nie ma jak tego odczytac sam - numer musi zostac
# wpisany przy budowaniu. Argument stoi tuz przed uruchomieniem, zeby jego
# zmiana nie uniewazniala warstw z zaleznosciami.
ARG CMDB_WERSJA=nieznana
ENV CMDB_WERSJA=$CMDB_WERSJA

# Serwer nie potrzebuje roota - uruchamiamy jako zwykly uzytkownik.
# Katalogi wolumenow zakladamy TERAZ, przed chown: pusty wolumen dziedziczy
# wlasciciela po katalogu z obrazu, wiec bez tego serwer nie moglby zapisac
# ani zalacznika, ani kopii.
RUN mkdir -p /app/releases /app/helpdesk /app/kopie \
    && useradd --system --create-home --uid 10001 cmdb \
    && chown -R cmdb:cmdb /app
USER cmdb

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

# TLS konczy sie na nginx - uvicorn slucha po HTTP wylacznie w sieci wewnetrznej
# kontenerow. Naglowki proxy sa niezbedne, zeby serwer widzial https i realne IP.
# Przywracanie kopii dzieje sie TUTAJ, przed uruchomieniem uvicorna: w tym
# momencie zadne polaczenie aplikacji jeszcze nie istnieje, wiec pg_restore
# moze usunac tabele, ktorych w innym razie pilnowaliby wlasni workerzy.
ENTRYPOINT ["python", "-m", "cmdb_server.wejscie"]

CMD ["uvicorn", "cmdb_server.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", \
     "--workers", "4"]
