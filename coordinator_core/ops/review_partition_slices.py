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

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.review_brightline_gate import (
    _CHAIN_NUMSTAT_RE,
    RangeMeasureError,
    _count_measured_row,
    _resolve_numstat_row_path,
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


_DIFF_SECTION = re.compile(r"(?m)^(?=diff --git )")


def _diff_sections(diff_text: str) -> List[tuple]:
    """``[(covered_paths, section_text)]`` for each ``diff --git`` section of `diff_text`."""
    from coordinator_core.ops.review_freeze_diff import _covered_paths_from_diff

    return [(_covered_paths_from_diff(s), s) for s in _DIFF_SECTION.split(diff_text) if s.startswith("diff --git ")]


def _measure_sections(sections: List[tuple]) -> tuple:
    """`(loc, surfaces, files)` of a frozen diff's sections: the rows `diff --numstat` would give for the
    same diff (added plus deleted hunk lines per file, 0 for a binary or mode-only file), through the same
    `_count_measured_row` filters. No git: it reads text the caller already holds."""
    from coordinator_core.ops.review_freeze_diff import _c_unquote, _parse_diff_git_header

    loc, surfaces, files = 0, set(), set()
    for _covered, text in sections:
        lines = text.split("\n")
        a_path, b_path = _parse_diff_git_header(lines[0][len("diff --git "):])
        path, in_hunk, changed = b_path or a_path, False, 0
        for line in lines[1:]:
            if in_hunk:
                if line[:1] in ("+", "-"):
                    changed += 1
            elif line.startswith("@@"):
                in_hunk = True
            elif line.startswith("rename to "):
                path = _c_unquote(line[len("rename to "):])
        if path:
            loc += _count_measured_row(path, changed, surfaces, files)
    return loc, surfaces, files


def _write_slice(repo_root: Path, slice_id: str, text: str, sha_content: str) -> dict:
    """Write a slice's diff and head.sha pair from text already held (no git), with `freeze_diff`'s
    collision rule: a differing occupant is shelved for a run-keyed prep id, an error otherwise."""
    from coordinator_core.ops.review_freeze_diff import (
        _error,
        _is_run_keyed_prep_id,
        _shelve_prior_attempt,
        _validate_slice_id,
        declare_write,
        register_diff_targets,
    )

    bad = _validate_slice_id(slice_id)
    if bad is not None:
        return _error(bad)
    diffs_dir = repo_root / "state" / "review-trail" / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    diff_path, sha_path = diffs_dir / f"{slice_id}.diff", diffs_dir / f"{slice_id}.head.sha"
    if diff_path.exists() and diff_path.read_bytes().decode("utf-8") != text:
        if not _is_run_keyed_prep_id(slice_id):
            return _error(
                f"slice_id '{slice_id}' already names a frozen diff at {diff_path} with different content"
            )
        _shelve_prior_attempt(diff_path, sha_path)
    diff_path.write_text(text, encoding="utf-8", newline="\n")
    declare_write(diff_path)
    sha_path.write_text(sha_content + "\n", encoding="utf-8", newline="\n")
    declare_write(sha_path)
    register_diff_targets(repo_root, text)
    return {"diff_path": str(diff_path), "error": None}


def _untracked_in_process(repo_root: Path, paths: List[str]) -> Optional[set]:
    """The `paths` that `git ls-files --others --exclude-standard` would list, decided from the index,
    the worktree and the ignore rules with no spawn; None when any path is a directory, magic or
    symlink, or an ignore verdict is git's alone: the caller asks git."""
    from coordinator_core.git.ignore_match import IgnoreMatcher
    from coordinator_core.ops.dispatch_emit.landing_halted_view import index_identity

    try:
        if any(p != p.strip("/") or "\\" in p or any(c in p for c in "*?[:") for p in paths):
            return None
        for p in paths:
            f = repo_root / p
            if f.is_symlink() or f.is_dir():
                return None
        indexed = index_identity(repo_root, paths)
        matcher, out = None, set()
        for p in paths:
            if p in indexed or not (repo_root / p).is_file():
                continue
            matcher = matcher or IgnoreMatcher(repo_root)
            verdict = matcher.ignored(p, False)
            if verdict is None:
                return None
            if verdict is False:
                out.add(p)
        return out
    except Exception:  # noqa: BLE001 -- undecided here: git decides
        return None


def _worktree_measure(base: str, paths: List[str], repo_root: Path) -> tuple:
    """`worktree_measure`, with the untracked split decided in process when it can be: one `diff
    --numstat` spawn instead of two. Same rows, same filters."""
    untracked = _untracked_in_process(repo_root, paths)
    if untracked is None:
        return worktree_measure(base, paths, str(repo_root))
    loc, surfaces, files = 0, set(), set()
    tracked = [p for p in paths if p not in untracked]
    if tracked:
        numstat_out, _ = _run_git(["diff", "--numstat", base, "--", *tracked], str(repo_root))
        for line in numstat_out.splitlines():
            m = _CHAIN_NUMSTAT_RE.match(line)
            if m:
                a, d = m.group(1), m.group(2)
                loc += _count_measured_row(
                    _resolve_numstat_row_path(m.group(3)),
                    (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0),
                    surfaces,
                    files,
                )
    for path in sorted(untracked):
        try:
            with open(repo_root / path, "r", encoding="utf-8", errors="replace") as fh:
                added = sum(1 for _ in fh)
        except OSError:
            continue
        loc += _count_measured_row(path, added, surfaces, files)
    return loc, surfaces, files


def _dirty_in_process(repo_root: Path, paths: List[str]) -> Optional[set]:
    """The `paths` whose worktree bytes differ from HEAD, read from the index, HEAD's tree and the
    worktree with no spawn; None when they cannot be decided there (the caller asks git). A path git
    status would list for any reason (staged, modified, untracked) counts: toward dirty."""
    from coordinator_core.ops.dispatch_emit.landing_halted_view import dirty_status

    found = dirty_status(repo_root, paths)
    return None if found is None else found & set(paths)


def _partition(
    repo_root: Path,
    base: str,
    paths: List[str],
    slice_prefix: str,
    *,
    head: Optional[str] = None,
    path_bases: Optional[Dict[str, str]] = None,
    head_fallback: Optional[List[str]] = None,
    whole_diff: Optional[Callable[[dict], dict]] = None,
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

    `whole_diff` (optional) is called once, with the result so far, when a slice is first needed;
    it returns the caller's frozen whole diff (`{diff_path, error}`). Each slice is then cut from
    that text in-process (a file's section goes to the slice naming it) instead of one git freeze
    per slice, so spawns do not grow with the slice count. The slice pairs are written uncommitted.

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
    held: dict = {}

    def load_whole() -> Optional[dict]:
        """The caller's frozen whole diff as sections plus its head sha, loaded once; the refusal when
        the freeze failed."""
        if "sections" not in held:
            whole = whole_diff(out)
            if whole.get("error"):
                return whole
            whole_path = Path(whole["diff_path"])
            held["sections"] = _diff_sections(whole_path.read_bytes().decode("utf-8"))
            try:
                held["sha"] = whole_path.with_name(whole_path.name[: -len(".diff")] + ".head.sha").read_text(
                    encoding="utf-8").strip()
            except OSError:
                held["sha"] = head or ""
        return None

    loc, surfaces, files = 0, set(), set()
    if not head and whole_diff is not None and load_whole() is None:
        # Worktree basis: the whole diff is already frozen, so its sections are the measure; no numstat spawn per base.
        loc, surfaces, files = _measure_sections(held["sections"])
        by_base = {}
    for b, group_declared in by_base.items():
        if head:
            try:
                g_loc, g_surfaces, g_files = range_measure(b, head, group_declared, str(repo_root))
            except RangeMeasureError as exc:
                out["error"] = str(exc)
                return out
        else:
            g_loc, g_surfaces, g_files = _worktree_measure(b, group_declared, repo_root)
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
        # Only when an own path has a range delta: such a path may also carry uncommitted bytes,
        # which a base..head freeze would drop; those freeze base..worktree. Decided in process
        # when the index and HEAD's tree allow, else one git spawn.
        decided = _dirty_in_process(repo_root, own_ranged)
        if decided is not None:
            dirty = decided
        else:
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

    def freeze(rev: str, slice_id: str, group: List[str], *, worktree: bool, ranged: bool = False) -> dict:
        if whole_diff is None:
            return freeze_diff(repo_root, rev, slice_id, group, worktree=worktree)
        refused = load_whole()
        if refused is not None:
            return {"error": refused["error"], "whole_error": True}
        wanted = set(group)
        text = "".join(s for covered, s in held["sections"] if covered & wanted)
        sha = held["sha"] if ranged else f"{rev} worktree"
        return _write_slice(repo_root, slice_id, text, sha)

    def refusal(frozen: dict, slice_id: str) -> str:
        return f"freeze of {slice_prefix if frozen.get('whole_error') else slice_id} failed: {frozen['error']}"

    if fb_files:
        for group in group_paths(sorted(fb_files)):
            n += 1
            slice_id = f"{slice_prefix}-{n}"
            frozen = freeze(head_sha, slice_id, group, worktree=True)
            if frozen.get("error"):
                out["error"] = refusal(frozen, slice_id)
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
                if whole_diff is None:
                    frozen = freeze_diff(repo_root, f"{b}..{head}", slice_id, sub_group)
                else:
                    frozen = freeze(b, slice_id, sub_group, worktree=False, ranged=True)
            else:
                frozen = freeze(b, slice_id, sub_group, worktree=True)
            if frozen.get("error"):
                out["error"] = refusal(frozen, slice_id)
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
