"""`percolate.dest_refresh` against real git repositories.

Real clones, not mocks: every rule this module enforces is a property of git's
own refusals (`fetch`'s non-fast-forward default, `merge --ff-only`), and a
mock would pin this module's *belief* about those refusals rather than the
refusals. The repos here are local `file://`-style clones — no network, so the
whole file stays inside the suite's process budget while still exercising the
real `fetch`/`merge` legs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.dest_refresh import (  # noqa: E402
    default_remote_branch,
    refresh_dest_from_origin,
)

#: Real git spawns, admitted by the spawn ratchet
#: (`coordinator_core/tests/test_no_new_spawning_tests.py`); `cadence` because
#: a dozen-odd clone/commit spawns per test is not per-commit work.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        **_NO_CONSOLE,
    )
    return proc.stdout.strip()


def _commit(root: Path, name: str) -> None:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", name)


@pytest.fixture()
def origin_and_clone(tmp_path: Path):
    """An `origin` with `main` + `candidate`, and a clone checked out on
    `candidate` — the shape the release repo actually has on every box."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, "seed")
    _git(origin, "checkout", "-b", "candidate")
    _commit(origin, "candidate-seed")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--branch", "candidate", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    # `git clone` only materializes the checked-out branch as a local ref; the
    # `main` leg is only meaningful when a local `main` exists, so create one.
    _git(clone, "branch", "main", "origin/main")
    # A bare push back into a non-bare `origin` is refused for its checked-out
    # branch; park `origin` on a throwaway branch so the tests can advance both.
    _git(origin, "checkout", "-b", "parked")
    return origin, clone


def _capture():
    import io

    return io.StringIO(), io.StringIO()


def test_level_clone_passes_and_reports_level(origin_and_clone):
    clone = origin_and_clone[1]
    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)
    assert result.ok
    assert result.branch == "candidate"
    assert result.behind == 0
    assert result.fast_forwarded is False
    assert "already level" in out.getvalue()


def test_behind_clone_is_fast_forwarded_to_the_landing_branch(origin_and_clone):
    origin, clone = origin_and_clone
    _git(origin, "checkout", "candidate")
    _commit(origin, "peer-landed")
    _git(origin, "checkout", "parked")

    before = _git(clone, "rev-parse", "HEAD")
    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok
    assert result.fast_forwarded is True
    assert result.behind == 1
    assert _git(clone, "rev-parse", "HEAD") != before
    # The peer's file is on disk — this is the whole point of the step: the
    # round that follows now syncs over a tree that HAS the peer's work.
    assert (clone / "peer-landed").exists()


def test_main_is_fast_forwarded_even_though_candidate_is_checked_out(origin_and_clone):
    origin, clone = origin_and_clone
    _git(origin, "checkout", "main")
    _commit(origin, "main-moved")
    _git(origin, "checkout", "parked")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok
    assert result.warnings == ()
    assert _git(clone, "rev-parse", "main") == _git(clone, "rev-parse", "origin/main")


def test_diverged_landing_branch_is_refused_not_reconciled(origin_and_clone):
    origin, clone = origin_and_clone
    _git(origin, "checkout", "candidate")
    _commit(origin, "peer-landed")
    _git(origin, "checkout", "parked")
    _commit(clone, "local-only")

    before = _git(clone, "rev-parse", "HEAD")
    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert not result.ok
    assert "diverged" in result.reason
    # Refusing means refusing: the local side is untouched, not rebased away.
    assert _git(clone, "rev-parse", "HEAD") == before
    assert (clone / "local-only").exists()


def test_ahead_only_clone_passes(origin_and_clone):
    """A round that committed but could not push leaves the clone ahead. That
    is not staleness and must not block the next round."""
    clone = origin_and_clone[1]
    _commit(clone, "unpushed-round")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok
    assert result.ahead == 1
    assert result.behind == 0


def test_untracked_landing_branch_is_measured_against_the_remote_default(origin_and_clone):
    """A fresh branch with no upstream PROCEEDS, measured against origin's
    default branch.

    This asserts the reversal of an earlier refusal, deliberately: a fresh clone
    on a new local branch is the ordinary cloud shape, and refusing it made the
    normal case the broken one. Nothing this module protects is given up -- the
    branch is still measured against the tip a peer would have landed on, it is
    still fast-forwarded when behind, and it is still refused when it has
    diverged (below).
    """
    clone = origin_and_clone[1]
    _git(clone, "checkout", "-b", "no-upstream")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok
    assert result.branch == "no-upstream"
    assert result.upstream in ("origin/main", "origin/candidate")
    assert "no upstream; measuring against" in out.getvalue()


def test_untracked_branch_behind_the_default_is_fast_forwarded(origin_and_clone):
    origin, clone = origin_and_clone
    _git(clone, "checkout", "-b", "no-upstream")
    # Advance whatever branch this clone resolves as origin's default -- the
    # fixture's `origin/HEAD` is a property of how it was cloned, not something
    # this behaviour depends on.
    base = default_remote_branch(clone)
    assert base is not None
    _git(origin, "checkout", base.split("/", 1)[1])
    _commit(origin, "peer-on-default")
    _git(origin, "checkout", "parked")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok, result.reason
    assert result.fast_forwarded is True
    assert (clone / "peer-on-default").exists()


def test_untracked_branch_with_no_remote_branch_at_all_is_refused_with_the_fix(tmp_path):
    """The one no-upstream case that stays a refusal: nothing to measure
    against. It names the command that fixes it rather than the state."""
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(solo, "init", "-b", "work")
    _git(solo, "config", "user.email", "t@example.invalid")
    _git(solo, "config", "user.name", "t")
    _commit(solo, "seed")

    out, err = _capture()
    result = refresh_dest_from_origin(solo, out=out, err=err)

    assert not result.ok
    assert "no remote default branch" in result.reason
    assert "remote set-head origin --auto" in result.reason


def test_detached_head_is_refused_with_its_own_reason(origin_and_clone):
    clone = origin_and_clone[1]
    _git(clone, "checkout", "--detach", "HEAD")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert not result.ok
    assert "detached HEAD" in result.reason


def test_unreachable_origin_is_refused_rather_than_skipped(origin_and_clone, tmp_path):
    """The failure mode this step exists to prevent is publishing from a clone
    whose staleness is unknown — so an unusable remote is a refusal, never a
    best-effort proceed."""
    clone = origin_and_clone[1]
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert not result.ok
    assert "could not fetch origin" in result.reason


def test_diverged_local_main_warns_but_does_not_block(origin_and_clone):
    """`main` is not what a round lands into, so a `main` that cannot
    fast-forward cannot make this round overwrite a peer."""
    origin, clone = origin_and_clone
    _git(origin, "checkout", "main")
    _commit(origin, "main-moved")
    _git(origin, "checkout", "parked")

    _git(clone, "checkout", "main")
    _commit(clone, "local-main-only")
    _git(clone, "checkout", "candidate")

    out, err = _capture()
    result = refresh_dest_from_origin(clone, out=out, err=err)

    assert result.ok
    assert len(result.warnings) == 1
    assert "could not be fast-forwarded to origin/main" in result.warnings[0]
    assert "WARNING" in err.getvalue()
