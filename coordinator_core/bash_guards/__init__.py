"""coordinator_core.bash_guards — PreToolUse:Bash guard engines.

Python engine-ification of coordinator-claude's PreToolUse:Bash guard cohort, per
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
payload-free enumeration coordinator-claude's `x-effective-delivery` emitter reads
across the plane boundary (`docs/reference/hook-delivery-manifest.md`).
Spec backlink: pln-guard-roster-export-minus-the-a4dec3, chunk C2.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # import-cost-free, type-checkers only
    from coordinator_core.bash_guards.roster import (  # noqa: F401
        GuardRosterEntry,
        guard_roster,
    )

#: Deliberately names ONLY the roster seam. This is the package's FIRST
#: `__all__`, so it narrows what `from ... import *` sees -- adding an
#: unrelated name here silently changes an existing caller's star-import.
__all__ = ["GuardRosterEntry", "guard_roster"]

#: The roster is re-exported LAZILY, never at module top. `roster.py` imports
#: `guard_settings_integrity._tail_key` for the script-tail normal form, and
#: that module's own dependency graph (`ipc`, subprocess/asyncio machinery)
#: is far heavier than anything this package otherwise touches. An eager
#: re-export would charge that import to every PreToolUse Bash call in a
#: spawn-per-call engine held to an end-to-end invocation budget -- paid on
#: the operator's primary shell, for a seam only the manifest emitter reads.
#: Negative spec: do not "simplify" this into a module-top
#: `from .roster import guard_roster`; that reintroduces the hot-path cost
#: this indirection exists to avoid.
def __getattr__(name: str) -> Any:
    if name in __all__:
        from coordinator_core.bash_guards import roster

        return getattr(roster, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    """PEP 562 lazy attributes (`__getattr__` above) are invisible to the
    default `dir()`, which only reflects the module's own `__dict__` --
    without this override, `dir(coordinator_core.bash_guards)` would omit
    `guard_roster`/`GuardRosterEntry` even though both are valid attributes
    and named in `__all__`. Cheap: no import triggered, just name merging."""
    return sorted(set(globals()) | set(__all__))
