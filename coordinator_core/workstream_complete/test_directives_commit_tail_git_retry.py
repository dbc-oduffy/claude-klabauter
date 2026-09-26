
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.workstream_complete import directives_commit_tail as _tail


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(_tail.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def test_transient_failure_then_success_resolves_cleanly(_no_real_sleep, monkeypatch):
    attempts = []

    def _fake_spawn_git(repo_root, args):
        attempts.append(args)
        if len(attempts) < 2:
            return 128, "", "fatal: Unable to create '.git/index.lock': File exists."
        return 0, "ok-stdout", ""

    monkeypatch.setattr(_tail, "_spawn_git", _fake_spawn_git)

    result = _tail._run_git_ok_retrying(Path("/repo"), ["log", "--no-walk"])

    assert result == "ok-stdout"
    assert len(attempts) == 2
    assert _no_real_sleep == [_tail._GIT_RETRY_BACKOFF_SECONDS[0]]


def test_persistent_failure_exhausts_budget_and_returns_none(_no_real_sleep, monkeypatch):
    attempts = []

    def _fake_spawn_git(repo_root, args):
        attempts.append(args)
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(_tail, "_spawn_git", _fake_spawn_git)

    result = _tail._run_git_ok_retrying(Path("/repo"), ["log", "--no-walk"])

    assert result is None
    assert len(attempts) == _tail._GIT_RETRY_ATTEMPTS
    assert _no_real_sleep == list(_tail._GIT_RETRY_BACKOFF_SECONDS)


def test_persistent_failure_still_raises_peer_attribution_unavailable(_no_real_sleep, monkeypatch):
    monkeypatch.setattr(_tail, "_run_git_ok_retrying", lambda *_a, **_k: None)

    with pytest.raises(_tail.PeerAttributionUnavailable):
        _tail._chunked_committed_paths(Path("/repo"), ["deadbeef" * 5])


def test_immediate_success_does_not_sleep(_no_real_sleep, monkeypatch):
    monkeypatch.setattr(_tail, "_spawn_git", lambda repo_root, args: (0, "clean-stdout", ""))

    result = _tail._run_git_ok_retrying(Path("/repo"), ["log", "--no-walk"])

    assert result == "clean-stdout"
    assert _no_real_sleep == []


def test_exhausted_retry_raises_peer_attribution_unavailable_end_to_end(_no_real_sleep, monkeypatch):
    attempts = []

    def _fake_spawn_git(repo_root, args):
        attempts.append(args)
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(_tail, "_spawn_git", _fake_spawn_git)

    with pytest.raises(_tail.PeerAttributionUnavailable):
        _tail._chunked_committed_paths(Path("/repo"), ["deadbeef" * 5])

    assert len(attempts) == _tail._GIT_RETRY_ATTEMPTS


def test_deadline_stops_starting_new_attempts_once_budget_spent(_no_real_sleep, monkeypatch):
    """A slow-attempt path (each attempt costs real wall-clock time, not an
    instant failure) must stop starting new attempts once
    `_GIT_RETRY_DEADLINE_SECONDS` is spent — pins the deadline half of the
    fix, exercised via an injected clock so this test pays no real wait."""
    attempts = []
    clock = [0.0]

    def _fake_spawn_git(repo_root, args):
        attempts.append(args)
        clock[0] += _tail._GIT_RETRY_DEADLINE_SECONDS + 1.0
        return 128, "", "fatal: Unable to create '.git/index.lock': File exists."

    monkeypatch.setattr(_tail, "_spawn_git", _fake_spawn_git)

    result = _tail._run_git_ok_retrying(
        Path("/repo"), ["log", "--no-walk"], now_fn=lambda: clock[0]
    )

    assert result is None
    # be `_GIT_RETRY_ATTEMPTS` (3).
    assert len(attempts) == 1


def test_deadline_does_not_shortcut_fast_attempts_within_budget(_no_real_sleep, monkeypatch):
    """A fast-failing attempt (no real time elapsed) must still get the
    full `_GIT_RETRY_ATTEMPTS` budget — the deadline bounds slow/hung
    attempts, not the ordinary fast-fail case this retry exists to
    absorb."""
    attempts = []

    def _fake_spawn_git(repo_root, args):
        attempts.append(args)
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(_tail, "_spawn_git", _fake_spawn_git)

    result = _tail._run_git_ok_retrying(
        Path("/repo"), ["log", "--no-walk"], now_fn=lambda: 0.0
    )

    assert result is None
    assert len(attempts) == _tail._GIT_RETRY_ATTEMPTS
