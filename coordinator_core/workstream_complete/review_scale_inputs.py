"""The review-scale quadruple, commit slices and attribution inputs, read from a SessionHistory.

`measure_review_scale_inputs(root, history, stage_paths, ...)` answers one question: how large is this
session's reviewable diff, committed (its own non-merge commits in the history window) plus the
caller-named uncommitted paths. At most one git spawn, and none when `stage_paths` names no tracked path.

An unresolved history yields an all-None quadruple, never zeros: a zeroed quadruple reads as a resolved,
trivially small diff and moves the verdict toward reviewing LESS. A zero returned after a resolved history
is honest. Never widen either leg to a branch range or an unrestricted worktree diff: a brightline over
work this session did not author invites a review attestation covering a peer's changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.workstream_complete.session_history import SessionCommit, SessionHistory

_MIN_REF_LEN = 7


@dataclass(frozen=True)
class ReviewScaleInputs:
    """`commit_slices` is [] and every count None when the history is unresolved.

    `attribution_gap` is {"recovered": [{"chunk","sha"}], "conflicting": [{"chunk","sha","committed_by"}]};
    recovered shas are already folded into the quadruple, slices and zero-diff count. `unattributed_shas`
    are non-merge window commits carrying no Session-Id, minus the recovered ones (None when unresolved).
    """

    gross_loc: Optional[int]
    code_loc: Optional[int]
    commit_count: Optional[int]
    surface_count: Optional[int]
    commit_slices: list[dict[str, Any]] = field(default_factory=list)
    zero_diff_commit_count: Optional[int] = None
    attribution_gap: dict[str, list[dict[str, str]]] = field(
        default_factory=lambda: {"recovered": [], "conflicting": []}
    )
    unattributed_shas: Optional[tuple[str, ...]] = None

    @property
    def quadruple(self) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        return self.gross_loc, self.code_loc, self.commit_count, self.surface_count


def _unresolved() -> ReviewScaleInputs:
    """Fresh each call: the list and dict fields are mutable, so a shared instance would leak edits."""
    return ReviewScaleInputs(None, None, None, None)


def _numstat_text(commit: SessionCommit) -> str:
    return "\n".join(
        f"{'-' if f.added is None else f.added}\t{'-' if f.deleted is None else f.deleted}\t{f.path}"
        for f in commit.files
    )


def _recovery_gap(
    window: Sequence[SessionCommit],
    own_shas: set[str],
    chunk_refs: Sequence[tuple[str, str]],
    plan_deliverable_id: str,
) -> tuple[list[tuple[str, SessionCommit]], list[dict[str, str]]]:
    """Plan-spine chunk commits inside the window that Session-Id attribution did not reach.

    A ref outside the window is neither recovered nor conflicting: the window is this session's own
    span, and folding a commit from elsewhere widens review over another session's work.
    """
    recoverable: list[tuple[str, SessionCommit]] = []
    conflicting: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk_id, ref in chunk_refs:
        ref = ref.strip()
        if len(ref) < _MIN_REF_LEN:
            continue
        commit = next((c for c in window if c.sha.startswith(ref)), None)
        if commit is None or commit.is_merge or commit.sha in own_shas or commit.sha in seen:
            continue
        seen.add(commit.sha)
        if not commit.session_ids or (plan_deliverable_id and commit.deliverable_id == plan_deliverable_id):
            recoverable.append((chunk_id, commit))
        else:
            conflicting.append({"chunk": chunk_id, "sha": commit.sha, "committed_by": commit.session_ids[0]})
    return recoverable, conflicting


def measure_review_scale_inputs(
    root: Path,
    history: SessionHistory,
    stage_paths: Sequence[str],
    *,
    chunk_refs: Sequence[tuple[str, str]] = (),
    plan_deliverable_id: str = "",
) -> ReviewScaleInputs:
    """Measure the review-scale inputs; `chunk_refs` is [(chunk_id, disposition_ref)] for coded plan rows."""
    if not history.resolved:
        return _unresolved()

    from coordinator_core import workstream_complete as wsc

    own = [c for c in history.own if not c.is_merge]
    own_shas = {c.sha for c in own}
    recoverable, conflicting = _recovery_gap(history.window, own_shas, chunk_refs, plan_deliverable_id)

    gross_loc = 0
    code_loc = 0
    surfaces: set[str] = set()
    slices: list[dict[str, Any]] = []

    def fold(commit: SessionCommit, via: Optional[str]) -> None:
        nonlocal gross_loc, code_loc
        text = _numstat_text(commit)
        commit_surfaces: set[str] = set()
        gross_loc += wsc._accumulate_numstat(text, surfaces)
        wsc._accumulate_numstat(text, commit_surfaces)
        commit_code_loc = wsc._accumulate_code_loc_numstat(text)
        code_loc += commit_code_loc
        entry: dict[str, Any] = {
            "sha": commit.sha,
            # `~1`, never `^`: cmd.exe eats a literal `^` in the --sha-range argv this feeds.
            "sha_range": f"{commit.sha}~1..{commit.sha}",
            "diff_loc": commit_code_loc,
            "reviewable_surface_count": len(commit_surfaces),
        }
        if via:
            entry["attributed_via"] = via
        slices.append(entry)

    for chunk_id, commit in recoverable:
        fold(commit, f"plan-spine {chunk_id} (no Session-Id trailer)")
    for commit in own:
        fold(commit, None)
    commit_count = len(slices)

    paths = list(stage_paths)
    if paths:
        split = wsc._split_tracked(root, paths)
        if split is None:
            return _unresolved()
        tracked, untracked = split
        if tracked:
            normalized = [p.replace("\\", "/") for p in tracked]
            dirty = wsc._run_git_read_only(["diff", "--numstat", "HEAD", "--", *normalized], root)
            if dirty is None:
                return _unresolved()
            gross_loc += wsc._accumulate_numstat(dirty, surfaces)
            code_loc += wsc._accumulate_code_loc_numstat(dirty)
        walk_budget = [wsc._UNTRACKED_WALK_BUDGET]
        for rel_path in untracked:
            members = wsc._expand_untracked(root, rel_path, budget=walk_budget)
            if members is None:
                return _unresolved()
            for member in members:
                added = wsc._count_lines(root / member)
                if added is None:
                    return _unresolved()
                gross_loc += added
                if wsc._reviewable_row(member):
                    code_loc += added
                    surfaces.add(wsc.classify_surface(member))
        if walk_budget[0] <= 0:
            # A tree too large to enumerate IS a big diff: the counted LOC is a floor, raised so the
            # brightline trips instead of reading the truncation as small.
            gross_loc = max(gross_loc, wsc.directives_review._BRIGHTLINE_LOC)
            code_loc = max(code_loc, wsc.directives_review._BRIGHTLINE_LOC)

    recovered_shas = {c.sha for _chunk, c in recoverable}
    return ReviewScaleInputs(
        gross_loc=gross_loc,
        code_loc=code_loc,
        commit_count=commit_count,
        surface_count=len(surfaces),
        commit_slices=slices,
        zero_diff_commit_count=sum(
            1 for s in slices if s["diff_loc"] == 0 or s["reviewable_surface_count"] == 0
        ),
        attribution_gap={
            "recovered": [{"chunk": chunk, "sha": c.sha} for chunk, c in recoverable],
            "conflicting": conflicting,
        },
        unattributed_shas=tuple(
            c.sha
            for c in history.window
            if not c.is_merge and not c.session_ids and c.sha not in recovered_shas
        ),
    )
