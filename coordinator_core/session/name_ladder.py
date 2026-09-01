# name_ladder — the shared three-rung claimant-name RESOLUTION policy behind
# both coordinator/bin/session-claim-cli.py's `who-claims-path` name column
# and coordinator_core/bash_guards/dispatch_checks.py's Check 5 owner-name
# clause. Extracted (state/debt-backlog/2026-09-01-shared-name-resolution-
# ladder-for-sessio-026b33fcd43d.yaml) after the two independent copies of
# this ~10-line policy already drifted once within a day of being written:
# the CLI split rung 3 into three distinguishable outcomes (no registry
# record vs. lookup failure vs. a resolved-but-nameless record) while the
# guard collapsed all three into one UNNAMED clause. Sharing the RESOLUTION
# here makes that drift structurally impossible; the two RENDERERS stay
# separate and unshared by design (see module docstring below) because they
# legitimately differ in byte budget and prose.
#
# RESOLUTION ONLY, NEVER RENDERING. This module returns facts
# (name, rung, reason) -- it prints nothing, formats nothing, and knows
# nothing about a 73-byte clause budget or a TAB-delimited CLI column. Each
# caller keeps its own renderer:
#   - session-claim-cli.py's `_render_claimant_name` has no byte budget and
#     renders full prose with an age/staleness sentence, and still
#     distinguishes all three rung-3 outcomes with their own markers.
#   - dispatch_checks.py's `_owner_writer_name_clause` renders inside a
#     ~73-byte clause and collapses every rung-3 outcome into one
#     "-- UNNAMED" marker -- that collapse is Check 5's own considered
#     choice (budget, not oversight) and this module does not second-guess
#     it by forcing a shared rendering.
#
# THE LOOKUP IS INJECTED, NEVER IMPORTED HERE. Both callers already own a
# per-functional-area import seam for `coordinator_core.session.
# harness_registry` (session-claim-cli.py's `_import_harness_registry_
# module`, purpose-built so `who-claims-path` tests can stub rung 2 in
# isolation; dispatch_checks.py's own late `from coordinator_core.session
# import harness_registry` inside `_resolve_owner_writer_name`, which its
# test suite stubs via `monkeypatch.setattr("coordinator_core.session.
# harness_registry.lookup", ...)`). This module importing harness_registry
# itself would bypass both seams and break that stubbing -- callers resolve
# `harness_registry.lookup` on their own terms and pass the callable in.
#
# RUNG 2 IS TRANSITIONAL. It exists only as a cheap fallback for a pre-C1
# claim record written before C1 (docs/plans/2026-09-01-the-claim-record-
# carries-the-name.md) started stamping a name onto the claim itself, and is
# correct only for a writer session still resident on THIS box at read time.
# Its retirement condition is recorded in that same plan: once every claim
# record in the corpus carries a rung-1 `writer_name` (i.e. no record older
# than C1's rollout remains reachable), rung 2 and its live-lookup rung
# become dead code and should be deleted rather than kept "just in case" --
# see that plan for the specific condition to check before deleting.
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

RUNG_RECORDED = "recorded"
RUNG_LIVE_LOOKUP = "live-lookup"
RUNG_UNRESOLVED = "unresolved"

#: Rung-3 sub-reasons -- only meaningful when ``rung == RUNG_UNRESOLVED``.
#: A caller that does not distinguish rung-3 outcomes (dispatch_checks.py's
#: Check 5, by budget) is free to ignore ``reason`` entirely; a caller that
#: does (session-claim-cli.py's ``who-claims-path``) reads it to pick among
#: its three distinct markers.
REASON_NO_REGISTRY_RECORD = "no_registry_record"
REASON_LOOKUP_FAILED = "lookup_failed"
REASON_UNNAMED_RECORD = "unnamed_record"


def resolve_name(
    recorded_name: Optional[str],
    sid: str,
    lookup: Callable[[str], Any],
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
    writer session is still resident.

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
