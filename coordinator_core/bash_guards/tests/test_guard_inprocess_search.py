"""Tests for coordinator_core.bash_guards.guard_inprocess_search (BX-14, AC5/AC10).

Covers the session latch on `_footer()`'s explanatory paragraph
(2026-08-01, `docs/plans/2026-08-01-advisory-firing-shape-predicate.md` C3):
the full ~45-word paragraph fires once per session, every subsequent
answered call in that session still carries the one-line
`[answered in-process]` invariant marker (never a bare deny with no
already-handled signal), and every latch failure mode (no session id, no
resolvable repo root, unwritable marker path) fails OPEN toward emitting
the full paragraph rather than crashing the hook.

Pure Python -- no real shell spawns. `coordinator_core.search.answer.answer`
is monkeypatched to a fixed non-None return so `check()` reaches `_footer()`
without depending on the real search engine's own behavior (that engine has
its own test module). The git repo root is a real `tmp_path` directory with
a plain `.git` subdirectory -- exercising `_repo_root_from_cwd` and
`resolve_git_common_dir`'s ordinary-clone leg for real, not mocked.

Spec backlink: coordinator_core/bash_guards/guard_inprocess_search.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_inprocess_search as guard


def _payload(command: str, cwd: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }


@pytest.fixture(autouse=True)
def _fixed_answer(monkeypatch):
    """Every command in this module reaches `_footer()` unconditionally --
    the search engine's own answer text is not under test here."""
    monkeypatch.setattr(
        "coordinator_core.search.answer.answer",
        lambda command, cwd=".": "ANSWERED-TEXT",
    )


@pytest.fixture
def repo(tmp_path):
    """A plain-clone `.git` directory (the ordinary-clone leg of
    `resolve_git_common_dir`) so the latch has somewhere real to land."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _deny_reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class TestSessionLatchDedupesTheParagraph:
    def test_first_call_carries_full_paragraph(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-1")
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        reason = _deny_reason(result)
        assert "Search already answered in-process" in reason
        assert guard._ANSWERED_MARKER not in reason

    def test_second_call_same_session_carries_marker_not_paragraph(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-2")
        guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        reason = _deny_reason(second)
        assert "Search already answered in-process" not in reason
        assert guard._ANSWERED_MARKER in reason

    def test_every_answered_call_carries_an_already_handled_signal(self, repo, monkeypatch):
        """First, second, and Nth calls all carry SOME unambiguous
        already-handled signal -- never a bare deny with no framing at all
        (the module docstring's "deny here means ALREADY HANDLED, not
        refused" contract)."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-3")
        reasons = [
            _deny_reason(guard.check(_payload("grep %d foo.py" % i, str(repo))))
            for i in range(4)
        ]
        assert "Search already answered in-process" in reasons[0]
        for reason in reasons[1:]:
            assert guard._ANSWERED_MARKER in reason

    def test_latch_is_marker_file_scoped_to_session_id(self, repo, monkeypatch):
        """A DIFFERENT session id in the same repo gets its own full
        paragraph -- the latch is per-(repo, session), not global."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-A")
        guard.check(_payload("grep foo bar.py", str(repo)))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-B")
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        assert "Search already answered in-process" in _deny_reason(result)

    def test_marker_survives_spawn_per_call_reinvocation(self, repo, monkeypatch):
        """The latch is disk state, not an in-process cache -- calling
        `_footer` directly (simulating a fresh process reading the same
        marker `check()` wrote) still sees it latched."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-spawn")
        guard.check(_payload("grep foo bar.py", str(repo)))
        assert guard._footer(str(repo)) == guard._ANSWERED_MARKER


class TestFailsOpenTowardTheFullParagraph:
    def test_no_session_id_env_var_never_latches(self, repo, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        first = guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        assert "Search already answered in-process" in _deny_reason(first)
        assert "Search already answered in-process" in _deny_reason(second)

    def test_current_session_id_sentinel_file_is_never_consulted(self, repo, monkeypatch, tmp_path):
        """SC-DR-009: `.current-session-id` is documented last-writer-wins
        under concurrency and is not an acceptable fallback for this latch
        -- presence of that sentinel must not influence latching at all."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        (repo / ".current-session-id").write_text("some-other-session", encoding="utf-8")
        first = guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        assert "Search already answered in-process" in _deny_reason(first)
        assert "Search already answered in-process" in _deny_reason(second)

    def test_unresolvable_repo_root_fails_open_to_full_paragraph(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-noroot")
        no_git_dir = tmp_path / "not-a-repo"
        no_git_dir.mkdir()
        result = guard.check(_payload("grep foo bar.py", str(no_git_dir)))
        assert "Search already answered in-process" in _deny_reason(result)

    def test_unwritable_marker_parent_fails_open_never_raises(self, repo, monkeypatch):
        """A latch WRITE failure (read-only `.git`, MinGit permissions) must
        never crash the hook -- `_footer` degrades to the full paragraph."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-unwritable")
        monkeypatch.setattr(
            guard,
            "_latch_path",
            lambda cwd, sid: (_ for _ in ()).throw(OSError("simulated unwritable path")),
        )
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        assert "Search already answered in-process" in _deny_reason(result)

    def test_stat_failure_on_read_fails_open_to_full_paragraph(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-statfail")

        class _BoomPath:
            def is_file(self):
                raise OSError("simulated stat failure")

        monkeypatch.setattr(guard, "_latch_path", lambda cwd, sid: _BoomPath())
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        assert "Search already answered in-process" in _deny_reason(result)


class TestHelpers:
    def test_repo_root_from_cwd_walks_up_to_dot_git(self, repo):
        nested = repo / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert guard._repo_root_from_cwd(str(nested)) == str(repo)

    def test_repo_root_from_cwd_returns_none_when_no_git_found(self, tmp_path):
        lone = tmp_path / "no-repo-here"
        lone.mkdir()
        assert guard._repo_root_from_cwd(str(lone)) is None

    def test_latch_path_lands_under_coordinator_sessions_subtree(self, repo):
        path = guard._latch_path(str(repo), "sess-x")
        assert path is not None
        assert path.parts[-3:] == ("coordinator-sessions", "sess-x", guard._LATCH_MARKER_NAME)
