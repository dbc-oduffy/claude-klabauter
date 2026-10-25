"""With _DECLARED_DELETE_CHANNEL_LIVE off, the guard returns its pre-channel verdicts."""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.bash_guards.tests.test_destructive_action_declared_git_rm import (
    MARK,
    DECLARED,
    _check,
    repo,  # noqa: F401  (fixture)
)


def test_flag_defaults_off():
    assert guard._DECLARED_DELETE_CHANNEL_LIVE is False


def test_off_declared_git_rm_is_default_denied_and_bare_rm_is_not_tracked_denied(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", False)
    assert _check(repo, f"{MARK} git rm -q -- {DECLARED}") is not None
    assert _check(repo, f"{MARK} rm {DECLARED}") is None

    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)
    assert _check(repo, f"{MARK} git rm -q -- {DECLARED}") is None
