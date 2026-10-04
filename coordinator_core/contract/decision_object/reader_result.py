"""
coordinator_core.contract.decision_object.reader_result — the shared
`ReaderResult` dataclass every reader family returns from its own
`collect(cadence)`, plus the two shared bounding helpers.

Purpose: the type is a decision-object contract, not the property of any one
assembler, so every consumer imports it from this neutral home.

`cap_judgment_points()` is the ONE cap-and-overflow helper for any reader
with an unbounded per-item judgment-point list. An uncapped list grows with
disk contents (a 124KB/148-judgment-point payload was the observed cost).

`truncate_external_text()` bounds entry SIZE where the cap bounds entry
COUNT: any unbounded EXTERNAL string interpolated into `question`/`evidence`
routes through it.
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

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def build_directive(id_: str, cli: str, args: list[str], detail: str) -> dict[str, Any]:
    """One unconditional `directives[]` entry naming an existing CLI."""
    return {
        "id": id_,
        "cli": cli,
        "args": list(args),
        "depends_on": None,
        "already_satisfied": False,
        "detail": detail,
    }


def cap_judgment_points(
    judgment_points: list[dict[str, Any]],
    *,
    cap: int,
    overflow_id: str,
    item_label: str,
    list_command: str,
) -> list[dict[str, Any]]:
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
#: a plain Python string slice, not a byte-count bound: a byte-based cut risks
#: splitting a multi-byte UTF-8 character mid-sequence for no real budget gain.
EXTERNAL_TEXT_TRUNCATE_CHARS = 200


def truncate_external_text(text: str) -> str:
    """Truncate `text` to `EXTERNAL_TEXT_TRUNCATE_CHARS` code points, appending
    an ellipsis when the cap binds. Callers building a judgment point from an
    external, unbounded string MUST route it through this before interpolating
    into `question`/`evidence` — `cap_judgment_points` bounds entry count, not
    entry size, and this is the size half of that budget."""
    if len(text) > EXTERNAL_TEXT_TRUNCATE_CHARS:
        return text[:EXTERNAL_TEXT_TRUNCATE_CHARS] + "…"
    return text
