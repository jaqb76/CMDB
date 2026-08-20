"""Klient HTTPS agenta (biblioteka standardowa, zero zaleznosci).

Zalozenia bezpieczenstwa:
  * wylacznie https, TLS >= 1.2, weryfikacja lancucha i nazwy hosta zawsze wlaczona
    (nie ma opcji jej wylaczenia - swiadomie),
  * opcjonalny wlasny CA dla wewnetrznego PKI,
  * opcjonalne przypiecie certyfikatu serwera (SHA-256 z DER),
  * token wedruje wylacznie w naglowku Authorization, nigdy w URL.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

# Ponizej tego progu kompresja nie oplaca sie (narzut > zysk).
GZIP_THRESHOLD_BYTES = 4096
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def worst_case_seconds(timeout: int, max_retries: int) -> float:
    """Gorne oszacowanie czasu, jaki klient moze spedzic na jednym zadaniu.

    Sumuje limity wszystkich prob i odczekania miedzy nimi, biorac gorna
    granice rozproszenia. Sluzy do sprawdzenia, czy limit nalozony z zewnatrz
    (np. przez okno ustawien) daje agentowi szanse dokonczyc prace.
    """
    total = float(timeout * max_retries)
    for attempt in range(1, max_retries):
        base = min(2.0**attempt, 120.0)
        total += base * 1.25  # baza + maksymalne rozproszenie (25%)
    return total


class TransportError(Exception):
    """Blad komunikacji, ktory ma sens ponowic pozniej."""


class ApiError(Exception):
    """Serwer odpowiedzial bledem, ktorego ponawianie nic nie da."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class CertificatePinError(Exception):
    """Certyfikat serwera nie zgadza sie z przypietym odciskiem."""


def build_ssl_context(ca_bundle: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_bundle)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection sprawdzajace odcisk certyfikatu tuz po handshake."""

    pin_sha256: str = ""

    def connect(self) -> None:
        super().connect()
        if not self.pin_sha256:
            return
        der = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(der).hexdigest()
        if actual != self.pin_sha256:
            self.close()
            raise CertificatePinError(
                f"odcisk certyfikatu serwera ({actual}) nie zgadza sie z przypietym "
                f"({self.pin_sha256})"
            )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, pin_sha256: str):
        super().__init__(context=context)
        self._pin = pin_sha256

    def https_open(self, req):
        def build(host, **kwargs):
            # do_open dokleda juz timeout z zadania - podanie wlasnego dalo by
            # "multiple values for keyword argument". Kontekst TLS wstrzykujemy tutaj.
            kwargs.pop("context", None)
            kwargs.pop("check_hostname", None)
            connection = _PinnedHTTPSConnection(host, context=self._context, **kwargs)
            connection.pin_sha256 = self._pin
            return connection

        return self.do_open(build, req)


class CmdbClient:
    """Minimalny klient REST serwera CMDB."""

    def __init__(
        self,
        server_url: str,
        ca_bundle: str | None = None,
        pin_sha256: str | None = None,
        timeout: int = 60,
        max_retries: int = 4,
    ):
        if not server_url.startswith("https://"):
            raise ValueError("adres serwera musi uzywac https://")
        self.base_url = server_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.pin_sha256 = (pin_sha256 or "").replace(":", "").lower()

        context = build_ssl_context(ca_bundle)
        if self.pin_sha256:
            handler = _PinnedHTTPSHandler(context, self.pin_sha256)
        else:
            handler = urllib.request.HTTPSHandler(context=context)
        # Bez obslugi przekierowan: przekierowanie moglo by przeniesc token
        # na inny host. Kazde 3xx traktujemy jako blad konfiguracji.
        self._opener = urllib.request.build_opener(handler, _NoRedirect())

    def post(self, path: str, token: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"cmdb-agent/{__version__}",
        }
        if len(body) > GZIP_THRESHOLD_BYTES:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._send(url, body, headers)
            except ApiError:
                raise
            except (TransportError, urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                delay = self._backoff(attempt, getattr(exc, "retry_after", None))
                log.warning(
                    "proba %d/%d nieudana (%s) - ponawiam za %.0f s",
                    attempt, self.max_retries, exc, delay,
                )
                time.sleep(delay)

        raise TransportError(f"nie udalo sie polaczyc z serwerem po {self.max_retries} probach: {last_error}")

    def get(self, path: str, token: str) -> dict:
        """Zapytanie GET zwracajace JSON (sprawdzenie oczekiwanej wersji agenta)."""
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"cmdb-agent/{__version__}",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = _extract_detail(exc)
            if exc.code in RETRYABLE_STATUS:
                raise TransportError(f"HTTP {exc.code}: {detail}") from exc
            raise ApiError(exc.code, detail) from exc

    def download(self, path: str, token: str, cel: Path, max_bytes: int) -> str:
        """Pobiera plik strumieniowo i zwraca jego skrot SHA-256.

        Adres skladamy z WLASNEJ konfiguracji - serwer nigdy nie podaje URL-a.
        Dzieki temu nawet podszycie sie pod serwer nie przekieruje agenta po
        plik na obcy host. Skrot liczymy w locie, zeby nie czytac
        trzydziestomegabajtowego pliku po raz drugi.
        """
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"cmdb-agent/{__version__}",
            },
            method="GET",
        )
        skrot = hashlib.sha256()
        pobrano = 0
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                with open(cel, "wb") as wyjscie:
                    while fragment := response.read(256 * 1024):
                        pobrano += len(fragment)
                        if pobrano > max_bytes:
                            raise TransportError(
                                f"pobierany plik przekracza {max_bytes} bajtow - przerwano"
                            )
                        skrot.update(fragment)
                        wyjscie.write(fragment)
        except urllib.error.HTTPError as exc:
            detail = _extract_detail(exc)
            if exc.code in RETRYABLE_STATUS:
                raise TransportError(f"HTTP {exc.code}: {detail}") from exc
            raise ApiError(exc.code, detail) from exc

        if pobrano == 0:
            raise TransportError("serwer zwrocil pusty plik")
        return skrot.hexdigest()

    def _send(self, url: str, body: bytes, headers: dict) -> dict:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = _extract_detail(exc)
            if exc.code in RETRYABLE_STATUS:
                error = TransportError(f"HTTP {exc.code}: {detail}")
                error.retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                raise error from exc
            raise ApiError(exc.code, detail) from exc

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after:
            return min(retry_after, 900.0)
        # Wykladniczo z rozproszeniem - zeby flota nie wracala zsynchronizowana.
        base = min(2.0**attempt, 120.0)
        return base + random.uniform(0, base * 0.25)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code, f"serwer odeslal przekierowanie na {newurl} - sprawdz adres w konfiguracji")


def _extract_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - strumien juz zamkniety
        return exc.reason or ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "detail" in parsed:
            return str(parsed["detail"])
    except json.JSONDecodeError:
        pass
    return body[:500] or (exc.reason or "")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
