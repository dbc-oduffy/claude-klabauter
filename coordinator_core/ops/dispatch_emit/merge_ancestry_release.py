"""merge_ancestry_release drops halted-holder holds that the open merge itself brings in.

A halted run H's hold on path P does not apply when all of these hold: a merge is open
(`<gitdir>/MERGE_HEAD`), H is halted, the merge moves P (`merge_state.read_merge_state(...)
.merge_paths`), and a commit reachable from MERGE_HEAD names H as either its landing commit
(`checkpoint_stamp.is_landing_message` plus the structured naming `halted_criterion_met._names`
uses) or its halt-record commit (subject `warp: record halted <H> `).

Traps:
- The merge-path condition is load-bearing. A halt record reachable from HEAD is also reachable
  from a MERGE_HEAD that contains it; without "the merge moves P", that would free the halted
  run's uncommitted hunks. git refuses to start a merge over a locally modified path, so dirt on
  a merge path is the resolution, never a peer's hunk.
- `merge_paths` omits a path whose resolution equals HEAD, so such a held path keeps its hold.
- Reachability is `git log MERGE_HEAD`, not `MERGE_HEAD ^HEAD`: a halt record present in both
  histories must still count.

Invariants: at most one `git.run.run_git` call, made only when a merge is open, some holder is
halted with a stamp and a held path is moved by the merge; zero otherwise. It never raises; any
read error, `MergeStateError`, octopus MERGE_HEAD or non-zero git rc returns `holds` unchanged.

Stdlib only at module top: land_run and terminal_commit have cold-import pins, so every reader is
imported lazily.
"""

from __future__ import annotations

from pathlib import Path

_FIELD = "\x1f"
_RECORD = "\x1e"
_HALT_SUBJECT = "warp: record halted "


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def release(worktree_root, holds: list) -> list:
    """`holds` (`[(path, holder_run_id)]`) minus each hold the open merge brings in. `holds`
    unchanged when no merge is open or anything cannot be read."""
    try:
        return _release(Path(worktree_root), holds)
    except Exception:  # noqa: BLE001 -- any read error: every hold stands
        return holds


def _release(root: Path, holds: list) -> list:
    if not holds:
        return holds
    from coordinator_core.git.git_dir import resolve_git_dir

    if not (resolve_git_dir(root) / "MERGE_HEAD").exists():
        return holds

    # Underscore-private readers shared with halted_criterion_met; a rename there makes release()
    # fall into its broad except and keep every hold, so test_merge_ancestry_release must stay green.
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit import stopped_runs

    holders = sorted({h for _p, h in holds if wrd._halted(root, h)})
    stamps = {h: stopped_runs._stamp_epoch(h) for h in holders}
    holders = [h for h in holders if stamps[h] is not None]
    if not holders:
        return holds

    from coordinator_core.git.merge_state import read_merge_state

    state = read_merge_state(root)
    if state is None or len(state.merge_head.split()) > 1:
        return holds
    moved = {_norm(p) for p in state.merge_paths}
    holders = [h for h in holders if any(s == h and _norm(p) in moved for p, s in holds)]
    if not holders:
        return holds

    deliverables = {
        h: wrd.sizing_scalar(root, wrd.record_sizing_rel(root, h), "deliverable_id") for h in holders
    }
    needles = []
    for h in holders:
        needles.extend(n for n in (h, deliverables[h]) if n and n not in needles)
    since = min(stamps[h] for h in holders)

    from coordinator_core.git.run import run_git

    result = run_git([
        "-C", str(root), "log", "MERGE_HEAD", f"--since=@{int(since)}", "-F",
        *[f"--grep={n}" for n in needles], f"--format=%H{_FIELD}%B{_RECORD}",
    ])
    if result.returncode != 0:
        return holds
    bodies = []
    for record in result.stdout.split(_RECORD):
        _sha, sep, body = record.strip("\n").partition(_FIELD)
        if sep:
            bodies.append(body)

    in_merge = {h for h in holders if _brought_in(bodies, h, deliverables[h])}
    return [(p, s) for p, s in holds if not (s in in_merge and _norm(p) in moved)]


def _brought_in(bodies: list, run_id: str, deliverable: str) -> bool:
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import is_landing_message
    from coordinator_core.ops.dispatch_emit.halted_criterion_met import _names

    for body in bodies:
        lines = body.splitlines()
        subject = lines[0].strip() if lines else ""
        if subject.startswith(f"{_HALT_SUBJECT}{run_id} "):
            return True
        if is_landing_message(body) and _names(body, run_id, deliverable):
            return True
    return False
