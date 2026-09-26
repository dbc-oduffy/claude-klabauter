"""coordinator_core.bash_guards — PreToolUse:Bash guard engines.

Python engine-ification of DoE's PreToolUse:Bash guard cohort, per
the W3a/W3b naked-Python hook migration recipe
(scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md).

Each guard is a module exposing the interface pinned in write_guards/INTERFACE.md
(CLASS / MATCHERS / PRIORITY / check(payload)), adapted for the Bash matcher --
per-guard modules land here as the W3b build wave ports them one at a time.

Shared primitives (identity resolution, confined-findings-agent SSOT,
filename-legality predicate) live in `_helpers.py` -- see that module's
docstring for provenance. Do NOT duplicate those primitives inside a per-guard
module; import them from here.

Public surface: `guard_roster` / `GuardRosterEntry` (see `roster.py`), the
payload-free enumeration DoE-claude's `x-effective-delivery` emitter reads
across the plane boundary (`docs/reference/hook-delivery-manifest.md`).
Spec backlink: pln-guard-roster-export-minus-the-a4dec3, chunk C2.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinator_core.bash_guards.roster import (  # noqa: F401
        GuardRosterEntry,
        guard_roster,
    )

__all__ = ["GuardRosterEntry", "guard_roster"]

def __getattr__(name: str) -> Any:
    if name in __all__:
        from coordinator_core.bash_guards import roster

        return getattr(roster, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(__all__))
