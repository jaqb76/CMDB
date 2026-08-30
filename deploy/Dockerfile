# Obraz serwera CMDB.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

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
# Skrypty instalacyjne dla Windows serwer wydaje po HTTPS, wiec musza zostac
# w obrazie - reszta zrodel agenta jest potrzebna wylacznie do zbudowania
# paczki i po tym kroku znika.
RUN python -m cmdb_server.pakiet /tmp/agent --do "$CMDB_AGENT_BUNDLE_DIR"     && cp /tmp/agent/packaging/install.ps1 /tmp/agent/packaging/install-agent.ps1           /app/cmdb_server/bootstrap/     && rm -rf /tmp/agent

# Serwer nie potrzebuje roota - uruchamiamy jako zwykly uzytkownik.
RUN useradd --system --create-home --uid 10001 cmdb && chown -R cmdb:cmdb /app
USER cmdb

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

# TLS konczy sie na nginx - uvicorn slucha po HTTP wylacznie w sieci wewnetrznej
# kontenerow. Naglowki proxy sa niezbedne, zeby serwer widzial https i realne IP.
CMD ["uvicorn", "cmdb_server.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", \
     "--workers", "4"]
