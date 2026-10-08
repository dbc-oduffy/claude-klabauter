"""coordinator_core.git.checkpoint_guard -- the commit route's refusal of an
execute run's checkpoint that would re-land a row the EM has closed or
reverted.

An emitted workflow commits each wave as `checkpoint(wave N): k rows -- A, B`.
A resumed run replays its emit-time script, blind to anything the EM did
since, so its executor can re-write a row the EM reverted or marked closed.
The executor brief checks this too, but a prompt is advisory and a stale
script carries the old brief; this check runs in the commit itself.

The script stamps two body lines the check reads: `Checkpoint-Plan: <path>`
and `Checkpoint-Base: <run base sha>`. A checkpoint without them comes from
a script emitted before this guard and is refused: re-emit it.

Zero spawns: history is walked in-process, first parent only, from HEAD back
to the run base.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.rollback_check import _blob_at_commit

_SUBJECT_RE = re.compile(r"^checkpoint\(wave [^)]*\): \d+ rows? — (?P<ids>.+)$")
_PLAN_RE = re.compile(r"^Checkpoint-Plan: (?P<v>\S.*?)\s*$", re.M)
_BASE_RE = re.compile(r"^Checkpoint-Base: (?P<v>[0-9a-f]{7,40})\s*$", re.M)

#: A run longer than this is not a checkpoint run; stop rather than walk all history.
_WALK_CAP = 500

_CLOSED = frozenset({"coded", "spun_off", "backlogged", "wont_do", "voided", "superseded", "abandoned"})


def checkpoint_row_ids(subject: str) -> List[str]:
    """Row ids a `checkpoint(wave N): k rows -- ids` subject names; `[]` for any other subject."""
    m = _SUBJECT_RE.match(subject.strip())
    return [r.strip() for r in m.group("ids").split(",") if r.strip()] if m else []


def _drop_repo_key(path: str) -> str:
    head, sep, tail = path.partition(":")
    return tail if sep and len(head) > 1 and "/" not in head else path


def checkpoint_plan_matches(trailer_value: str, plan: str) -> bool:
    """True when a `Checkpoint-Plan:` value names `plan`; a `repo_key:` prefix on either side is ignored."""
    a = trailer_value.strip().replace("\\", "/")
    b = plan.strip().replace("\\", "/")
    return a == b or _drop_repo_key(a) == _drop_repo_key(b)


def checkpoint_refusal(
    repo_root: Path,
    common_dir: Path,
    head: Optional[str],
    message: str,
    paths: Sequence[str],
) -> Optional[str]:
    """The refusal text for a checkpoint commit that must not land, else None.
    Any message whose subject is not a checkpoint returns None."""
    subject = message.split("\n", 1)[0].strip()
    m = _SUBJECT_RE.match(subject)
    if not m:
        return None
    plan = _PLAN_RE.search(message)
    base = _BASE_RE.search(message)
    if not plan or not base:
        return (
            "refusing checkpoint: no Checkpoint-Plan/Checkpoint-Base lines, so the script "
            "predates the resume guard -- re-emit it with emit-dispatch-workflow"
        )
    row_ids = [r.strip() for r in m.group("ids").split(",") if r.strip()]
    closed = _closed_rows(repo_root / plan.group("v"), row_ids)
    if closed:
        return (
            f"refusing checkpoint: row(s) closed on the spine: {', '.join(closed)}. "
            "Re-landing a closed row undoes the EM's disposition."
        )
    moved = _moved_outside_run(common_dir, head, base.group("v"), paths)
    if moved:
        return (
            f"refusing checkpoint: writes changed outside this run: {'; '.join(moved)}. "
            "A resumed row must not overwrite a revert."
        )
    return None


def _closed_rows(plan_path: Path, row_ids: List[str]) -> List[str]:
    from coordinator_core.ops.dispatch_emit.spine_read import (
        load_rows_memo,
        with_canonical_disposition,
    )

    try:
        result = load_rows_memo(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    wanted = set(row_ids)
    out = []
    for raw in getattr(result, "rows", None) or []:
        row = with_canonical_disposition(raw)
        if not isinstance(row, dict) or str(row.get("id")) not in wanted:
            continue
        state = {str(row.get("disposition") or ""), str(row.get("status") or "")}
        if state & _CLOSED:
            out.append(str(row.get("id")))
    return out


def _moved_outside_run(
    common_dir: Path, head: Optional[str], base: str, paths: Sequence[str]
) -> List[str]:
    out: List[str] = []
    sha = head
    for _ in range(_WALK_CAP):
        if not sha or sha.startswith(base) or base.startswith(sha):
            return out
        meta = commit_meta(common_dir, sha)
        if meta is None:
            return out
        parents = meta.get("parents") or []
        msg_subject = (meta.get("message") or "").split("\n", 1)[0]
        if parents and not msg_subject.startswith("checkpoint("):
            for p in paths:
                if _blob_at_commit(common_dir, sha, p) != _blob_at_commit(common_dir, parents[0], p):
                    out.append(f"{p} by {sha[:10]} ({msg_subject[:60]})")
        sha = parents[0] if parents else None
    return out
