"""One op parameter accepted under more than one spelling.

Ops an operator runs back-to-back disagree on what to call the same value
(`plan.prep_gate` takes `plan`, `dispatch.emit` takes `plan_path`), and each
mismatch costs a refused call (claude-klabauter#22). An alias makes the chain
forgiving without breaking any caller of the canonical spelling.

NEGATIVE-SPEC: an alias never silently wins a disagreement. Two spellings
carrying different values is refused, naming both — picking one would act on
a value the caller may not have meant.
"""

from __future__ import annotations

from typing import Any


def aliased_param(params: dict, canonical: str, *aliases: str) -> Any:
    found = [(key, params[key]) for key in (canonical, *aliases) if params.get(key) is not None]
    if not found:
        return None
    key, value = found[0]
    for other_key, other_value in found[1:]:
        if other_value != value:
            raise ValueError(
                f"{key} and {other_key} name the same parameter with different values; pass one"
            )
    return value


def spellings(canonical: str, *aliases: str) -> str:
    if not aliases:
        return canonical
    return f"{canonical} (or {', '.join(aliases)})"
