"""coordinator_core.warm.engine_root -- the single definition of an engine root.

Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C2

THE RULE, stated once: **an engine root is a stamped build. No stamp, no
engine.** A directory is an engine root iff it carries a valid
`coordinator_core/_engine_stamp` (`skew.ENGINE_STAMP_FILENAME`,
`skew._engine_stamp_path`) -- nothing else about the directory (its
existence, whether `coordinator_core/` is importable from it, its name)
enters the predicate.

WHY A SHARED MODULE RATHER THAN THREE EDITS -- this codebase's convention is
not to reach into a peer module's private name, which is exactly what
produced the seven local copies of `Path(__file__).resolve().parents[2]`
this plan collapses (C3). A shared PUBLIC name is the only shape that
removes the duplication without violating that convention.

REUSE, NOT REINVENTION -- the stamp filename and path helper already exist
in `coordinator_core.warm.skew` (`ENGINE_STAMP_FILENAME`,
`_engine_stamp_path`) and are pinned equal to `publish.py`'s own copy by
test. This module reuses them rather than re-deriving the stamp path. The
stamp format is one line, `sha:<sha>\n`, and only its BYTES matter --
`is_engine_root` therefore validates readability and non-emptiness, not any
particular internal shape.

NEGATIVE SPEC -- this module does NOT raise on an unstamped tree, does NOT
change `compute_client_token`'s fallback behaviour, and does NOT touch any
of the seven duplicated call sites. C2 is definition only: a resolver and a
predicate, nothing wired to a caller yet. C3 collapses the seven sites onto
this module; C4 is the chunk that adds the fail-closed raise for dispatch.
Landing the raise here would make C3's pure collapse an accidental
behaviour change, which its own body forbids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.warm.skew import ENGINE_STAMP_FILENAME, _engine_stamp_path

__all__ = [
    "ENGINE_STAMP_FILENAME",
    "current_engine_clone",
    "is_engine_root",
    "resolve_engine_root",
]


def is_engine_root(path: Path) -> bool:
    """True iff `path` carries a valid `coordinator_core/_engine_stamp`.

    "Valid" means readable and non-empty -- the stamp's own contract is
    that only its bytes matter (see `skew.write_engine_stamp`), so this
    predicate does not parse or otherwise interpret its contents beyond
    confirming there is a real stamp there, not an empty or unreadable
    file left behind by a partial write.
    """
    stamp = _engine_stamp_path(Path(path))
    try:
        stamp_bytes = stamp.read_bytes()
    except OSError:
        return False
    return len(stamp_bytes) > 0


def current_engine_clone() -> Path:
    """Return this running interpreter's engine clone root.

    Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C3

    THE SINGLE SITE, per this plan's own argument: seven modules
    (`warm.skew`, `warm.election`, `warm.supervisor`, `warm.breadcrumb`,
    `warm.client`, `warm.server`, `ops.session.warm_start`) each kept a
    local `Path(__file__).resolve().parents[N]` copy of "the clone this
    process is running from" -- differing only in `N` because each file
    sits at a different depth under the repo root. Anchoring the
    computation HERE, on this module's own location, makes the anchor
    depth-independent for every caller: `engine_root.py` lives at
    `coordinator_core/warm/engine_root.py`, so `parents[2]` from here is
    the repo root regardless of where the caller itself lives.

    NEGATIVE SPEC -- deliberately does not consult `is_engine_root`: this
    is "the clone this process happens to be running from", not "a
    validated engine root". Callers that need the latter compose this
    with `is_engine_root`/`resolve_engine_root` themselves; C4 owns
    wiring any fail-closed behaviour, not this function.
    """
    return Path(__file__).resolve().parents[2]


def resolve_engine_root(candidate: Path) -> Optional[Path]:
    """Return `candidate` (as a `Path`) iff it is a valid engine root per
    `is_engine_root`, else `None`.

    Deliberately not raising here -- see the module's NEGATIVE SPEC. C4
    owns the fail-closed behaviour for the dispatch axis; this resolver is
    the definition callers will be routed through, not the enforcement
    point itself.
    """
    candidate = Path(candidate)
    if is_engine_root(candidate):
        return candidate
    return None
