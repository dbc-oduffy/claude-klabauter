"""
coordinator_core.ops.cascade_baton_rows — AC6g baton-row depth for
`deliverable.cascade_terminal` (`coordinator_core/ops/deliverable_cascade.py`).

Purpose: R1a's cascade depth is NOT satisfied by flipping a roadmap-baton
handoff terminal alone — the baton carries its own `## Tasks` row spine
(same fenced-YAML shape and parser `execute_plan_assemble/close_out_and_stamp.py`
already established for a plan's spine, see `docs/wiki/writing-plans.md`
§ Machine-Parseable Task Spine), and a row inside it can be genuinely
uncommitted, deferred, or ruled-out even after the baton itself ships. This
module closes the gap `deliverable_cascade.py`'s own docstring names and
disclaims ("Does NOT implement AC6g's baton-row depth").
Spec backlink: pln-terminal-state-propagation-giv-c85539
§ C6g (AC6g).

EVIDENCE JOIN DELETED (P153-C22, `docs/plans/2026-09-22-spawn-budget-and-
census.md`, R1, kill means kill forever — DR-344 §6): this module's ONLY
evidence source for flipping a row was the shared chunk-evidence
`Deliverable-Id`-trailer/subject-chunk-id `git log` join
(`_committed_chunk_shas`, relocated here per C4 2026-08-20 "the close
ceremony stops paying for the join"). Spike S3 (P153-C21,
`docs/research/spike-verdicts/2026-09-22-evidence-join-range-bound.md`)
measured that join, cold, on the mandated worst-case fixture and 21 further
sampled plans: EVERY tested range-bound candidate either stayed over
DR-344's 500ms cold bar (a date-`--since` bound) or silently dropped real
committed-chunk evidence the unbounded walk finds (a `merge-base`-only
bound) — verdict `not-viable`, both candidates, no further candidate
closes the gap on this repo's actual commit-date distribution (see that
verdict's "Why no further candidate helps" section). Per DR-344 §6's
kill-bar disposition (no refactor lane, no suspension, no reinstatement),
the whole evidence-join closure this module carried since C4 is deleted
outright, not narrowed or rebounded. `resolve_baton_rows` below therefore
never resolves a row against commit evidence any more — every
commit-required `open` row is reported `unresolved`, permanently, with a
reason naming this deletion. The requirement question the verdict leaves
open (does the chunk-evidence join get re-approached by a non-range-bounded
mechanism, e.g. an index, sized and spiked as its own plan) is R1's, not
this row's — resurrecting the join here would be exactly the "rewrite the
join" this row's own body forbids.

Row-level provenance (advanced_by/advanced_at write path): DELETED
alongside the join it existed to serve — with no evidence source, this
module never flips a row, so it never writes to a candidate's spine at
all. `_stamp_row_provenance`/`_assert_row_stamp_fidelity`/the
`locked_rmw` write path and `_batch_commit_subjects`'s N+1 subject-batch
fix are all unreachable once `resolve_baton_rows` can no longer produce
an `updates` dict, and are removed rather than kept as dead code.

Live substrate note (verified at authorship time, 2026-08-04): zero live
`state/handoffs/*.md` records of `kind: roadmap-baton` currently carry a
`## Tasks` fenced spine in their own body — every live roadmap-baton
handoff today is flat (gate-narrative frontmatter only, no row content).
This does NOT make the mechanism unbuildable (a prior dispatch of this
chunk reported the absence and stopped there) — `locate_fenced_block`
returning `LocateStatus.ABSENT` is this module's ordinary, honest "nothing
to resolve" outcome (mirrors D7's absent-spine-is-fully-shipped posture in
`_parse_spine_rows`).

Negative-spec:
  - Does NOT call `git log`, or any other git subprocess, for row evidence.
    The evidence join is deleted, not rebounded — see EVIDENCE JOIN DELETED
    above.
  - Does NOT re-derive commit-coverage matching. `_commit_required_chunk_ids`/
    `_parse_spine_rows` are still imported from `close_out_and_stamp.py`
    verbatim.
  - Does NOT touch a row already at a non-`open` disposition, or a row
    carrying `deferred: true` — mirrors `_auto_resolve_committed_open_rows`'s
    own skip rules exactly; those rows are not this module's business.
  - Does NOT decide whether the OWNING baton itself advances — that is
    `deliverable_cascade.py`'s existing per-target predicate (AC6h). This
    module is called ONLY for a candidate `deliverable_cascade` has
    already decided to advance, as the row-depth half of that same
    decision.
  - Does NOT scan `archive/handoffs/` — same live-only containment
    discipline as `deliverable_cascade.py`'s own candidate collection.
  - Does NOT write to a candidate's spine, under any code path — with the
    evidence join deleted there is nothing left that could ever resolve a
    row, so `advanced` is always `[]`.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

# Imported from the LEAF (`row_spans`), never from `close_out_and_stamp`.
# Reaching back into that module put `deliverable_cascade` and
# `cascade_backstop_sweep` behind a partially-initialized-module ImportError
# whenever close-out was the entry point -- see `row_spans`'s own docstring for
# the three times this cycle has now been closed.
from coordinator_core.execute_plan_assemble.row_spans import (
    _OPEN,
    _commit_required_chunk_ids,
    _parse_spine_rows,
    _row_disposition,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

#: Reason string attached to every commit-required `open` row this module
#: can no longer resolve, now that the evidence join is deleted (P153-C22).
_EVIDENCE_JOIN_DELETED_REASON = (
    "no mechanical evidence source available -- the chunk-evidence "
    "commit-log join was deleted per DR-344's kill bar (P153-C22, "
    "docs/research/spike-verdicts/2026-09-22-evidence-join-range-bound.md, "
    "verdict: not-viable); left open"
)


def resolve_baton_rows(
    candidate_path: Path,
    deliverable_id: str,
    advanced_at: str,
    repo_root: Path,
) -> dict:
    """AC6g entrypoint. Called by `deliverable_cascade._handler` once per
    handoff it has ALREADY decided to advance (never for a refused
    candidate — deciding whether the owning baton advances is entirely
    `deliverable_cascade.py`'s own per-target predicate, AC6h; this
    function only ever resolves the ALREADY-ADVANCING candidate's own
    contained rows).

    `deliverable_id`/`advanced_at`/`repo_root` are kept on the signature
    for caller-contract stability (`deliverable_cascade._handler`'s call
    site is outside this chunk's footprint) even though the evidence join
    that once consumed them is deleted (see module docstring, EVIDENCE
    JOIN DELETED) -- `advanced` is now always `[]`.

    Returns:
        {
          "spine_status": "absent" | "located" | "malformed",
          "advanced": [],
          "unresolved": [{"row_id": ..., "reason": ...}, ...],
          "error": <str, present only on a genuine failure -- the spine
                    was unreadable or malformed>,
        }

    `spine_status: "absent"` (no `## Tasks` block in the handoff's own
    body at all) is the honest, ordinary common case today (see this
    module's docstring § Live substrate note) -- `advanced`/`unresolved`
    are both `[]` and there is no `error`."""
    try:
        text = candidate_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "spine_status": "malformed",
            "advanced": [],
            "unresolved": [],
            "error": f"{candidate_path}: could not read for row resolution: {exc}",
        }

    located = locate_fenced_block(text)
    if located.status == LocateStatus.ABSENT:
        return {"spine_status": "absent", "advanced": [], "unresolved": []}

    path_rel = str(candidate_path)
    rows, parse_error = _parse_spine_rows(text, path_rel)
    if parse_error is not None or rows is None:
        return {
            "spine_status": "malformed",
            "advanced": [],
            "unresolved": [],
            "error": parse_error or f"{path_rel}: baton row spine unparseable",
        }

    chunk_ids = _commit_required_chunk_ids(rows)
    if not chunk_ids:
        return {"spine_status": "located", "advanced": [], "unresolved": []}

    unresolved: List[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        chunk_id = row.get("id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        if _row_disposition(row) != _OPEN:
            continue
        unresolved.append({"row_id": chunk_id, "reason": _EVIDENCE_JOIN_DELETED_REASON})

    return {"spine_status": "located", "advanced": [], "unresolved": unresolved}
