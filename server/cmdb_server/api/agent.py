"""API dla agentow: rejestracja maszyny i przyjmowanie raportow.

Tenant NIGDY nie pochodzi z ciala zadania - wynika wylacznie z tokenu.
Agent nie moze wiec zaraportowac maszyny do cudzej firmy.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import wersja
from ..config import get_settings
from ..db import get_db
from ..models import (AgentCredential, Asset, DiscoveryPolicy, EnrollmentToken,
                      MonitorUslugi, Tenant, utcnow)
from ..discovery_policy import ScanPolicy
from ..monitoring_schema import OdpowiedzMonitorowania, RaportDostepnosci
from ..nutanix_schema import WynikNutanix
from ..schemas import (
    EnrollRequest,
    EnrollResponse,
    InventoryReport,
    InventoryResponse,
    UpgradeOffer,
    UpgradeResult,
)
from ..security import generate_token
from ..services.auth import client_ip, require_agent, require_enrollment_token
from ..services import architektura, funkcje_agenta, upgrades, ustawienia
from ..services.inventory import store_report
from ..services.scoping import TenantContext, audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["agent"])


@router.get("/health")
def health() -> dict:
    # Wersja jest tu celowo: pytanie "czy wdrozenie doszlo" trzeba dac sie
    # rozstrzygnac bez logowania do panelu i bez dostepu do serwera. Nie ma
    # w niej nic wrazliwego - to ten sam napis, ktory widac w stopce.
    return {"status": "ok", "schema_version": 1, "server_time": utcnow().isoformat(),
            "wersja": wersja.opis()}


@router.post("/agents/enroll", response_model=EnrollResponse, status_code=status.HTTP_201_CREATED)
def enroll(
    payload: EnrollRequest,
    request: Request,
    db: Session = Depends(get_db),
    auth: tuple[EnrollmentToken, Tenant] = Depends(require_enrollment_token),
) -> EnrollResponse:
    """Wymienia firmowy token rejestracyjny na indywidualne poswiadczenie agenta."""
    enrollment_token, tenant = auth
    settings = get_settings()
    ip = client_ip(request)

    ctx = TenantContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        actor=f"enroll:{enrollment_token.prefix}",
        can_write=True,
    )

    # Serializuje enrollment takze wtedy, gdy zasob jeszcze nie istnieje.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": tenant.id + ":" + payload.machine_id})
    asset = db.execute(
        select(Asset).where(Asset.tenant_id == tenant.id, Asset.machine_id == payload.machine_id).with_for_update()
    ).scalar_one_or_none()

    if asset is not None and asset.enrollment_blocked:
        raise HTTPException(status_code=403, detail="maszyna zablokowana; administrator musi zezwolic na rejestracje")

    created = asset is None
    if asset is None:
        asset = Asset(
            tenant_id=tenant.id,
            machine_id=payload.machine_id,
            hostname=payload.identity.hostname,
            fqdn=payload.identity.fqdn,
            domain=payload.identity.domain,
            os_family=payload.identity.os_family,
            arch=architektura.normalizuj(payload.identity.arch),
            agent_version=payload.agent_version,
            tags=[],
            facts={},
        )
        db.add(asset)
        db.flush()
    else:
        asset.hostname = payload.identity.hostname
        asset.agent_version = payload.agent_version
        asset.is_active = True
        # Ponowna rejestracja tej samej maszyny uniewaznia poprzednie poswiadczenia,
        # zeby nie zostawiac dzialajacych kluczy po reinstalacji agenta.
        previous = db.execute(
            select(AgentCredential).where(
                AgentCredential.asset_id == asset.id, AgentCredential.revoked_at.is_(None)
            )
        ).scalars().all()
        for cred in previous:
            cred.revoked_at = utcnow()

    token = generate_token("agt")
    db.add(
        AgentCredential(
            tenant_id=tenant.id,
            asset_id=asset.id,
            enrollment_token_id=enrollment_token.id,
            prefix=token.prefix,
            token_hash=token.token_hash,
        )
    )
    audit(
        db,
        ctx,
        action="agent.enroll",
        target=asset.hostname,
        detail={
            "asset_id": asset.id,
            "machine_id": payload.machine_id,
            "new_asset": created,
            "credential_prefix": token.prefix,
        },
        ip=ip,
    )
    db.commit()

    log.info(
        "enrollment tenant=%s asset=%s hostname=%s new=%s",
        tenant.slug,
        asset.id,
        asset.hostname,
        created,
    )
    return EnrollResponse(
        asset_id=asset.id,
        tenant_slug=tenant.slug,
        agent_token=token.plaintext,
        server_time=utcnow(),
        report_interval_seconds=ustawienia.interwal_raportowania(tenant),
    )


@router.post("/inventory", response_model=InventoryResponse)
def submit_inventory(
    report: InventoryReport,
    request: Request,
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> InventoryResponse:
    """Przyjmuje pelny raport inwentaryzacyjny i zapisuje go jako snapshot JSON."""
    credential, ctx = auth
    settings = get_settings()

    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    # Poswiadczenie jest przypisane do konkretnej maszyny - raport musi sie zgadzac.
    if report.machine_id != asset.machine_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="machine_id nie zgadza sie z poswiadczeniem - wymagana ponowna rejestracja",
        )

    snapshot, changed = store_report(db, ctx, asset, report)
    db.commit()

    if report.errors:
        log.warning(
            "agent zglosil %d bledow kolektorow asset=%s", len(report.errors), asset.hostname
        )

    return InventoryResponse(
        asset_id=asset.id,
        snapshot_id=snapshot.id if snapshot else None,
        changed=changed,
        server_time=utcnow(),
        report_interval_seconds=ustawienia.interwal_raportowania(
            db.get(Tenant, ctx.tenant_id)
        ),
        upgrade=_oferta_aktualizacji(db, asset),
    )


@router.get("/agent/discovery-policy")
def discovery_policy(response: Response, nonce: str = Query(..., pattern=r"^[0-9a-f]{32}$"),
                     db: Session = Depends(get_db),
                     auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "maszyna nie istnieje")
    row = db.get(DiscoveryPolicy, asset.id)
    if row is not None and row.tenant_id != ctx.tenant_id:
        raise HTTPException(403, "niezgodna firma polityki")
    policy = ScanPolicy.model_validate(row.config) if row else ScanPolicy()
    if not asset.is_active or asset.enrollment_blocked:
        policy = ScanPolicy()
    funkcje_agenta.zapisz_odbior(db, asset, funkcje_agenta.SKANER, funkcje_agenta.wersja_skanera(row))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"protocol": 1, "nonce": nonce, "asset_id": asset.id, "machine_id": asset.machine_id,
            "revision": row.revision if row else "unassigned",
            "expires_at": (utcnow() + timedelta(seconds=60)).isoformat(),
            "policy": policy.model_dump()}


@router.get("/agent/monitoring-policy")
def monitoring_policy(response: Response, nonce: str = Query(..., pattern=r"^[0-9a-f]{32}$"),
                      db: Session = Depends(get_db),
                      auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Cele, ktore ta maszyna ma sprawdzac.

    Ta sama zasada co przy polityce skanowania: swieza, zwiazana z jednorazowa
    wartoscia, wazna przez chwile. Zapisany stan agenta NIGDY nie jest
    upowaznieniem - inaczej odebranie celu w panelu nie odbieraloby go
    naprawde, bo agent chodzilby dalej po ostatniej znanej liscie.

    Agent dostaje wylacznie cele przypisane JEMU. Nie widzi ani celow innych
    maszyn tej samej firmy, ani niczego o pozostalych firmach.
    """
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "maszyna nie istnieje")

    policy = funkcje_agenta.polityka_monitorowania(db, asset)
    # Slad odbioru: panel pokazuje, czy zmiana celow juz dotarla do agenta.
    funkcje_agenta.zapisz_odbior(db, asset, funkcje_agenta.MONITOROWANIE,
                                 funkcje_agenta.wersja_monitorowania(policy))
    db.commit()

    response.headers["Cache-Control"] = "no-store"
    return {"protocol": 1, "nonce": nonce, "asset_id": asset.id, "machine_id": asset.machine_id,
            "expires_at": (utcnow() + timedelta(seconds=60)).isoformat(),
            "policy": policy}


@router.post("/agent/monitoring", response_model=OdpowiedzMonitorowania)
def monitoring_report(raport: RaportDostepnosci, request: Request,
                      db: Session = Depends(get_db),
                      auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Przyjmuje podsumowania i przerwy zebrane przez agenta.

    Agent moze zglosic wylacznie cele, ktore SAM ma sprawdzac. Identyfikator
    celu przychodzi z zewnatrz, wiec sprawdzamy go warunkiem na wykonawce
    i firme - inaczej wystarczyloby zgadnac cudze id, zeby oglosic komus
    awarie albo wyciszyc prawdziwa.
    """
    from ..services import monitoring

    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "maszyna nie istnieje")

    teraz = utcnow()
    przyjeto = pominieto = 0
    for wpis in raport.cele:
        monitor = db.execute(
            select(MonitorUslugi).where(
                MonitorUslugi.id == wpis.id,
                MonitorUslugi.tenant_id == ctx.tenant_id,
                MonitorUslugi.wykonawca_id == asset.id,
            ).with_for_update()
        ).scalar_one_or_none()
        if monitor is None or not monitor.aktywny:
            # Cel usuniety albo przepisany innemu agentowi w miedzyczasie.
            # To nie jest blad agenta - polityke dostanie odswiezona.
            pominieto += 1
            continue
        monitoring.przyjmij_raport(db, monitor, wpis.model_dump(mode="json"), teraz)
        przyjeto += 1
    db.commit()

    return OdpowiedzMonitorowania(
        przyjeto=przyjeto, pominieto=pominieto, server_time=teraz,
        interwal_raportu=get_settings().monitoring_report_seconds,
    )


# --- wirtualizacja: Nutanix Prism Central, VMware vCenter ----------------------

def _polityka_wirtualizacji(dostawca: str, response: Response, nonce: str, db: Session,
                            auth: tuple[AgentCredential, TenantContext]) -> dict:
    """Konfiguracja odczytu platformy wirtualizacji dla tej maszyny.

    Ta sama zasada co przy skanerze i monitorowaniu: swieza odpowiedz,
    zwiazana z jednorazowa wartoscia i wazna przez chwile. Niesie haslo,
    wiec nigdy nie trafia do pamieci podrecznej po drodze.
    """
    from ..services import nutanix

    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "maszyna nie istnieje")
    row = nutanix.ustawienia(db, asset, dostawca)
    polityka = nutanix.polityka_dla_agenta(db, asset, dostawca)
    funkcje_agenta.zapisz_odbior(db, asset, dostawca, nutanix.wersja(row))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"protocol": 1, "nonce": nonce, "asset_id": asset.id, "machine_id": asset.machine_id,
            "revision": nutanix.wersja(row),
            "expires_at": (utcnow() + timedelta(seconds=60)).isoformat(),
            "policy": polityka}


def _wynik_wirtualizacji(dostawca: str, wynik: WynikNutanix, db: Session,
                         auth: tuple[AgentCredential, TenantContext]) -> dict:
    """Wynik testu polaczenia albo pelnego odczytu platformy.

    Zakres wyznacza poswiadczenie maszyny: wynik trafia do firmy agenta
    i do jego wlasnej konfiguracji, niezaleznie od tego, co jest w tresci.
    """
    from ..services import nutanix

    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "maszyna nie istnieje")
    if not asset.is_active or asset.enrollment_blocked:
        raise HTTPException(403, "maszyna jest wycofana")
    # Dwa agenty (albo Prism i vCenter) nie moga przeplatac zapisow tej samej
    # firmy - kazdy odczyt przepina relacje, laczy VM z agentami i ocenia
    # znikniecia. Jedna blokada dla obu dostawcow.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "nutanix:" + ctx.tenant_id})
    wynik_przyjecia = nutanix.przyjmij(db, asset, wynik, dostawca)
    db.commit()
    return {"wynik": wynik_przyjecia, "server_time": utcnow().isoformat()}


@router.get("/agent/nutanix-policy")
def nutanix_policy(response: Response, nonce: str = Query(..., pattern=r"^[0-9a-f]{32}$"),
                   db: Session = Depends(get_db),
                   auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Konfiguracja odczytu Prism Central dla tej maszyny."""
    return _polityka_wirtualizacji(funkcje_agenta.NUTANIX, response, nonce, db, auth)


@router.post("/agent/nutanix")
def nutanix_wynik(wynik: WynikNutanix, db: Session = Depends(get_db),
                  auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Wynik testu albo odczytu Prism Central."""
    return _wynik_wirtualizacji(funkcje_agenta.NUTANIX, wynik, db, auth)


@router.get("/agent/vmware-policy")
def vmware_policy(response: Response, nonce: str = Query(..., pattern=r"^[0-9a-f]{32}$"),
                  db: Session = Depends(get_db),
                  auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Konfiguracja odczytu vCenter dla tej maszyny."""
    return _polityka_wirtualizacji(funkcje_agenta.VMWARE, response, nonce, db, auth)


@router.post("/agent/vmware")
def vmware_wynik(wynik: WynikNutanix, db: Session = Depends(get_db),
                 auth: tuple[AgentCredential, TenantContext] = Depends(require_agent)):
    """Wynik testu albo odczytu vCenter."""
    return _wynik_wirtualizacji(funkcje_agenta.VMWARE, wynik, db, auth)


# --- aktualizacja agenta ----------------------------------------------------
#
# Komunikacja pozostaje jednostronna: serwer nigdy nie laczy sie z maszyna.
# Agent sam pyta o oczekiwana wersje i sam pobiera plik tym samym polaczeniem
# HTTPS, ktorym raportuje.

def _oferta_aktualizacji(db: Session, asset: Asset) -> UpgradeOffer:
    wydanie = upgrades.wersja_docelowa(db, asset)
    if not upgrades.czy_wymaga_aktualizacji(asset, wydanie):
        # Maszyna jest na wersji docelowej, wiec poprzednie niepowodzenie
        # przestalo cokolwiek opisywac. Bez tego panel pokazywal obok siebie
        # "aktualna" i "odrzucona" z komunikatem sprzed naprawy - a poniewaz
        # nie ma juz czego proponowac, nic by tego stanu nie nadpisalo.
        # Przebieg zostaje w dzienniku aktualizacji; kasujemy tylko znacznik
        # przy maszynie, ktory ma opisywac stan biezacy.
        upgrades.wyczysc_nieaktualne_niepowodzenie(asset, wydanie)
        return UpgradeOffer(available=False, current_version=asset.agent_version)
    return UpgradeOffer(
        available=True,
        version=wydanie.version,
        sha256=wydanie.sha256,
        size_bytes=wydanie.size_bytes,
        current_version=asset.agent_version,
        kind="zrodla" if upgrades.czy_zrodla(wydanie) else "plik",
    )


@router.get("/agent/version", response_model=UpgradeOffer)
def sprawdz_wersje(
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> UpgradeOffer:
    """Odpytywane przez agenta przed zaplanowana synchronizacja.

    Dzieki temu raport powstaje juz z wersji, ktora ma byc na maszynie,
    zamiast czekac na kolejny cykl.
    """
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")
    oferta = _oferta_aktualizacji(db, asset)
    db.commit()
    return oferta


@router.get("/agent/release")
def pobierz_wersje(
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> FileResponse:
    """Wydaje plik wersji oczekiwanej na TEJ maszynie.

    Agent nie podaje, co chce pobrac - serwer wydaje dokladnie to, co sam
    wskazal jako wersje docelowa. Nie ma tu wiec parametru, ktorym dalo by
    sie wyciagnac dowolny plik z magazynu.
    """
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    wydanie = upgrades.wersja_docelowa(db, asset)
    if wydanie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="dla tej maszyny nie ustawiono wersji docelowej",
        )

    sciezka = upgrades.sciezka_pliku(wydanie)
    if not sciezka.is_file():
        log.error("brak pliku wersji %s w magazynie (%s)", wydanie.version, sciezka)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plik wersji jest niedostepny na serwerze",
        )

    upgrades.zapisz_wynik(db, asset, wydanie, "pobrana")
    db.commit()
    log.info("maszyna %s pobiera wersje agenta %s", asset.hostname, wydanie.version)

    return FileResponse(
        path=sciezka,
        media_type="application/octet-stream",
        filename=wydanie.filename,
        headers={"X-CMDB-SHA256": wydanie.sha256, "X-CMDB-Version": wydanie.version},
    )


@router.post("/agent/upgrade-result")
def zglos_wynik_aktualizacji(
    wynik: UpgradeResult,
    db: Session = Depends(get_db),
    auth: tuple[AgentCredential, TenantContext] = Depends(require_agent),
) -> dict:
    """Agent zglasza, co sie stalo - dzieki temu w panelu widac nieudane proby."""
    credential, ctx = auth
    asset = db.get(Asset, credential.asset_id)
    if asset is None or asset.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="maszyna nie istnieje")

    wydanie = upgrades.wersja_docelowa(db, asset)
    upgrades.zapisz_wynik(db, asset, wydanie, wynik.status, wynik.detail)
    db.commit()

    if wynik.status == "blad":
        log.warning(
            "aktualizacja agenta na %s nie powiodla sie (%s): %s",
            asset.hostname, wynik.version, wynik.detail,
        )
    return {"status": "przyjeto"}
