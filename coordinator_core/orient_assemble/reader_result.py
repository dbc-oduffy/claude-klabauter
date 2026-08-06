"""
coordinator_core.orient_assemble.reader_result — the shared `ReaderResult`
dataclass every C2 reader family (`readers_clean_ops`, `readers_branch_reconcile`,
`readers_handoff_triage`, `readers_health_reaper`) returns from its own
`collect(cadence)`.

Purpose: `ReaderResult` used to live inside `readers_clean_ops.py` — one
reader family among four peers, each written by a different agent blind to
the others — and was imported sideways by its three siblings. Nothing about
the type is specific to "clean ops"; it is homed here so all five modules
(including `readers_clean_ops` itself) import it symmetrically (Review:
code-reviewer — Finding 4).

This module also homes `cap_judgment_points()`, the ONE shared cap-and-
overflow helper every reader family with an unbounded per-item judgment-point
list must call. Two families (`readers_clean_ops._read_memo_surface`,
`readers_branch_reconcile._read_auto_reconcile`) build an unbounded list —
one entry per inbound memo / per surfaced handoff — that grows with disk
contents. A 124KB/148-judgment-point `brief('session')` payload was the
observed cost of shipping that list uncapped. The cap-and-overflow logic
exists in exactly this one place, shared by both callers, per the
improvement-queue entry that identified the flood § "Shape to avoid —
state it exactly once."

It also homes `truncate_external_text()` — the cap above bounds entry
COUNT only, not entry SIZE (Review: code-reviewer — Finding 3): both
callers interpolate an unbounded EXTERNAL string (a raw cross-repo-memo
inbox line; a handoff's own `evidence`/`reason`) into `question`/`evidence`,
and one unusually long external string can blow the byte budget the count
cap exists to defend. Same shared-single-implementation discipline as the
cap helper.

Spec backlink: state/improvement-queue/2026-07-30-orientation-targets-work-finding-but-ems-d7e494b501a2.yaml
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)


@dataclass(frozen=True)
class ReaderResult:
    """One reader family's contribution to the decision-object envelope."""

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def cap_judgment_points(
    judgment_points: list[dict[str, Any]],
    *,
    cap: int,
    overflow_id: str,
    item_label: str,
    list_command: str,
) -> list[dict[str, Any]]:
    """Cap an unbounded per-item judgment-point list at `cap` entries.

    When `len(judgment_points) <= cap`, returns the list unchanged. When the
    cap binds, returns the first `cap` entries plus EXACTLY ONE overflow
    judgment point naming how many were withheld and the concrete command
    that lists them in full (`list_command`) — never a silent truncation
    that reads as complete.

    Negative-spec: does NOT summarize or count-only the withheld entries in
    place of the kept ones — the withheld count lives solely on the single
    overflow judgment point, never duplicated as a top-level aggregate.
    """
    if len(judgment_points) <= cap:
        return judgment_points

    withheld = len(judgment_points) - cap
    overflow = build_judgment_point(
        None,
        id=overflow_id,
        question=(
            f"{withheld} more {item_label} withheld from this brief "
            f"(showing {cap} of {len(judgment_points)}) — review the rest?"
        ),
        dispositions=[
            build_disposition("run_list_command"),
            build_disposition("leave_for_now"),
        ],
        evidence=(
            f"{len(judgment_points)} total {item_label}, {cap} shown, "
            f"{withheld} withheld | run `{list_command}` to see them all"
        ),
        reason="recommendation-forbidden",
    )
    return judgment_points[:cap] + [overflow]


#: Max rendered length of one interpolated EXTERNAL string, in CODE POINTS —
#: a plain Python string slice, not a byte-count bound. Mirrors
#: `coordinator_core.orientation.regenerate_cache`'s
#: `_HOUSEKEEPING_DETAIL_TRUNCATE_CHARS` precedent and its rationale
#: verbatim: a byte-based cut risks splitting a multi-byte UTF-8 character
#: (CJK, emoji) mid-sequence for no real budget gain, since the count cap
#: above already catches the genuine flood case this exists to backstop
#: (Review: code-reviewer — Finding 3).
EXTERNAL_TEXT_TRUNCATE_CHARS = 200


def truncate_external_text(text: str) -> str:
    """Truncate `text` to `EXTERNAL_TEXT_TRUNCATE_CHARS` code points, appending
    an ellipsis when the cap binds. Callers building a judgment point from an
    external, unbounded string (a raw memo-inbox line, a handoff's own
    `evidence`/`reason` field) MUST route it through this before
    interpolating into `question`/`evidence` — `cap_judgment_points` bounds
    entry count, not entry size, and this is the size half of that budget."""
    if len(text) > EXTERNAL_TEXT_TRUNCATE_CHARS:
        return text[:EXTERNAL_TEXT_TRUNCATE_CHARS] + "…"
    return text
