"""Write the mock-mode fixtures in src/pulse/mock/fixtures/.

PLAN §6.1 wants fixtures recorded from a lab server (`pulse record`, v1.1). Until a
lab recording exists, this script authors deterministic, anonymised stand-ins. Every
file is validated against the 1.3-rev2 spec by tests/contract/test_mock_fixtures.py.

    uv run python scripts/make_mock_fixtures.py
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "pulse" / "mock" / "fixtures"

# All date-times are written relative to this instant; the mock shifts them to "now".
CAPTURED_AT = datetime(2026, 9, 30, 11, 40, tzinfo=UTC)
NS = uuid.UUID("6f1f3c52-2b8e-4f0e-9d5c-5a1f0c7e2a10")
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
SERVER = "vbr01.lab.local"


def uid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def ts(delta: timedelta = timedelta()) -> str:
    return (CAPTURED_AT + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def h(hours: float) -> timedelta:
    return timedelta(hours=hours)


def page(data: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(data)
    return {"data": data, "pagination": {"total": n, "count": n, "skip": 0, "limit": max(n, 1)}}


# name, type, workload, last result, status, last run (h ago), next run (h ahead), objects
JOBS: list[tuple[str, str, str, str, str, float, float | None, int]] = [
    ("SQL Daily", "VSphereBackup", "Vm", "Success", "Inactive", 3.67, 8.33, 4),
    ("SQL Logs hourly", "VSphereBackup", "Vm", "Success", "Inactive", 0.67, 0.33, 4),
    ("SQL Reporting", "HyperVBackup", "Vm", "Success", "Inactive", 5.0, 19.0, 2),
    ("File servers", "HyperVBackup", "Vm", "Success", "Inactive", 4.5, 19.5, 3),
    ("DR replica", "VSphereReplica", "Vm", "Warning", "Inactive", 5.67, 6.33, 12),
    ("Exchange mailbox servers", "VSphereBackup", "Vm", "Success", "Inactive", 6.0, 18.0, 3),
    ("Domain controllers", "VSphereBackup", "Vm", "Success", "Inactive", 7.0, 17.0, 2),
    ("Web tier", "VSphereBackup", "Vm", "Success", "Inactive", 2.0, 10.0, 8),
    ("App tier", "VSphereBackup", "Vm", "Success", "Inactive", 2.0, 10.0, 6),
    ("Oracle ERP", "LinuxAgentBackup", "Server", "Success", "Inactive", 8.0, 16.0, 2),
    ("Linux build agents", "LinuxAgentBackup", "Server", "Warning", "Inactive", 9.0, 15.0, 14),
    ("Windows file cluster", "WindowsAgentBackup", "Server", "Success", "Inactive", 6.5, 17.5, 2),
    (
        "Branch office laptops",
        "WindowsAgentBackup",
        "Workstation",
        "Success",
        "Inactive",
        10.0,
        14.0,
        48,
    ),
    ("Offsite copy", "BackupCopy", "Vm", "Success", "Inactive", 1.0, None, 36),
    ("Hyper-V lab hosts", "HyperVBackup", "Vm", "Success", "Inactive", 11.0, 13.0, 5),
    ("VDI golden images", "VSphereBackup", "Vm", "Success", "Inactive", 30.0, 138.0, 3),
    ("SharePoint farm", "VSphereBackup", "Vm", "Success", "Inactive", 6.0, 18.0, 4),
    ("CRM database", "VSphereBackup", "Vm", "Success", "Inactive", 4.0, 20.0, 2),
    ("HR systems", "HyperVBackup", "Vm", "Success", "Inactive", 7.5, 16.5, 3),
    ("Finance close", "VSphereBackup", "Vm", "Failed", "Inactive", 3.0, 21.0, 5),
    ("Print servers", "HyperVBackup", "Vm", "Success", "Disabled", 170.0, None, 2),
    ("Jump hosts", "VSphereBackup", "Vm", "Success", "Inactive", 8.0, 16.0, 3),
    ("Kubernetes etcd VMs", "VSphereBackup", "Vm", "Success", "Inactive", 1.5, 4.5, 3),
    ("Monitoring stack", "VSphereBackup", "Vm", "Success", "Inactive", 9.0, 15.0, 4),
    ("Ticketing system", "HyperVBackup", "Vm", "Success", "Inactive", 5.5, 18.5, 2),
    ("Intranet", "VSphereBackup", "Vm", "Success", "Inactive", 6.0, 18.0, 2),
    ("Payroll", "VSphereBackup", "Vm", "Success", "Inactive", 12.0, 12.0, 2),
    ("Legal archive", "WindowsAgentBackup", "Server", "Success", "Inactive", 20.0, 148.0, 1),
    ("Dev databases", "VSphereBackup", "Vm", "Warning", "Inactive", 10.0, 14.0, 9),
    ("QA environment", "HyperVBackup", "Vm", "Success", "Inactive", 10.0, 14.0, 11),
    ("Staging web", "VSphereBackup", "Vm", "Success", "Inactive", 10.0, 14.0, 4),
    ("CI runners", "LinuxAgentBackup", "Server", "Success", "Inactive", 3.0, 21.0, 6),
    ("Backup proxies", "VSphereBackup", "Vm", "Success", "Inactive", 22.0, 2.0, 4),
    ("Mail relays", "VSphereBackup", "Vm", "Success", "Inactive", 6.0, 18.0, 2),
    ("DNS and DHCP", "HyperVBackup", "Vm", "Success", "Inactive", 7.0, 17.0, 2),
    ("Identity services", "VSphereBackup", "Vm", "Success", "Inactive", 7.0, 17.0, 3),
    ("Data warehouse", "VSphereBackup", "Vm", "Success", "Inactive", 13.0, 11.0, 2),
    ("ETL workers", "LinuxAgentBackup", "Server", "Success", "Inactive", 13.0, 11.0, 4),
    ("Analytics notebooks", "VSphereBackup", "Vm", "Success", "Inactive", 14.0, 10.0, 3),
    ("Call center", "HyperVBackup", "Vm", "Success", "Inactive", 5.0, 19.0, 4),
    (
        "Warehouse scanners",
        "WindowsAgentBackup",
        "Workstation",
        "Success",
        "Inactive",
        15.0,
        9.0,
        22,
    ),
    ("Retail POS gateways", "WindowsAgentBackup", "Server", "Warning", "Inactive", 4.0, 20.0, 16),
]

REPOSITORIES = [
    # name, type, host, path, capacity, free, online
    ("Primary ReFS", "WinLocal", "repo01.lab.local", "E:\\Backups", 40960.0, 11264.5, True),
    (
        "Hardened Linux",
        "LinuxLocal",
        "hardened01.lab.local",
        "/mnt/xfs/backups",
        81920.0,
        52011.25,
        True,
    ),
    (
        "Immutable object storage",
        "S3Compatible",
        None,
        "pulse-lab-immutable",
        102400.0,
        71680.0,
        True,
    ),
    ("Offsite S3", "AmazonS3", None, "pulse-offsite", 204800.0, 169984.0, True),
    ("NAS share", "Smb", "nas01.lab.local", "\\\\nas01\\veeam", 16384.0, 1228.8, True),
    ("Old NFS repository", "Nfs", "nfs02.lab.local", "/exports/veeam", 8192.0, 4096.0, False),
]

REPO_BY_JOB_TYPE = {
    "BackupCopy": "Offsite S3",
    "LinuxAgentBackup": "Hardened Linux",
    "WindowsAgentBackup": "Hardened Linux",
    "VSphereReplica": "Primary ReFS",
}


def job_id(name: str) -> str:
    return uid("job", name)


def repo_id(name: str) -> str:
    return uid("repository", name)


def job_states() -> dict[str, Any]:
    data = []
    for name, jtype, workload, result, status, last, nxt, objects in JOBS:
        repo = REPO_BY_JOB_TYPE.get(
            jtype, "Primary ReFS" if jtype != "HyperVBackup" else "Hardened Linux"
        )
        state: dict[str, Any] = {
            "id": job_id(name),
            "name": name,
            "type": jtype,
            "description": "[pulse-demo] Demo job" if name == "SQL Daily" else "",
            "status": status,
            "lastRun": ts(-h(last)),
            "lastResult": result,
            "workload": workload,
            "repositoryId": repo_id(repo),
            "repositoryName": repo,
            "objectsCount": objects,
            "highPriority": name in ("SQL Daily", "Domain controllers"),
            "progressPercent": 0,
        }
        if nxt is not None and status != "Disabled":
            state["nextRun"] = ts(h(nxt))
        data.append(state)
    return {"operationId": "GetAllJobsStates", "body": page(data)}


def repositories() -> dict[str, Any]:
    data = []
    for name, rtype, host, path, cap, free, online in REPOSITORIES:
        repo: dict[str, Any] = {
            "id": repo_id(name),
            "name": name,
            "type": rtype,
            "description": "",
            "path": path,
            "capacityGB": cap,
            "freeGB": free,
            "usedSpaceGB": round(cap - free, 2),
            "isOnline": online,
            "isOutOfDate": False,
        }
        if host:
            repo["hostId"] = uid("host", host)
            repo["hostName"] = host
        data.append(repo)
    return {"operationId": "GetAllRepositoriesStates", "body": page(data)}


SESSION_TYPE = {"VSphereReplica": "ReplicaJob", "BackupCopy": "BackupCopyJob"}
PLATFORM = {
    "VSphereBackup": "VMware",
    "VSphereReplica": "VMware",
    "HyperVBackup": "HyperV",
    "WindowsAgentBackup": "WindowsPhysical",
    "LinuxAgentBackup": "LinuxPhysical",
    "BackupCopy": "VMware",
}


def sessions() -> dict[str, Any]:
    data = []
    usn = 90_000
    for name, jtype, _w, result, status, last, _n, _o in JOBS:
        if status == "Disabled":
            continue
        for run in range(2):
            start = -h(last + run * 24)
            duration = timedelta(minutes=4 + (len(name) % 9), seconds=len(name) * 3 % 60)
            usn += 1
            run_result = result if run == 0 else "Success"
            data.append(
                {
                    "id": uid("session", name, str(run)),
                    "name": name,
                    "jobId": job_id(name),
                    "sessionType": SESSION_TYPE.get(jtype, "BackupJob"),
                    "creationTime": ts(start),
                    "endTime": ts(start + duration),
                    "state": "Stopped",
                    "progressPercent": 100,
                    "result": {
                        "result": run_result,
                        "message": {
                            "Warning": "1 of the machines was processed with warnings.",
                            "Failed": "Error: Failed to create a VM snapshot.",
                        }.get(run_result, ""),
                        "isCanceled": False,
                    },
                    "usn": usn,
                    "platformName": PLATFORM.get(jtype, "VMware"),
                    "platformId": ZERO_UUID,
                    "initiatedBy": "Scheduler",
                }
            )
    data.sort(key=lambda s: s["creationTime"], reverse=True)
    return {"operationId": "GetAllSessions", "body": page(data)}


FS02 = {"name": "FS-02", "uuid": "5b0c9a61-3f7e-4c2a-8d11-0f6a2e9b7c42"}
FILE_BACKUP = uid("backup", "File servers")


def backups() -> dict[str, Any]:
    data = []
    for name, jtype, *_ in JOBS:
        if jtype == "VSphereReplica":
            continue
        data.append(
            {
                "id": uid("backup", name),
                "jobId": job_id(name),
                "name": name,
                "platformName": PLATFORM.get(jtype, "VMware"),
                "platformId": ZERO_UUID,
                "creationTime": ts(-h(24 * 90)),
                "repositoryId": repo_id(REPO_BY_JOB_TYPE.get(jtype, "Primary ReFS")),
                "repositoryName": REPO_BY_JOB_TYPE.get(jtype, "Primary ReFS"),
                "jobType": jtype,
            }
        )
    return {"operationId": "GetAllBackups", "body": page(data)}


def restore_points() -> dict[str, Any]:
    data = []
    for vm in ("FS-01", "FS-02", "FS-03"):
        for n in range(3):
            data.append(
                {
                    "id": uid("restorePoint", vm, str(n)),
                    "name": vm,
                    "platformName": "HyperV",
                    "platformId": ZERO_UUID,
                    "creationTime": ts(-h(4.5 + 24 * n)),
                    "backupId": FILE_BACKUP,
                    "type": "Full" if n == 2 else "Increment",
                    "sessionId": uid("session", "File servers", str(min(n, 1))),
                    "allowedOperations": ["StartFlrRestore"],
                    "malwareStatus": "Clean",
                    "guestOsFamily": "Windows",
                    "originalSize": 214_748_364_800,
                }
            )
    return {"operationId": "GetAllObjectRestorePoints", "body": page(data)}


def backup_object_id(vm: str) -> str:
    return uid("backupObject", vm)


def malware_events() -> dict[str, Any]:
    event = {
        "id": uid("malwareEvent", "FS-02"),
        "type": "RenamedFiles",
        "creationTimeUtc": ts(-timedelta(minutes=28)),
        "detectionTimeUtc": ts(-timedelta(minutes=29)),
        "machine": {
            "displayName": "FS-02",
            "uuid": FS02["uuid"],
            "backupObjectId": backup_object_id("FS-02"),
        },
        "state": "Created",
        "details": "Suspicious file activity on FS-02: 1,284 files renamed with an unknown "
        "extension in 3 minutes.",
        "source": "External",
        "severity": "Suspicious",
        "createdBy": "svc-pulse-ir",
        "engine": "Lab EDR",
    }
    return {"operationId": "ViewSuspiciousActivityEvents", "body": page([event])}


def incident_objects() -> dict[str, Any]:
    """Machine descriptions the incident flow sends to the Quick Backup operations."""
    return {
        "hyperV": {
            "schema": "HyperVObjectModel",
            "body": {
                "platform": "HyperV",
                "hostName": "hv01.lab.local",
                "name": "FS-02",
                "type": "VirtualMachine",
                "objectId": FS02["uuid"],
                "urn": f"HyperV:hv01.lab.local:{FS02['uuid']}",
            },
        },
        "backupObjectPair": {
            "schema": "BackupObjectPair",
            "body": {"backupId": FILE_BACKUP, "backupObjectId": backup_object_id("FS-02")},
        },
    }


def authorization_events() -> dict[str, Any]:
    rows = [
        ("Delete backup 'Legal archive'", "Pending", 0.5, None),
        ("Remove immutability from 'Hardened Linux'", "Rejected", 26.0, "secops-lead"),
        ("Add user 'contractor-07' as Backup Administrator", "Approved", 50.0, "secops-lead"),
    ]
    data = []
    for name, state, ago, processed_by in rows:
        event: dict[str, Any] = {
            "id": uid("authEvent", name),
            "name": name,
            "description": "Four-eyes authorization request.",
            "state": state,
            "creationTime": ts(-h(ago)),
            "createdBy": "LAB\\backup-admin",
            "expirationTime": ts(-h(ago) + h(168)),
        }
        if processed_by:
            event["processedBy"] = processed_by
            event["processedTime"] = ts(-h(ago) + h(1))
        data.append(event)
    return {"operationId": "GetAllAuthorizationEvents", "body": page(data)}


def server() -> dict[str, Any]:
    return {
        "info": {
            "operationId": "GetServerInfo",
            "body": {
                "vbrId": uid("server", SERVER),
                "name": SERVER,
                "buildVersion": "13.1.0.411",
                "patches": [],
                "databaseVendor": "PostgreSql",
                "sqlServerEdition": "",
                "sqlServerVersion": "",
                "databaseSchemaVersion": "13.1.0.411",
                "databaseContentVersion": "13.1.0.411",
                "veeamRegistration": {"isRegistered": False},
                "platform": "Windows",
            },
        },
        "certificate": {
            "operationId": "GetServerCertificate",
            "body": {
                "thumbprint": "8F3A2C6E1B9D47F0A5C3E2D1B0A9F8E7D6C5B4A3",
                "serialNumber": "5F00000012A7C3B9E0D4F1A2000000000012",
                "keyAlgorithm": "RSA",
                "keySize": "2048",
                "subject": f"CN={SERVER}",
                "issuedTo": SERVER,
                "issuedBy": "Pulse Lab Root CA",
                "validFrom": ts(-h(24 * 200)),
                "validBy": ts(h(24 * 530)),
                "isTrusted": True,
            },
        },
    }


def write(name: str, content: dict[str, Any]) -> None:
    path = OUT / name
    path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"capturedAt": ts(), "source": "authored by scripts/make_mock_fixtures.py"}
    write("_meta.json", meta)
    write("job_states.json", job_states())
    write("repositories.json", repositories())
    write("sessions.json", sessions())
    write("backups.json", backups())
    write("restore_points.json", restore_points())
    write("malware_events.json", malware_events())
    write("incident_objects.json", incident_objects())
    write("authorization_events.json", authorization_events())
    srv = server()
    write("server_info.json", srv["info"])
    write("server_certificate.json", srv["certificate"])


if __name__ == "__main__":
    main()
