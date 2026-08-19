"""Punkt wejscia aplikacji FastAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .api import agent as agent_api
from .api import ui as ui_api
from .config import get_settings
from .db import init_db
from .services.auth import LoginRequired

log = logging.getLogger("cmdb")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.validate_for_runtime()
    init_db()
    log.info("CMDB wystartowal (env=%s, db=%s)", settings.env, settings.database_url.split("@")[-1])
    yield


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

    app.include_router(agent_api.router)
    app.include_router(ui_api.router)

    @app.middleware("http")
    async def guard_and_harden(request: Request, call_next):
        settings = get_settings()

        # 1. Wymuszenie HTTPS - agenci wysylaja tokeny w naglowku, wiec czysty
        #    HTTP nie moze byc nawet przekierowany dla API (token juz wyciekl).
        if settings.require_https:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if proto != "https":
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": "wymagane polaczenie HTTPS"},
                        status_code=status.HTTP_403_FORBIDDEN,
                    )
                return RedirectResponse(
                    request.url.replace(scheme="https"), status_code=status.HTTP_308_PERMANENT_REDIRECT
                )

        # 2. Limit rozmiaru raportu - odrzucamy przed zbudowaniem ciala w pamieci.
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_report_bytes:
                return JSONResponse(
                    {"detail": "raport przekracza dozwolony rozmiar"},
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

    @app.exception_handler(LoginRequired)
    async def login_required_handler(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    return app


app = create_app()
