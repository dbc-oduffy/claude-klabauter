"""In-process check that a landing drops no line a foreign commit added since the run's base.

For each landed path whose HEAD blob differs from its base blob, every HEAD line the worktree
bytes remove is attributed by position to the first-parent commit that introduced it; a line
introduced by a commit that is not the run's own is a finding. Zero spawns: objects are read
through ``git_objects``. A run's own commits are those whose message carries ``Checkpoint-Base:``
(or ``Checkpoint-Review-Base:``) equal to the run base; when the plan is known, those carrying
``Checkpoint-Plan:`` equal to it with a stamped base anywhere on the walked chain. ``Session-Id``
cannot discriminate (concurrent runs share it).
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.git.checkpoint_guard import checkpoint_plan_matches
from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_objects import read_object, read_ref_loose_or_packed
from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

WALK_CAP = 1000
VERSIONS_PER_PATH_CAP = 64

NOTE_WALK_CAPPED = "walk-capped"
NOTE_UNREADABLE = "unreadable"

_PLAN_RE = re.compile(r"^Checkpoint-Plan: (\S.*?)\s*$", re.M)
_BASE_RE = re.compile(r"^Checkpoint-(?:Review-)?Base: ([0-9a-f]{7,40})\s*$", re.M)


def is_own_commit(message: str, plan: Optional[str], base_sha: str, chain: Sequence[str] = ()) -> bool:
    """True when the message's checkpoint trailers name this run's plan and base.

    With ``plan`` a stamped base is accepted when it prefix-matches ``base_sha`` or any sha in
    ``chain`` (every re-fire of a run stamps a new base, so the run base alone under-matches);
    without ``plan`` only a stamped base prefix-matching ``base_sha`` (either direction) counts.
    """
    stamped = _BASE_RE.findall(message)
    if plan is None:
        return any(base_sha.startswith(b) or b.startswith(base_sha) for b in stamped)
    if not any(checkpoint_plan_matches(p, plan) for p in _PLAN_RE.findall(message)):
        return False
    anchors = (base_sha, *chain)
    return any(a.startswith(b) for b in stamped for a in anchors)


def _head_sha(worktree_root: Path) -> Optional[str]:
    gitdir = resolve_git_dir(worktree_root)
    try:
        text = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("ref:"):
        return read_ref_loose_or_packed(resolve_git_common_dir(worktree_root), text[4:].strip())
    return text or None


def _chain_to_base(common_dir: Path, head: str, base: str) -> Optional[Tuple[List[str], str]]:
    """First-parent shas newest -> oldest excluding base, plus base's full sha; None when base
    is not reached within the cap."""
    out: List[str] = []
    sha: Optional[str] = head
    while sha and not sha.startswith(base):
        if len(out) >= WALK_CAP:
            return None
        meta = commit_meta(common_dir, sha)
        if meta is None:
            return None
        out.append(sha)
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return (out, sha) if sha else None


def _lines(common_dir: Path, blob: object) -> Optional[List[bytes]]:
    if blob is ABSENT:
        return []
    if blob is None:
        return None
    obj = read_object(common_dir, blob)  # type: ignore[arg-type]
    return None if obj is None else obj[1].splitlines(keepends=True)


def _opcodes(a: List[bytes], b: List[bytes]) -> List[Tuple[str, int, int, int, int]]:
    """difflib opcodes after trimming the common prefix/suffix, so the quadratic matcher sees
    only the changed middle."""
    n = min(len(a), len(b))
    lo, hi_bound = 0, n
    while lo < hi_bound:
        mid = (lo + hi_bound + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi_bound = mid - 1
    room = n - lo
    hi, hb = 0, room
    while hi < hb:
        mid = (hi + hb + 1) // 2
        if a[len(a) - mid :] == b[len(b) - mid :]:
            hi = mid
        else:
            hb = mid - 1
    ops: List[Tuple[str, int, int, int, int]] = []
    if lo:
        ops.append(("equal", 0, lo, 0, lo))
    sm = difflib.SequenceMatcher(None, a[lo : len(a) - hi], b[lo : len(b) - hi], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ops.append((tag, i1 + lo, i2 + lo, j1 + lo, j2 + lo))
    if hi:
        ops.append(("equal", len(a) - hi, len(a), len(b) - hi, len(b)))
    return ops


def check(
    worktree_root: Path,
    paths: Sequence[str],
    base_sha: str,
    *,
    run_id: Optional[str] = None,
    plan: Optional[str] = None,
    head: Optional[str] = None,
) -> Tuple[list, List[str]]:
    """Return ``(findings, notes)``. Findings are ``landing_integrity.Finding`` with reason
    ``SINCE_BASE_OVERLAP``; notes carry finding-free markers (``walk-capped``, ``unreadable:<path>``)
    the caller surfaces. ``run_id`` is accepted for the pinned signature; a commit whose subject
    ``ask_contract.halted_run_id`` parses to ``run_id`` is the caller's own halt record. ``plan``
    narrows the own-commit test to this run's plan."""
    from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP, Finding

    root = Path(worktree_root)
    common_dir = resolve_git_common_dir(root)
    head = head or _head_sha(root)
    findings: list = []
    notes: List[str] = []
    if not head or not base_sha:
        return findings, notes
    walked = _chain_to_base(common_dir, head, base_sha)
    if walked is None:
        notes.append(NOTE_WALK_CAPPED)
        return findings, notes
    newest_first, base_sha = walked
    if not newest_first:
        return findings, notes
    from coordinator_core.ops.dispatch_emit.ask_contract import halted_run_id

    own = set()
    for sha in newest_first:
        message = (commit_meta(common_dir, sha) or {}).get("message", "")
        if is_own_commit(message, plan, base_sha, newest_first) or (
            run_id is not None and halted_run_id(message.split("\n", 1)[0]) == run_id
        ):
            own.add(sha)
    oldest_first = list(reversed(newest_first))
    for path in paths:
        try:
            raw = (root / path).read_bytes()
        except OSError:
            notes.append(f"{NOTE_UNREADABLE}:{path}")
            continue
        head_blob = _blob_at_commit(common_dir, head, path)
        base_blob = _blob_at_commit(common_dir, base_sha, path)
        if head_blob == base_blob or head_blob is None or head_blob is ABSENT:
            continue
        head_lines = _lines(common_dir, head_blob)
        # Trap: under core.autocrlf the worktree holds CRLF while blobs hold LF, so a raw
        # line compare reads every line as dropped. Compare in the blob's line-ending form.
        if head_lines is not None and b"\r\n" in raw and not any(l.endswith(b"\r\n") for l in head_lines):
            raw = raw.replace(b"\r\n", b"\n")
        new_lines = raw.splitlines(keepends=True)
        if head_lines is None or head_lines == new_lines:
            if head_lines is None:
                notes.append(f"{NOTE_UNREADABLE}:{path}")
            continue
        tracked = [
            i
            for tag, i1, i2, _j1, _j2 in _opcodes(head_lines, new_lines)
            if tag in ("delete", "replace")
            for i in range(i1, i2)
        ]
        if not tracked:
            continue
        versions: List[Tuple[str, object]] = []
        prev: object = base_blob
        for sha in oldest_first:
            blob = _blob_at_commit(common_dir, sha, path)
            if blob is None or blob == prev:
                continue
            versions.append((sha, blob))
            prev = blob
        versions.reverse()
        if len(versions) > VERSIONS_PER_PATH_CAP:
            notes.append(NOTE_WALK_CAPPED)
            continue
        removed_by: Dict[str, int] = {}
        cur = head_lines
        unreadable = False
        for k, (sha, _blob) in enumerate(versions):
            older_blob = versions[k + 1][1] if k + 1 < len(versions) else base_blob
            older = _lines(common_dir, older_blob)
            if older is None:
                unreadable = True
                break
            still: List[int] = []
            ops = _opcodes(older, cur)
            for i in tracked:
                for tag, i1, _i2, j1, j2 in ops:
                    if j1 <= i < j2:
                        if tag == "equal":
                            still.append(i1 + (i - j1))
                        elif sha not in own:
                            removed_by[sha] = removed_by.get(sha, 0) + 1
                        break
            tracked, cur = still, older
            if not tracked or removed_by:
                break
        if unreadable:
            notes.append(f"{NOTE_UNREADABLE}:{path}")
        for sha, n in removed_by.items():
            findings.append(
                Finding(path, SINCE_BASE_OVERLAP, f"drops {n} line(s) added by {sha[:10]} since base {base_sha[:10]}")
            )
    return findings, notes


def find_overlaps(
    worktree_root: Path,
    paths: Sequence[str],
    base_sha: str,
    *,
    run_id: Optional[str] = None,
    plan: Optional[str] = None,
) -> list:
    """The ``landing_integrity.since_base_findings`` contract: findings only, no notes."""
    return check(worktree_root, paths, base_sha, run_id=run_id, plan=plan)[0]
