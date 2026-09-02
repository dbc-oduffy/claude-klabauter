"""tests/test_session_ensure_branch.py — pytest coverage for
lib/session_ensure_branch.py.

This is new coverage, not a port of a prior test suite.

Port: docs/plans/2026-07-19-debash-coordinator-windows.md (chunk E3-f)
Spec backlink: state/handoffs/2026-07-04_220004_roadmap-strang-04.md § Phase 1
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB_DIR = os.path.join(_PLUGIN_ROOT, "lib")
_BIN_LIB_DIR = os.path.join(_PLUGIN_ROOT, "bin", "lib")
for _p in (_LIB_DIR, _BIN_LIB_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import session_ensure_branch as seb  # noqa: E402


def _claude_klabauter_root() -> str:
    try:
        from cc_invoke import _resolve_claude_klabauter_root  # noqa: E402
    except ImportError:
        pytest.skip("cc_invoke not importable — claude-klabauter root resolution unavailable")
    try:
        return _resolve_claude_klabauter_root()
    except RuntimeError:
        pytest.skip("CLAUDE_KLABAUTER_ROOT unresolvable in this environment")


@pytest.fixture(autouse=True)
def _claude_klabauter_on_path():
    root = _claude_klabauter_root()
    if root not in sys.path:
        sys.path.insert(0, root)


_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **_NO_CONSOLE)


@pytest.fixture
def sandbox_repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(["git", "init", "--bare", "-q", str(origin)], str(tmp_path))
    _run(["git", "clone", "-q", str(origin), str(work)], str(tmp_path))
    _run(["git", "config", "user.email", "t@t.com"], str(work))
    _run(["git", "config", "user.name", "T"], str(work))
    (work / "f.txt").write_text("x", encoding="utf-8")
    _run(["git", "add", "f.txt"], str(work))
    _run(["git", "commit", "-q", "-m", "init"], str(work))
    _run(["git", "push", "-q", "-u", "origin", "master"], str(work))
    _run(["git", "checkout", "-q", "-b", "main"], str(work))
    _run(["git", "push", "-q", "-u", "origin", "main"], str(work))
    monkeypatch.chdir(work)
    return work


def _branches(work) -> str:
    return _run(["git", "branch"], str(work)).stdout


def test_on_main_zero_ahead_cuts_daily_branch(sandbox_repo):
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "FRESH-CUT"
    assert res.new_branch == "work/testmachine/2026-07-21"
    assert "work/testmachine/2026-07-21" in _branches(sandbox_repo)


def test_parseable_span_branch_zero_ahead_is_noop(sandbox_repo):
    seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "work/testmachine/2026-07-21", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == ""
    assert res.new_branch == ""


def test_detached_head_collision_safe_suffix(sandbox_repo):
    seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    _run(["git", "checkout", "-q", "--detach", "HEAD"], str(sandbox_repo))
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "", "yes", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "FRESH-CUT"
    assert res.new_branch == "work/testmachine/2026-07-21-2"


def test_nonspan_branch_with_commits_ahead_is_noop(sandbox_repo):
    _run(["git", "checkout", "-q", "-b", "feature/foo"], str(sandbox_repo))
    (sandbox_repo / "g.txt").write_text("y", encoding="utf-8")
    _run(["git", "add", "g.txt"], str(sandbox_repo))
    _run(["git", "commit", "-q", "-m", "wip"], str(sandbox_repo))

    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "feature/foo", "no", "1",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == ""


def test_nonspan_branch_zero_ahead_cuts_daily_branch(sandbox_repo):
    _run(["git", "checkout", "-q", "-b", "feature/foo"], str(sandbox_repo))
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "feature/foo", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "FRESH-CUT"
    assert res.new_branch == "work/testmachine/2026-07-21"


class _FakeVerdict:
    def __init__(self, outcome, reason):
        self.outcome = outcome
        self.reason = reason


def test_refused_verdict_blocks_cut_and_leaves_branch_untouched(sandbox_repo, monkeypatch):
    monkeypatch.setattr(
        seb,
        "_branch_mutation_verdict",
        lambda: (lambda **kw: _FakeVerdict("refused", "1 live peer session(s): abc123 (on main)")),
    )
    before = _branches(sandbox_repo)
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "REFUSED-LIVE-PEERS"
    assert res.new_branch == ""
    assert _branches(sandbox_repo) == before
    assert "work/testmachine/2026-07-21" not in _branches(sandbox_repo)


def test_unknown_verdict_treated_same_as_refused(sandbox_repo, monkeypatch):
    monkeypatch.setattr(
        seb,
        "_branch_mutation_verdict",
        lambda: (lambda **kw: _FakeVerdict("unknown", "cannot resolve live-session set")),
    )
    before = _branches(sandbox_repo)
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "REFUSED-LIVE-PEERS"
    assert res.new_branch == ""
    assert _branches(sandbox_repo) == before


def test_ok_verdict_cuts_as_before(sandbox_repo, monkeypatch):
    monkeypatch.setattr(
        seb,
        "_branch_mutation_verdict",
        lambda: (lambda **kw: _FakeVerdict("ok", "no live peer sessions")),
    )
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr,
    )
    assert res.result == "FRESH-CUT"
    assert res.new_branch == "work/testmachine/2026-07-21"
    assert "work/testmachine/2026-07-21" in _branches(sandbox_repo)


# ---------------------------------------------------------------------------
# The boot path's merged-away-day-branch repair (2026-09-02).
#
# The state these three pin is not exotic: `/merging-to-main` merges today's
# day branch into `main` and leaves the tree on `main`, which puts the local
# day-branch ref BEHIND HEAD. Every boot afterwards found the branch existing,
# found HEAD not at its tip, refused, printed the banner, and left the tree on
# `main`. On 2026-09-02 that ran for forty minutes and fifteen commits landed
# on `main`, because the assert reported the state at SessionStart and nothing
# ever repaired it. A detector that fires once and changes nothing is what
# these tests exist to keep out.
# ---------------------------------------------------------------------------


def _commit(work, name: str) -> None:
    (work / name).write_text(name, encoding="utf-8")
    _run(["git", "add", name], str(work))
    _run(["git", "commit", "-q", "-m", name], str(work))


def _sha(work, rev: str) -> str:
    return _run(["git", "rev-parse", rev], str(work)).stdout.strip()


def test_boot_advances_a_day_branch_that_main_has_moved_past(sandbox_repo):
    """THE REPAIR. Today's branch exists and lags HEAD -- every commit it
    carries is already reachable from HEAD, the ordinary post-merge state.
    The boot path advances the ref and checks it out instead of refusing, and
    HEAD's commit does not move.
    """
    work = sandbox_repo
    _run(["git", "checkout", "-q", "-b", "work/testmachine/2026-07-21"], str(work))
    _commit(work, "on-day-branch.txt")
    # The merge-to-main: `main` fast-forwards past the day branch and the tree
    # returns to it, exactly as /merging-to-main leaves things.
    _run(["git", "checkout", "-q", "main"], str(work))
    _run(["git", "merge", "-q", "--ff-only", "work/testmachine/2026-07-21"], str(work))
    _commit(work, "on-main-after-merge.txt")

    head_before = _sha(work, "HEAD")
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr, caller="boot",
    )

    assert res.result == seb.ADVANCED_TO_HEAD
    assert res.new_branch == "work/testmachine/2026-07-21"
    assert _current_branch(work) == "work/testmachine/2026-07-21"
    assert _sha(work, "HEAD") == head_before
    assert _sha(work, "work/testmachine/2026-07-21") == head_before


def test_boot_still_refuses_a_day_branch_carrying_commits_head_lacks(sandbox_repo):
    """THE LIMIT. Genuine divergence -- the day branch has a commit `main`
    does not -- is NOT repaired. Checking it out would move HEAD under every
    live peer, and advancing the ref would discard that commit. Unchanged
    refusal, both.
    """
    work = sandbox_repo
    _run(["git", "checkout", "-q", "-b", "work/testmachine/2026-07-21"], str(work))
    _commit(work, "unmerged.txt")
    day_sha = _sha(work, "HEAD")
    _run(["git", "checkout", "-q", "main"], str(work))
    _commit(work, "diverged-on-main.txt")

    head_before = _sha(work, "HEAD")
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr, caller="boot",
    )

    assert res.result == "REFUSED-LIVE-PEERS"
    assert _current_branch(work) == "main"
    assert _sha(work, "HEAD") == head_before
    # The unmerged commit is still there. Nothing was discarded.
    assert _sha(work, "work/testmachine/2026-07-21") == day_sha


def test_boot_still_adopts_when_head_is_already_at_the_branch_tip(sandbox_repo):
    """THE UNCHANGED ARM. Equal shas keep taking ADOPTED-EXISTING, not the
    new one -- the repair must not swallow the every-boot routine case.
    """
    work = sandbox_repo
    _run(["git", "branch", "work/testmachine/2026-07-21"], str(work))
    res = seb.session_ensure_branch(
        "testmachine", "2026-07-21", "main", "no", "0",
        env=dict(os.environ), stderr=sys.stderr, caller="boot",
    )
    assert res.result == seb.ADOPTED_EXISTING


def _current_branch(work) -> str:
    return _run(["git", "branch", "--show-current"], str(work)).stdout.strip()
