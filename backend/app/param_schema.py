"""
A deliberately small JSON Schema validator.

Why not `jsonschema`: this project is a uv workspace with a committed lockfile that
the Docker build installs with `uv sync --frozen`. Adding a dependency means
regenerating that lock, and a lock/`pyproject.toml` mismatch makes the build fail
outright. For the handful of keywords a service parameter schema actually needs, a
small validator is cheaper and has no supply-chain cost.

What it supports: type, required, properties, additionalProperties, enum, const,
minimum/maximum, exclusiveMinimum/exclusiveMaximum, minLength/maxLength, pattern,
items, minItems/maxItems, anyOf, oneOf, allOf, and default.

What it deliberately does NOT support: $ref, definitions/$defs, if/then/else,
dependentSchemas, propertyNames, unevaluated*, and remote references. A schema using
those keywords is REJECTED rather than silently under-validated -- accepting a schema
we only partly enforce would be worse than refusing it, because the caller would
believe validation had happened.

`apply_defaults` fills in declared defaults, which is what lets a service schema
carry its own defaults instead of the broker hard-coding them.
"""

from __future__ import annotations

import re
from typing import Any

# keywords we enforce; anything outside this set is refused unless listed in
# IGNORED_KEYWORDS, which exist for annotation only
SUPPORTED_KEYWORDS = {
    "type", "required", "properties", "additionalProperties", "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "items", "minItems", "maxItems",
    "anyOf", "oneOf", "allOf", "default", "title", "description",
}
IGNORED_KEYWORDS = {"title", "description", "examples", "$schema", "$id"}

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


class SchemaError(ValueError):
    """The schema itself is unsupported or malformed.

    Distinct from a validation failure: this is the service author's bug, and it is
    surfaced at registration so it cannot become a submission-time mystery.
    """


class ValidationError(ValueError):
    def __init__(self, message: str, path: tuple = ()):
        self.message = message
        self.path = path
        where = "/".join(str(p) for p in path) or "root"
        super().__init__(f"{message} (at {where})")


def check_schema(schema: Any, path: str = "root") -> None:
    """Refuse a schema we cannot fully enforce."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object")
    for key in schema:
        if key in SUPPORTED_KEYWORDS or key in IGNORED_KEYWORDS:
            continue
        raise SchemaError(
            f"{path}: unsupported schema keyword {key!r}. Supported: "
            f"{', '.join(sorted(SUPPORTED_KEYWORDS))}"
        )
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise SchemaError(f"{path}/properties: must be an object")
        for name, sub in schema["properties"].items():
            check_schema(sub, f"{path}/properties/{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}/items")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise SchemaError(f"{path}/{key}: must be a non-empty array")
            for i, sub in enumerate(schema[key]):
                check_schema(sub, f"{path}/{key}/{i}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        for t in types:
            if t not in _TYPE_MAP:
                raise SchemaError(f"{path}/type: unknown type {t!r}")


def _type_ok(value: Any, expected: str) -> bool:
    py = _TYPE_MAP[expected]
    if expected == "integer":
        # bool is an int subclass; a boolean is not an integer here
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, py)


def _check(value: Any, schema: dict, path: tuple) -> None:
    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"must equal {schema['const']!r}", path)
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"must be one of {schema['enum']!r}", path)

    if "allOf" in schema:
        for sub in schema["allOf"]:
            _check(value, sub, path)
    if "anyOf" in schema:
        if not any(_passes(value, sub, path) for sub in schema["anyOf"]):
            raise ValidationError("does not match any of the allowed forms", path)
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if _passes(value, sub, path))
        if matches != 1:
            raise ValidationError(
                f"must match exactly one form, matched {matches}", path
            )

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            raise ValidationError(
                f"expected {' or '.join(types)}, got {type(value).__name__}", path
            )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError(f"shorter than {schema['minLength']}", path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError(f"longer than {schema['maxLength']}", path)
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValidationError(f"does not match {schema['pattern']!r}", path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"must be >= {schema['minimum']}", path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"must be <= {schema['maximum']}", path)
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValidationError(f"must be > {schema['exclusiveMinimum']}", path)
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValidationError(f"must be < {schema['exclusiveMaximum']}", path)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(f"needs at least {schema['minItems']} items", path)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(f"allows at most {schema['maxItems']} items", path)
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], path + (i,))

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ValidationError(f"missing required property {name!r}", path)
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                _check(value[name], sub, path + (name,))
        extra = schema.get("additionalProperties", True)
        if extra is False:
            unknown = [k for k in value if k not in props]
            if unknown:
                raise ValidationError(
                    f"unknown propert{'y' if len(unknown)==1 else 'ies'} "
                    f"{sorted(unknown)!r}", path
                )
        elif isinstance(extra, dict):
            for name in value:
                if name not in props:
                    _check(value[name], extra, path + (name,))


def _passes(value: Any, schema: dict, path: tuple) -> bool:
    try:
        _check(value, schema, path)
        return True
    except ValidationError:
        return False


def validate(*, instance: Any, schema: dict) -> None:
    """Raise ValidationError if `instance` does not satisfy `schema`."""
    check_schema(schema)
    _check(instance, schema, ())


def apply_defaults(instance: dict, schema: dict) -> dict:
    """Fill in declared defaults for absent object properties.

    Lets a service schema carry its own defaults -- so the broker never needs a
    service's parameter names, which is the whole point of the plugin boundary.
    """
    if not isinstance(instance, dict) or schema.get("type") not in (None, "object"):
        return instance
    out = dict(instance)
    for name, sub in (schema.get("properties") or {}).items():
        if name not in out and isinstance(sub, dict) and "default" in sub:
            out[name] = sub["default"]
    return out
