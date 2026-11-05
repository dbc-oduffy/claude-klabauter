"""In-process check that a landing drops no line a foreign commit added since the run's base.

For each landed path whose HEAD blob differs from its base blob, every HEAD line the worktree
bytes remove is attributed by position to the first-parent commit that introduced it; a line
introduced by a commit that is not the run's own is a finding. Zero spawns: objects are read
through ``git_objects``. The landing commits the worktree bytes verbatim, so a landing that only inserts
lines over the run base still drops a sibling's since-base line and is a finding. A run's own commits are those whose message carries ``Checkpoint-Base:`` (or ``Checkpoint-Review-Base:``) equal to the run base; when the plan is known, those carrying
``Checkpoint-Plan:`` equal to it and either no stamped base, one on the walked chain, one among the run
base's first-parent ancestors, or one the run's own record names (``landing_integrity.closing_bases``).
``Session-Id`` cannot discriminate (concurrent runs share it), nor can plan text alone (a prior run of the
same plan stamps it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.git.checkpoint_guard import checkpoint_plan_matches
from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_objects import read_object, read_ref_loose_or_packed
from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit
from coordinator_core.session import record_homes

WALK_CAP = 1000
VERSIONS_PER_PATH_CAP = 64
ANCESTOR_CAP = 256

NOTE_WALK_CAPPED = "walk-capped"
NOTE_UNREADABLE = "unreadable"

_PLAN_RE = re.compile(r"^Checkpoint-Plan: (\S.*?)\s*$", re.M)
_BASE_RE = re.compile(r"^Checkpoint-(?:Review-)?Base: ([0-9a-f]{7,40})\s*$", re.M)


def is_own_commit(
    message: str,
    plan: Optional[str],
    base_sha: str,
    chain: Sequence[str] = (),
    ancestors: Sequence[str] = (),
    run_bases: Sequence[str] = (),
) -> bool:
    """True when the message's checkpoint trailers name this run's plan and base.

    With ``plan`` a matching ``Checkpoint-Plan`` with no stamped base is own; a stamped base is
    accepted when it prefix-matches ``base_sha``, any sha in ``chain`` (every re-fire of a run stamps a
    new base, so the run base alone under-matches), any sha in ``ancestors`` (first-parent history below
    the run base: a re-fire stamps the base it started from, which may predate the run base) or any
    ``run_bases`` entry (the bases the run's own record names: an emitted script stamps its emit sha, which
    is off the walked chain; a same-plan checkpoint of another run stamps a different one);
    without ``plan`` only a stamped base prefix-matching ``base_sha`` (either direction) counts.
    """
    stamped = _BASE_RE.findall(message)
    if plan is None:
        return any(base_sha.startswith(b) or b.startswith(base_sha) for b in stamped)
    if not any(checkpoint_plan_matches(p, plan) for p in _PLAN_RE.findall(message)):
        return False
    if not stamped:
        return True
    anchors = (base_sha, *chain, *ancestors, *run_bases)
    return any(a.startswith(b) for b in stamped for a in anchors)


def _halt_names_plan(message: str, plan: str, halt_plan_re: "re.Pattern[str]") -> bool:
    """True when a ``Halt-Plan`` trailer names ``plan``: a halt record of this plan under any run id."""
    return any(checkpoint_plan_matches(p, plan) for p in halt_plan_re.findall(message))


def _base_ancestors(common_dir: Path, base: str) -> List[str]:
    """First-parent shas below ``base``, newest first, bounded by ``ANCESTOR_CAP``."""
    out: List[str] = []
    meta = commit_meta(common_dir, base)
    while meta and len(out) < ANCESTOR_CAP:
        parents = meta.get("parents") or []
        if not parents:
            break
        out.append(parents[0])
        meta = commit_meta(common_dir, parents[0])
    return out


def _ask_sizing_lineage(root: Path, run_id: Optional[str]) -> Optional[str]:
    """``warp-<sizing stem>`` for an ``ask-`` run, read from its ``run.json``; None otherwise.

    A child warp run id is ``warp-<sizing stem>-<stamp>`` (``run_lineage``), so this is the lineage
    its halt records carry. One small file read, no spawn.
    """
    if not run_id or not run_id.startswith("ask-") or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    try:
        data = json.loads((Path(record_homes.home_dir(str(root), "warp")) / run_id / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rel = data.get("sizing_rel") if isinstance(data, dict) else None
    if not isinstance(rel, str) or not rel:
        return None
    return "warp-" + Path(rel).stem


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
    """Full opcodes (equal included) tiling both sides. The common prefix/suffix is trimmed, so the
    quadratic matcher sees only the changed middle; a middle past ``MAX_MATCHED_MIDDLE`` is split at
    patience anchors, and a segment past the bound or the per-call difflib budget is one coarse
    ``replace`` (``run_hunk_claims.anchored_opcodes``)."""
    from coordinator_core.ops.dispatch_emit.run_hunk_claims import MAX_DIFFLIB_LINES, anchored_opcodes

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
    mid_ops, _coarse = anchored_opcodes(a[lo : len(a) - hi], b[lo : len(b) - hi], line_budget=MAX_DIFFLIB_LINES)
    for tag, i1, i2, j1, j2 in mid_ops:
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
    ``ask_contract.halted_run_id`` parses to ``run_id`` or to a run id of the same sizing lineage
    (``ask_contract.run_lineage``: a refire's predecessor; for an ``ask-`` run, the lineage of the sizing
    its ``state/warp/<run_id>/run.json`` names, so a child warp run's halt record is own), or whose
    ``Halt-Plan`` trailer names ``plan`` (an earlier run id of the same plan), is the caller's own halt
    record. ``plan`` narrows the own-commit test to this run's plan."""
    from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP, Finding

    root = Path(worktree_root)
    common_dir = resolve_git_common_dir(root)
    head = head or _head_sha(root)
    findings: list = []
    notes: List[str] = []
    if not head or not base_sha:
        return findings, notes
    # The first-parent chain is read only once a path has a removed line to attribute; a landing
    # that drops nothing never pays the commit walk.
    newest_first: List[str] = []
    oldest_first: List[str] = []
    chain_walked = False
    if len(base_sha) != 40:
        walked = _chain_to_base(common_dir, head, base_sha)
        if walked is None:
            notes.append(NOTE_WALK_CAPPED)
            return findings, notes
        newest_first, base_sha = walked
        oldest_first = list(reversed(newest_first))
        chain_walked = True
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_TRAILER, halted_run_id, run_lineage

    lineage = run_lineage(run_id) or _ask_sizing_lineage(root, run_id)
    halt_plan_re = re.compile(r"^" + re.escape(HALT_PLAN_TRAILER) + r":[ \t]*(\S+)[ \t]*$", re.M)
    own_cache: List[set] = []

    def own_commits() -> set:
        """The run's own commits, computed once on the first path that reaches the version walk."""
        if own_cache:
            return own_cache[0]
        run_bases: Tuple[str, ...] = ()
        if run_id and plan is not None:
            from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

            run_bases = tuple(closing_bases(root, run_id, {}))
        own = set()
        ancestors: Optional[List[str]] = None
        for sha in newest_first:
            message = (commit_meta(common_dir, sha) or {}).get("message", "")
            is_own = is_own_commit(message, plan, base_sha, newest_first, (), run_bases)
            if not is_own and plan is not None and _BASE_RE.search(message):
                if ancestors is None:
                    ancestors = _base_ancestors(common_dir, base_sha)
                is_own = is_own_commit(message, plan, base_sha, newest_first, ancestors, run_bases)
            halted = halted_run_id(message.split("\n", 1)[0])
            if (
                is_own
                or (run_id is not None and halted == run_id)
                or (lineage is not None and halted is not None and run_lineage(halted) == lineage)
                or (plan is not None and _halt_names_plan(message, plan, halt_plan_re))
            ):
                own.add(sha)
        own_cache.append(own)
        return own

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
        if not chain_walked:
            walked = _chain_to_base(common_dir, head, base_sha)
            if walked is None:
                notes.append(NOTE_WALK_CAPPED)
                return findings, notes
            newest_first, base_sha = walked
            oldest_first = list(reversed(newest_first))
            chain_walked = True
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
        own = own_commits()
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
