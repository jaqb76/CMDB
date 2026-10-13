"""Pomocnicze parsowanie metadanych repozytorium (updateinfo).

Wspiera dwa warianty danych:

* repo metadata w payloadzie jako lista repo z polami ``updateinfo`` /
  ``advisories`` / ``metadata.updateinfo``
* bezposrednio lista ``patched_cves`` na poziomie pakietu, ktora jest
  wygenerowana przez agenta z changelogu RPM lub z repo metadata.

Gdy updateinfo repo zawiera CVE dla tego pakietu w danym release / repo,
CMDB traktuje to jako poprawke pochodzaca z repo, a nie z ogolnego kanału
OVAL. To eliminuje falszywe alarmy przy backportach do starszych podwersji
RHEL, kiedy OVAL pokazuje fix w nowszym podwydaniu el9_8, a repo 9.7 ma juz
poprawe w backportach.
"""
from __future__ import annotations

import base64
import gzip
import io
import re
from typing import Iterable
import xml.etree.ElementTree as ET


def _rob_dane(data: bytes | str | None) -> bytes | None:
    """Dekoduje przychodzace dane z updateinfo.

    Wspierane formy:
    * raw XML
    * gzip-compressed XML
    * base64-encoded XML (w payloadzie JSON)
    """
    if data is None:
        return None
    if isinstance(data, bytes):
        dane = data
    else:
        dane = data.strip()
        if not dane:
            return None
        try:
            dane = base64.b64decode(dane, validate=True)
        except ValueError:
            dane = dane.encode("utf-8")
    if dane.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(dane)
        except OSError:
            return dane
    return dane


_XMLNS = re.compile(r"\{.*\}")


def _nazwa(element: ET.Element) -> str:
    return _XMLNS.sub("", element.tag)


def parse_updateinfo_bytes(data: bytes | str | None) -> dict[str, set[str]]:
    """Zwraca mapowanie CVE -> set(pakietow), zczytana z updateinfo.xml*."""
    dane = _rob_dane(data)
    if not dane:
        return {}
    try:
        root = ET.fromstring(dane)
    except ET.ParseError:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(dane)) as gz:
                root = ET.fromstring(gz.read())
        except (OSError, ET.ParseError):
            return {}

    wynik: dict[str, set[str]] = {}
    for update in root.iter():
        if _nazwa(update) != "update":
            continue
        cves: set[str] = set()
        for ref in update.iter():
            if _nazwa(ref) != "reference":
                continue
            ref_id = (ref.get("id") or ref.get("ref_id") or "").strip()
            if not ref_id.startswith("CVE-"):
                continue
            cves.add(ref_id)
        pkgi = set()
        for pkg in update.iter():
            if _nazwa(pkg) != "package":
                continue
            name = (pkg.get("name") or "").strip()
            if name:
                pkgi.add(name)
        if not cves:
            continue
        for cve in cves:
            wynik.setdefault(cve, set()).update(pkgi)
    return wynik


def _package_names_from_scan(payload: dict | None) -> set[str]:
    pakiety = (payload.get("software") or {}).get("packages") or []
    return {((p.get("name") or "").strip()) for p in pakiety if (p.get("name") or "").strip()}


def repo_resolved_cves(payload: dict | None) -> set[str]:
    """Szukaj CVE rozstrzygnietych przez metadane repo w payloadzie hosta.

    Oczekiwany kształt payloadu:

    payload = {
      "os": {
        "repos": [
          {"id": "rhel-9-for-x86_64-baseos-rpms",
           "updateinfo": "<xml...>"}
        ]
      }
    }

    Dodatkowo wspierane jest pole `patched_cves` na pakiecie z agenta, aby
    bylo możliwe podanie puli CVE już backportowanych w danym repo.
    """
    if not payload:
        return set()

    wynik: set[str] = set()
    pakiety = (payload.get("software") or {}).get("packages") or []
    pakieti = {((p.get("name") or "").strip()): p for p in pakiety if (p.get("name") or "").strip()}

    # Direct agent hint: per-package list of CVEs already backported by the
    # package changelog / repo metadata.
    for pakiet in pakiety:
        for cve in pakiet.get("patched_cves") or []:
            if cve:
                wynik.add(str(cve).strip())

    repozytoria = ((payload.get("os") or {}).get("repos") or [])
    if not repozytoria and (payload.get("software") or {}).get("repositories"):
        repozytoria = (payload.get("software") or {}).get("repositories")

    if not repozytoria:
        return wynik

    znamy_pakiety = set(pakieti)
    for repo in repozytoria:
        if not repo:
            continue
        updateinfo = repo.get("updateinfo") or repo.get("metadata", {}).get("updateinfo") or repo.get("advisories")
        if not updateinfo:
            continue
        if isinstance(updateinfo, list):
            for wpis in updateinfo:
                if not isinstance(wpis, dict):
                    continue
                cves = wpis.get("cve") or wpis.get("cves") or []
                if isinstance(cves, str):
                    cves = [cves]
                for cve in cves:
                    if cve:
                        wynik.add(str(cve).strip())
            continue
        mapa = parse_updateinfo_bytes(updateinfo)
        if not mapa:
            continue
        for cve, pakiety_repo in mapa.items():
            if znamy_pakiety.intersection(pakiety_repo):
                wynik.add(cve)
    return wynik


def updateinfo_for_repo(repo: dict | None) -> dict[str, set[str]]:
    """Wersja bardziej szczegolowa do przyszlego uzycia z repo metadata."""
    if not repo:
        return {}
    updateinfo = repo.get("updateinfo") or repo.get("metadata", {}).get("updateinfo")
    if not updateinfo:
        return {}
    return parse_updateinfo_bytes(updateinfo)


__all__ = [
    "parse_updateinfo_bytes",
    "repo_resolved_cves",
    "updateinfo_for_repo",
]

