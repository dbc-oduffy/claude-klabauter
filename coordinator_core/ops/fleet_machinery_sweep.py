"""
coordinator_core.ops.fleet_machinery_sweep -- C14 of
pln-state-keeps-the-work-not-the-machinery-*: fan the C6 (untrack) + C7
(relocate) machinery-bucket move out across the sibling repos, unchanged,
excluding publish repos (PM ruling, 2026-09-02, verbatim: "we don't do
publish repos, and they don't have this stuff anyway... siblings do but the
publish repos do not. we move it all.").

WHAT THIS OP DOES, PER REPO, IN ORDER (aborting THAT repo, never the whole
sweep, on any leg failing):
    1. Run C4's `extract_cited_sidecars.run_extraction` against the repo,
       writing its two audit records there -- same reason C6 gates on C4:
       untracking removes cited sidecars from git for every OTHER clone of
       that repo, and a citation that leaves the repository is silent from
       the reader's side.
    2. Write the two-stanza `.gitignore` block (C6's shape): stanza 1 is
       regenerated-by-writer (`state/subagent-share/`), stanza 2 is
       single-copy-after-this-sweep (`state/review-trail/`,
       `state/ceremony/`, `state/dispatch-briefs/`, `state/plan-sidecars/`).
       Both stanzas land together, covering BOTH the old `state/` paths and
       `.coordinator-local/` -- sibling sessions keep writing the old paths
       until the new engine is published, so a single-stanza block would
       re-dirty immediately.
    3. `git rm --cached -r` the machinery paths that are actually tracked.
       Index only -- the working tree is untouched, and a live session's
       sidecar is never removed from under it.
    4. Relocate the buckets on disk via `publish.py::_rename_with_retry`,
       `state/subagent-share/` last (C7's ordering and primitive -- a live
       writer's directory gets the smallest exposure window).
    5. Append the repo's before/after counts to
       `state/audits/2026-09-02-fleet-machinery-sweep.md` (in CLAUDE-KLABAUTER's own
       tree -- the sweep's own audit trail lives where the sweep ran from,
       not scattered one copy per repo).

A MANDATORY DRY RUN GATES ALL OF THE ABOVE. `--dry-run` prints the FULL
selected path set for one repo -- every path, not a count and not a sample
-- and this module's CLI refuses to mutate anything until a human has
invoked `--dry-run` and read its output. This is not a courtesy: the C14
stub cites this plan's own 21-site census, which reported a complete answer
to the question it asked and missed a disjoint 7-site set it never asked
about at all -- across nine repos and ~55,000 files a selector nobody has
seen the output of has exactly that failure available to it.

SELECTOR DISCIPLINE. Selection is a full repo-relative path PREFIX anchored
at `state/`, never a name token: `state/subagent-share/` is a bucket,
`coordinator_core/session/subagent_share.py` is engine source one
underscore away from it and must never match. See `_select_machinery_paths`
and its test's negative case.

SPAWN BUDGET. One `git ls-files` process and zero filesystem walks per
repo -- git already returns every tracked-or-not-ignored path in one call,
and this op filters that single list rather than walking the tree a second
time. No per-file, per-bucket, or per-path subprocess loop anywhere in this
module -- nine repos, ~55,000 files, and the repo's own 500ms brightline
(`docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md`)
governs every process this op spawns.

RENAME PRIMITIVE. This module writes no retry loop of its own. It imports
`_rename_with_retry` from `coordinator/bin/publish.py` (a non-package
script, loaded via `importlib.util` -- see `_load_rename_with_retry`) and
calls it exactly as C7 does. Per-rename deadline is that function's own
0.5s budget, read off the module at call time, never copied into a
constant here (a number written into this file would be stale by
construction the moment publish.py's own tuning changes). A bucket that
still refuses after the deadline is DEFERRED -- recorded with repo, bucket
and error -- and the sweep continues to the next bucket/repo rather than
waiting on it; deferred buckets are retried once, at the end of the run
for that repo, never blocked on mid-sweep.

Negative-spec (RAG-bait):
    - This op NEVER commits in a sibling's tree. Every leg that mutates a
      sibling repo (`git rm --cached`, the `.gitignore` write, the on-disk
      relocation) leaves that repo staged-or-dirty for ITS OWN EM. Writing
      a commit into a tree this op does not own is the one thing the fleet
      authorization does not license, and `sweep_repo` never calls
      `git commit` under any code path.
    - This op does NOT discover repos by name-token guessing. Sibling
      repos are the git-toplevel directories sitting alongside this repo's
      own toplevel on disk (`discover_sibling_repos`); publish repos are
      excluded by an explicit denylist (`_PUBLISH_REPO_NAMES`) checked
      against the directory's own basename, never a substring/token match.
    - This op does NOT write a second `PermissionError`-narrowing retry.
      If `_load_rename_with_retry` cannot import the primitive from
      `publish.py`, the caller gets a clear `RuntimeError` -- it does NOT
      silently fall back to a bare `os.rename` (which would strand a
      briefly-busy bucket) or a blanket `except OSError` retry (which
      would retry permanent errors like `FileExistsError` pointlessly).
    - This op does NOT widen `--mutate` to run unattended across every
      discovered repo by default: the CLI requires either `--dry-run` (the
      read-only path) or an explicit `--repo <path> --mutate` naming ONE
      repo. There is no `--all --mutate` flag. Fleet-wide execution is an
      operator running the CLI once per repo after reading that repo's own
      dry-run output, not a single unattended fan-out call.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops import extract_cited_sidecars
from coordinator_core.win_portability import leaf_spawn_creationflags

_LOG_PREFIX = "fleet-machinery-sweep"

#: Publish repos are excluded by PM ruling. Checked against a candidate
#: sibling directory's own basename -- never a substring/token match, and
#: never inferred from tracked-`state/` emptiness (that's a corroborating
#: fact recorded in the audit, not the selector).
_PUBLISH_REPO_NAMES = frozenset({"claude-klabauter"})

#: Stanza 1 -- regenerated-by-writer (C6's split). Safe to untrack because
#: each is rebuilt from scratch by its own producer on the next run.
_STANZA1_BUCKETS: Tuple[str, ...] = (
    "state/subagent-share/",
)

#: Stanza 2 -- single-copy-after-this-sweep (C6's split, opposite
#: rationale). NOT regenerated by anything; once relocated and ignored,
#: the working-tree copy on the machine that ran the sweep is the only
#: copy that exists going forward.
_STANZA2_BUCKETS: Tuple[str, ...] = (
    "state/review-trail/",
    "state/ceremony/",
    "state/dispatch-briefs/",
    "state/plan-sidecars/",
)

#: subagent-share/ moves LAST -- C7's ordering, carried verbatim: it is the
#: one bucket with live writers at any given moment, so it gets the
#: smallest exposure window and the most settled script.
_ALL_BUCKETS: Tuple[str, ...] = _STANZA2_BUCKETS + _STANZA1_BUCKETS

_AUDIT_PATH = "state/audits/2026-09-02-fleet-machinery-sweep.md"

_IGNORE_BLOCK = """
# 2026-09-02 fleet-machinery-sweep (C14) -- fanned out from claude-klabauter's own C6.
# Sibling sessions keep writing the OLD state/ machinery paths until the new
# engine is published and picked up, so BOTH the old paths and
# .coordinator-local/ are ignored together -- landing only one stanza would
# re-dirty the tree immediately as live sessions write the other.
.coordinator-local/

# Stanza 1 -- regenerated-by-writer. Untracking DELETES this on every other
# machine's next pull, which is safe only because it is regenerated by its
# own writer (coordinator_core.session.machinery_paths).
state/subagent-share/

# Stanza 2 -- single-copy-after-this-sweep, the OPPOSITE rationale: none of
# these are regenerated. Once this sweep relocates their content under
# .coordinator-local/ and this ignore lands, the working-tree copy on the
# machine that ran the sweep is the only copy that exists; every other
# machine's next pull deletes its copy permanently. Accepted, not hidden,
# consequence of the PM's relocation ruling -- the durable copy going
# forward is the one machine's .coordinator-local/ tree, not git.
state/review-trail/
state/ceremony/
state/dispatch-briefs/
state/plan-sidecars/
""".strip("\n") + "\n"

_IGNORE_MARKER = "# 2026-09-02 fleet-machinery-sweep (C14)"


def _resolve_root(root: Optional[str]) -> str:
    if root:
        return root
    found = show_toplevel(cwd=os.getcwd())
    return found or os.getcwd()


def _load_rename_with_retry():
    """Import `_rename_with_retry` from `coordinator/bin/publish.py` --
    a non-package script, so this loads it by file path via
    `importlib.util` rather than `import coordinator.bin.publish`. Never
    rediscovers the retry shape (see module docstring's negative-spec)."""
    claude_klabauter_root = _resolve_root(None)
    publish_path = os.path.join(claude_klabauter_root, "coordinator", "bin", "publish.py")
    if not os.path.isfile(publish_path):
        raise RuntimeError(
            f"{_LOG_PREFIX}: cannot find coordinator/bin/publish.py under {claude_klabauter_root} "
            "to import _rename_with_retry -- refusing to write a second retry primitive."
        )
    spec = importlib.util.spec_from_file_location(
        "_fleet_machinery_sweep_publish", publish_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{_LOG_PREFIX}: could not build an import spec for {publish_path}")
    module = importlib.util.module_from_spec(spec)
    # publish.py uses @dataclass, whose machinery resolves `cls.__module__`
    # via `sys.modules` -- register the module under its synthetic name
    # before executing it, or dataclass field resolution raises on a
    # `sys.modules.get(...)` miss that has nothing to do with this sweep.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    fn = getattr(module, "_rename_with_retry", None)
    if fn is None:
        raise RuntimeError(
            f"{_LOG_PREFIX}: publish.py no longer defines _rename_with_retry -- "
            "the C7 primitive this sweep depends on has moved or been renamed."
        )
    return fn


def discover_sibling_repos(self_root: str) -> List[str]:
    """Git-toplevel directories sitting alongside `self_root` on disk,
    excluding `self_root` itself and any publish repo (`_is_publish_repo`).
    One `os.listdir` -- no subprocess, no recursive walk."""
    parent = os.path.dirname(os.path.normpath(self_root))
    out: List[str] = []
    try:
        entries = sorted(os.listdir(parent))
    except OSError:
        return out
    self_norm = os.path.normcase(os.path.normpath(self_root))
    for name in entries:
        candidate = os.path.join(parent, name)
        if os.path.normcase(os.path.normpath(candidate)) == self_norm:
            continue
        if not os.path.isdir(os.path.join(candidate, ".git")):
            continue
        if _is_publish_repo(candidate):
            continue
        out.append(candidate)
    return out


def _is_publish_repo(repo_path: str) -> bool:
    """Excluded by basename denylist ONLY -- never a substring/token
    match, and never inferred from tracked-`state/` emptiness (see module
    docstring's negative-spec)."""
    return os.path.basename(os.path.normpath(repo_path)) in _PUBLISH_REPO_NAMES


def _git_ls_files(root: str) -> Optional[List[str]]:
    """ONE `git ls-files` spawn for the whole repo -- tracked + untracked-
    but-not-ignored, so the same call sees paths C6 will `git rm --cached`
    and paths a fresh `.gitignore` stanza would otherwise miss. `None` if
    `root` is not a git worktree / git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=60,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    return sorted(p.replace(os.sep, "/") for p in raw.split("\0") if p)


def _git_ls_files_cached_only(root: str) -> Optional[List[str]]:
    """ONE `git ls-files --cached` spawn -- the tracked-only view `git rm
    --cached` needs (untracked paths were never in the index to remove)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=root, capture_output=True, timeout=60,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    return sorted(p.replace(os.sep, "/") for p in raw.split("\0") if p)


def select_machinery_paths(
    all_paths: List[str], buckets: Tuple[str, ...] = _ALL_BUCKETS
) -> List[str]:
    """The selector. Anchored at a full repo-relative path PREFIX --
    `state/subagent-share/` -- never a name token. A path merely
    CONTAINING a bucket-like token (e.g.
    `coordinator_core/session/subagent_share.py`) is never selected; only
    a path that literally starts with one of `buckets` is."""
    return sorted(
        p for p in all_paths
        if any(p == b.rstrip("/") or p.startswith(b) for b in buckets)
    )


def dry_run_select(root: str) -> List[str]:
    """The full selected set for one repo -- every path that WOULD be
    untracked-and-relocated, mandatory reading before any `--mutate` run
    (module docstring). One `git ls-files` spawn."""
    all_paths = _git_ls_files(root)
    if all_paths is None:
        return []
    return select_machinery_paths(all_paths)


def _write_ignore_block(root: str) -> bool:
    """Appends the two-stanza block to `.gitignore` unless it is already
    present (idempotent -- a re-run of the sweep against an already-swept
    repo does not duplicate the block). Returns True if written."""
    ignore_path = os.path.join(root, ".gitignore")
    existing = ""
    if os.path.isfile(ignore_path):
        with open(ignore_path, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    if _IGNORE_MARKER in existing:
        return False
    sep = "" if (not existing or existing.endswith("\n\n")) else (
        "\n" if existing.endswith("\n") else "\n\n"
    )
    with open(ignore_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(sep + _IGNORE_BLOCK)
    return True


def _git_rm_cached(root: str, paths: List[str]) -> Tuple[bool, str]:
    """ONE `git rm --cached -r` spawn per repo for the whole selected set
    -- never per-bucket, never per-file. Index only; `--cached` is the
    whole point (C6).

    PATHS GO IN ON STDIN, NEVER IN ARGV. The first fleet run put the whole
    selected set on the command line and every large repo aborted with
    `[WinError 206] The filename or extension is too long` -- Windows caps a
    command line at ~32 KB and the smallest failing set here was ~8,800 paths.
    `--pathspec-from-file=-` with NUL separation keeps this at one spawn while
    removing the limit entirely; chunking argv would have traded the limit for
    a per-chunk spawn multiplier against a 500ms brightline. NUL separation is
    not optional: these are Windows paths and a newline-separated list would
    also cap path length and mangle anything with a quote in it."""
    if not paths:
        return True, ""
    try:
        proc = subprocess.run(
            [
                "git", "rm", "--cached", "-r", "-q",
                "--pathspec-from-file=-", "--pathspec-file-nul",
            ],
            input="\0".join(paths).encode("utf-8"),
            cwd=root, capture_output=True, timeout=120,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        # `input=` is bytes, so this call cannot pass `text=True` and both
        # streams come back as bytes. Decode here rather than returning them
        # raw: this string IS the abort reason a human reads to decide whether
        # a repo was skipped for a transient reason or a real one, and
        # `b'...'` in that line is how a legible failure becomes a mystery.
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        return False, err or out
    return True, ""


def _relocate_buckets(
    root: str, rename_fn, buckets: Tuple[str, ...] = _ALL_BUCKETS
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Relocates each bucket that exists on disk under
    `<root>/.coordinator-local/<bucket-leaf>` via `rename_fn`
    (`_rename_with_retry`). Returns `(moved, deferred)` where `deferred`
    entries carry `{bucket, error}` for a bucket that refused within the
    primitive's own deadline -- recorded and skipped, never waited on
    longer than that."""
    dest_root = os.path.join(root, ".coordinator-local")
    moved: List[str] = []
    deferred: List[Dict[str, str]] = []
    for bucket in buckets:
        leaf = bucket.rstrip("/").split("/")[-1]
        src = os.path.join(root, *bucket.rstrip("/").split("/"))
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest_root, leaf)
        os.makedirs(dest_root, exist_ok=True)
        if os.path.exists(dst):
            deferred.append({"bucket": bucket, "error": f"destination already exists: {dst}"})
            continue
        try:
            rename_fn(Path(src), Path(dst))
            moved.append(bucket)
        except OSError as exc:
            deferred.append({"bucket": bucket, "error": str(exc)})
    return moved, deferred


def sweep_repo(root: str, mutate: bool = False) -> Dict[str, object]:
    """Runs the five-leg sweep against one repo. `mutate=False` (default)
    performs leg 1 (extraction) plus a dry-run selection ONLY -- no
    `.gitignore` write, no `git rm --cached`, no relocation. `mutate=True`
    is the full five-leg sweep; the caller (CLI) is responsible for
    enforcing that a dry-run has been read first (module docstring).

    Never commits. Never raises out of a per-repo failure -- a failed leg
    is recorded in the returned dict's `error` key and the repo is
    reported as aborted; the caller loops over repos and this function's
    job is to make ONE repo's failure legible, not fatal to the sweep."""
    result: Dict[str, object] = {
        "repo": root,
        "before_count": None,
        "after_count": None,
        "selected": [],
        "moved": [],
        "deferred": [],
        "ignore_written": False,
        "error": None,
    }
    try:
        before = _git_ls_files_cached_only(root) or []
        result["before_count"] = len(before)

        selected = dry_run_select(root)
        result["selected"] = selected

        # Leg 1 -- C4's extraction, always run (dry-run or mutate) so the
        # audit exists before anything is untracked.
        extract_cited_sidecars.main(["--root", root])

        if not mutate:
            return result

        # Leg 2 -- the two-stanza ignore block.
        result["ignore_written"] = _write_ignore_block(root)

        # Leg 3 -- git rm --cached -r over the tracked subset of `selected`.
        tracked = set(_git_ls_files_cached_only(root) or [])
        cached_selected = [p for p in selected if p in tracked]
        ok, err = _git_rm_cached(root, cached_selected)
        if not ok:
            result["error"] = f"git rm --cached failed: {err}"
            return result

        # Leg 4 -- relocate, subagent-share/ last.
        rename_fn = _load_rename_with_retry()
        moved, deferred = _relocate_buckets(root, rename_fn)
        result["moved"] = moved
        result["deferred"] = deferred

        after = _git_ls_files_cached_only(root) or []
        result["after_count"] = len(after)
    except Exception as exc:  # noqa: BLE001 -- abort THIS repo, never the sweep
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _render_audit_entry(result: Dict[str, object]) -> str:
    lines = [f"## {result['repo']}", ""]
    lines.append(f"- tracked `state/` before: {result['before_count']}")
    lines.append(f"- tracked `state/` after: {result['after_count']}")
    lines.append(f"- machinery paths selected: {len(result['selected'] or [])}")
    lines.append(f"- buckets moved: {', '.join(result['moved']) or '(none)'}")
    if result["deferred"]:
        lines.append("- deferred buckets:")
        for entry in result["deferred"]:
            lines.append(f"  - `{entry['bucket']}`: {entry['error']}")
    if result["error"]:
        lines.append(f"- **ABORTED**: {result['error']}")
    lines.append("")
    return "\n".join(lines)


def append_audit(claude_klabauter_root: str, result: Dict[str, object]) -> str:
    """Appends one repo's before/after counts to the sweep's audit record
    (leg 5), which lives in CLAUDE-KLABAUTER's own tree regardless of which repo was
    swept."""
    audit_path = os.path.join(claude_klabauter_root, _AUDIT_PATH.replace("/", os.sep))
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    header = (
        "# Fleet machinery sweep (C14)\n\n"
        "Per-repo before/after tracked-`state/`-file counts for the C6+C7 "
        "relocation fanned out across sibling repos. Generated by "
        "`coordinator_core.ops.fleet_machinery_sweep`.\n\n"
    )
    entry = _render_audit_entry(result)
    if not os.path.isfile(audit_path):
        with open(audit_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + entry)
    else:
        with open(audit_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(entry)
    return audit_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="Sibling repo path (defaults to fleet-wide discovery for --dry-run).")
    parser.add_argument("--dry-run", action="store_true", help="Print the full selected path set for one repo; mutates nothing.")
    parser.add_argument("--mutate", action="store_true", help="Run the full five-leg sweep against ONE repo (--repo required).")
    parser.add_argument("--out", default=None, help="Write --dry-run output to this file instead of stdout.")
    args = parser.parse_args(argv)

    claude_klabauter_root = _resolve_root(None)

    if args.mutate:
        if not args.repo:
            print(f"{_LOG_PREFIX}: --mutate requires --repo <path> -- refusing an unattended fleet-wide mutate.", file=sys.stderr)
            return 2
        if _is_publish_repo(args.repo):
            print(f"{_LOG_PREFIX}: {args.repo} is a publish repo, excluded by PM ruling.", file=sys.stderr)
            return 2
        result = sweep_repo(args.repo, mutate=True)
        audit_path = append_audit(claude_klabauter_root, result)
        print(f"{_LOG_PREFIX}: wrote {audit_path}")
        if result["error"]:
            print(f"{_LOG_PREFIX}: {args.repo} ABORTED: {result['error']}", file=sys.stderr)
            return 1
        return 0

    # --dry-run (default path if neither --mutate nor an explicit repo list)
    repos = [args.repo] if args.repo else discover_sibling_repos(claude_klabauter_root)
    out_lines: List[str] = []
    for repo in repos:
        selected = dry_run_select(repo)
        out_lines.append(f"# {repo} ({len(selected)} paths)")
        out_lines.extend(selected)
        out_lines.append("")
    text = "\n".join(out_lines)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"{_LOG_PREFIX}: wrote dry-run output to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
