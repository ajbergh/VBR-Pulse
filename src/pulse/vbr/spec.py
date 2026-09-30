"""Access to the pinned OpenAPI spec: request-body validation by operationId.

The spec is the source of truth for request shapes (PLAN §5.1 rule 4). We validate
with jsonschema against the spec directly rather than importing the generated
Pydantic models, which take ~10 s to import and would blow the cold-start budget.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry
from referencing.jsonschema import DRAFT7

from pulse.vbr.errors import VbrRequestInvalid

SPEC_PATH = Path(__file__).resolve().parents[3] / "openapi" / "vbr-1.3-rev2.json"
_SPEC_URI = "urn:vbr-pulse:openapi"


@cache
def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return spec


@cache
def _registry() -> Registry[Any]:
    registry: Registry[Any] = Registry().with_resource(
        _SPEC_URI, DRAFT7.create_resource(_validation_document(load_spec()))
    )
    return registry


_SCHEMAS = "#/components/schemas/"
_BASE_SUFFIX = "__base"


def _validation_document(spec: dict[str, Any]) -> dict[str, Any]:
    """Rewrite OpenAPI-only constructs into plain JSON Schema (Draft 7).

    VBR models polymorphism as a base schema with `oneOf: [subtypes]` + `discriminator`,
    where each subtype `allOf`-includes the base. Taken literally that recurses forever,
    so each such base is split into:

    - `<Name>__base`: the base without `oneOf`/`discriminator`; subtypes include this.
    - `<Name>`: `<Name>__base` plus an if/then per discriminator value that routes to
      the matching subtype — the OpenAPI discriminator semantics.

    `nullable: true` becomes a `null` member of `type`.
    """
    schemas: dict[str, Any] = copy.deepcopy(spec["components"]["schemas"])
    renames: dict[str, dict[str, str]] = {}  # subtype name -> {base ref -> base__base ref}

    for name, schema in list(schemas.items()):
        discriminator = schema.get("discriminator")
        if not isinstance(discriminator, dict) or "oneOf" not in schema:
            continue
        prop = discriminator["propertyName"]
        mapping: dict[str, str] = discriminator.get("mapping") or {
            ref["$ref"].removeprefix(_SCHEMAS): ref["$ref"] for ref in schema["oneOf"]
        }
        base = {k: v for k, v in schema.items() if k not in ("oneOf", "discriminator")}
        schemas[name + _BASE_SUFFIX] = base
        schemas[name] = {
            "allOf": [
                {"$ref": _SCHEMAS + name + _BASE_SUFFIX},
                *(
                    {
                        "if": {"properties": {prop: {"const": value}}, "required": [prop]},
                        "then": {"$ref": ref},
                    }
                    for value, ref in mapping.items()
                ),
            ]
        }
        for ref in {*mapping.values(), *(r["$ref"] for r in schema["oneOf"])}:
            renames.setdefault(ref.removeprefix(_SCHEMAS), {})[_SCHEMAS + name] = (
                _SCHEMAS + name + _BASE_SUFFIX
            )

    for subtype, refs in renames.items():
        for part in schemas.get(subtype, {}).get("allOf", []):
            if part.get("$ref") in refs:
                part["$ref"] = refs[part["$ref"]]

    _apply_nullable(schemas)
    return {"components": {"schemas": schemas}}


def _apply_nullable(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("nullable") is True and isinstance(node.get("type"), str):
            node["type"] = [node["type"], "null"]
        for value in node.values():
            _apply_nullable(value)
    elif isinstance(node, list):
        for value in node:
            _apply_nullable(value)


def _operation(method: str, path: str) -> dict[str, Any]:
    op: dict[str, Any] = load_spec()["paths"][path][method.lower()]
    return op


def _schema_pointer(schema: dict[str, Any]) -> dict[str, Any]:
    """Turn a spec-local `$ref` (or inline schema) into one resolvable through the registry."""
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        return {"$ref": f"{_SPEC_URI}{ref}"}
    return schema


@cache
def _body_validator(method: str, path: str) -> Draft7Validator | None:
    body = _operation(method, path).get("requestBody")
    if not body:
        return None
    content = body.get("content", {}).get("application/json")
    if not content or "schema" not in content:
        return None
    return Draft7Validator(_schema_pointer(content["schema"]), registry=_registry())


def validate_request_body(operation_id: str, method: str, path: str, body: Any) -> None:
    """Raise VbrRequestInvalid if `body` doesn't match the operation's JSON request schema."""
    validator = _body_validator(method, path)
    if validator is None:
        return
    error = best_match(validator.iter_errors(body))
    if error is not None:
        raise VbrRequestInvalid(
            _describe(error), operation_id=operation_id, error_code="SpecValidation"
        )


def _describe(error: ValidationError) -> str:
    where = "/".join(str(p) for p in error.absolute_path) or "body"
    return f"Request body doesn't match the 1.3-rev2 spec at '{where}': {error.message}"
