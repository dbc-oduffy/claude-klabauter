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
    """The value under `canonical` or the first present alias, `None` when no
    spelling is present. Raises `ValueError` when two spellings disagree.

    `None` is the only "absent" value: `False`/`0`/`""` under either spelling
    count as present and participate in disagreement-checking. Fine for this
    module's actual callers (path-shaped string params, where a falsy value
    is never legitimate); a future caller aliasing a bool or count param
    should confirm that's still what they want.
    """
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
    """`plan` (or `plan_path`) — the accepted spellings, for a refusal."""
    if not aliases:
        return canonical
    return f"{canonical} (or {', '.join(aliases)})"
