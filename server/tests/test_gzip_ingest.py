"""Raport spakowany gzipem musi byc przyjety tak samo jak zwykly JSON."""
from __future__ import annotations

import gzip
import json
import zlib

from .factories import build_report
from .test_agent_api import enroll


def test_gzipped_report_is_accepted(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"], machine_id="maszyna-gzip-01").json()[
        "agent_token"
    ]
    report = build_report(machine_id="maszyna-gzip-01")
    body = gzip.compress(json.dumps(report).encode("utf-8"))

    response = client.post(
        "/api/v1/inventory",
        headers={
            "Authorization": f"Bearer {agent_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        content=body,
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True


def test_broken_gzip_is_rejected(client, tenant_a):
    agent_token = enroll(client, tenant_a["token"], machine_id="maszyna-gzip-01").json()[
        "agent_token"
    ]
    response = client.post(
        "/api/v1/inventory",
        headers={
            "Authorization": f"Bearer {agent_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        content=b"to nie jest gzip",
    )
    assert response.status_code == 400


def test_zip_bomb_is_rejected(client, tenant_a):
    """Maly plik rozpakowujacy sie do gigabajtow nie moze zjesc pamieci serwera."""
    agent_token = enroll(client, tenant_a["token"], machine_id="maszyna-gzip-01").json()[
        "agent_token"
    ]
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    bomb = b"".join(compressor.compress(b"A" * 1024 * 1024) for _ in range(64))
    bomb += compressor.flush()

    response = client.post(
        "/api/v1/inventory",
        headers={
            "Authorization": f"Bearer {agent_token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        content=bomb,
    )
    assert response.status_code == 413
