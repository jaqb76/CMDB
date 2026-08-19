"""Warstwa transportowa: polityka bezpieczenstwa, ponawianie, kompresja."""
from __future__ import annotations

import gzip
import io
import json
import urllib.error

import pytest

from cmdb_agent.transport import (
    GZIP_THRESHOLD_BYTES,
    ApiError,
    CmdbClient,
    TransportError,
    _extract_detail,
    _parse_retry_after,
    build_ssl_context,
)


def test_plain_http_is_refused():
    with pytest.raises(ValueError, match="https"):
        CmdbClient("http://cmdb.firma.pl")


def test_ssl_context_enforces_modern_tls():
    import ssl

    context = build_ssl_context()
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_backoff_grows_and_is_jittered():
    client = CmdbClient("https://cmdb.firma.pl")
    first = client._backoff(1, None)
    later = client._backoff(4, None)
    assert first < later
    # Rozproszenie chroni przed powrotem calej floty w tej samej sekundzie.
    assert len({round(client._backoff(3, None), 4) for _ in range(5)}) > 1


def test_retry_after_header_wins_over_backoff():
    client = CmdbClient("https://cmdb.firma.pl")
    assert client._backoff(1, 42.0) == 42.0
    # ...ale nie pozwalamy serwerowi uspic agenta na dowolnie dlugo.
    assert client._backoff(1, 100000.0) == 900.0


def test_parse_retry_after():
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert _parse_retry_after(None) is None


def test_extract_detail_prefers_json_field():
    error = urllib.error.HTTPError(
        "https://x", 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"nieprawidlowy token"}')
    )
    assert _extract_detail(error) == "nieprawidlowy token"


def test_extract_detail_falls_back_to_body():
    error = urllib.error.HTTPError("https://x", 500, "err", {}, io.BytesIO(b"cos poszlo nie tak"))
    assert "cos poszlo nie tak" in _extract_detail(error)


def test_large_payload_is_compressed(monkeypatch):
    """Raport z tysiacami pakietow jedzie gzipem - inaczej marnujemy lacze."""
    client = CmdbClient("https://cmdb.firma.pl")
    captured = {}

    def fake_send(url, body, headers):
        captured["body"] = body
        captured["headers"] = headers
        return {"ok": True}

    monkeypatch.setattr(client, "_send", fake_send)
    payload = {"packages": [{"name": f"pakiet-{i}"} for i in range(2000)]}
    client.post("/api/v1/inventory", token="cmdb_agt_x", payload=payload)

    assert captured["headers"]["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(captured["body"]).decode("utf-8")) == payload


def test_small_payload_is_not_compressed(monkeypatch):
    client = CmdbClient("https://cmdb.firma.pl")
    captured = {}
    monkeypatch.setattr(client, "_send", lambda url, body, headers: captured.update(
        body=body, headers=headers) or {})

    client.post("/api/v1/agents/enroll", token="cmdb_ent_x", payload={"machine_id": "m"})
    assert "Content-Encoding" not in captured["headers"]
    assert len(captured["body"]) < GZIP_THRESHOLD_BYTES


def test_token_travels_in_header_only(monkeypatch):
    client = CmdbClient("https://cmdb.firma.pl")
    captured = {}
    monkeypatch.setattr(client, "_send", lambda url, body, headers: captured.update(
        url=url, headers=headers) or {})

    client.post("/api/v1/inventory", token="cmdb_agt_tajne", payload={})
    assert captured["headers"]["Authorization"] == "Bearer cmdb_agt_tajne"
    assert "tajne" not in captured["url"]


def test_client_errors_are_not_retried(monkeypatch):
    """401 nie naprawi sie przez ponowienie - nie ma sensu meczyc serwera."""
    client = CmdbClient("https://cmdb.firma.pl", max_retries=4)
    calls = {"n": 0}

    def fake_send(url, body, headers):
        calls["n"] += 1
        raise ApiError(401, "nieprawidlowy token")

    monkeypatch.setattr(client, "_send", fake_send)
    with pytest.raises(ApiError):
        client.post("/api/v1/inventory", token="zly", payload={})
    assert calls["n"] == 1


def test_transport_errors_are_retried(monkeypatch):
    client = CmdbClient("https://cmdb.firma.pl", max_retries=3)
    calls = {"n": 0}

    def fake_send(url, body, headers):
        calls["n"] += 1
        raise TransportError("HTTP 503: chwilowa awaria")

    monkeypatch.setattr(client, "_send", fake_send)
    monkeypatch.setattr(client, "_backoff", lambda attempt, retry_after: 0.0)

    with pytest.raises(TransportError):
        client.post("/api/v1/inventory", token="cmdb_agt_x", payload={})
    assert calls["n"] == 3
