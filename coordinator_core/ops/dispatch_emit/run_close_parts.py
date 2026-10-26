"""P-A: decide a run's close outcome (done|partial) from the requirement-register rows' evidence.

Pure functions, no I/O, no git: the caller supplies commit contents. A part closes only at status
`met` with a commit, a test and a passing test verdict, and the commit must carry a tracked file.

Negative-spec:
  - A met row lacking commit or a passing test stays open.
  - A stated lettered part with no register row (dropped at sizing) is open, labelled "(a)".
  - A plan row absent from the staged manifest with no recorded defer reason is open, named.
  - Gitignored state (``IGNORED_EVIDENCE_PREFIXES``) never counts as a shipped commit file.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Collection, Iterable, Mapping, Optional

from coordinator_core.ops import requirement_register as _rr
from coordinator_core.ops.dispatch_emit.run_truth_contract import (
    OUTCOME_DONE,
    OUTCOME_PARTIAL,
    Close,
    Evidence,
    Part,
)

IGNORED_EVIDENCE_PREFIXES: tuple[str, ...] = (
    ".coordinator-local/",
    ".project-rag/",
    "scratch/",
)

CommitPaths = Callable[[str], Iterable[str]]


def part_from_row(row: Mapping[str, Any]) -> Part:
    """The `Part` a register row denotes; absent evidence reads as empty."""
    ev = row.get("evidence") or {}
    return Part(
        id=str(row.get("id", "")),
        source_text=str(row.get("source_text", "")),
        anchor=row.get("anchor"),
        status=str(row.get("status", "open")),
        evidence=Evidence(ev.get("commit"), ev.get("test"), ev.get("test_verdict")),
    )


def commit_ships_tracked_file(paths: Iterable[str]) -> bool:
    """True when at least one path is outside the gitignored evidence prefixes."""
    return any(p and not p.replace("\\", "/").startswith(IGNORED_EVIDENCE_PREFIXES) for p in paths)


_LEAD_LABEL = re.compile(r"^\s*\(([a-z])\)\s", re.IGNORECASE)


def _label_of(row: Mapping[str, Any]) -> str:
    """The row's part label: an explicit `label`, else a leading "(a) " of its text, else its id."""
    lead = _LEAD_LABEL.match(str(row.get("source_text") or ""))
    return str(row.get("label") or (lead.group(1) if lead else "") or row.get("id") or "").strip().lower()


def _ruled_out(row: Mapping[str, Any]) -> bool:
    """A deferred or waived row carrying a ruling is out of the ask, not an open part."""
    return row.get("status") in (_rr.STATUS_DEFERRED, _rr.STATUS_WAIVED) and bool(row.get("ruling"))


def part_closes(
    rows: Iterable[Mapping[str, Any]],
    *,
    stated_labels: Collection[str] = (),
    plan_row_ids: Collection[str] = (),
    staged_row_ids: Optional[Collection[str]] = None,
    defer_reasons: Optional[Mapping[str, str]] = None,
    commit_paths: Optional[CommitPaths] = None,
) -> Close:
    """`done` only when every part is closed; otherwise `partial` naming the open parts.

    *stated_labels* are the ask's lettered parts ("a", "b"); one with no row is open as "(a)".
    *plan_row_ids* missing from *staged_row_ids* (when given) with no entry in *defer_reasons* are open.
    *commit_paths* maps a commit to its file paths; when given, a part whose commit ships no tracked
    file is open. Without it the commit's content is not judged.
    """
    rows = list(rows)
    open_parts: list[str] = []
    for row in rows:
        if _ruled_out(row):
            continue
        part = part_from_row(row)
        closed = part.closed()
        if closed and commit_paths is not None:
            closed = commit_ships_tracked_file(commit_paths(part.evidence.commit or ""))
        if not closed:
            open_parts.append(part.id)
    have = {_label_of(r) for r in rows}
    for label in stated_labels:
        if label.strip().lower() not in have:
            open_parts.append(f"({label.strip().lower()})")
    if staged_row_ids is not None:
        staged = set(staged_row_ids)
        reasons = defer_reasons or {}
        for rid in plan_row_ids:
            if rid not in staged and not reasons.get(rid) and rid not in open_parts:
                open_parts.append(rid)
    outcome = OUTCOME_DONE if not open_parts else OUTCOME_PARTIAL
    return Close(outcome=outcome, open_parts=tuple(open_parts))


def close_from_sizing_text(sizing_text: Optional[str], **kwargs: Any) -> Close:
    """`part_closes` over the register in *sizing_text*; no register reads as no parts (done)."""
    register = _rr.read_register(sizing_text) if sizing_text else None
    return part_closes(register.rows if register is not None else [], **kwargs)


def with_parts(block: Mapping[str, Any], close: Close) -> dict[str, Any]:
    """A copy of a run-close block carrying `outcome`, `open_parts` and `missing_receipts`."""
    out = dict(block)
    out["outcome"] = close.outcome
    out["open_parts"] = list(close.open_parts)
    out["missing_receipts"] = list(close.missing_receipts)
    return out
