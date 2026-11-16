"""Own-hunks-only blobs for baselined landing paths that hold unclaimed dirt.

Contract: ``own_only_blobs`` answers, per landing path with a recorded write baseline, whether the
worktree file is wholly the run's own (omitted: stage it as-is) or carries other hunks (a blob with
the run's hunks over HEAD, to stage index-only) or cannot be split (a refusal row; a row with run
None and a real header is an unattributed hunk sharing lines with an own hunk, which the landing
stages whole for the clean-overlay verify to judge, not a refusal). Zero spawns;
stdlib only at module top, readers imported lazily (cold-import pin).

Contract: ``touched_own_split`` covers a measured-touched path with no write baseline: every hunk is the
run's own unless proven foreign (not in the run's kept or write-print blocks and inside a peer holder's
baseline delta); only proven-foreign hunks are reverted to HEAD.
Trap: ``own_only_blobs`` omits paths with no baseline, the run's whole file is not distinguishable from dirt.
Trap: the blob is derived from worktree bytes at call time; an edit made before the commit that
consumes it is not carried.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


def own_only_blobs(
    root: Path,
    run_id: str,
    paths: Iterable[str],
    baseline: Mapping[str, Optional[str]],
) -> Tuple[Dict[str, str], List[Tuple[str, Optional[str], str]]]:
    """``(blobs, refusals)``: ``blobs[path]`` is the sha of the worktree text with every non-own
    hunk reverted to HEAD; ``refusals`` is ``(path, run_or_None, header)`` for each hunk that
    shares lines with an own hunk, or ``(path, None, "unreadable")``. Never raises."""
    blobs: Dict[str, str] = {}
    refusals: List[Tuple[str, Optional[str], str]] = []
    root = Path(root)
    for path in paths:
        base = baseline.get(path)
        if base is None:
            continue
        try:
            from coordinator_core.ops.dispatch_emit import hunk_stage, kept_work

            got = hunk_stage.split(
                root, path, own_run_id=run_id, own_baseline_blob=base, foreign=[]
            )
            if got.overlaps or got.staged_blob is None:
                for run, header in got.overlaps or [(None, "unreadable")]:
                    refusals.append((path, run, header))
                continue
            form = kept_work.checkin_form(root, path, (root / path).read_bytes())
            if form is None:
                refusals.append((path, None, "unreadable"))
                continue
            if got.staged_blob == kept_work.blob_sha(form):
                continue
            blobs[path] = got.staged_blob
        except Exception:
            refusals.append((path, None, "unreadable"))
    return blobs, refusals


def touched_own_split(root: Path, run_id: str, paths: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """``{path: {"blob", "owner", "lines"}}`` for each of ``paths`` the run has no write baseline for
    and whose worktree carries a proven-foreign hunk: ``blob`` is HEAD plus every other hunk. A path
    with a baseline, no foreign hunk, no own evidence or any unreadable input is absent (lands whole).
    Zero spawns; never raises."""
    out: Dict[str, Dict[str, str]] = {}
    root = Path(root)
    try:
        from coordinator_core.ops.dispatch_emit import hunk_provenance as hp

        with hp.invariants_scope(root, run_id):
            for raw in paths:
                path = str(raw or "").replace("\\", "/")
                try:
                    got = _foreign_split(root, run_id, path, hp) if path else None
                except Exception:  # noqa: BLE001 -- unknown is own
                    got = None
                if got is not None:
                    out[path] = got
    except Exception:  # noqa: BLE001
        return {}
    return out


def _foreign_split(root: Path, run_id: str, path: str, hp) -> Optional[Dict[str, str]]:
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_blobs
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
    from coordinator_core.ops.dispatch_emit import kept_work, run_write_prints
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    inv = hp._inv(root, run_id)
    base = inv.baseline()
    if base is not None and base.get(path) is not None:
        return None
    entry = head_blobs(root, [path]).get(path)
    if entry is None:
        return None
    common = resolve_git_common_dir(root)
    obj = git_objects.read_object(common, entry[1])
    form = kept_work.checkin_form(root, path, (root / path).read_bytes())
    if obj is None or obj[0] != "blob" or form is None:
        return None
    head_bytes = obj[1]
    head = head_bytes.splitlines(keepends=True)
    committed = form.splitlines(keepends=True)
    hunks = hp._hunk_ops(head, committed)
    prints = run_write_prints.read(root, run_id) or {}
    evidence = set(inv.own_kept().get(path, ())) | ({prints[path]} if prints.get(path) else set())
    mine = hp._kept_blocks(root, common, path, head, sorted(evidence)) if evidence else set()
    if not hunks or not mine:
        return None

    deltas: Dict[str, list] = {}
    for holder in hp._owners(root, run_id, path, None):
        peer_base = hha._baseline(root, holder)
        if hp._own_chain(root, run_id, holder) or not peer_base or path not in peer_base:
            continue
        sha = peer_base[path]
        old: list = []
        if sha is not None:
            peer_obj = git_objects.read_object(common, sha)
            lifted = kept_work.checkin_form(root, path, peer_obj[1]) if peer_obj is not None else None
            if lifted is None:
                continue
            old = lifted.splitlines(keepends=True)
        if not rhc.exceeds_match_bound(old, committed):
            deltas[holder] = rhc._delta_spans(old, committed)

    own: list = []
    ranges: list = []
    owner: Optional[str] = None
    for hunk in hunks:
        _tag, i1, i2, j1, j2 = hunk
        lo, hi = hp._span(hunk)
        holder = next((h for h, d in deltas.items() if any(a <= hi and lo <= b for a, b in d)), None)
        if holder is not None and (tuple(head[i1:i2]), tuple(committed[j1:j2])) not in mine:
            owner = owner or holder
            ranges.append(f"{j1 + 1}-{j2}" if j2 > j1 + 1 else str(j1 + 1))
        else:
            own.append((i1, i2, j1, j2))
    if owner is None:
        return None
    return {
        "blob": hp._settled_blob(root, path, head_bytes, own),
        "owner": owner,
        "lines": hp._line_spans(", ".join(ranges)),
    }
