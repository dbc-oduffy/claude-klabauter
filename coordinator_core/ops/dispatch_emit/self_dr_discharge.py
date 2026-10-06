"""Discharge clauses for rows gated on a decision record the same plan writes.

A row R with a `depends_on` edge onto row D, where D writes under `docs/decisions/`, would
otherwise stop on the DR's `status: proposed`. When the sizing's `exit_criterion.accepted` is a
pm/ceo-mode acceptance with a non-empty `pm_quote`, R's brief carries a clause saying so.

Pure: no I/O. Does NOT write any DR status; does NOT discharge on hands-on or unaccepted sizings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from coordinator_core.ops.sizing_acceptance import acceptance_source, acceptance_words

_DISCHARGING_MODES = frozenset({"pm", "ceo"})
_DR_PREFIX = "docs/decisions/"


def _dr_paths(row: Mapping) -> list[str]:
    paths = row.get("writes")
    if not isinstance(paths, (list, tuple)):
        return []
    return [
        str(p) for p in paths
        if str(p).replace("\\", "/").removeprefix("./").startswith(_DR_PREFIX)
    ]


def discharge_clauses(
    raw_rows: Sequence[Mapping], sizing: Mapping | None
) -> dict[str, str]:
    """Return {row id: clause} for rows gated on a DR row of the same plan, or {}."""
    if not isinstance(sizing, Mapping):
        return {}
    criterion = sizing.get("exit_criterion")
    if not isinstance(criterion, Mapping):
        return {}
    accepted = criterion.get("accepted")
    if not isinstance(accepted, Mapping):
        return {}
    mode = str(accepted.get("mode") or "").strip()
    quote = (acceptance_words(accepted) or "").strip()
    if mode not in _DISCHARGING_MODES or not quote:
        return {}
    who = "The APM ruling accepted" if acceptance_source(accepted) == "apm" else "The PM accepted"
    statement = str(criterion.get("statement") or "").strip()
    on = accepted.get("on")

    dr_by_row = {
        str(r.get("id")): paths
        for r in raw_rows
        if isinstance(r, Mapping) and (paths := _dr_paths(r))
    }
    if not dr_by_row:
        return {}

    out: dict[str, str] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        edges = row.get("depends_on")
        if not isinstance(edges, (list, tuple)):
            continue
        hits = [
            (str(e["chunk"]), dr_by_row[str(e["chunk"])])
            for e in edges
            if isinstance(e, Mapping) and str(e.get("chunk")) in dr_by_row
        ]
        if not hits:
            continue
        gate = "; ".join(
            f"row {rid} writes {', '.join(paths)}" for rid, paths in hits
        )
        out[str(row.get("id"))] = (
            f"Decision-record gate discharged ({gate}). {who} this plan's exit "
            f"criterion (mode {mode}, on {on}): \"{quote}\". Accepted statement: "
            f"\"{statement}\". A `proposed` status on that decision record is not a stop. "
            "Stop only if the record's decision falls outside the accepted statement, "
            "and name the gap."
        )
    return out
