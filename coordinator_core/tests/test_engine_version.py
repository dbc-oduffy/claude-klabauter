"""
Tests for coordinator_core.engine_version — the engine self-version surface.

Covers: real-repo SHA resolution shape, graceful-degradation to None on git
failure (never raises), and the committed floor constant's shape.

Spec backlink: pln-claude-klabauter-engine-version-surface--c130a8
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.engine_version import (
    MIN_KNOWN_GOOD_SHA,
    engine_build,
    resolve_engine_dirty,
    resolve_engine_sha,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_resolve_engine_sha_returns_40_char_lowercase_hex_in_real_repo():
    sha = resolve_engine_sha()
    assert sha is not None
    assert len(sha) == 40
    # Review: code-reviewer (Finding 4) — literal lowercase-hex set states
    # intent directly instead of relying on `.lower()`'s redundant haystack.
    assert all(c in "0123456789abcdef" for c in sha)


def _fake_git(monkeypatch, *, returncode=0, stdout="", timed_out=False, capture=None):
    """Stand in for the engine_version module's OWN `run_git` reference.

    Patched at `coordinator_core.engine_version.run_git`, not at
    `subprocess.run`: since the G7 migration these resolvers call the shared
    runner, which absorbs every spawn failure into a `GitResult` and never lets
    `subprocess` raise through. Doubles that patched `subprocess.run` were
    therefore patching a seam the code under test no longer reaches — they went
    on asserting against the REAL repo's answer and could not fail for the
    reason they were written to catch. Named here because that failure mode is
    silent by construction.
    """

    def fake_run_git(args, **kwargs):
        if capture is not None:
            capture["args"] = list(args)
            capture["kwargs"] = kwargs
        return GitResult(
            returncode=returncode, stdout=stdout, stderr="", timed_out=timed_out
        )

    monkeypatch.setattr("coordinator_core.engine_version.run_git", fake_run_git)


def test_resolve_engine_sha_returns_none_when_git_missing(monkeypatch):
    # 127 is the shared runner's "the process never ran" sentinel (no git on
    # PATH, bad cwd) — the spawn failure that used to arrive as FileNotFoundError.
    _fake_git(monkeypatch, returncode=127)
    assert resolve_engine_sha() is None


def test_resolve_engine_sha_strips_trailing_newline_from_git_stdout(monkeypatch):
    # Review: code-reviewer (Finding 5) — explicitly exercises `.strip()` via
    # a mocked stdout so a regression (e.g. accidental `.rstrip` swap,
    # or `.strip()` removal) fails here rather than only incidentally via the
    # real-repo test.
    _fake_git(monkeypatch, stdout="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" + chr(10))
    assert resolve_engine_sha() == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_resolve_engine_sha_returns_none_when_git_returns_nonzero(monkeypatch):
    _fake_git(monkeypatch, returncode=1)
    assert resolve_engine_sha() is None


def test_resolve_engine_sha_returns_none_when_timeout(monkeypatch):
    # -1 plus `timed_out` is the runner's timeout shape; the resolver reads it
    # through `ok`, so a timeout degrades to None like every other failure.
    _fake_git(monkeypatch, returncode=-1, timed_out=True)
    assert resolve_engine_sha() is None


def test_resolve_engine_dirty_returns_bool_in_real_repo():
    dirty = resolve_engine_dirty()
    assert dirty is None or isinstance(dirty, bool)


def test_resolve_engine_dirty_false_when_porcelain_output_empty(monkeypatch):
    _fake_git(monkeypatch, stdout="")
    assert resolve_engine_dirty() is False


def test_resolve_engine_dirty_true_when_porcelain_output_nonempty(monkeypatch):
    _fake_git(monkeypatch, stdout=" M engine_version.py" + chr(10))
    assert resolve_engine_dirty() is True


def test_resolve_engine_dirty_scopes_status_to_engine_dir(monkeypatch):
    captured: dict = {}
    _fake_git(monkeypatch, stdout="", capture=captured)
    resolve_engine_dirty()
    engine_dir = str(Path(__file__).resolve().parent.parent)
    # `run_git` prepends the binary itself, so the argv the resolver builds
    # starts at `-C` rather than at "git".
    assert captured["args"][:2] == ["-C", engine_dir]
    assert captured["args"][-2:] == ["--", "."]


def test_resolve_engine_dirty_returns_none_when_git_missing(monkeypatch):
    _fake_git(monkeypatch, returncode=127)
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_git_returns_nonzero(monkeypatch):
    _fake_git(monkeypatch, returncode=1)
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_timeout(monkeypatch):
    _fake_git(monkeypatch, returncode=-1, timed_out=True)
    assert resolve_engine_dirty() is None


def test_min_known_good_sha_is_40_char_hex():
    assert len(MIN_KNOWN_GOOD_SHA) == 40
    # Review: code-reviewer (Finding 4) — literal lowercase-hex set states
    # intent directly instead of relying on `.lower()`'s redundant haystack.
    assert all(c in "0123456789abcdef" for c in MIN_KNOWN_GOOD_SHA)


def test_min_known_good_sha_is_a_real_commit_in_this_repo():
    # Review: code-reviewer (Finding 3) — a shape-only check lets a
    # typo'd/orphaned floor SHA pass silently and surfaces later as a
    # misclassified "indeterminate" drift result rather than a fast local
    # test failure. Tolerant of a missing git binary (skip, don't fail).
    engine_dir = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(engine_dir), "cat-file", "-e",
             f"{MIN_KNOWN_GOOD_SHA}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=5,
            **no_console_creationflags(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable in this test environment")

    assert result.returncode == 0, (
        f"MIN_KNOWN_GOOD_SHA {MIN_KNOWN_GOOD_SHA!r} is not a resolvable "
        f"commit in this repo's history: {result.stderr}"
    )


def test_engine_build_reports_the_running_copys_head_in_a_real_repo():
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    build = engine_version.engine_build()
    assert set(build) == {"engine_sha", "engine_dirty"}
    assert build["engine_sha"] == resolve_engine_sha(), (
        "the spawn-free read must agree with `git rev-parse HEAD` on a real repo; "
        "if it does not, the cheap path is reporting a different build from the one "
        "every other provenance consumer records"
    )
    engine_version._BUILD_MEMO = None


def test_engine_build_never_spawns_a_process(monkeypatch):
    """The design constraint, pinned. 93ms of git per verdict is what this avoids."""
    import subprocess

    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    engine_version.engine_build()  # warm the deferred imports
    engine_version._BUILD_MEMO = None

    def _boom(*args, **kwargs):
        raise AssertionError("engine_build must not create a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert engine_build()["engine_sha"] is not None
    engine_version._BUILD_MEMO = None


def test_engine_build_leaves_dirty_unanswered_rather_than_asserting_clean():
    """`engine_dirty` is None — "not determined" — never False.

    Reporting False would claim a clean tree this function never checked, and a
    consumer reading a bare sha as byte-exact is the misread `resolve_engine_dirty`
    exists to prevent.
    """
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    assert engine_build()["engine_dirty"] is None
    engine_version._BUILD_MEMO = None


def test_engine_build_resolves_once_per_process(monkeypatch):
    """The memo is the budget: one read, then a dict copy for every later verdict."""
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    calls = {"n": 0}

    def fake_head():
        calls["n"] += 1
        return "a" * 40

    monkeypatch.setattr(engine_version, "_engine_head_sha", fake_head)

    first = engine_version.engine_build()
    second = engine_version.engine_build()

    assert calls["n"] == 1
    assert first == second == {"engine_sha": "a" * 40, "engine_dirty": None}
    engine_version._BUILD_MEMO = None


def test_engine_build_returns_a_copy_callers_cannot_poison():
    """A caller mutating the returned dict must not rewrite every later verdict's
    provenance — the reply dict is handed to op callers that may edit in place."""
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    engine_version.engine_build()["engine_sha"] = "tampered"
    assert engine_version.engine_build()["engine_sha"] != "tampered"
    engine_version._BUILD_MEMO = None


def test_engine_build_carries_an_unresolvable_head_as_none(monkeypatch):
    """A copy outside any repo reports None, never raises — a vendored drop with
    no `.git` must still get a verdict, just an unattributable one."""
    from coordinator_core import engine_version

    engine_version._BUILD_MEMO = None
    monkeypatch.setattr(engine_version, "_engine_head_sha", lambda: None)
    assert engine_version.engine_build() == {"engine_sha": None, "engine_dirty": None}
    engine_version._BUILD_MEMO = None


def test_engine_head_sha_is_none_outside_a_repo(monkeypatch, tmp_path):
    from coordinator_core import engine_version

    monkeypatch.setattr(
        "coordinator_core.git.repo_root._walk_for_repo", lambda start: None
    )
    assert engine_version._engine_head_sha() is None
