"""Spawn budget for `bump_foreign_repo_write`'s git-root resolution: how
many `git rev-parse --show-toplevel` processes ONE guard invocation costs.

WHY THIS MODULE EXISTS. `_evaluate_foreign_repo_candidate` used to read as
three `resolve_git_root` calls per write-sink candidate (`probe_root`,
`marker_probe_root`, `session_repo_label`), and was filed as exactly that on
2026-08-21 (`state/bug-backlog/2026-08-21-bump-foreign-repo-write-up-to-
three-per-a9c8f2cef500.yaml`) -- a static call-site read, never measured.
Measured, it was TWO per invocation and CONSTANT in candidate count, because
`resolve_git_root` has been process-memoized on an explicit `cwd` since
`36e9e3d3c` and two of the three sites re-ask a question already asked:
`marker_probe` IS `probe_dir` on both branches, and `anchor` was already
resolved by `check_bump_foreign_repo_write` for `anchor_root`.

It is now ONE per invocation. The remaining spawn (`probe_root`, feeding
only `target_repo_label` -- a display label and a publish-destination
classification input, never a same-repo verdict) has been replaced with
`coordinator_core.git.repo_root.show_toplevel`, a symlink-safe, spawn-free
walk that reuses the SAME per-cwd memo `resolve_gitdir(probe_dir)` already
warmed just above it -- see `_evaluate_foreign_repo_candidate`'s own comment
at that assignment for the four gitdir shapes (ordinary/worktree/submodule/
separated-gitdir) this was checked against. Only `anchor_root` (the
SESSION's own root, computed once by the caller and threaded through --
identity, not a label) still spawns.

That collapse is load-bearing under the box's load norm and invisible at the
call sites -- which is the whole reason the three-spawn shape got filed as a
defect by two independent readers before it was measured. This module pins
the current (one-spawn) shape the same way: it counts UNCACHED resolutions
(`_resolve_git_root_uncached`), not `resolve_git_root` calls, so it fails if
the memoization is removed, if its explicit-`cwd` key changes, or if a
future edit reintroduces a second `resolve_git_root` spawn on this path --
none of which any behavioural test in this package would notice.

NEGATIVE SPEC. This module asserts nothing about the VERDICT (that is
`test_bump_foreign_repo_write.py`'s oracle) and nothing about wall clock --
DR-344 names process count as the unit precisely because wall clock on this
box measures peer load. It counts processes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.subagent_sandbox import engine

from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _clean_bump_env,  # noqa: F401 -- fixture, autoused by name in this package.
    _git,
    _init_repo,
    _posix,
    _set_anchor,
    repos,  # noqa: F401 -- fixture reused rather than re-founded.
)

#: Extracts the FIRST backtick-quoted token from a deny message -- both
#: `render_em_message` and `render_subagent_message` (`_write_bump_message.py`)
#: place `target_repo_label` there first, via `_target_phrase`, before any
#: other backtick-quoted value (`session_repo`, a sandbox root) appears.
_FIRST_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")


def _target_label_in_message(result: dict) -> str:
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    m = _FIRST_BACKTICK_TOKEN_RE.search(reason)
    assert m is not None, reason
    return m.group(1)

# Real `git` runs in the `repos` fixture's repo setup, same as the module
# these fixtures come from. Spawn ratchet:
# coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


#: Measured on HEAD 2026-08-21 (post `probe_root` spawn removal), one
#: `check_bump_foreign_repo_write` call with a foreign write sink: one
#: resolution, for the SESSION anchor's own root (`anchor_root`, threaded to
#: every candidate). The TARGET's own root (`probe_root`, feeding only
#: `target_repo_label`) no longer spawns -- see `_evaluate_foreign_repo_
#: candidate`'s comment at that assignment for why a spawn-free, symlink-safe
#: walk is safe for a LABEL/classification input that never gates
#: allow-vs-deny, unlike the AC14 same-repo comparison (still gitdir-derived,
#: untouched).
_DENY_PATH_ROOT_RESOLUTION_BUDGET = 1

#: An own-repo write sink resolves the anchor and nothing else: the same-repo
#: comparison it would have fed is answered from the gitdir, spawn-free.
_OWN_REPO_ROOT_RESOLUTION_BUDGET = 1


@pytest.fixture()
def root_resolutions(monkeypatch) -> Iterator[List[Optional[str]]]:
    """Every `cwd` that reaches an actual `git rev-parse --show-toplevel`,
    in order -- cache HITS never appear here, which is the point.

    Clears the process-local cache first: this suite shares a process with
    every other module that resolved a root, and a warm cache would report a
    budget of zero and pass vacuously.
    """
    engine.reset_resolve_git_root_cache()
    seen: List[Optional[str]] = []
    inner = engine._resolve_git_root_uncached

    def counting(cwd=None):
        seen.append(cwd)
        return inner(cwd)

    monkeypatch.setattr(engine, "_resolve_git_root_uncached", counting)
    yield seen
    engine.reset_resolve_git_root_cache()


def test_foreign_write_sink_costs_one_root_resolution(
    repos, monkeypatch, root_resolutions  # noqa: F811
):
    _set_anchor(monkeypatch, repos, "sess-budget-1")
    cmd = f"echo hi > {_posix(repos['foreign'] / 'redir.txt')}"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-1", str(repos["anchor"]), {}
    )

    assert result is not None, "fixture drift: this command must reach the deny path"
    assert len(root_resolutions) == _DENY_PATH_ROOT_RESOLUTION_BUDGET, root_resolutions


def test_root_resolutions_do_not_scale_with_candidate_count(
    repos, monkeypatch, root_resolutions  # noqa: F811
):
    """The filed defect was "up to three PER CANDIDATE". Three candidates in
    one command cost the same single resolution as one -- the per-candidate
    sites are cache hits (or, for `probe_root`, spawn-free), so the budget
    is a property of the invocation."""
    _set_anchor(monkeypatch, repos, "sess-budget-2")
    foreign = repos["foreign"]
    cmd = "; ".join(
        f"echo {n} > {_posix(foreign / f'sub-{n}' / 'f.txt')}" for n in ("a", "b", "c")
    )

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-2", str(repos["anchor"]), {}
    )

    assert result is not None, "fixture drift: this command must reach the deny path"
    assert len(root_resolutions) == _DENY_PATH_ROOT_RESOLUTION_BUDGET, root_resolutions


def test_own_repo_write_sink_costs_one_root_resolution(
    repos, monkeypatch, root_resolutions  # noqa: F811
):
    """The dominant WRITE case across the fleet: a session writing into its
    own repo. It allows, and it pays for the anchor alone."""
    _set_anchor(monkeypatch, repos, "sess-budget-3")
    cmd = f"echo hi > {_posix(repos['anchor'] / 'own.txt')}"

    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-3", str(repos["anchor"]), {}
    )

    assert result is None
    assert len(root_resolutions) <= _OWN_REPO_ROOT_RESOLUTION_BUDGET, root_resolutions


def test_command_with_no_write_sink_resolves_no_root_at_all(
    repos, monkeypatch, root_resolutions  # noqa: F811
):
    """The dominant case, period: applicability is decided from the parsed
    command alone, so `echo hello` never reaches a spawn."""
    _set_anchor(monkeypatch, repos, "sess-budget-4")

    result = guard.check_bump_foreign_repo_write(
        "echo hello", "sess-budget-4", str(repos["anchor"]), {}
    )

    assert result is None
    assert root_resolutions == []


def test_sibling_outside_repo_guard_resolves_no_root(repos, monkeypatch, root_resolutions):  # noqa: F811
    """The filed defect suspected `bump_outside_repo_write` of carrying the
    same seam. It does not carry it: that guard reaches for `resolve_gitdir`
    (filesystem-backed) and never `resolve_git_root`, so its root-resolution
    budget is zero on the deny path, not merely small. Refuted here by
    measurement rather than by reading its imports."""
    from coordinator_core.bash_guards import bump_outside_repo_write as sibling

    _set_anchor(monkeypatch, repos, "sess-budget-5")
    outside = repos["home"] / "not-a-repo"
    cmd = f"echo hi > {_posix(outside / 'f.txt')}"

    sibling.check_bump_outside_repo_write(
        cmd, "sess-budget-5", str(repos["anchor"]), {}
    )

    assert root_resolutions == []


# ---------------------------------------------------------------------------
# `target_repo_label` shape pins -- the four gitdir cases named in
# `_evaluate_foreign_repo_candidate`'s comment at the `probe_root` assignment.
# Each asserts the LABEL the guard's own deny message carries, not merely
# that it denies, so a future edit that silently degrades a label to win a
# spawn count (the exact failure mode this baton named) fails loudly here.
# ---------------------------------------------------------------------------


def test_linked_worktree_target_label_is_the_worktree_root_not_main(
    repos, monkeypatch, root_resolutions  # noqa: F811
):
    """A write into a LINKED WORKTREE of the foreign repo labels the
    worktree's own root -- matching real `git rev-parse --show-toplevel` run
    from inside that worktree -- never the main checkout's root the worktree
    was created from. `show_toplevel`'s walk stops at the FIRST `.git` entry
    it meets climbing from `probe_dir`, which is the worktree's own `.git`
    FILE, not the main repo's `.git` DIRECTORY."""
    _set_anchor(monkeypatch, repos, "sess-budget-worktree")
    wt = repos["foreign"].parent / "foreign-wt"
    _git(str(repos["foreign"]), "worktree", "add", "-b", "wt-branch", str(wt))

    cmd = f"echo hi > {_posix(wt / 'redir.txt')}"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-worktree", str(repos["anchor"]), {}
    )

    assert result is not None, "fixture drift: this command must reach the deny path"
    label = _target_label_in_message(result)
    assert Path(label).resolve() == wt.resolve(), (label, wt)
    assert Path(label).resolve() != repos["foreign"].resolve()
    assert len(root_resolutions) == _DENY_PATH_ROOT_RESOLUTION_BUDGET, root_resolutions


def test_submodule_target_label_is_the_submodule_root_not_super(
    repos, monkeypatch, root_resolutions, tmp_path  # noqa: F811
):
    """A write into a SUBMODULE checked out under the foreign repo labels
    the submodule's own root, not the superproject's -- the submodule's
    gitdir lives at `<super>/.git/modules/<name>`, but `show_toplevel`'s walk
    from inside the submodule's own worktree stops at ITS `.git` file first."""
    _set_anchor(monkeypatch, repos, "sess-budget-submodule")
    sub_source = _init_repo(tmp_path, "sub-source")
    _git(
        str(repos["foreign"]),
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        _posix(sub_source),
        "sub",
    )
    sub_dir = repos["foreign"] / "sub"

    cmd = f"echo hi > {_posix(sub_dir / 'redir.txt')}"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-submodule", str(repos["anchor"]), {}
    )

    assert result is not None, "fixture drift: this command must reach the deny path"
    label = _target_label_in_message(result)
    assert Path(label).resolve() == sub_dir.resolve(), (label, sub_dir)
    assert Path(label).resolve() != repos["foreign"].resolve()
    assert len(root_resolutions) == _DENY_PATH_ROOT_RESOLUTION_BUDGET, root_resolutions


def test_separated_gitdir_target_label_degrades_to_target_dir(
    repos, monkeypatch, root_resolutions, tmp_path  # noqa: F811
):
    """`GIT_DIR=<gitdir>` naming a gitdir with no `.git` suffix and no
    worktree binding -- `_worktree_root_for_gitdir_override` leaves it
    verbatim (only a `.git`-suffixed override is rewritten to its parent), so
    `probe_dir` IS the gitdir itself. `show_toplevel` there finds no NESTED
    `.git` entry, recognises the `HEAD`/`objects`/`refs` markers as a bare
    repo, and correctly reports no toplevel (`None`) -- a NAMED degradation
    to the pre-existing `target_dir` fallback, not a silent one, and not a
    false worktree root asserted for a shape that has none."""
    _set_anchor(monkeypatch, repos, "sess-budget-separated-gitdir")
    sep_gitdir = tmp_path / "separated.git"
    # `git init --bare` alone already writes the three markers
    # `_looks_like_git_dir` (`show_toplevel`'s bare-repo recognizer) checks
    # for -- `HEAD`, `objects/`, `refs/` -- no fetch/commit needed to make
    # this directory present as a bare gitdir.
    _git(str(repos["foreign"].parent), "init", "-q", "--bare", str(sep_gitdir))
    assert show_toplevel(str(sep_gitdir)) is None, "fixture drift: expected a bare-shaped gitdir"

    cmd = f"GIT_DIR={_posix(sep_gitdir)} git commit --allow-empty -m x"
    result = guard.check_bump_foreign_repo_write(
        cmd, "sess-budget-separated-gitdir", str(repos["anchor"]), {}
    )

    assert result is not None, "fixture drift: this command must reach the deny path"
    label = _target_label_in_message(result)
    assert Path(label).resolve() == sep_gitdir.resolve(), (label, sep_gitdir)
    assert len(root_resolutions) == _DENY_PATH_ROOT_RESOLUTION_BUDGET, root_resolutions
