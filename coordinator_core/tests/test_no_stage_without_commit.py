"""Structural guard: no engine module stages into git and never commits (the "giver" half of
the shared-index footgun).

A module that builds a git `add`/`mv`/`rm` argv and carries no `commit` argv and no
`commit_paths` call leaves staged state in the shared index for whichever peer bare-commits
next. This gate walks `coordinator_core/` and `coordinator/bin/` with `ast` and flags such
modules.

BLIND SPOT -- READ BEFORE TRUSTING A GREEN. This is a literal-argv, module-granularity read:

  - Runtime-built argv is invisible: a verb held in a variable (`["git", verb, ...]`), an
    argv assembled by concatenation or a helper, or an f-string flag is not seen. A staging
    site built that way is missed; a commit built that way is not recognised as a commit.
  - In-process staging is invisible: `git.commit.stage_paths_in_process` (dulwich-style
    index writes with no git argv) is not detected. A future caller would pass this gate.
  - Pairing is coarse: "the module has a commit somewhere" does not show the commit covers
    the staged paths or runs in the same op.
  - Only `coordinator_core/` and `coordinator/bin/` Python is walked; test trees are skipped.

An exemption is by declaration only: `ALLOWLIST` (path -> reason) for owned repos or
no-commit-by-contract, `PENDING_FIX` (path -> fix shape) for shared-tree sites awaiting a
scoped commit. Each entry must still match a real staging site, so a stale entry fails.
"""

from __future__ import annotations

import ast
import pathlib

from coordinator_core.spawn_policy import is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()
_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator/bin")
_STAGING_VERBS = ("add", "mv", "rm")

#: repo-relative path -> why the staging is safe.
ALLOWLIST: dict[str, str] = {
    "coordinator_core/ops/renormalize_index.py": (
        "re-stage of phantom-dirty paths; content renormalizes to the blob already in HEAD, "
        "so nothing committable is produced; never commits by contract"
    ),
}

#: repo-relative path -> fix shape. Sites are shared-tree stage-with-no-commit, awaiting a
#: scoped commit (`--pathspec-from-file`). Remove an entry when its site is fixed or deleted.
PENDING_FIX: dict[str, str] = {
    "coordinator_core/ops/migrate_cross_repo_layout.py": (
        "retire the one-shot migration (P01-C5)"
    ),
}


def _strs(node: ast.AST) -> list[str]:
    return [
        e.value
        for e in getattr(node, "elts", [])
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def analyse_source(source: str) -> tuple[list[int], bool]:
    """Return (staging-site line numbers, module-has-commit) for one module's source."""
    tree = ast.parse(source)
    stages: list[int] = []
    has_commit = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            strs = _strs(node)
            if "commit" in strs:
                has_commit = True
            elif "git" in strs and any(v in strs for v in _STAGING_VERBS):
                stages.append(node.lineno)
        elif isinstance(node, ast.Call) and _callee_name(node) == "commit_paths":
            has_commit = True
    return stages, has_commit


def _giver_modules() -> dict[str, list[int]]:
    """Every in-scope module that stages and never commits -> its staging line numbers."""
    out: dict[str, list[int]] = {}
    for root_name in _SCOPE_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.exists():
            continue
        discovered, _ = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel, path in discovered:
            if path.resolve() == _THIS_FILE:
                continue
            rel_repo = f"{root_name}/{rel}"
            if is_test_tree_site(rel_repo):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if '"add"' not in source and "'add'" not in source and '"mv"' not in source \
                    and "'mv'" not in source and '"rm"' not in source and "'rm'" not in source:
                continue
            try:
                stages, has_commit = analyse_source(source)
            except SyntaxError:
                continue
            if stages and not has_commit:
                out[rel_repo] = stages
    return out


def test_no_unlisted_stage_without_commit_module() -> None:
    givers = _giver_modules()
    declared = set(ALLOWLIST) | set(PENDING_FIX)
    new = sorted(set(givers) - declared)
    assert not new, (
        "module stages into git (add/mv/rm) with no commit argv and no commit_paths call: "
        f"{[(p, givers[p]) for p in new]}. Commit the same paths in the same op "
        "(`--pathspec-from-file`), or declare in ALLOWLIST with a reason."
    )


def test_pending_fix_entries_still_match_a_giver_site() -> None:
    givers = _giver_modules()
    stale = sorted(p for p in PENDING_FIX if p not in givers)
    assert not stale, f"PENDING_FIX entries no longer flagged (remove them): {stale}"


def test_allowlist_entries_still_match_a_staging_site() -> None:
    stale = []
    for rel in ALLOWLIST:
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(rel)
            continue
        stages, _ = analyse_source(path.read_text(encoding="utf-8", errors="replace"))
        if not stages:
            stale.append(rel)
    assert not stale, f"ALLOWLIST entries with no staging site (remove them): {stale}"


def test_detector_flags_stage_without_commit() -> None:
    stages, has_commit = analyse_source(
        'import subprocess\nsubprocess.run(["git", "-C", d, "mv", a, b])\n'
    )
    assert stages == [2] and not has_commit


def test_detector_passes_stage_with_commit_argv() -> None:
    src = (
        'run(["git", "add", "--", p])\n'
        'run(["git", "commit", "-m", "x", "--", p])\n'
    )
    stages, has_commit = analyse_source(src)
    assert stages and has_commit


def test_detector_passes_stage_with_commit_paths_call() -> None:
    src = 'run(["git", "rm", p])\ncommit_paths(root, [p], "m")\n'
    stages, has_commit = analyse_source(src)
    assert stages and has_commit


def test_detector_blind_to_runtime_built_verb() -> None:
    stages, _ = analyse_source('verb = "add"\nrun(["git", verb, p])\n')
    assert stages == []
