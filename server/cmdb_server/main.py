"""Punkt wejscia aplikacji FastAPI."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from .api import admin as admin_api
from .api import agent as agent_api
from .api import download as download_api
from .api import ui as ui_api
from .config import get_settings
from .db import init_db
from .middleware import GzipRequestMiddleware
from .services.auth import LoginRequired, SuperadminRequired

log = logging.getLogger("cmdb")


def _silence_windows_disconnect_noise() -> None:
    """Wycisza halas asyncio na Windows przy zerwanym polaczeniu klienta.

    Petla ProactorEventLoop zglasza ConnectionResetError (WinError 10054) jako
    "Exception in callback _ProactorBasePipeTransport._call_connection_lost"
    z pelnym sladem stosu, gdy przegladarka albo agent zamknie polaczenie bez
    ceregieli - co jest calkowicie normalne. Trace w logu wyglada jak awaria,
    a nia nie jest, wiec przy tym jednym przypadku milczymy. Kazdy inny wyjatek
    trafia do domyslnej obslugi.
    """
    if sys.platform != "win32":
        return

    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(active_loop, context):
        exception = context.get("exception")
        message = str(context.get("message", ""))
        if isinstance(exception, ConnectionResetError) and "_call_connection_lost" in message:
            log.debug("klient zerwal polaczenie (WinError 10054) - pomijam")
            return
        if previous is not None:
            previous(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.validate_for_runtime()
    _silence_windows_disconnect_noise()
    init_db()
    _odswiez_paczke_agenta(settings)
    log.info("CMDB wystartowal (env=%s, db=%s)", settings.env, settings.database_url.split("@")[-1])
    yield


# Endpoint zywotnosci: bez tokenu, bez danych, odpytywany lokalnie.
SCIEZKA_ZDROWIA = "/api/v1/health"


def _odswiez_paczke_agenta(settings) -> None:
    """Przygotowuje paczke zrodel agenta do wydawania po HTTPS.

    Dwie osobne sprawy, ktore latwo pomylic:

    1. BUDOWANIE paczki ma sens tylko tam, gdzie leza zrodla agenta, czyli
       w repozytorium. W obrazie produkcyjnym zrodel nie ma - paczka jest tam
       budowana przy tworzeniu obrazu.
    2. REJESTRACJA paczki jako wydania musi sie dziac ZAWSZE, niezaleznie od
       tego, skad paczka pochodzi. Dopoki nie ma jej w magazynie wydan,
       instalacja jednym poleceniem zwraca 503 - a wlasnie tak zachowywal sie
       obraz produkcyjny, w ktorym plik lezal na dysku, ale nikt go nie wpisal.
    """
    from .services import pakiet

    katalog = pakiet.katalog_paczki()
    metadane = None
    if settings.agent_source_dir:
        metadane = pakiet.zbuduj_jesli_trzeba(Path(settings.agent_source_dir), katalog)
    else:
        metadane = pakiet.opis(katalog)

    if metadane is None:
        log.warning(
            "nie ma paczki zrodel agenta w %s - instalacja po HTTPS bedzie "
            "zwracac blad", katalog,
        )
        return

    from .db import SessionLocal

    with SessionLocal() as db:
        pakiet.zarejestruj(db, metadane, katalog, Path(settings.release_dir))


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CMDB",
        description="Inwentaryzacja maszyn z agentow, wielofirmowa (multi-tenant).",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """Przegladarki pytaja o /favicon.ico niezaleznie od tego, co jest
        w naglowku strony - bez tej trasy kazde wejscie zostawialo w logu 404."""
        return FileResponse(
            static_dir / "favicon.ico",
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    app.include_router(agent_api.router)
    app.include_router(download_api.router)
    app.include_router(admin_api.router)
    app.include_router(ui_api.router)

    # Agenci wysylaja raporty spakowane gzipem - rozpakowujemy z limitem.
    app.add_middleware(GzipRequestMiddleware, max_bytes=settings.max_report_bytes)

    @app.middleware("http")
    async def guard_and_harden(request: Request, call_next):
        settings = get_settings()

        # 1. Wymuszenie HTTPS - agenci wysylaja tokeny w naglowku, wiec czysty
        #    HTTP nie moze byc nawet przekierowany dla API (token juz wyciekl).
        if settings.require_https and request.url.path != SCIEZKA_ZDROWIA:
            # Sprawdzenie zywotnosci idzie po HTTP z wnetrza kontenera, przed
            # nginx - nie niesie tokenu ani danych, wiec wymaganie od niego
            # HTTPS oznaczaloby kontener na zawsze uznawany za niesprawny.
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if proto != "https":
                # Zadania z tokenem nie przekierowujemy, tylko odrzucamy:
                # naglowek juz doszedl po czystym HTTP, wiec token juz wyciekl
                # i przekierowanie niczego nie ratuje - jedynie ukrywa problem.
                niesie_token = "authorization" in request.headers
                if request.url.path.startswith("/api/") or niesie_token:
                    return JSONResponse(
                        {"detail": "wymagane polaczenie HTTPS"},
                        status_code=status.HTTP_403_FORBIDDEN,
                    )
                return RedirectResponse(
                    request.url.replace(scheme="https"), status_code=status.HTTP_308_PERMANENT_REDIRECT
                )

        # 2. Limit rozmiaru zadania - odrzucamy przed zbudowaniem ciala w pamieci.
        #    Wgrywanie wersji agenta ma osobny, znacznie wyzszy limit: raport
        #    to kilkaset kB tekstu, a agent spakowany PyInstallerem ~30 MB.
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            wgrywanie = request.url.path.startswith("/admin/releases")
            limit = settings.max_release_bytes if wgrywanie else settings.max_report_bytes
            if int(content_length) > limit:
                return JSONResponse(
                    {"detail": "zadanie przekracza dozwolony rozmiar"},
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

        response: Response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if settings.require_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(SuperadminRequired)
    async def superadmin_required_handler(request: Request, exc: SuperadminRequired) -> Response:
        return JSONResponse(
            {"detail": "ta czesc panelu jest dostepna wylacznie dla superadmina"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.exception_handler(LoginRequired)
    async def login_required_handler(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    return app


app = create_app()
