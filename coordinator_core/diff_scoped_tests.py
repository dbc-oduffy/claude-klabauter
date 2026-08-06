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
trivial bit" of a larger diff-scoped-ceremony-gates plan. The larger
version (source-to-test mapping via a ``<mod>/tests/`` convention, and
consolidating the resolver duplication between
``coordinator_core/resolve_validation_cmd.py`` and
``coordinator/bin/coordinator-resolve-validation-cmd.py``) is a separate
spinoff and is explicitly anti-scope here.

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

Negative-spec (anti-scope, PM-ratified, do not build any of this here):
  - Does NOT map changed SOURCE files to their covering tests via a
    ``<mod>/tests/`` convention or any other heuristic. Only test files
    that changed directly are ever appended.
  - Does NOT consolidate ``coordinator_core/resolve_validation_cmd.py``
    and ``coordinator/bin/coordinator-resolve-validation-cmd.py`` -- that
    duplication is untouched by this module.
  - Does NOT add a hook or a ``coordinator.local.md`` config key for the
    source-to-test mapping leg -- there is nothing "ready for" it here.
  - Does NOT rebuild or reorder the resolved command string. It only
    APPENDS shell-quoted paths after the caller's already-resolved
    command, so the load-bearing ``-m '...'`` marker selector this repo's
    ``fast_test_cmd`` carries is never touched, dropped, or rebuilt.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

#: Matches this repo's test-file convention (``test_*.py``), applied to the
#: basename only -- never the full path, so a directory named e.g.
#: ``test_fixtures/`` does not itself get treated as a test file.
_TEST_FILE_RE = re.compile(r"^test_.*\.py$")


def _run_git(args: Sequence[str], repo_root: str) -> list[str]:
    """Run a git subcommand and return its stdout, split into non-empty
    lines. Returns ``[]`` on any failure (git absent, not a repo, etc.) --
    the empty-set fallback is the caller's fail-safe path (§ module
    docstring): "no changed test files found" degrades to "run the full
    configured tier," never to "run nothing."
    """
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
    """Configured pytest ``testpaths`` roots for ``repo_root`` (flat
    ``[tool.pytest.ini_options] testpaths`` in ``pyproject.toml``). Returns
    ``[]`` when unreadable -- degrades the testpaths-membership filter to
    "reject everything," which is the fail-safe direction (a changed test
    file that fails this filter simply isn't appended, and the caller
    still runs its full configured tier).
    """
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
    """Is ``posix_path`` the configured testpaths root itself, or a
    descendant of one? Empty ``testpaths`` (unreadable/unconfigured)
    rejects everything -- see ``_read_testpaths``'s fail-safe note."""
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


def append_test_paths(cmd: str, paths: Sequence[str]) -> str:
    """Append ``paths`` onto the resolved command string ``cmd``, one
    shell-quoted token per path.

    APPENDS ONLY -- never rebuilds, reorders, or drops any part of
    ``cmd``. This is the load-bearing property: ``cmd`` carries this
    repo's ``-m 'not cadence and not pending_fix and not designed_red'``
    marker selector, and dropping or rebuilding it would fire
    ``designed_red`` tests, which are red by design. ``cmd`` is ultimately
    handed to ``bash -c`` by every caller of this module, so
    ``shlex.quote`` (POSIX quoting) is the correct quoting discipline
    regardless of the host OS -- the subprocess that interprets the
    resulting string is always bash, on Windows and POSIX alike (see
    caller sites' own ``bash_bin = os.environ.get("BASH") or "bash"``).

    Returns ``cmd`` unchanged when ``paths`` is empty -- the identity
    case a caller relies on for "no changed test files -> behaviour
    unchanged."
    """
    if not paths:
        return cmd
    import shlex

    quoted = " ".join(shlex.quote(p) for p in paths)
    return f"{cmd} {quoted}"


#: pytest's own exit-code contract (not this module's invention): 5 means
#: "no tests were collected." A diff-scoped command that names a changed
#: test file the marker filter then deselects entirely (e.g. the changed
#: file carries only ``designed_red``-marked tests) collects zero tests
#: and exits 5. That is NOT a test failure (nothing ran) and NOT a clean
#: pass (nothing ran) -- callers MUST treat it as "the diff-scoped run
#: proved nothing" and fall back to running the full configured tier
#: rather than reporting either a false-green pass or a false-red
#: failure. This constant exists so every caller checks the SAME rc value
#: rather than re-deriving pytest's exit-code table by hand.
PYTEST_NO_TESTS_COLLECTED = 5


def diag(msg: str) -> None:
    """Print a diff-scoped-tests diagnostic line to stderr. Shared so both
    call sites emit the same ``[diff-scoped-tests]``-prefixed prose rather
    than each inventing their own wording."""
    print(f"[diff-scoped-tests] {msg}", file=sys.stderr)
