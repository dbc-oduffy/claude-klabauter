"""Shared params validation for the dispatch.* op handlers.

`validate_params` checks that params is a dict whose named fields have the declared kinds and
returns the ops' structured refusal `{"error": str}`, or None when the request is well formed.
Pure stdlib; checks shape only, never cross-field semantics.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Field(NamedTuple):
    """One param: `kind` is a key of `KINDS`; an absent optional field is not checked."""

    name: str
    kind: str
    required: bool = False


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


KINDS: dict[str, tuple[Any, str]] = {
    "str": (lambda v: isinstance(v, str), "a string"),
    "nonempty_str": (lambda v: isinstance(v, str) and bool(v.strip()), "a non-empty string"),
    "str_list": (_is_str_list, "a list of strings"),
    "list": (lambda v: isinstance(v, list), "a list"),
    "dict": (lambda v: isinstance(v, dict), "an object"),
    "bool": (lambda v: isinstance(v, bool), "a boolean"),
}


def validate_params(op: str, params: Any, fields: tuple[Field, ...]) -> dict | None:
    """Return `{"error": ...}` naming the first malformed field of `params`, else None."""
    if not isinstance(params, dict):
        return {"error": f"{op} params must be an object, got {type(params).__name__}"}
    for field in fields:
        if field.name not in params or params[field.name] is None:
            if field.required:
                return {"error": f"{op} params.{field.name} is required"}
            continue
        check, label = KINDS[field.kind]
        if not check(params[field.name]):
            return {"error": f"{op} params.{field.name} must be {label}"}
    return None
