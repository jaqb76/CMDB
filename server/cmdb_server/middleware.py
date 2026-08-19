"""Middleware ASGI: rozpakowanie raportow wyslanych z Content-Encoding: gzip.

Raport z kilku tysiacami pakietow potrafi miec kilkaset kB tekstu, a po gzipie
kilkanascie - przy flocie kilkuset maszyn to realna oszczednosc lacza. Starlette
nie rozpakowuje cial zadan, wiec robimy to tutaj.

Dekompresja jest limitowana (ochrona przed "zip bomb"): przerywamy, gdy tresc po
rozpakowaniu przekroczy max_bytes, zamiast zjadac pamiec serwera.
"""
from __future__ import annotations

import zlib

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_GZIP_WBITS = 16 + zlib.MAX_WBITS
_CHUNK = 64 * 1024


class DecompressionLimitExceeded(Exception):
    pass


def _gunzip_limited(data: bytes, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    out = bytearray()
    # Przy limicie max_length reszta wejscia laduje w unconsumed_tail i to ja
    # trzeba podac w kolejnej iteracji - podanie b"" urywa dekompresje.
    chunk = decompressor.decompress(data, _CHUNK)
    while chunk:
        out.extend(chunk)
        if len(out) > max_bytes:
            raise DecompressionLimitExceeded()
        chunk = decompressor.decompress(decompressor.unconsumed_tail, _CHUNK)
    if not decompressor.eof:
        raise zlib.error("niekompletny strumien gzip")
    return bytes(out)


class GzipRequestMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if headers.get("content-encoding", "").lower() != "gzip":
            await self.app(scope, receive, send)
            return

        compressed = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            compressed.extend(message.get("body", b""))
            if len(compressed) > self.max_bytes:
                await JSONResponse(
                    {"detail": "raport przekracza dozwolony rozmiar"}, status_code=413
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        try:
            body = _gunzip_limited(bytes(compressed), self.max_bytes)
        except DecompressionLimitExceeded:
            await JSONResponse(
                {"detail": "raport po rozpakowaniu przekracza dozwolony rozmiar"},
                status_code=413,
            )(scope, receive, send)
            return
        except zlib.error:
            await JSONResponse(
                {"detail": "nie udalo sie rozpakowac tresci zadania"}, status_code=400
            )(scope, receive, send)
            return

        new_scope = dict(scope)
        new_headers = MutableHeaders(raw=list(scope["headers"]))
        del new_headers["content-encoding"]
        new_headers["content-length"] = str(len(body))
        new_scope["headers"] = new_headers.raw

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(new_scope, replay, send)
