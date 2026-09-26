"""
coordinator_core.diff_scoped_tests -- append changed test files onto a
resolved fast-test command, instead of running the whole configured tier.

Purpose: the routine ceremony gates (``workday-complete-step1-validate.py``
Gate 2, ``validate-fast-and-packageability.py``'s ``fast`` subcommand) run
the FULL configured ``fast_test_cmd`` on every invocation, even when only a
handful of test files changed. This module is the ONE place that computes
"which test files changed" and appends them onto an already-resolved
command string -- both call sites import it rather than each re-deriving
the git-diff-and-filter logic. It does NOT decide what to run when nothing
changed (that stays the caller's existing full-tier command, unmodified),
and it does NOT map changed SOURCE files to covering tests -- see the
module's own negative-spec below for the anti-scope this deliberately
leaves alone.

Spec backlink: PM-ratified scope cut, sizing record
state/sizings/2026-07-30-diff-scoped-routine-ceremony-gates.yaml -- "the
trivial bit" of a larger diff-scoped-ceremony-gates plan, landed in two
stages per that record's own two-stage-landing rationale. The FIRST stage
(this module's original scope) appended only DIRECTLY-changed test files.
The SECOND stage -- docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
(C3) -- is this module's CURRENT scope: it also maps changed SOURCE files to
their covering tests via ``coordinator_core.source_test_map`` and unions the
two sets, and it is the consolidation-completed seam (the resolver
duplication between ``coordinator_core/resolve_validation_cmd.py`` and
``coordinator/bin/coordinator-resolve-validation-cmd.py`` was resolved by
that same plan's C1). Both lines below that the module's ORIGINAL negative-
spec named as anti-scope are deliberately REVERSED here, as the planned
second stage, not as drift.

Placement decision: this module lives in ``coordinator_core/`` (not
duplicated into ``coordinator/bin/``) because both call sites --
``coordinator/bin/workday-complete-step1-validate.py`` and
``coordinator/bin/validate-fast-and-packageability.py`` -- already
``sys.path``-bootstrap the repo root and import other ``coordinator_core``
modules in-process (``coordinator_core.session.tier_u_gate``,
``coordinator_core.win_portability``). Adding a THIRD duplicated copy of
this logic into ``coordinator/bin`` would recreate exactly the
resolver-duplication problem this dispatch's brief calls out as
anti-scope to fix, in a brand new module instead of an existing one --
there is no reason to mint a second copy when the existing in-process
import path already reaches both callers.

Negative-spec (anti-scope, do not build any of this here):
  - Does NOT add a hook or a ``coordinator.local.md`` config key for the
    source-to-test mapping leg -- ``coordinator_core.source_test_map`` is a
    pure convention reader, not a configurable one.
  - Does NOT rebuild or reorder the resolved command string. It only
    APPENDS shell-quoted paths after the caller's already-resolved
    command, so the load-bearing ``-m '...'`` marker selector this repo's
    ``fast_test_cmd`` carries is never touched, dropped, or rebuilt.
  - Does NOT itself decide to run the full tier -- ``fully_mapped=False``
    from ``compute_diff_scoped_paths`` is a SIGNAL; the caller is the one
    that reacts to it by keeping its already-resolved unscoped command
    (see each gate CLI's own call site).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


_TEST_FILE_RE = re.compile(r"^test_.*\.py$")


def _run_git(args: Sequence[str], repo_root: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _read_testpaths(repo_root: str) -> list[str]:
    pyproject = Path(repo_root) / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        import tomllib

        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
        raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
    except Exception:
        return []
    if isinstance(raw, str):
        roots = raw.split()
    elif isinstance(raw, list):
        roots = [str(x) for x in raw]
    else:
        return []
    return [r.strip("/") for r in roots if r.strip("/")]


def _under_testpaths(posix_path: str, testpaths: Sequence[str]) -> bool:
    for root in testpaths:
        if posix_path == root or posix_path.startswith(root + "/"):
            return True
    return False


def find_changed_test_files(repo_root: Optional[str] = None) -> list[str]:
    """Changed test files in ``repo_root``'s working tree, relative to
    ``repo_root``, POSIX-separated.

    "Changed" = working-tree + staged changes vs ``HEAD`` (``git diff
    --name-only HEAD``, which already covers both staged and unstaged
    modifications to tracked files in one call) UNION untracked files
    (``git ls-files --others --exclude-standard``). This is the
    pre-commit-shaped definition: it is exactly the set a commit made
    right now would touch, so a routine ceremony gate scoped to it is
    scoped to what this session is actually about to land.

    Filters applied, in order:
      1. Must still exist on disk -- a path git reports for a DELETION
         (tracked, now removed) must never be handed to pytest as a
         positional argument; pytest would error on a nonexistent path
         rather than simply skip it.
      2. Basename must match ``test_*.py`` (this repo's test-file
         convention).
      3. Must be the configured ``testpaths`` root itself, or a descendant
         of one -- a ``test_*.py`` file that pytest would never collect
         anyway (outside every configured root) is not "a changed test
         file" for this gate's purposes.

    Any git-invocation failure (not a repo, git absent) degrades to an
    empty list -- the caller's existing fail-safe: no changed test files
    found means "run the full configured tier," never "run nothing."
    """
    root = repo_root if repo_root is not None else "."
    root_path = Path(root)

    changed = set(_run_git(["diff", "--name-only", "HEAD"], root))
    changed.update(_run_git(["ls-files", "--others", "--exclude-standard"], root))

    testpaths = _read_testpaths(root)

    result: list[str] = []
    for raw in changed:
        posix_path = raw.replace("\\", "/").strip()
        if not posix_path:
            continue
        if not (root_path / posix_path).is_file():
            continue
        if not _TEST_FILE_RE.match(Path(posix_path).name):
            continue
        if not _under_testpaths(posix_path, testpaths):
            continue
        result.append(posix_path)

    return sorted(result)


def find_changed_source_files(repo_root: Optional[str] = None) -> list:
    """Changed NON-test ``.py`` source files in ``repo_root``'s working tree
    under a configured testpaths root, relative to ``repo_root``,
    POSIX-separated. Sibling to `find_changed_test_files`, sharing its exact
    "changed" definition (working-tree + staged vs HEAD, union untracked)
    and its existence/testpaths-membership filters -- the only difference is
    the basename filter is INVERTED (excludes ``test_*.py`` instead of
    requiring it) and a ``.py`` extension is required (a changed non-Python
    file under a testpaths root has no test-map entry to look up).
    """
    root = repo_root if repo_root is not None else "."
    root_path = Path(root)

    changed = set(_run_git(["diff", "--name-only", "HEAD"], root))
    changed.update(_run_git(["ls-files", "--others", "--exclude-standard"], root))

    testpaths = _read_testpaths(root)

    result: list = []
    for raw in changed:
        posix_path = raw.replace("\\", "/").strip()
        if not posix_path:
            continue
        if not posix_path.endswith(".py"):
            continue
        if not (root_path / posix_path).is_file():
            continue
        if _TEST_FILE_RE.match(Path(posix_path).name):
            continue
        if not _under_testpaths(posix_path, testpaths):
            continue
        result.append(posix_path)

    return sorted(result)


def compute_diff_scoped_paths(repo_root: Optional[str] = None):
    changed_tests = find_changed_test_files(repo_root)
    changed_sources = find_changed_source_files(repo_root)

    from coordinator_core.source_test_map import map_changed_sources

    mapped, fully_mapped = map_changed_sources(changed_sources, repo_root)

    paths = sorted(set(changed_tests) | set(mapped))
    return (paths, fully_mapped)


def append_test_paths(cmd: str, paths: Sequence[str]) -> str:
    if not paths:
        return cmd
    import shlex

    quoted = " ".join(shlex.quote(p) for p in paths)
    return f"{cmd} {quoted}"


PYTEST_NO_TESTS_COLLECTED = 5


def diag(msg: str) -> None:
    print(f"[diff-scoped-tests] {msg}", file=sys.stderr)
