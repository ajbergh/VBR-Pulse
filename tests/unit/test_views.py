"""Presentation helpers and incident request builders."""

from __future__ import annotations

import shlex
from typing import Any

import pytest

from pulse.inspector.bus import InspectorEvent
from pulse.inspector.redact import MASK
from pulse.vbr.incident import UnsupportedMachine, backup_scan_request, quick_backup_request
from pulse.vbr.operations import OPERATIONS
from pulse.vbr.sessions import SessionSnapshot
from pulse.vbr.spec import validate_request_body
from pulse.web import views


def event(**fields: Any) -> InspectorEvent:
    base: dict[str, Any] = {
        "operation_id": "StartJob",
        "docs_url": OPERATIONS["StartJob"].docs_url,
        "method": "POST",
        "url": "https://vbr01.lab.local/api/v1/jobs/3c5557b1-71e8-4508-8dce-4e743b294ef5/start",
        "request_headers": {"x-api-version": "1.3-rev2", "Authorization": "Bearer eyJhb…Xk9Q"},
        "request_body": {"performActiveFull": False},
        "status": 201,
    }
    return InspectorEvent(**{**base, **fields})


def test_curl_uses_a_token_variable() -> None:
    command = views.curl(event())
    assert "$VBR_TOKEN" in command
    assert "eyJhb" not in command
    assert shlex.split(command)[:4] == [
        "curl",
        "-X",
        "POST",
        "https://vbr01.lab.local/api/v1/jobs/3c5557b1-71e8-4508-8dce-4e743b294ef5/start",
    ]
    assert "-d" in shlex.split(command)


def test_curl_for_the_token_call_never_contains_the_password() -> None:
    login = event(
        operation_id="CreateToken",
        url="https://vbr01.lab.local/api/oauth2/token",
        request_headers={"x-api-version": "1.3-rev2"},
        request_body={"grant_type": "password", "username": "svc-pulse-ops", "password": MASK},
    )
    command = views.curl(login)
    assert "password=$VBR_PASSWORD" in command
    assert MASK not in command
    assert "grant_type=password" in command


def test_json_html_escapes_and_tints() -> None:
    html, cut = views.json_html({"name": "<script>alert(1)</script>", "n": 3, "ok": True})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '<span class="j-key">&#34;name&#34;</span>' in html
    assert '<span class="j-number">3</span>' in html
    assert '<span class="j-literal">true</span>' in html
    assert cut is False


def test_json_html_truncates() -> None:
    _, cut = views.json_html({"data": "x" * 100}, limit=20)
    assert cut is True


def test_display_path_decodes_the_query() -> None:
    url = "https://vbr01.lab.local/api/v1/jobs/states?nameFilter=SQL%2A&limit=25"
    assert views.display_path(url) == "/api/v1/jobs/states?nameFilter=SQL*&limit=25"


def test_inspector_rows_group_polls() -> None:
    polls = [
        event(method="GET", operation_id="GetSession", group="session:abc", status=200)
        for _ in range(14)
    ]
    rows = views.inspector_rows([event(), *polls, event()])
    assert len(rows) == 3
    grouped = next(r for r in rows if r.event.group)
    assert grouped.count == 14
    assert grouped.dom_id == "insp-grp-session-abc"


def test_short_ids_in_collapsed_rows() -> None:
    assert views.shorten_ids(event().url) == "https://vbr01.lab.local/api/v1/jobs/3c55…/start"


def test_forbidden_message() -> None:
    assert views.forbidden_message("svc-pulse-view", "Backup Viewer", "StartJob") == (
        "svc-pulse-view is a Backup Viewer and can't start jobs."
    )
    assert views.forbidden_message("svc-pulse-ir", "Incident API Operator", "GetAllJobsStates") == (
        "svc-pulse-ir is an Incident API Operator and can't view jobs."
    )
    assert views.forbidden_message("x", None, None) == "x's role can't do that."


def snap(state: str, history: tuple[str, ...]) -> SessionSnapshot:
    return SessionSnapshot(
        id="s",
        name="n",
        job_id="j",
        session_type="BackupJob",
        state=state,
        progress=0,
        result="None",
        message="",
        is_canceled=False,
        creation_time=None,
        end_time=None,
        poll_seconds=5,
        history=history,
    )


def test_track_states() -> None:
    assert views.track_states(snap("Working", ("Starting", "Working"))) == [
        ("Starting", "past"),
        ("Working", "current"),
        ("Stopped", "future"),
    ]
    stopping = views.track_states(snap("Stopped", ("Starting", "Working", "Stopping", "Stopped")))
    assert [label for label, _ in stopping] == ["Starting", "Working", "Stopping", "Stopped"]
    waiting = views.track_states(snap("WaitingRepository", ("Starting", "WaitingRepository")))
    assert ("Waiting for repository", "current") in waiting


def test_clock_formats() -> None:
    assert views.clock_time(None) == "–"
    assert views.duration("2026-09-30T08:00:00Z", "2026-09-30T08:04:30Z") == "4 min 30 s"
    assert views.size_gb(40960) == "40.0 TB"


# ---------------------------------------------------------------- incident request builders

VSPHERE = {
    "id": "0403c0de-0000-4000-8000-000000000002",
    "name": "FS-02",
    "platformName": "VMware",
    "backupId": "5c0c2442-0000-4000-8000-000000000001",
    "objectId": "vm-1042",
    "viType": "VirtualMachine",
    "path": "vcsa01.lab.local\\DC-Lab\\Cluster-01\\FS-02",
}


@pytest.mark.parametrize("path", [VSPHERE["path"], "vcsa01.lab.local/DC-Lab/FS-02"])
def test_vsphere_quick_backup_request_matches_the_spec(path: str) -> None:
    op_id, body = quick_backup_request({**VSPHERE, "path": path})
    assert op_id == "StartVSphereQuickBackupJob"
    assert body["hostName"] == "vcsa01.lab.local"
    assert (body["platform"], body["objectId"]) == ("VSphere", "vm-1042")
    op = OPERATIONS[op_id]
    validate_request_body(op_id, op.method, op.path, body)


def test_agent_quick_backup_request() -> None:
    agent = {
        "name": "LAPTOP-042",
        "platformName": "WindowsPhysical",
        "computerId": "9a7c5d1e-2b3f-4a6e-8c0d-1e2f3a4b5c6d",
        "protectionGroupIds": ["1e2f3a4b-5c6d-4e7f-8a9b-0c1d2e3f4a5b"],
    }
    op_id, body = quick_backup_request(agent)
    assert op_id == "StartAgentQuickBackupJob"
    op = OPERATIONS[op_id]
    validate_request_body(op_id, op.method, op.path, body)


def test_unsupported_machine() -> None:
    with pytest.raises(UnsupportedMachine):
        quick_backup_request({"name": "tenant", "platformName": "EntraID"})
    with pytest.raises(UnsupportedMachine):  # the lab is VMware-only; Hyper-V isn't wired up
        quick_backup_request({**VSPHERE, "platformName": "HyperV"})
    with pytest.raises(UnsupportedMachine, match="protection group"):
        quick_backup_request({"name": "LAPTOP", "platformName": "LinuxPhysical"})


def test_scan_request_matches_the_spec() -> None:
    body = backup_scan_request(VSPHERE["backupId"], VSPHERE["id"])
    op = OPERATIONS["StartMalwareBackupScan"]
    validate_request_body(op.operation_id, op.method, op.path, body)


def test_session_type_labels() -> None:
    assert views.session_type("BackupJob") == "Backup job"
    assert views.session_type("VeeamUpdaterSettingsSync") == "Veeam updater settings sync"
    assert views.session_type("VolumesDiscover") == "Volumes discover"
    assert views.session_type("SecurityComplianceAnalyzer") == "Security compliance analyzer"


def test_short_durations() -> None:
    assert views.duration("2026-09-30T08:00:00Z", "2026-09-30T08:00:18Z") == "18 s"
    assert (
        views.duration("2026-09-30T17:08:38.754362-04:00", "2026-09-30T17:10:24.1-04:00")
        == "1 min 45 s"
    )
