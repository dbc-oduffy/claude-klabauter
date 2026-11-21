"""One merge for every landing that commits a run's recorded blob instead of the worktree bytes.

Invariant: the landed bytes for a path are the 3-way merge of (the run's base blob, the run's blob, the
blob HEAD holds at the land), so the run contributes only its own delta and a line HEAD gained after the
run's base survives unless the run's own diff removed it. A run blob committed as-is reverts every line
HEAD gained since the run's base (508aa081f4 dropped four lines e767f4fbcb had added).

A clean merge lands silently as a new blob; a collision is a typed `LAND_CONFLICT` naming the path and
hunks, never an overwrite. Trap: a run with no resolvable base cannot be merged; its blob passes
through unchanged and the path is reported in `unbased`. In process, no spawn: one tree-spine read per
side and one object read per blob.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, NamedTuple, Optional, Sequence

LAND_CONFLICT = "land_conflict"


class Merged(NamedTuple):
    blobs: dict  # {path: blob sha to land}
    conflicts: dict  # {path: [hunk dicts]} -- non-empty means the caller refuses with LAND_CONFLICT
    unbased: tuple  # paths passed through unmerged because no run base resolved


def run_base_sha(root: Path, run_id: object) -> Optional[str]:
    """The sha the run's work was written on: the pinned run base (`ask_fanout_contract.pinned_run_base`: the
    write-once run-base file, then the halt marker's `run_base_sha`, then the manifest `head_sha` as a last
    resort, since a restage or a sibling commit moves `head_sha` off the run's base). Each is read in
    process; None only when all are absent, which `merge_into_head` reports as `unbased`."""
    if not isinstance(run_id, str) or not run_id:
        return None
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import pinned_run_base

    return pinned_run_base(root, run_id, hha._manifest(root, run_id))


def unbased_trailer(paths: Sequence[str]) -> Optional[str]:
    """`Land-Unbased: <path>[, <path>...]`, the commit trailer `git log --grep Land-Unbased` finds; None
    when every path was merged against a resolved base."""
    return "Land-Unbased: " + ", ".join(sorted(paths)) if paths else None


def _blob_at(spine: Optional[dict], path: str) -> Optional[str]:
    d, _, name = path.rpartition("/")
    entry = (spine or {}).get(d, {}).get(name)
    return entry[1] if entry is not None and entry[0] != 0o40000 else None


def merge_into_head(root: Path, base_sha: Optional[str], supplied: Mapping[str, str]) -> Merged:
    """`supplied` `{path: run blob sha}` rewritten to `{path: merged blob sha}` against HEAD now."""
    if not supplied:
        return Merged({}, {}, ())
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.own_delta_merge import apply_own_delta, conflict_hunks

    paths = sorted(supplied)
    common = resolve_git_common_dir(root)
    head = git_state.read_tree_spine(root, paths)
    info = git_state.read_commit(root, base_sha) if base_sha else None
    base = git_state.read_tree_spine(root, paths, root_tree_sha=info.tree) if info is not None else None
    if head is None or (base_sha and info is not None and base is None):
        return Merged(dict(supplied), {}, tuple(paths))

    def text(sha: Optional[str]) -> Optional[bytes]:
        got = git_objects.read_object(common, sha) if sha else None
        return got[1] if got is not None and got[0] == "blob" else None

    out: dict = {}
    conflicts: dict = {}
    unbased: list = []
    for path in paths:
        # `run_sha`, never `run`: the amplification gate's route d matches a bare argument name against every
        # repo-wide `run` spawner, so `text(run)` reads as an injected runner though nothing here spawns.
        run_sha, h, b = supplied[path], _blob_at(head, path), _blob_at(base, path)
        if info is None:
            out[path] = run_sha
            unbased.append(path)
        elif h == run_sha or h is None and b is None or b == h:
            out[path] = run_sha  # the run wrote it on HEAD's bytes, or created it, or HEAD never moved it
        elif b == run_sha:
            out[path] = h if h is not None else run_sha  # the run changed nothing: HEAD's bytes stand
        else:
            run_b, head_b, base_b = text(run_sha), text(h), text(b)
            merged = apply_own_delta(base_b, run_b, head_b) if None not in (run_b, head_b, base_b) else None
            if merged is None:
                conflicts[path] = conflict_hunks(base_b, run_b, head_b) if None not in (run_b, head_b, base_b) else []
                out[path] = run_sha
            else:
                out[path] = git_objects.write_object(common, b"blob", merged)
    return Merged(out, conflicts, tuple(unbased))


def conflict_reply(conflicts: Mapping[str, Sequence[dict]]) -> dict:
    """The typed refusal fields: `{refused: LAND_CONFLICT, conflict_paths, land_conflicts, message}`."""
    first = sorted(conflicts)[0]
    where = "; ".join(f"lines {h['base_lines']}" for h in conflicts[first][:3]) or "whole file"
    return {
        "refused": LAND_CONFLICT, "conflict_paths": sorted(conflicts),
        "land_conflicts": {p: list(h) for p, h in sorted(conflicts.items())},
        "message": f"land_conflict: {first} ({where}) changed in both the run and HEAD since the run's base; "
                   "nothing committed. Re-run the path on HEAD, or land it by hand.",
    }
