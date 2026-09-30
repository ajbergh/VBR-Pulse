"""Request bodies Pulse sends are validated against the 1.3-rev2 spec (PLAN §5.1 rule 4, §11)."""

from __future__ import annotations

from typing import Any

import pytest

from pulse.vbr.errors import VbrRequestInvalid
from pulse.vbr.operations import OPERATIONS
from pulse.vbr.spec import load_spec, validate_request_body

# Operations that carry a JSON body in v1 (PLAN §5.2).
BODY_OPERATIONS = [
    "StartJob",
    "StartAgentQuickBackupJob",
    "StartVSphereQuickBackupJob",
    "StartMalwareBackupScan",
    "CreateSuspiciousActivityEvent",
]


def _examples(operation_id: str) -> list[tuple[str, Any]]:
    spec = load_spec()
    op = OPERATIONS[operation_id]
    content = spec["paths"][op.path][op.method.lower()]["requestBody"]["content"]
    shared = spec["components"].get("examples", {})
    out = []
    for name, example in content["application/json"].get("examples", {}).items():
        if "$ref" in example:
            example = shared[example["$ref"].rsplit("/", 1)[-1]]
        out.append((name, example["value"]))
    return out


def _validate(operation_id: str, body: Any) -> None:
    op = OPERATIONS[operation_id]
    validate_request_body(operation_id, op.method, op.path, body)


@pytest.mark.parametrize(
    ("operation_id", "name", "body"),
    [(op_id, name, body) for op_id in BODY_OPERATIONS for name, body in _examples(op_id)],
)
def test_spec_examples_validate(operation_id: str, name: str, body: Any) -> None:
    _validate(operation_id, body)


def test_every_body_operation_has_an_example() -> None:
    for op_id in BODY_OPERATIONS:
        assert _examples(op_id), op_id


def _scan_example() -> dict[str, Any]:
    return dict(_examples("StartMalwareBackupScan")[0][1])


def test_polymorphic_body_routes_on_discriminator() -> None:
    body = _scan_example()
    assert body["type"] == "Backup"
    body.pop("backupObjectPair")  # required by the `Backup` subtype only

    with pytest.raises(VbrRequestInvalid, match="backupObjectPair"):
        _validate("StartMalwareBackupScan", body)


def test_polymorphic_body_rejects_unknown_type() -> None:
    body = {**_scan_example(), "type": "Tape"}
    with pytest.raises(VbrRequestInvalid, match="type"):
        _validate("StartMalwareBackupScan", body)


def test_quick_backup_requires_object_fields() -> None:
    _, body = _examples("StartVSphereQuickBackupJob")[0]
    broken = {k: v for k, v in body.items() if k != "hostName"}
    with pytest.raises(VbrRequestInvalid, match="hostName"):
        _validate("StartVSphereQuickBackupJob", broken)
