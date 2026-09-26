# name_ladder — the shared three-rung claimant-name RESOLUTION policy behind
# Sharing the RESOLUTION here makes that drift structurally impossible.
# RESOLUTION ONLY, NEVER RENDERING. This module returns facts
# RUNG 2 IS TRANSITIONAL. It exists only as a cheap fallback for a pre-C1
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

RUNG_RECORDED = "recorded"
RUNG_LIVE_LOOKUP = "live-lookup"
RUNG_UNRESOLVED = "unresolved"

#: Rung-3 sub-reasons -- only meaningful when ``rung == RUNG_UNRESOLVED``.
REASON_NO_REGISTRY_RECORD = "no_registry_record"
REASON_LOOKUP_FAILED = "lookup_failed"
REASON_UNNAMED_RECORD = "unnamed_record"


def resolve_name(
    recorded_name: Optional[str],
    sid: str,
    lookup: Optional[Callable[[str], Any]],
) -> Tuple[Optional[str], str, Optional[str]]:
    """The three-rung ladder, extracted verbatim from the two call sites
    named in this module's docstring.

    Rung 1 -- ``recorded_name`` (the caller's own read of whatever field
    carries the name stamped on the claim at write time -- ``fact.
    writer_name`` for the guard, the claim index's ``recorded_name[path]
    [sid]`` for the CLI). Survives the writer exiting, re-pointing its
    session id, or the machine restarting.

    Rung 2 -- failing that, ``lookup(sid)`` (the caller's own
    ``harness_registry.lookup``, injected rather than imported here -- see
    module docstring). Cheap, and correct only for a pre-C1 record whose
    writer session is still resident. ``lookup`` may be ``None`` -- a
    caller whose own import of the registry module failed passes ``None``
    rather than synthesising a closure that raises just to be re-caught;
    this degrades straight to rung 3 (``REASON_LOOKUP_FAILED``), same as a
    real ``lookup`` that raises.

    Rung 3 -- failing both, ``(None, RUNG_UNRESOLVED, reason)`` where
    ``reason`` distinguishes the three sub-cases a caller MAY care about:
    ``REASON_LOOKUP_FAILED`` (``lookup`` raised -- best-effort, never
    propagated), ``REASON_NO_REGISTRY_RECORD`` (``lookup`` returned
    ``None`` -- the registry was asked and holds nothing for this sid, NOT
    proof the session ended), and ``REASON_UNNAMED_RECORD`` (``lookup``
    returned a record whose own ``.name`` is falsy).

    Best-effort by construction: an exception raised by ``lookup`` degrades
    to rung 3 (``REASON_LOOKUP_FAILED``) rather than propagating -- this is
    advisory/display infrastructure on both call sites and must never turn
    a lookup failure into a crash.

    Rendered as PROVENANCE, never as an ADDRESS -- both callers' own
    renderers carry that discipline; this function only decides which rung
    answered and why, never how to print it.
    """
    if recorded_name:
        return recorded_name, RUNG_RECORDED, None

    if lookup is None:
        return None, RUNG_UNRESOLVED, REASON_LOOKUP_FAILED

    try:
        record = lookup(sid)
    except Exception:  # noqa: BLE001 - rung 2 is best-effort, see docstring
        return None, RUNG_UNRESOLVED, REASON_LOOKUP_FAILED

    if record is None:
        return None, RUNG_UNRESOLVED, REASON_NO_REGISTRY_RECORD

    live_name = getattr(record, "name", None)
    if live_name:
        return live_name, RUNG_LIVE_LOOKUP, None

    return None, RUNG_UNRESOLVED, REASON_UNNAMED_RECORD
