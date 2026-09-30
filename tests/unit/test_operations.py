"""Phase 0 acceptance: every operationId the plan relies on exists in the pinned spec."""

from __future__ import annotations

import pytest

from pulse.vbr.operations import OPERATIONS, SPEC_VERSION

# PLAN §5.2
PLANNED = [
    "CreateToken",
    "Logout",
    "GetServerInfo",
    "GetServerTime",
    "GetServerCertificate",
    "GetAllJobs",
    "GetAllJobsStates",
    "StartJob",
    "StopJob",
    "RetryJob",
    "StartHyperVQuickBackupJob",
    "StartAgentQuickBackupJob",
    "StartVSphereQuickBackupJob",
    "GetSession",
    "GetSessionLogs",
    "GetAllSessions",
    "GetAllRepositoriesStates",
    "GetAllBackups",
    "GetAllObjectRestorePoints",
    "ViewSuspiciousActivityEvents",
    "CreateSuspiciousActivityEvent",
    "StartMalwareBackupScan",
    "GetAllAuthorizationEvents",
]


def test_spec_version_pinned() -> None:
    assert SPEC_VERSION == "1.3-rev2"


@pytest.mark.parametrize("operation_id", PLANNED)
def test_planned_operation_exists(operation_id: str) -> None:
    assert operation_id in OPERATIONS


@pytest.mark.parametrize(
    ("operation_id", "method", "path"),
    [
        ("CreateToken", "POST", "/api/oauth2/token"),
        ("GetAllJobsStates", "GET", "/api/v1/jobs/states"),
        ("StartJob", "POST", "/api/v1/jobs/{id}/start"),
        ("GetSession", "GET", "/api/v1/sessions/{id}"),
        ("StartMalwareBackupScan", "POST", "/api/v1/malwareDetection/scanBackup"),
    ],
)
def test_operation_paths(operation_id: str, method: str, path: str) -> None:
    op = OPERATIONS[operation_id]
    assert (op.method, op.path) == (method, path)


def test_docs_url_uses_hyphenated_tag() -> None:
    assert OPERATIONS["GetAllObjectRestorePoints"].docs_url == (
        "https://helpcenter.veeam.com/references/vbr/13/rest/1.3-rev2/tag/Restore-Points"
        "#operation/GetAllObjectRestorePoints"
    )


def test_no_port_9419_anywhere() -> None:
    assert not any("9419" in op.path for op in OPERATIONS.values())
