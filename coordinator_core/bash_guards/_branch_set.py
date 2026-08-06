"""coordinator_core.bash_guards._branch_set -- canonical-branch-set enumerator.

Purpose: answer "are there other canonical workstream branches besides the
current one, with commits not in main?" -- and return each survivor's
last-commit epoch so a caller (guard_branch_set_precedence.py, C5) can filter
on recency (AC16's `should_prompt_rename` two-leg filter) without spending a
second `git` call per candidate.

Single-subprocess shape (required, not optional -- see plan body): one
`git for-each-ref --no-merged=<main_ref> --format='%(refname:short)
%(committerdate:unix)' refs/heads/` answers both "which local branches carry
commits not in main" and "when was each one last touched" in ONE call.
`for-each-ref` supports `--no-merged` natively; there is no per-branch
fan-out here. A SECOND subprocess -- `git rev-list --count
<main_ref>..<branch>` -- is spent only for the single branch a caller
actually names (exposed as `ahead_of_main`, called at most once by C5), never
for every candidate. Worst case across both public functions is 2
subprocesses total, not one per candidate branch.

Cost discipline (plan ruling R3): this module must never be called until the
caller has already established, with zero subprocesses, that the command in
hand is a real daily-branch creation -- that gate lives upstream (C1/C5), not
here. This module has no import-time or module-load git calls; every
subprocess is spent only inside `other_canonical_branches` /
`ahead_of_main`, and only when a caller invokes them.

No dirty-state leg (plan ruling R1): a non-checked-out branch has no
uncommitted-state concept in git; ahead-count vs main is the only honest
signal this module reports.

Lifted (not re-rolled) from `coordinator_core/ops/orphan_branch_sweep.py`:
`_main_ref`, `_current_branch`, `_rev_list_count`, and `_ref_exists` -- the
git-plumbing primitives this enumerator is built on. Unlike the source
module, `_main_ref` gained a `cwd` param on lift: this module threads `cwd`
as a first-class param throughout, and C5 calls it with a payload-derived
`cwd` that need not equal the process cwd, so `_main_ref` must resolve
against the same repo as everything else or it silently degrades callers to
empty/zero.

Spec backlink: docs/plans/2026-08-01-branch-creation-seam-guards.md chunk C3
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from coordinator_core import daily_branch

_PathLike = Union[str, "Path", None]

#: House value (`dispatch_checks._run_git`) -- was 15s with no stated
#: reason; brought down 2026-08-05 hardening pass. A hang here surfaces as
#: an uncaught `subprocess.TimeoutExpired`, which propagates out of
#: `other_canonical_branches`/`ahead_of_main` to `guard_branch_set_
#: precedence.check`'s own unconditional `try/except Exception: return
#: None` (CLASS = "advisory", fail-open by construction) -- so a shorter
#: cap only shortens the stall, it does not change the fail direction.
#:
#: Review: code-reviewer (F4, nit) -- this is a PER-INVOCATION budget, not
#: a budget for either public function's whole call sequence: every `_git`
#: call (`_run` -> `subprocess.run`) gets its own fresh 2.0s cap, threaded
#: as this module-level default through `_git`/`_run`'s own `timeout=`
#: parameter (see both functions above). `other_canonical_branches` can
#: spend up to 4 such calls (`_main_ref`'s up to 2 `_ref_exists` probes,
#: one `for-each-ref`, one `_current_branch`); `ahead_of_main` up to 3
#: (`_main_ref`'s up to 2, one `rev-list --count`). Each of those git
#: subcommands (`rev-parse`, `for-each-ref --no-merged`, `rev-list
#: --count`) is a plumbing read with no full-history walk, so a single one
#: exceeding 2.0s on a cold-but-not-pathological repo is not expected --
#: this was not empirically re-measured against a large repo as part of
#: this hardening pass, only reasoned about from each subcommand's shape.
#: Do NOT raise this value without a PM ruling -- a coverage regression
#: here is a real tradeoff, not a free lunch.
_GIT_TIMEOUT = 2.0


def _run(cmd: list, timeout: float = _GIT_TIMEOUT, cwd: _PathLike = None) -> subprocess.CompletedProcess:
    """Run a subprocess with a hang cap and no stdin -- mirrors the sibling
    guard-module convention (`ops/orphan_branch_sweep.py::_run`)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=cwd,
    )


def _git(args: list, timeout: float = _GIT_TIMEOUT, cwd: _PathLike = None) -> subprocess.CompletedProcess:
    """Module-level git seam -- tests monkeypatch this attribute directly to
    inject a fake runner and count invocations (per this chunk's cost-
    discipline requirement)."""
    return _run(["git", *args], timeout=timeout, cwd=cwd)


def _ref_exists(ref: str, cwd: _PathLike = None) -> bool:
    return _git(["rev-parse", ref], cwd=cwd).returncode == 0


def _main_ref(cwd: _PathLike = None) -> Optional[str]:
    """Lifted from `ops/orphan_branch_sweep.py::_main_ref`, with `cwd` added
    on lift -- unlike that module, every sibling primitive here threads
    `cwd` as a first-class param and C5 calls this module with a
    payload-derived `cwd` that need not equal the process cwd. Without
    forwarding `cwd` here too, a repo with a perfectly good `main` would
    silently resolve against the wrong repo and degrade callers to
    empty/zero rather than checking the intended repo.
    # Review: coordinator:code-reviewer -- P1, cwd not threaded into _main_ref
    while every sibling primitive in this module does (Finding 1)
    """
    if _ref_exists("origin/main", cwd=cwd):
        return "origin/main"
    if _ref_exists("main", cwd=cwd):
        return "main"
    return None


def _current_branch(cwd: _PathLike = None) -> Optional[str]:
    res = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if res.returncode != 0:
        return None
    branch = res.stdout.strip()
    return None if not branch or branch == "HEAD" else branch


def _rev_list_count(*args: str, cwd: _PathLike = None) -> int:
    res = _git(["rev-list", "--count", *args], cwd=cwd)
    if res.returncode != 0:
        return 0
    try:
        return int(res.stdout.strip())
    except ValueError:
        return 0


def _parse_for_each_ref_row(line: str) -> Optional[Tuple[str, int]]:
    """Parse one `%(refname:short) %(committerdate:unix)` row. Returns None
    on a malformed row (blank line, missing epoch, non-integer epoch) rather
    than raising -- a caller filters these out."""
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.rsplit(" ", 1)
    if len(parts) != 2:
        return None
    name, epoch_str = parts
    if not name:
        return None
    try:
        epoch = int(epoch_str)
    except ValueError:
        return None
    return (name, epoch)


def other_canonical_branches(cwd: _PathLike = None) -> List[Tuple[str, int]]:
    """Enumerate local branches with commits not in main, excluding the
    current branch, filtered to canonical shape via `daily_branch.
    is_allowed_branch`.

    Single subprocess: `git for-each-ref --no-merged=<main_ref> --format=
    '%(refname:short) %(committerdate:unix)' refs/heads/`. Returns
    `(name, committer_epoch)` pairs, not bare names -- callers need the
    epoch to apply a recency filter without a second per-branch git call.

    Returns an empty list (zero findings, not an error) when there is no
    resolvable main ref, or the `for-each-ref` call itself fails.
    """
    main_ref = _main_ref(cwd=cwd)
    if main_ref is None:
        return []

    res = _git(
        [
            "for-each-ref",
            f"--no-merged={main_ref}",
            "--format=%(refname:short) %(committerdate:unix)",
            "refs/heads/",
        ],
        cwd=cwd,
    )
    if res.returncode != 0:
        return []

    current = _current_branch(cwd=cwd)

    results: List[Tuple[str, int]] = []
    for line in res.stdout.splitlines():
        row = _parse_for_each_ref_row(line)
        if row is None:
            continue
        name, epoch = row
        if name == current:
            continue
        if not daily_branch.is_allowed_branch(name):
            continue
        results.append((name, epoch))
    return results


def ahead_of_main(branch: str, cwd: _PathLike = None) -> int:
    """`git rev-list --count <main_ref>..<branch>` -- spent only for the
    single branch a caller (C5) has already decided to name in its advisory,
    never fanned out across every candidate from `other_canonical_branches`.

    Returns 0 (not an error) when there is no resolvable main ref.
    """
    main_ref = _main_ref(cwd=cwd)
    if main_ref is None:
        return 0
    return _rev_list_count(f"{main_ref}..{branch}", cwd=cwd)
