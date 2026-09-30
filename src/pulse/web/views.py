"""Presentation helpers for templates: status words + icons, formatting, JSON tinting, cURL."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from markupsafe import Markup, escape

from pulse.inspector.bus import InspectorEvent
from pulse.inspector.redact import MASK

INSPECTOR_BODY_LIMIT = 64 * 1024

# Status is always an icon and a word (PLAN §7.1 principle 4). Icon names map to the
# SVG sprite in templates/partials/icons.html.
JOB_STATUS = {
    "Inactive": ("idle", "Idle"),
    "Enabled": ("idle", "Idle"),
    "Running": ("running", "Running"),
    "Starting": ("running", "Starting"),
    "Stopping": ("running", "Stopping"),
    "Stopped": ("idle", "Idle"),  # 13.1 reports idle jobs as Stopped
    "Disabled": ("disabled", "Disabled"),
}
RESULT = {
    "Success": ("success", "Success", "ok"),
    "Warning": ("warning", "Warning", "warn"),
    "Failed": ("failed", "Failed", "bad"),
    "None": ("none", "–", "none"),
}
JOB_TYPES = {
    "VSphereBackup": "vSphere backup",
    "VSphereReplica": "vSphere replica",
    "BackupCopy": "Backup copy",
    "WindowsAgentBackup": "Windows agent",
    "LinuxAgentBackup": "Linux agent",
}
SESSION_TYPES = {
    "BackupJob": "Backup job",
    "ReplicaJob": "Replica job",
    "BackupCopyJob": "Backup copy job",
    "MalwareDetection": "Backup scan",
}
REPOSITORY_TYPES = {
    "WinLocal": "Windows",
    "LinuxLocal": "Linux",
    "Smb": "SMB share",
    "Nfs": "NFS share",
    "S3Compatible": "S3 compatible",
    "AmazonS3": "Amazon S3",
    "AzureBlob": "Azure Blob",
    "GoogleCloud": "Google Cloud",
}

# What a 403 on each operation stops the account from doing (PLAN §7.8 copy).
ACTIONS = {
    "StartJob": "start jobs",
    "StopJob": "stop jobs",
    "RetryJob": "retry jobs",
    "StartVSphereQuickBackupJob": "start quick backups",
    "StartAgentQuickBackupJob": "start quick backups",
    "StartMalwareBackupScan": "start backup scans",
    "CreateSuspiciousActivityEvent": "create malware events",
    "ViewSuspiciousActivityEvents": "view malware events",
    "GetAllJobsStates": "view jobs",
    "GetAllRepositoriesStates": "view repositories",
    "GetAllSessions": "view sessions",
    "GetBackupObject": "read backup objects",
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def clock_time(value: str | None, now: datetime | None = None) -> str:
    """`08:00` today, `Sep 29 08:00` otherwise, in the presenter laptop's time zone."""
    moment = parse_time(value)
    if moment is None:
        return "–"
    local = moment.astimezone()
    today = (now or datetime.now(UTC)).astimezone().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    return f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"


def clock_seconds(value: str | None) -> str:
    moment = parse_time(value)
    return moment.astimezone().strftime("%H:%M:%S") if moment else "–"


def duration(start: str | None, end: str | None) -> str:
    begin, finish = parse_time(start), parse_time(end)
    if not begin or not finish:
        return "–"
    seconds = int((finish - begin).total_seconds())
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def session_type(value: str) -> str:
    """`VeeamUpdaterSettingsSync` → `Veeam updater settings sync` (sentence case, PLAN §7.3)."""
    if value in SESSION_TYPES:
        return SESSION_TYPES[value]
    words = _WORD.findall(value) or [value]
    return " ".join([words[0], *(w if w.isupper() else w.lower() for w in words[1:])])


def size_gb(value: float | None) -> str:
    if value is None:
        return "–"
    if value >= 1024:
        return f"{value / 1024:,.1f} TB"
    return f"{value:,.0f} GB"


def short_id(value: str) -> str:
    return value[:8]


def shorten_ids(path: str) -> str:
    """`/api/v1/jobs/3c5557b1-…/start` → `/api/v1/jobs/3c55…/start` for collapsed rows."""
    return _UUID.sub(lambda m: m.group(0)[:4] + "…", path)


def display_path(url: str) -> str:
    """Path + query as a human reads it (`nameFilter=SQL*`, not `SQL%2A`)."""
    parts = urlsplit(url)
    query = unquote(parts.query)
    return parts.path + (f"?{query}" if query else "")


def display_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{display_path(url)}"


# ---------------------------------------------------------------- JSON tinting

_TOKEN = re.compile(
    r'(?P<key>"(?:[^"\\]|\\.)*")(?=\s*:)'
    r'|(?P<string>"(?:[^"\\]|\\.)*")'
    r"|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<literal>\btrue\b|\bfalse\b|\bnull\b)"
)


def json_html(value: Any, limit: int = INSPECTOR_BODY_LIMIT) -> tuple[Markup, bool]:
    """Pretty-printed, syntax-tinted, escaped JSON; truncated at `limit` characters."""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    truncated = len(text) > limit
    text = text[:limit]
    out: list[str] = []
    position = 0
    for match in _TOKEN.finditer(text):
        out.append(str(escape(text[position : match.start()])))
        kind = match.lastgroup or "string"
        out.append(f'<span class="j-{kind}">{escape(match.group(0))}</span>')
        position = match.end()
    out.append(str(escape(text[position:])))
    return Markup("".join(out)), truncated


# ---------------------------------------------------------------- copy as cURL

_PLACEHOLDERS = {"password": "$VBR_PASSWORD", "refresh_token": "$VBR_REFRESH_TOKEN"}


def curl(event: InspectorEvent) -> str:
    """A runnable cURL command; secrets become shell variables (PLAN §7.6)."""
    parts = ["curl", "-X", event.method, shlex.quote(event.url)]
    for name, value in event.request_headers.items():
        if name.lower() == "authorization":
            parts += ["-H", '"Authorization: Bearer $VBR_TOKEN"']
        else:
            parts += ["-H", shlex.quote(f"{name}: {value}")]
    body = event.request_body
    if isinstance(body, dict) and event.operation_id == "CreateToken":
        for key, value in body.items():
            if key in _PLACEHOLDERS:
                parts += ["--data-urlencode", f'"{key}={_PLACEHOLDERS[key]}"']
            elif value != MASK:
                parts += ["--data-urlencode", shlex.quote(f"{key}={value}")]
    elif body is not None:
        parts += ["-d", shlex.quote(json.dumps(body, separators=(",", ":")))]
    return " ".join(parts)


# ---------------------------------------------------------------- inspector rows


@dataclass(frozen=True)
class InspectorRow:
    event: InspectorEvent
    count: int  # how many events share this row's group
    dom_id: str

    @property
    def label(self) -> str:
        path = shorten_ids(display_path(self.event.url))
        if self.event.operation_id == "CreateToken" and isinstance(self.event.request_body, dict):
            grant = self.event.request_body.get("grant_type")
            if grant:
                return f"{path} ({grant})"
        return path


def group_dom_id(group: str) -> str:
    return "insp-grp-" + re.sub(r"[^A-Za-z0-9_-]", "-", group)


def inspector_rows(events: Iterable[InspectorEvent]) -> list[InspectorRow]:
    """Newest first; polling events sharing a `group` collapse into one row (PLAN §7.6)."""
    counts: dict[str, int] = {}
    rows: list[InspectorRow] = []
    for event in reversed(list(events)):
        if event.group:
            counts[event.group] = counts.get(event.group, 0) + 1
            if counts[event.group] > 1:
                continue
            rows.append(InspectorRow(event, 0, group_dom_id(event.group)))
        else:
            rows.append(InspectorRow(event, 1, f"insp-{event.id}"))
    return [
        InspectorRow(r.event, counts[r.event.group], r.dom_id) if r.event.group else r for r in rows
    ]


def row_for(event: InspectorEvent, events: Iterable[InspectorEvent]) -> InspectorRow:
    if not event.group:
        return InspectorRow(event, 1, f"insp-{event.id}")
    count = sum(1 for e in events if e.group == event.group)
    return InspectorRow(event, count, group_dom_id(event.group))


STATE_LABELS = {
    "WaitingRepository": "Waiting for repository",
    "WaitingSlot": "Waiting for a slot",
    "WaitingTape": "Waiting for tape",
    "ActionRequired": "Action required",
    "Postprocessing": "Post-processing",
}


def track_states(snapshot: Any) -> list[tuple[str, str]]:
    """Labels under the session track: past, current and future states (PLAN §7.5.3)."""
    states = ["Starting", "Working"]
    states += [s for s in snapshot.history if s not in (*states, "Stopped")]
    if snapshot.state not in states and snapshot.state != "Stopped":
        states.append(snapshot.state)
    states.append("Stopped")
    current = states.index(snapshot.state)
    return [
        (
            STATE_LABELS.get(state, state),
            "past" if i < current else "current" if i == current else "future",
        )
        for i, state in enumerate(states)
    ]


def forbidden_message(username: str, role: str | None, operation_id: str | None) -> str:
    action = ACTIONS.get(operation_id or "", "do that")
    if role:
        article = "an" if role[:1].lower() in "aeiou" else "a"
        return f"{username} is {article} {role} and can't {action}."
    return f"{username}'s role can't {action}."
