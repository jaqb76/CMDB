"""Network observations never create or overwrite inventory without approval."""
from sqlalchemy import select

from ..models import DiscoveryDevice, DiscoveryScanner, as_utc


def store_discovery(db, ctx, scanner, report):
    # Caller holds scanner asset row lock: concurrent reports cannot duplicate IPs.
    status = db.get(DiscoveryScanner, scanner.id)
    observed = as_utc(report.scanned_at)
    if status is not None and observed <= as_utc(status.scanned_at):
        return
    if status is None:
        status = DiscoveryScanner(asset_id=scanner.id, tenant_id=ctx.tenant_id)
        db.add(status)
    status.scanned_at = observed
    status.details = report.model_dump(mode="json", exclude={"devices"})
    existing = {row.ip: row for row in db.execute(select(DiscoveryDevice).where(
        DiscoveryDevice.scanner_id == scanner.id, DiscoveryDevice.tenant_id == ctx.tenant_id)).scalars()}
    for device in report.devices:
        ip, mac = str(device.ip), device.mac.lower()
        row = existing.get(ip)
        if row is None:
            row = DiscoveryDevice(tenant_id=ctx.tenant_id, scanner_id=scanner.id, ip=ip, first_seen=observed)
            db.add(row)
        elif (mac and row.mac and mac != row.mac) or (device.hostname and row.hostname and
                device.hostname.casefold() != row.hostname.casefold()):
            # DHCP reassignment must not retain an approval for a different device.
            row.asset_id = None
            row.first_seen = observed
        # Keep last known identity for DHCP comparisons; UI shows only observation.mac.
        row.mac = mac or row.mac or ""
        row.hostname = device.hostname
        row.device_type = device.device_type
        row.last_seen = observed
        row.observation = device.model_dump(mode="json")
    # Absence from a partial scan does not retire/delete a device or refresh last_seen.
