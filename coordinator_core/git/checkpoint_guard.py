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

Zero spawns: history is walked in-process, over every parent, for the commits
in base..HEAD; a merge counts only where it differs from all its parents.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.rollback_check import _blob_at_commit

_SUBJECT_RE = re.compile(r"^checkpoint\(wave [^)]*\): \d+ rows? — (?P<ids>.+)$")
_PLAN_RE = re.compile(r"^Checkpoint-Plan: (?P<v>\S.*?)\s*$", re.M)
_BASE_RE = re.compile(r"^Checkpoint-Base: (?P<v>[0-9a-f]{7,40})\s*$", re.M)

#: A run longer than this is not a checkpoint run; stop rather than walk all history.
_WALK_CAP = 500

_CLOSED = frozenset({
    "coded", "spun_off", "backlogged", "wont_do", "voided", "superseded", "abandoned",
    "delivered_elsewhere",
})


def checkpoint_row_ids(subject: str) -> List[str]:
    """Row ids a `checkpoint(wave N): k rows -- ids` subject names; `[]` for any other subject."""
    m = _SUBJECT_RE.match(subject.strip())
    return [r.strip() for r in m.group("ids").split(",") if r.strip()] if m else []


_CHECKPOINT_PREFIX_RE = re.compile(r"^checkpoint\(wave [^)]*\):")
_RUN_END_TRAILER_RE = re.compile(r"^(?:Lands-Chunks|Inline-Review|Halt-Plan):", re.M)


def is_checkpoint_commit(message: str) -> bool:
    """True for a wave checkpoint of any subject shape: a `checkpoint(wave N):` subject, or
    `Checkpoint-Plan:` plus `Checkpoint-Base:` lines on a message that is not a run landing or halt.
    Wider than `checkpoint_row_ids`, which needs the em-dash id list."""
    message = message or ""
    if _CHECKPOINT_PREFIX_RE.match(message.split("\n", 1)[0].strip()):
        return True
    return bool(
        _PLAN_RE.search(message) and _BASE_RE.search(message) and not _RUN_END_TRAILER_RE.search(message)
    )


_LEADING_IDS_RE = re.compile(r"^([A-Za-z][\w.\-]*(?:\s*,\s*[A-Za-z][\w.\-]*)*)\s*:(?:\s|$)")


def plan_commit_row_ids(subject: str) -> List[str]:
    """Row ids a plan-trailered commit subject names: a checkpoint subject's ids, else
    the leading `<id>(, <id>)*:` list; `[]` otherwise. Not a commit-refusal predicate."""
    ids = checkpoint_row_ids(subject)
    if ids:
        return ids
    m = _LEADING_IDS_RE.match(subject.strip())
    return [r.strip() for r in m.group(1).split(",")] if m else []


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
    moved = _moved_outside_run(common_dir, head, base.group("v"), paths, plan.group("v"))
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


_STAMP_PREFIX = "plan: gated_exit_criteria met ("


def _after_frontmatter(blob: bytes) -> Optional[bytes]:
    """The bytes after a leading `---` frontmatter block, else None."""
    if not blob.startswith(b"---"):
        return None
    m = re.search(rb"\n---[ \t]*\r?\n", blob)
    return blob[m.end():] if m else None


def _is_engine_stamp(
    common_dir: Path, sha: str, parent: str, subject: str, path: str, plan: str
) -> bool:
    """True when `sha` is the engine's gated-exit stamp of `plan` and changes only `path`'s
    frontmatter. Subject plus diff shape is the provenance; a shared Session-Id is not."""
    if not subject.startswith(_STAMP_PREFIX) or not checkpoint_plan_matches(path, plan):
        return False
    if not checkpoint_plan_matches(subject.rpartition(" — ")[2], plan):
        return False
    new = _blob_at_commit(common_dir, sha, path)
    old = _blob_at_commit(common_dir, parent, path)
    n = _read_object(common_dir, new) if isinstance(new, str) else None
    o = _read_object(common_dir, old) if isinstance(old, str) else None
    if n is None or o is None:
        return False
    nb, ob = _after_frontmatter(n[1]), _after_frontmatter(o[1])
    return nb is not None and nb == ob


def _is_base(sha: str, base: str) -> bool:
    return sha.startswith(base) or base.startswith(sha)


def _reachable(common_dir: Path, start: str, stop: Optional[str] = None) -> List[str]:
    """Commits reachable from `start` over every parent, `start` first, capped at `_WALK_CAP`.
    A commit named by the `stop` sha or prefix is listed but not expanded."""
    seen: List[str] = []
    marked = set()
    queue = [start]
    while queue and len(seen) < _WALK_CAP:
        sha = queue.pop(0)
        if sha in marked:
            continue
        marked.add(sha)
        seen.append(sha)
        if stop is not None and _is_base(sha, stop):
            continue
        meta = commit_meta(common_dir, sha)
        if meta is not None:
            queue.extend(meta.get("parents") or [])
    return seen


def _moved_outside_run(
    common_dir: Path, head: Optional[str], base: str, paths: Sequence[str], plan: str
) -> List[str]:
    """Declared paths changed by a commit in base..HEAD (not the run's own checkpoints).
    A path restored to its base bytes still counts: the revert is the change."""
    if not head or not paths:
        return []
    candidates = _reachable(common_dir, head, stop=base)
    base_sha = next((s for s in candidates if _is_base(s, base)), None)
    walk = [s for s in candidates if s != base_sha]
    if base_sha is not None:
        # Capped like the head walk: base history deeper than `_WALK_CAP` is not excluded, so a
        # merge side line reaching that far back can list pre-base commits as in base..HEAD.
        ancestors = set(_reachable(common_dir, base_sha))
        walk = [s for s in walk if s not in ancestors]
    out: List[str] = []
    for sha in walk:
        meta = commit_meta(common_dir, sha)
        if meta is None:
            continue
        parents = meta.get("parents") or []
        msg_subject = (meta.get("message") or "").split("\n", 1)[0]
        if not parents or msg_subject.startswith("checkpoint("):
            continue
        for p in paths:
            blob = _blob_at_commit(common_dir, sha, p)
            if any(blob == _blob_at_commit(common_dir, parent, p) for parent in parents):
                continue
            if _is_engine_stamp(common_dir, sha, parents[0], msg_subject, p, plan):
                continue
            out.append(f"{p} by {sha[:10]} ({msg_subject[:60]})")
    return out
