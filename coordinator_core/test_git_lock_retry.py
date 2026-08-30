"""Tests for coordinator_core.git_lock_retry — the shared `.git/index.lock`
contention predicate and bounded-retry runner every ceremony commit seam
(`contract/apply_base.py::scoped_commit`,
`ops/ceremony/detached_render_commit.py`,
`backlog_grind_assemble/apply.py::_commit_one`) composes.

Spec backlink: archive/handoffs/2026-08/2026-08-07-git-index-lock-retry-at-the-ceremony-commit-seam.md
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core import git_lock_retry


class TestIsLockContention:
    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: Unable to create '.git/index.lock': File exists.",
            "error: Another git process seems to be running in this repository...",
        ],
    )
    def test_lock_shaped_stderr_is_recognized(self, stderr):
        assert git_lock_retry.is_lock_contention(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "disk full",
            "fatal: pathspec 'x' did not match any files",
            "hook declined to commit",
            "",
        ],
    )
    def test_non_lock_stderr_is_not_recognized(self, stderr):
        assert git_lock_retry.is_lock_contention(stderr) is False


def _result(returncode: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


class TestRunWithLockRetry:
    def test_success_on_first_attempt_never_retries(self, monkeypatch):
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda *_a: (_ for _ in ()).throw(
            AssertionError("should never sleep on first-attempt success")
        ))
        calls = []

        def invoke():
            calls.append(1)
            return _result(0)

        result = git_lock_retry.run_with_lock_retry(invoke)

        assert result.returncode == 0
        assert len(calls) == 1

    def test_lock_contention_then_success_completes_and_retries_exactly_once(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda s: sleeps.append(s))
        calls = []

        def invoke():
            calls.append(1)
            if len(calls) == 1:
                return _result(128, "fatal: Unable to create '.git/index.lock': File exists.")
            return _result(0)

        result = git_lock_retry.run_with_lock_retry(invoke)

        assert result.returncode == 0
        assert len(calls) == 2
        assert sleeps == [git_lock_retry.DEFAULT_BACKOFF_SCHEDULE_S[0]]

    def test_non_lock_failure_fails_fast_with_zero_retries(self, monkeypatch):
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda *_a: (_ for _ in ()).throw(
            AssertionError("should never sleep on a non-lock failure")
        ))
        calls = []

        def invoke():
            calls.append(1)
            return _result(1, "disk full")

        result = git_lock_retry.run_with_lock_retry(invoke)

        assert result.returncode == 1
        assert result.stderr == "disk full"
        assert len(calls) == 1

    def test_exhausted_lock_contention_returns_last_result_without_raising(self, monkeypatch):
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda *_a: None)
        calls = []

        def invoke():
            calls.append(1)
            return _result(128, "fatal: Unable to create '.git/index.lock': File exists.")

        result = git_lock_retry.run_with_lock_retry(invoke)

        assert result.returncode == 128
        assert len(calls) == git_lock_retry.DEFAULT_MAX_ATTEMPTS

    def test_max_attempts_and_backoff_schedule_are_overridable(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda s: sleeps.append(s))
        calls = []

        def invoke():
            calls.append(1)
            return _result(128, "index.lock")

        result = git_lock_retry.run_with_lock_retry(
            invoke, max_attempts=2, backoff_schedule_s=(5.0,)
        )

        assert result.returncode == 128
        assert len(calls) == 2
        assert sleeps == [5.0]

    def test_mismatched_backoff_schedule_raises_value_error(self):
        def invoke():
            return _result(0)

        with pytest.raises(ValueError, match="backoff_schedule_s"):
            git_lock_retry.run_with_lock_retry(
                invoke, max_attempts=4, backoff_schedule_s=(0.1,)
            )

    def test_max_attempts_below_one_raises_value_error(self):
        def invoke():
            return _result(0)

        with pytest.raises(ValueError, match="max_attempts"):
            git_lock_retry.run_with_lock_retry(invoke, max_attempts=0)

    def test_timeout_expired_propagates_immediately_without_retry(self, monkeypatch):
        """Pins finding F3 (P2): `subprocess.TimeoutExpired` raised by `invoke`
        must escape `run_with_lock_retry` on the very first occurrence --
        never swallowed, never treated as lock contention, never retried,
        never burning the remaining attempt budget. The function itself
        never catches exceptions; this pins that its loop structure doesn't
        accidentally start doing so."""
        import subprocess

        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda *_a: (_ for _ in ()).throw(
            AssertionError("should never sleep when invoke raises")
        ))
        calls = []

        def invoke():
            calls.append(1)
            raise subprocess.TimeoutExpired(cmd=["git", "commit"], timeout=30)

        with pytest.raises(subprocess.TimeoutExpired):
            git_lock_retry.run_with_lock_retry(invoke)

        assert len(calls) == 1

    def test_os_error_propagates_immediately_without_retry(self, monkeypatch):
        """Pins finding F3 (P2): `OSError` (e.g. git not on PATH) raised by
        `invoke` must escape `run_with_lock_retry` on the first occurrence,
        same as `TimeoutExpired` -- a raise is not a non-zero returncode and
        is never mistaken for retriable lock contention."""
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda *_a: (_ for _ in ()).throw(
            AssertionError("should never sleep when invoke raises")
        ))
        calls = []

        def invoke():
            calls.append(1)
            raise OSError("git not found")

        with pytest.raises(OSError):
            git_lock_retry.run_with_lock_retry(invoke)

        assert len(calls) == 1

    def test_lock_contention_then_timeout_expired_propagates_mid_schedule(self, monkeypatch):
        """C1's [P2] deferred finding (`coordinator_core.ops.ceremony.
        git_native._git`, reviewer 3119ce2c): the corpus above pins
        `invoke` raising `TimeoutExpired`/`OSError` on its FIRST call
        escaping immediately. This pins the previously-untested mid-
        schedule case -- a lock-contention attempt already burned (so the
        loop is genuinely mid-retry, not first-attempt), then the NEXT
        `invoke` call raises `TimeoutExpired` -- and asserts the raise
        still propagates immediately, from wherever in the schedule it
        happens, rather than being swallowed/retried/converted into
        another lock-contention attempt. This is the exact shape
        `git_native._git` relies on: its own `try/except
        TimeoutExpired/OSError` sits OUTSIDE the `run_with_lock_retry`
        call, so a timeout raised at ANY point in this loop must reach
        that outer `except` on the first occurrence, never be retried by
        this function first."""
        import subprocess

        sleeps: list[float] = []
        monkeypatch.setattr(git_lock_retry.time, "sleep", lambda s: sleeps.append(s))
        calls = []

        def invoke():
            calls.append(1)
            if len(calls) == 1:
                return _result(128, "fatal: Unable to create '.git/index.lock': File exists.")
            raise subprocess.TimeoutExpired(cmd=["git", "commit"], timeout=30)

        with pytest.raises(subprocess.TimeoutExpired):
            git_lock_retry.run_with_lock_retry(invoke)

        # One lock-contention attempt burned (with its one sleep), then the
        # timeout on the SECOND call propagates immediately -- the loop
        # never reaches a third attempt.
        assert len(calls) == 2
        assert sleeps == [git_lock_retry.DEFAULT_BACKOFF_SCHEDULE_S[0]]

    def test_never_calls_subprocess_or_shells_out(self):
        # Structural guard for the module's own negative-spec: it never
        # imports `subprocess` (prose in its own docstring mentions the
        # word, so this checks for the import statement, not bare
        # substring presence).
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(git_lock_retry))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "subprocess" not in imported_names
        assert "subprocess" not in imported_modules
