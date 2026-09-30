"""13.1 incident-response building blocks (PLAN §7.5.5): malware event → quick backup → scan.

Pure functions: they turn API responses into the next request, so the same code runs in
live and mock mode. Bodies are validated against the spec by the client before sending.
"""

from __future__ import annotations

from typing import Any

QUICK_BACKUP_OPERATIONS = {
    "HyperV": "StartHyperVQuickBackupJob",
    "VMware": "StartVSphereQuickBackupJob",
    "WindowsPhysical": "StartAgentQuickBackupJob",
    "LinuxPhysical": "StartAgentQuickBackupJob",
}


class UnsupportedMachine(ValueError):
    """The machine's platform has no Quick Backup operation in 1.3-rev2."""


def _host_from_path(path: str | None) -> str:
    # Backup-object paths look like `host\vm` (Hyper-V) or `vcenter\datacenter\…\vm`.
    return (path or "").replace("/", "\\").split("\\", 1)[0]


def quick_backup_request(backup_object: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(operationId, body) for a Quick Backup of the machine behind a backup object."""
    platform = backup_object.get("platformName", "")
    operation_id = QUICK_BACKUP_OPERATIONS.get(platform)
    if operation_id is None:
        raise UnsupportedMachine(f"Quick Backup isn't available for {platform or 'this'} machines.")
    name = backup_object.get("name", "")
    if platform == "HyperV":
        return operation_id, {
            "platform": "HyperV",
            "hostName": _host_from_path(backup_object.get("path")),
            "name": name,
            "type": backup_object.get("hvType") or "VirtualMachine",
            "objectId": backup_object["objectId"],
        }
    if platform == "VMware":
        return operation_id, {
            "platform": "VSphere",
            "hostName": _host_from_path(backup_object.get("path")),
            "name": name,
            "type": backup_object.get("viType") or "VirtualMachine",
            "objectId": backup_object["objectId"],
        }
    groups = backup_object.get("protectionGroupIds") or []
    if not backup_object.get("computerId") or not groups:
        raise UnsupportedMachine(f"{name} has no protection group, so it can't be quick-backed-up.")
    return operation_id, {
        "platform": "Agent",
        "id": backup_object["computerId"],
        "name": name,
        "type": "LinuxComputer" if platform == "LinuxPhysical" else "WindowsComputer",
        "protectionGroupId": groups[0],
    }


def backup_scan_request(backup_id: str, backup_object_id: str) -> dict[str, Any]:
    """Scan the most recent restore point of one machine with the antivirus engine."""
    return {
        "type": "Backup",
        "scanMode": "MostRecent",
        "scanEngine": {"useAntivirusEngine": True, "useYaraRule": False},
        "backupObjectPair": [{"backupId": backup_id, "backupObjectId": backup_object_id}],
    }
