"""In-memory state of the mock VBR server: tokens, roles, jobs, sessions, events.

Everything the HTTP layer (pulse.mock.app) serves comes from here. Time is driven by
an injectable clock so tests and the scenario runner can fast-forward.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import secrets
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pulse.mock.scenarios import FIXTURES, Scenario, SessionKind, Timeline, load_scenarios

ACCOUNTS = {
    "svc-pulse-ops": "Backup Operator",
    "svc-pulse-ir": "Incident API Operator",
    "svc-pulse-view": "Backup Viewer",
}

_COMMON = {
    "Logout",
    "GetServerInfo",
    "GetServerTime",
    "GetServerCertificate",
    "GetSession",
    "GetSessionLogs",
    "GetAllSessions",
    "GetAllBackups",
    "GetAllObjectRestorePoints",
}
_READ_INFRA = {
    "GetAllJobs",
    "GetAllJobsStates",
    "GetAllRepositoriesStates",
    "GetAllAuthorizationEvents",
}
_JOB_TRIGGERS = {"StartJob", "StopJob", "RetryJob"}
_QUICK_BACKUP = {
    "StartHyperVQuickBackupJob",
    "StartAgentQuickBackupJob",
    "StartVSphereQuickBackupJob",
}
_MALWARE_READ = {"ViewSuspiciousActivityEvents"}
_MALWARE_WRITE = {"CreateSuspiciousActivityEvent", "StartMalwareBackupScan"}

# Approximation of VBR's built-in roles, enough for the RBAC demo moment.
ROLE_OPERATIONS: dict[str, set[str]] = {
    "Backup Viewer": _COMMON | _READ_INFRA | _MALWARE_READ,
    "Backup Operator": _COMMON | _READ_INFRA | _MALWARE_READ | _JOB_TRIGGERS | _QUICK_BACKUP,
    "Incident API Operator": _COMMON | _MALWARE_READ | _MALWARE_WRITE | _QUICK_BACKUP,
}

ACCESS_TOKEN_LIFETIME = 900
REFRESH_TOKEN_LIFETIME = 24 * 3600
STOP_DURATION = 3.0
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SESSION_TYPE = {"VSphereReplica": "ReplicaJob", "BackupCopy": "BackupCopyJob"}
_PLATFORM = {
    "VSphereBackup": "VMware",
    "VSphereReplica": "VMware",
    "HyperVBackup": "HyperV",
    "WindowsAgentBackup": "WindowsPhysical",
    "LinuxAgentBackup": "LinuxPhysical",
}


class MockError(Exception):
    """An error response in the spec's `Error` shape."""

    def __init__(self, status: int, error_code: str, message: str, resource_id: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body: dict[str, Any] = {"errorCode": error_code, "message": message}
        if resource_id:
            self.body["resourceId"] = resource_id


@dataclass
class _AccessToken:
    username: str
    issued: float


@dataclass
class MockSession:
    id: str
    name: str
    job_id: str
    session_type: str
    kind: SessionKind
    timeline: Timeline
    started: float
    usn: int
    platform_name: str
    initiated_by: str
    object_name: str = ""
    backup_id: str | None = None
    stop_requested: float | None = None


@dataclass
class Request:
    """What a handler needs from an HTTP request."""

    username: str | None
    token: str | None
    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rebase(node: Any, delta: timedelta) -> Any:
    """Shift every ISO date-time in a fixture so captured data looks fresh."""
    if isinstance(node, dict):
        return {k: _rebase(v, delta) for k, v in node.items()}
    if isinstance(node, list):
        return [_rebase(v, delta) for v in node]
    if isinstance(node, str) and _ISO.match(node):
        return _iso(datetime.strptime(node, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + delta)
    return node


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _page(
    items: Iterable[dict[str, Any]],
    query: dict[str, str],
    *,
    filters: dict[str, Callable[[dict[str, Any], str], bool]] | None = None,
    order: dict[str, str] | None = None,
    default_order: tuple[str, bool] | None = None,
) -> dict[str, Any]:
    """Server-side filtering, ordering and paging, like the real collection endpoints."""
    rows = list(items)
    if name := query.get("nameFilter"):
        rows = [r for r in rows if fnmatch.fnmatchcase(r.get("name", "").lower(), name.lower())]
    for param, predicate in (filters or {}).items():
        if (value := query.get(param)) is not None:
            rows = [r for r in rows if predicate(r, value)]

    column = query.get("orderColumn")
    if column is not None:
        if not order or column not in order:
            raise MockError(400, "UnexpectedContent", f"Unsupported orderColumn '{column}'.")
        key, ascending = order[column], query.get("orderAsc", "true").lower() == "true"
    elif default_order:
        key, ascending = default_order
    else:
        key = ""
        ascending = True
    if key:
        rows.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=not ascending)

    try:
        skip = int(query.get("skip", "0"))
        limit = int(query.get("limit", "200"))
    except ValueError as exc:
        raise MockError(400, "UnexpectedContent", "skip and limit must be integers.") from exc
    if skip < 0 or limit < 1:
        raise MockError(400, "UnexpectedContent", "skip must be ≥ 0 and limit ≥ 1.")
    chunk = rows[skip : skip + limit]
    return {
        "data": chunk,
        "pagination": {"total": len(rows), "count": len(chunk), "skip": skip, "limit": limit},
    }


def _equals(field_name: str) -> Callable[[dict[str, Any], str], bool]:
    return lambda row, value: str(row.get(field_name, "")).lower() == value.lower()


class MockVbr:
    """A scripted VBR 13.1 server. Construct one per app; tests may share a fake clock."""

    def __init__(
        self,
        scenario: str = "happy",
        *,
        clock: Callable[[], float] = time.monotonic,
        speed: float = 1.0,
    ) -> None:
        self._clock = clock
        self._speed = speed
        self.scenarios = load_scenarios()
        self.calls: Counter[str] = Counter()
        self._faults: dict[str, deque[int]] = {}
        self.reset()
        self.set_scenario(scenario)

    # ------------------------------------------------------------------ control

    def reset(self) -> None:
        self._t0 = self._clock()
        self._wall0 = datetime.now(UTC).replace(microsecond=0)
        captured = datetime.strptime(_fixture("_meta.json")["capturedAt"], "%Y-%m-%dT%H:%M:%SZ")
        delta = self._wall0 - captured.replace(tzinfo=UTC)

        def load(name: str) -> Any:
            return _rebase(_fixture(name), delta)

        self.server_info: dict[str, Any] = _fixture("server_info.json")["body"]
        self.certificate: dict[str, Any] = load("server_certificate.json")["body"]
        self.jobs: dict[str, dict[str, Any]] = {
            j["id"]: j for j in load("job_states.json")["body"]["data"]
        }
        self.repositories: list[dict[str, Any]] = load("repositories.json")["body"]["data"]
        self.history: list[dict[str, Any]] = load("sessions.json")["body"]["data"]
        self.backups: list[dict[str, Any]] = load("backups.json")["body"]["data"]
        self.restore_points: list[dict[str, Any]] = load("restore_points.json")["body"]["data"]
        self.authorization_events: list[dict[str, Any]] = load("authorization_events.json")["body"][
            "data"
        ]
        self._seed_events: list[dict[str, Any]] = load("malware_events.json")["body"]["data"]
        self.incident_objects: dict[str, Any] = {
            k: v["body"] for k, v in _fixture("incident_objects.json").items()
        }
        self._created_events: list[dict[str, Any]] = []
        self.sessions: dict[str, MockSession] = {}
        self._access: dict[str, _AccessToken] = {}
        self._refresh: dict[str, _AccessToken] = {}
        self._usn = 100_000
        self.calls.clear()
        self._faults.clear()

    def set_scenario(self, key: str) -> None:
        if key not in self.scenarios:
            raise KeyError(f"Unknown scenario {key!r}. Known: {', '.join(self.scenarios)}")
        self.scenario: Scenario = self.scenarios[key]

    def inject_fault(self, operation_id: str, status: int = 500, times: int = 1) -> None:
        """Make the next `times` calls to `operation_id` fail. `status=0` drops the connection."""
        self._faults.setdefault(operation_id, deque()).extend([status] * times)

    def take_fault(self, operation_id: str) -> int | None:
        queue = self._faults.get(operation_id)
        return queue.popleft() if queue else None

    def elapsed(self) -> float:
        return (self._clock() - self._t0) * self._speed

    def now(self) -> datetime:
        return self._wall0 + timedelta(seconds=self.elapsed())

    def role_of(self, username: str) -> str:
        return self.scenario.force_role or ACCOUNTS[username]

    # ------------------------------------------------------------------ auth

    def create_token(self, form: dict[str, str]) -> dict[str, Any]:
        grant = form.get("grant_type", "").lower()
        if grant == "password":
            username = form.get("username", "").rpartition("\\")[2].lower()
            if username not in ACCOUNTS or not form.get("password"):
                raise MockError(401, "AccessDenied", "Invalid user name or password.")
        elif grant == "refresh_token":
            record = self._refresh.pop(form.get("refresh_token", ""), None)
            if record is None or self.elapsed() - record.issued > REFRESH_TOKEN_LIFETIME:
                raise MockError(401, "InvalidToken", "The refresh token is invalid or expired.")
            username = record.username
        else:
            raise MockError(400, "UnexpectedContent", "Unsupported grant_type.")

        access = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + secrets.token_urlsafe(96)
        refresh = secrets.token_urlsafe(48)
        issued = self.elapsed()
        self._access[access] = _AccessToken(username, issued)
        self._refresh[refresh] = _AccessToken(username, issued)
        now = self.now()
        return {
            "access_token": access,
            "token_type": "bearer",
            "refresh_token": refresh,
            "expires_in": ACCESS_TOKEN_LIFETIME,
            ".issued": _iso(now),
            ".expires": _iso(now + timedelta(seconds=ACCESS_TOKEN_LIFETIME)),
        }

    def authenticate(self, authorization: str | None) -> tuple[str, str]:
        scheme, _, token = (authorization or "").partition(" ")
        record = self._access.get(token) if scheme.lower() == "bearer" else None
        if record is None:
            raise MockError(401, "InvalidToken", "The access token is missing or invalid.")
        lifetime = self.scenario.access_token_lifetime or ACCESS_TOKEN_LIFETIME
        if self.elapsed() - record.issued >= lifetime:
            raise MockError(401, "ExpiredToken", "The access token has expired.")
        return record.username, token

    def authorize(self, username: str, operation_id: str) -> None:
        if operation_id not in ROLE_OPERATIONS.get(self.role_of(username), set()):
            raise MockError(403, "AccessDenied", "Access denied.")

    def logout(self, req: Request) -> dict[str, Any]:
        self._access.pop(req.token or "", None)
        return {}

    # ------------------------------------------------------------------ service

    def get_server_info(self, req: Request) -> dict[str, Any]:
        return self.server_info

    def get_server_time(self, req: Request) -> dict[str, Any]:
        return {"serverTime": _iso(self.now()), "timeZone": "UTC", "ianaTimeZoneId": "Etc/UTC"}

    def get_server_certificate(self, req: Request) -> dict[str, Any]:
        return self.certificate

    # ------------------------------------------------------------------ jobs

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise MockError(404, "NotFound", "The job doesn't exist.", job_id)
        return job

    def _latest_session(self, job_id: str) -> MockSession | None:
        runs = [s for s in self.sessions.values() if s.job_id == job_id and s.kind == "job"]
        return max(runs, key=lambda s: s.started) if runs else None

    def job_state(self, job_id: str) -> dict[str, Any]:
        state = copy.deepcopy(self._job(job_id))
        latest = self._latest_session(job_id)
        if latest is None:
            return state
        body = self.session_body(latest)
        status = {"Starting": "Starting", "Working": "Running", "Stopping": "Stopping"}
        state["sessionId"] = latest.id
        state["lastRun"] = body["creationTime"]
        if body["state"] == "Stopped":
            state["status"] = "Inactive"
            state["lastResult"] = body["result"]["result"]
            state["progressPercent"] = 0
        else:
            state["status"] = status.get(body["state"], "Running")
            state["progressPercent"] = body["progressPercent"]
        return state

    def get_all_jobs_states(self, req: Request) -> dict[str, Any]:
        return _page(
            (self.job_state(job_id) for job_id in self.jobs),
            req.query,
            filters={
                "idFilter": _equals("id"),
                "typeFilter": _equals("type"),
                "statusFilter": _equals("status"),
                "lastResultFilter": _equals("lastResult"),
                "workloadFilter": _equals("workload"),
            },
            order={
                "Name": "name",
                "Type": "type",
                "Status": "status",
                "LastRun": "lastRun",
                "LastResult": "lastResult",
                "NextRun": "nextRun",
                "Description": "description",
                "RepositoryId": "repositoryId",
                "ObjectsCount": "objectsCount",
                "RepositoryName": "repositoryName",
            },
        )

    def _running(self, job_id: str) -> MockSession | None:
        latest = self._latest_session(job_id)
        if latest is not None and self.session_body(latest)["state"] != "Stopped":
            return latest
        return None

    def start_job(self, req: Request) -> dict[str, Any]:
        job = self._job(req.path["id"])
        if job["status"] == "Disabled":
            raise MockError(400, "UnexpectedContent", "The job is disabled.", job["id"])
        if self._running(job["id"]):
            raise MockError(400, "UnexpectedContent", "The job is already running.", job["id"])
        return self.session_body(self._start_job_session(job, req))

    def _start_job_session(self, job: dict[str, Any], req: Request) -> MockSession:
        return self._new_session(
            name=job["name"],
            job_id=job["id"],
            session_type=_SESSION_TYPE.get(job["type"], "BackupJob"),
            kind="job",
            platform_name=_PLATFORM.get(job["type"], "VMware"),
            initiated_by=req.username or "",
        )

    def stop_job(self, req: Request) -> dict[str, Any]:
        job = self._job(req.path["id"])
        session = self._running(job["id"])
        if session is None:
            raise MockError(400, "UnexpectedContent", "The job isn't running.", job["id"])
        session.stop_requested = self.elapsed()
        return self.session_body(session)

    def retry_job(self, req: Request) -> dict[str, Any]:
        job = self.job_state(req.path["id"])
        if job["lastResult"] not in ("Failed", "Warning") or job["status"] != "Inactive":
            raise MockError(
                400,
                "UnexpectedContent",
                "Only a stopped job that failed can be retried.",
                job["id"],
            )
        return self.session_body(self._start_job_session(self._job(job["id"]), req))

    # ------------------------------------------------------------------ quick backup

    def _job_for_object(self, name: str, job_type: str) -> dict[str, Any] | None:
        backup_ids = {rp["backupId"] for rp in self.restore_points if rp["name"] == name}
        for backup in self.backups:
            if backup["id"] in backup_ids and backup.get("jobId") in self.jobs:
                return self.jobs[backup["jobId"]]
        return next((j for j in self.jobs.values() if j["type"] == job_type), None)

    def _quick_backup(self, req: Request, job_type: str, platform: str) -> MockSession:
        name = req.body["name"]
        job = self._job_for_object(name, job_type)
        backup_id = next((rp["backupId"] for rp in self.restore_points if rp["name"] == name), None)
        return self._new_session(
            name=job["name"] if job else f"Quick backup {name}",
            job_id=job["id"] if job else str(uuid.uuid4()),
            session_type="BackupJob",
            kind="quickBackup",
            platform_name=platform,
            initiated_by=req.username or "",
            object_name=name,
            backup_id=backup_id,
        )

    def start_hyperv_quick_backup(self, req: Request) -> dict[str, Any]:
        return self.session_body(self._quick_backup(req, "HyperVBackup", "HyperV"))

    def start_vsphere_quick_backup(self, req: Request) -> dict[str, Any]:
        return self.session_body(self._quick_backup(req, "VSphereBackup", "VMware"))

    def start_agent_quick_backup(self, req: Request) -> dict[str, Any]:
        # The agent variant answers with the job id only; find the session via GetAllSessions.
        session = self._quick_backup(req, "WindowsAgentBackup", "WindowsPhysical")
        return {"jobId": session.job_id}

    # ------------------------------------------------------------------ sessions

    def _new_session(self, *, kind: SessionKind, **fields: Any) -> MockSession:
        self._usn += 1
        session = MockSession(
            id=str(uuid.uuid4()),
            kind=kind,
            timeline=self.scenario.timeline(kind),
            started=self.elapsed(),
            usn=self._usn,
            **fields,
        )
        self.sessions[session.id] = session
        return session

    def _stop_view(self, session: MockSession) -> tuple[dict[str, Any], float] | None:
        """If the user stopped the session, its view and end offset; otherwise None."""
        if session.stop_requested is None:
            return None
        stop_at = session.stop_requested - session.started
        view = session.timeline.at(stop_at)
        if view["state"] == "Stopped":  # finished on its own before the stop landed
            return None
        since = self.elapsed() - session.stop_requested
        if since < STOP_DURATION:
            return {**view, "state": "Stopping"}, stop_at + STOP_DURATION
        result = {"result": "Failed", "message": "Stopped by user.", "isCanceled": True}
        return {**view, "state": "Stopped", "result": result}, stop_at + STOP_DURATION

    def _offset(self, session: MockSession) -> float:
        return self.elapsed() - session.started

    def session_body(self, session: MockSession) -> dict[str, Any]:
        stopped = self._stop_view(session)
        if stopped:
            view, end = stopped
        else:
            view, end = session.timeline.at(self._offset(session)), session.timeline.duration
        created = self._wall0 + timedelta(seconds=session.started)
        body: dict[str, Any] = {
            "id": session.id,
            "name": session.name,
            "jobId": session.job_id,
            "sessionType": session.session_type,
            "creationTime": _iso(created),
            "state": view["state"],
            "progressPercent": view["progressPercent"],
            "result": view["result"],
            "usn": session.usn,
            "platformName": session.platform_name,
            "platformId": ZERO_UUID,
            "initiatedBy": session.initiated_by,
        }
        if view["state"] == "Stopped":
            body["endTime"] = _iso(created + timedelta(seconds=end))
        return body

    def _all_session_bodies(self) -> list[dict[str, Any]]:
        return [self.session_body(s) for s in self.sessions.values()] + self.history

    def get_session(self, req: Request) -> dict[str, Any]:
        session_id = req.path["id"]
        if session_id in self.sessions:
            return self.session_body(self.sessions[session_id])
        for past in self.history:
            if past["id"] == session_id:
                return past
        raise MockError(404, "NotFound", "The session doesn't exist.", session_id)

    def get_all_sessions(self, req: Request) -> dict[str, Any]:
        return _page(
            self._all_session_bodies(),
            req.query,
            filters={
                "typeFilter": _equals("sessionType"),
                "stateFilter": _equals("state"),
                "resultFilter": lambda row, v: row["result"]["result"].lower() == v.lower(),
                "jobIdFilter": _equals("jobId"),
                "createdAfterFilter": lambda row, v: row["creationTime"] >= v,
            },
            order={
                "Name": "name",
                "SessionType": "sessionType",
                "CreationTime": "creationTime",
                "EndTime": "endTime",
                "State": "state",
                "InitiatedBy": "initiatedBy",
            },
            default_order=("creationTime", False),
        )

    def get_session_logs(self, req: Request) -> dict[str, Any]:
        body = self.get_session(req)
        created = datetime.strptime(body["creationTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        session = self.sessions.get(body["id"])
        if session is None:
            lines = [(0.0, "Succeeded", "Job started at {start}")]
            if body["result"]["message"]:
                status = {"Warning": "Warning", "Failed": "Failed"}.get(body["result"]["result"])
                lines.append((60.0, status or "Succeeded", body["result"]["message"]))
            lines.append((120.0, "Succeeded", "Job finished at {end}"))
            end_text = body.get("endTime", "")
            object_name = ""
        else:
            stopped = self._stop_view(session)
            cutoff = self._offset(session)
            if stopped and session.stop_requested is not None:
                cutoff = session.stop_requested - session.started
            lines = [(ln.after, ln.status, ln.title) for ln in session.timeline.logs_until(cutoff)]
            if stopped and stopped[0]["state"] == "Stopped":
                lines.append((stopped[1], "Failed", "Job has been stopped by user"))
            end_text = body.get("endTime", "")
            object_name = session.object_name

        records: list[dict[str, Any]] = []
        for i, (after, status, title) in enumerate(lines, start=1):
            moment = _iso(created + timedelta(seconds=after))
            text = title.format(start=body["creationTime"], end=end_text, object=object_name)
            records.append(
                {
                    "id": i,
                    "status": status,
                    "startTime": moment,
                    "updateTime": moment,
                    "title": text,
                }
            )
        if status_filter := req.query.get("statusFilter"):
            records = [r for r in records if r["status"].lower() == status_filter.lower()]
        return {"records": records, "totalRecords": len(records)}

    # ------------------------------------------------------------------ infrastructure

    def get_all_repositories_states(self, req: Request) -> dict[str, Any]:
        return _page(
            self.repositories,
            req.query,
            filters={
                "idFilter": _equals("id"),
                "typeFilter": _equals("type"),
                "isOnlineFilter": lambda row, v: str(row["isOnline"]).lower() == v.lower(),
            },
            order={
                "Name": "name",
                "Type": "type",
                "Host": "hostName",
                "Path": "path",
                "CapacityGB": "capacityGB",
                "FreeGB": "freeGB",
                "UsedSpaceGB": "usedSpaceGB",
                "Description": "description",
                "Status": "isOnline",
            },
        )

    def get_all_backups(self, req: Request) -> dict[str, Any]:
        return _page(
            self.backups,
            req.query,
            filters={"jobIdFilter": _equals("jobId"), "jobTypeFilter": _equals("jobType")},
            order={"Name": "name", "CreationTime": "creationTime", "JobId": "jobId"},
        )

    def _backup_object_names(self) -> dict[str, str]:
        names = {}
        for event in self._seed_events + self._created_events:
            machine = event.get("machine") or {}
            if machine.get("backupObjectId") and machine["backupObjectId"] != ZERO_UUID:
                names[machine["backupObjectId"]] = machine["displayName"]
        return names

    def _all_restore_points(self) -> list[dict[str, Any]]:
        points = list(self.restore_points)
        for session in self.sessions.values():
            body = self.session_body(session)
            if session.kind != "quickBackup" or body["state"] != "Stopped":
                continue
            if body["result"]["result"] not in ("Success", "Warning") or not session.backup_id:
                continue
            points.append(
                {
                    "id": str(uuid.uuid5(uuid.UUID(session.id), "restorePoint")),
                    "name": session.object_name,
                    "platformName": session.platform_name,
                    "platformId": ZERO_UUID,
                    "creationTime": body["endTime"],
                    "backupId": session.backup_id,
                    "type": "Increment",
                    "sessionId": session.id,
                    "allowedOperations": ["StartFlrRestore"],
                    "malwareStatus": "Clean",
                }
            )
        return points

    def get_all_object_restore_points(self, req: Request) -> dict[str, Any]:
        object_names = self._backup_object_names()
        return _page(
            self._all_restore_points(),
            req.query,
            filters={
                "backupIdFilter": _equals("backupId"),
                "platformNameFilter": _equals("platformName"),
                "malwareStatusFilter": _equals("malwareStatus"),
                "backupObjectIdFilter": lambda row, v: row["name"] == object_names.get(v),
            },
            order={
                "CreationTime": "creationTime",
                "PlatformId": "platformId",
                "BackupId": "backupId",
            },
            default_order=("creationTime", False),
        )

    def get_all_authorization_events(self, req: Request) -> dict[str, Any]:
        return _page(
            self.authorization_events,
            req.query,
            filters={"stateFilter": _equals("state")},
            order={"Name": "name", "State": "state", "CreationTime": "creationTime"},
            default_order=("creationTime", False),
        )

    # ------------------------------------------------------------------ malware detection

    def malware_events(self) -> list[dict[str, Any]]:
        seeded = self._seed_events if self.scenario.seed_malware_events else []
        return seeded + self._created_events

    def view_suspicious_activity_events(self, req: Request) -> dict[str, Any]:
        return _page(
            self.malware_events(),
            req.query,
            filters={
                "stateFilter": _equals("state"),
                "severityFilter": _equals("severity"),
                "sourceFilter": _equals("source"),
                "typeFilter": _equals("type"),
                "machineNameFilter": lambda row, v: fnmatch.fnmatchcase(
                    row["machine"]["displayName"].lower(), v.lower()
                ),
            },
            order={
                "Type": "type",
                "DetectionTimeUtc": "detectionTimeUtc",
                "CreationTimeUtc": "creationTimeUtc",
                "State": "state",
                "Source": "source",
                "Severity": "severity",
                "CreatedBy": "createdBy",
                "Engine": "engine",
                "Details": "details",
            },
            default_order=("detectionTimeUtc", False),
        )

    def create_suspicious_activity_event(self, req: Request) -> dict[str, Any]:
        spec, machine = req.body, req.body["machine"]
        event = {
            "id": str(uuid.uuid4()),
            "type": "Unknown",
            "creationTimeUtc": _iso(self.now()),
            "detectionTimeUtc": spec["detectionTimeUtc"],
            "machine": {
                "displayName": machine.get("fqdn")
                or machine.get("ipv4")
                or machine.get("uuid", ""),
                "uuid": machine.get("uuid", ""),
                "backupObjectId": machine.get("backupObjectId", ZERO_UUID),
            },
            "state": "Created",
            "details": spec["details"],
            "source": "External",
            "severity": spec.get("severity", "Suspicious"),
            "createdBy": req.username or "",
            "engine": spec["engine"],
        }
        self._created_events.append(event)
        return {"data": [event], "pagination": {"total": 1, "count": 1, "skip": 0, "limit": 1}}

    def start_malware_backup_scan(self, req: Request) -> dict[str, Any]:
        pairs = req.body.get("backupObjectPair") or []
        backup_id = pairs[0]["backupId"] if pairs else None
        names = self._backup_object_names()
        object_name = names.get(pairs[0]["backupObjectId"], "") if pairs else ""
        backup = next((b for b in self.backups if b["id"] == backup_id), None)
        if pairs and backup is None:
            raise MockError(404, "NotFound", "The backup doesn't exist.", str(backup_id))
        session = self._new_session(
            name="Backup scan",
            job_id=(backup or {}).get("jobId", ZERO_UUID),
            session_type="MalwareDetection",
            kind="malwareScan",
            platform_name=(backup or {}).get("platformName", "VMware"),
            initiated_by=req.username or "",
            object_name=object_name,
            backup_id=backup_id,
        )
        return self.session_body(session)

    # ------------------------------------------------------------------ routing table

    def handlers(self) -> dict[str, tuple[int, Callable[[Request], Any]]]:
        """operationId -> (success status, handler)."""
        return {
            "Logout": (200, self.logout),
            "GetServerInfo": (200, self.get_server_info),
            "GetServerTime": (200, self.get_server_time),
            "GetServerCertificate": (200, self.get_server_certificate),
            "GetAllJobsStates": (200, self.get_all_jobs_states),
            "StartJob": (201, self.start_job),
            "StopJob": (201, self.stop_job),
            "RetryJob": (201, self.retry_job),
            "StartHyperVQuickBackupJob": (201, self.start_hyperv_quick_backup),
            "StartVSphereQuickBackupJob": (201, self.start_vsphere_quick_backup),
            "StartAgentQuickBackupJob": (201, self.start_agent_quick_backup),
            "GetSession": (200, self.get_session),
            "GetSessionLogs": (200, self.get_session_logs),
            "GetAllSessions": (200, self.get_all_sessions),
            "GetAllRepositoriesStates": (200, self.get_all_repositories_states),
            "GetAllBackups": (200, self.get_all_backups),
            "GetAllObjectRestorePoints": (200, self.get_all_object_restore_points),
            "GetAllAuthorizationEvents": (200, self.get_all_authorization_events),
            "ViewSuspiciousActivityEvents": (200, self.view_suspicious_activity_events),
            "CreateSuspiciousActivityEvent": (201, self.create_suspicious_activity_event),
            "StartMalwareBackupScan": (201, self.start_malware_backup_scan),
        }
