"""
coordinator_core.ops.review_partition_slices — registers ``review.partition_slices``:
the brightline verdict plus, when it is PARTITION-MANDATORY, the run's product files
grouped into at most six review slices, each frozen in-process through
``review_freeze_diff.freeze_diff``: committed ``base..head`` in range mode (``head`` given),
``base``->worktree otherwise.

Grouping rule: the first two DIRECTORY segments of a product path (``a/b/c.py`` -> ``a/b``;
a top-level file -> ``.``); while more than ``MAX_SLICES`` groups remain, the two smallest
(file count, then key) merge. Slice ids are ``<slice_prefix>-<n>``, n = 1.. in key order.

Roster rules (optional ``roster`` = the review-wave stage): an agent ``merge`` shared-paths rule
adds ``reviewer_groups`` (slice ids one reviewer takes); ``lens_fold`` adds ``lens_folds``.

Negative-spec: a ``single-reviewer-ok`` verdict returns ``slices: []`` -- no partition; the
caller reviews the whole diff as one slice. Exactly one of ``head`` / ``worktree=true``: both
or neither is a structured error. Slices carry different pathspecs, which
``freeze_diffs_batch`` refuses to mix, so each slice is its own freeze call.
"""

from __future__ import annotations

MUTATES = ["state/review-trail/diffs/*.diff", "state/review-trail/diffs/*.head.sha"]

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.review_brightline_gate import (
    RangeMeasureError,
    _run_git,
    _verdict,
    range_measure,
    worktree_measure,
)
from coordinator_core.ops.review_freeze_diff import freeze_diff
from coordinator_core.ops.review_slice_limits import MAX_SLICES


def _group_key(path: str) -> str:
    return "/".join(path.split("/")[:-1][:2]) or "."


def group_paths(paths: List[str], max_slices: int = MAX_SLICES) -> List[List[str]]:
    """Group `paths` by `_group_key`, merge to at most `max_slices` groups; deterministic."""
    groups: Dict[str, List[str]] = {}
    for p in sorted(set(paths)):
        groups.setdefault(_group_key(p), []).append(p)
    ordered = [(key, files) for key, files in sorted(groups.items())]
    while len(ordered) > max_slices:
        by_size = sorted(range(len(ordered)), key=lambda i: (len(ordered[i][1]), ordered[i][0]))
        a, b = sorted(by_size[:2])
        merged = (ordered[a][0], sorted(ordered[a][1] + ordered[b][1]))
        ordered = [g for i, g in enumerate(ordered) if i not in (a, b)] + [merged]
        ordered.sort(key=lambda g: g[0])
    return [files for _, files in ordered]


def _ancestor_dirs(paths: List[str]) -> set:
    return {"/".join(p.split("/")[:i]) for p in paths for i in range(1, len(p.split("/")))}


def reviewer_groups(slices: List[dict], min_shared_pct: float) -> List[List[str]]:
    """Slice ids grouped one-reviewer-multi-slice: two slices join when the ancestor directories
    they share are at least `min_shared_pct` percent of the smaller slice's ancestor set
    (transitive); a top-level-only slice has none and stays alone."""
    dirs = [_ancestor_dirs(s["paths"]) for s in slices]
    parent = list(range(len(slices)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(slices)):
        for j in range(i + 1, len(slices)):
            smaller = min(len(dirs[i]), len(dirs[j]))
            if smaller and 100 * len(dirs[i] & dirs[j]) >= min_shared_pct * smaller:
                parent[find(j)] = find(i)
    out: Dict[int, List[str]] = {}
    for i, s in enumerate(slices):
        out.setdefault(find(i), []).append(s["slice_id"])
    return list(out.values())


def _stage_agents(roster: dict) -> List[dict]:
    return [a for a in (roster.get("agents") or []) if isinstance(a, dict)]


def apply_roster_rules(out: dict, roster: Optional[dict], rostered: Optional[List[str]]) -> None:
    """Add `reviewer_groups` (an agent's `merge` rule) and `lens_folds` (the stage's `lens_fold`
    rule, when its `when_rostered` reviewer is in `rostered`) to `out`; no roster, no change."""
    if not roster:
        return
    for agent in _stage_agents(roster):
        merge = agent.get("merge")
        if isinstance(merge, dict) and merge.get("rule") == "shared-paths":
            out["reviewer_groups"] = reviewer_groups(out["slices"], float(merge.get("min_shared_pct") or 0))
            break
    fold = roster.get("lens_fold")
    if isinstance(fold, dict) and fold.get("when_rostered") in set(rostered or []):
        out["lens_folds"] = [{
            "folded": fold["when_rostered"], "into": fold.get("fold"),
            "as": fold.get("as"), "verdict": fold.get("verdict"),
        }]


def _effective_base(path: str, base: str, path_bases: Dict[str, str]) -> str:
    """Base for `path`: the longest `path_bases` key equal to it or an ancestor directory."""
    best_len, result = -1, base
    for key, value in path_bases.items():
        k = key.rstrip("/")
        if (path == k or path.startswith(k + "/")) and len(k) > best_len:
            best_len, result = len(k), value
    return result


def _partition(
    repo_root: Path,
    base: str,
    paths: List[str],
    slice_prefix: str,
    *,
    head: Optional[str] = None,
    path_bases: Optional[Dict[str, str]] = None,
    head_fallback: Optional[List[str]] = None,
) -> dict:
    """Gate verdict over `paths`, and the frozen slices when partitioned.

    With `head` set, measures and freezes committed `base..head` only (range mode); otherwise
    `base`->worktree. Range mode never reads the working tree.

    `path_bases` maps a declared path to its own base; absent paths use `base`. Measure and
    freeze run once per distinct base (spawns grow with bases, not paths); a directory slice
    spanning two bases is frozen as one slice per base (after `group_paths`, so the slice count
    can exceed MAX_SLICES). None/{} is the single-base behaviour.

    `head_fallback` (range mode only) lists own paths; those with no range delta are measured and
    frozen as HEAD->worktree slices (withheld, untracked, or peer-held edits), always, and are
    named in `fallback_paths`.

    Public `partition_slices` also takes `roster` (the roster fragment's review-wave stage) and
    `rostered` (agent types in the wave): an agent `merge` rule adds `reviewer_groups`, the
    `lens_fold` rule adds `lens_folds`; without a roster the result is unchanged.

    Returns `{verdict, loc, surfaces, product_paths: [..], slices: [{slice_id, paths, diff_path}],
    error}`; `error` is None on success. A freeze refusal is returned as `error` with the
    slices frozen so far, never raised.
    """
    out: dict = {"verdict": None, "loc": 0, "surfaces": 0, "product_paths": [], "slices": [], "fallback_paths": [], "worktree_paths": [], "error": None}
    if not base or not paths or not slice_prefix:
        out["error"] = "base, paths and slice_prefix are required"
        return out
    bases = path_bases or {}
    by_base: Dict[str, List[str]] = {}
    for p in paths:
        by_base.setdefault(_effective_base(p, base, bases), []).append(p)
    loc, surfaces, files = 0, set(), set()
    for b, group_declared in by_base.items():
        if head:
            try:
                g_loc, g_surfaces, g_files = range_measure(b, head, group_declared, str(repo_root))
            except RangeMeasureError as exc:
                out["error"] = str(exc)
                return out
        else:
            g_loc, g_surfaces, g_files = worktree_measure(b, group_declared, str(repo_root))
        loc += g_loc
        surfaces |= g_surfaces
        files |= g_files
    fb_files: set = set()
    head_sha = ""
    missing = sorted(set(head_fallback or []) - files) if head else []
    if missing:
        # One spawn: the worktree measure and freeze need a concrete sha, not the moving name.
        head_sha, rc = _run_git(["rev-parse", "HEAD"], str(repo_root))
        head_sha = head_sha.strip()
        if rc != 0 or not head_sha:
            out["error"] = "head_fallback: git rev-parse HEAD failed"
            return out
        f_loc, f_surfaces, fb_files = worktree_measure(head_sha, missing, str(repo_root))
        loc += f_loc
        surfaces |= f_surfaces
    dirty: set = set()
    own_ranged = sorted(set(head_fallback or []) & files) if head else []
    if own_ranged:
        # One extra spawn, only when an own path has a range delta: such a path may also carry
        # uncommitted bytes, which a base..head freeze would drop; those freeze base..worktree.
        names, rc = _run_git(["diff", "--name-only", "HEAD", "--", *own_ranged], str(repo_root))
        if rc == 0:
            dirty = {ln.strip() for ln in names.splitlines() if ln.strip()} & set(own_ranged)
    verdict = _verdict(loc, 0, len(surfaces))
    out.update(
        worktree_paths=sorted(dirty),
        verdict=verdict,
        loc=loc,
        surfaces=len(surfaces),
        product_paths=sorted(files | fb_files),
        fallback_paths=sorted(fb_files),
    )
    n = 0
    if fb_files:
        for group in group_paths(sorted(fb_files)):
            n += 1
            slice_id = f"{slice_prefix}-{n}"
            frozen = freeze_diff(repo_root, head_sha, slice_id, group, worktree=True)
            if frozen.get("error"):
                out["error"] = f"freeze of {slice_id} failed: {frozen['error']}"
                return out
            out["slices"].append({"slice_id": slice_id, "paths": group, "diff_path": frozen["diff_path"]})
    if verdict != "PARTITION-MANDATORY" or not files:
        return out
    for group in group_paths(sorted(files)):
        sub: Dict[str, List[str]] = {}
        for f in group:
            sub.setdefault(_effective_base(f, base, bases), []).append(f)
        for b, sub_group in sorted(sub.items()):
            n += 1
            slice_id = f"{slice_prefix}-{n}"
            if head and not (set(sub_group) & dirty):
                frozen = freeze_diff(repo_root, f"{b}..{head}", slice_id, sub_group)
            elif head:
                frozen = freeze_diff(repo_root, b, slice_id, sub_group, worktree=True)
            else:
                frozen = freeze_diff(repo_root, b, slice_id, sub_group, worktree=True)
            if frozen.get("error"):
                out["error"] = f"freeze of {slice_id} failed: {frozen['error']}"
                return out
            out["slices"].append({"slice_id": slice_id, "paths": sub_group, "diff_path": frozen["diff_path"]})
    return out


def partition_slices(*args, roster: Optional[dict] = None, rostered: Optional[List[str]] = None, **kwargs) -> dict:
    """`_partition` plus the roster's merge and lens_fold rules (see `apply_roster_rules`)."""
    out = _partition(*args, **kwargs)
    if not out["error"]:
        apply_roster_rules(out, roster, rostered)
    return out


@register_op("review.partition_slices")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.partition_slices". Params: ``base`` (str), ``paths`` (list[str]),
    ``slice_prefix`` (str), and exactly one of ``head`` (str, committed range mode) or
    ``worktree: true``. Keying scope: show_top."""
    if repo_root is None:
        return {"error": "review.partition_slices requires a show_top-keyed dispatch; repo_root was not supplied"}
    paths = params.get("paths")
    if not isinstance(paths, list):
        return {"error": "review.partition_slices requires params.paths as a list"}
    head = str(params.get("head") or "")
    worktree = bool(params.get("worktree"))
    if bool(head) == worktree:
        return {"error": "review.partition_slices requires exactly one of head (range mode) or worktree=true"}
    return partition_slices(
        repo_root,
        str(params.get("base") or ""),
        [str(p) for p in paths],
        str(params.get("slice_prefix") or ""),
        head=head or None,
        path_bases={str(k): str(v) for k, v in (params.get("path_bases") or {}).items()},
        head_fallback=[str(p) for p in (params.get("head_fallback") or [])],
        roster=params.get("roster") if isinstance(params.get("roster"), dict) else None,
        rostered=[str(a) for a in (params.get("rostered") or [])],
    )
