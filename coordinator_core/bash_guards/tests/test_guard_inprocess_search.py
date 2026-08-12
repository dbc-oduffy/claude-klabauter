"""Tests for coordinator_core.bash_guards.guard_inprocess_search (BX-14, AC5/AC10).

Covers the session latch on `_footer()`'s explanatory paragraph
(2026-08-01, `docs/plans/2026-08-01-advisory-firing-shape-predicate.md` C3):
the full ~45-word paragraph fires once per session, every subsequent
answered call in that session still carries the one-line
`_ANSWERED_MARKER` invariant marker (never a bare deny with no
already-handled signal), and every latch failure mode (no session id, no
resolvable repo root, unwritable marker path) fails OPEN toward emitting
the full paragraph rather than crashing the hook.

Deny-versus-substitution contract (2026-08-11, `TestDenyVersusSubstitutionContract`
below) -- driven by `cross-repo/inbox/2026-08-11-example-doctrine-repo-em-guard-unlock-
banner-still-reads-as-agent-instruction.md` Item 2: dispatched reviewers in a
sibling repo met real, correct search results under what read as a bare
denial and had to infer, from position in the text alone, whether "denied"
meant refused or substituted. `guard_inprocess_search.py` now states the
distinction explicitly ("Answered in-process") and composes
`footer + rendered` so that statement leads the composed results, not just
the paragraph in isolation. Modeled on `test_operator_only_disclaimer_leads.py`'s
posture in the same package: a claim that the text says the right thing, or sits
in the right place, is not evidence that it does -- these tests call the real
`check()` and `_footer()` and assert on their real output, and are written to
FAIL against the prior `rendered + footer` composition and bare
`[answered in-process]` marker (see each test's docstring for how that was
established from `git diff` on the guard module).

Lead-word reframe (2026-08-11, this dispatch): the message used to lead with
"Denied as written" -- correct in substance but distrusted in practice; three
independently dispatched agents cross-verified genuine in-process results
against direct `Read` calls rather than trust a message that opened with
"Denied". The composed text now leads with "Answered in-process" and mentions
the no-subprocess-ran fact second, not first -- the tests below assert both
the new lead text and that the no-subprocess/not-a-refusal fact still
precedes the rendered results.

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

from coordinator_core.bash_guards import _verdict
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
        assert "recognized as a search" in reason
        assert guard._ANSWERED_MARKER not in reason

    def test_second_call_same_session_carries_marker_not_paragraph(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-2")
        guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        reason = _deny_reason(second)
        assert "recognized as a search" not in reason
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
        assert "recognized as a search" in reasons[0]
        for reason in reasons[1:]:
            assert guard._ANSWERED_MARKER in reason

    def test_latch_is_marker_file_scoped_to_session_id(self, repo, monkeypatch):
        """A DIFFERENT session id in the same repo gets its own full
        paragraph -- the latch is per-(repo, session), not global."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-A")
        guard.check(_payload("grep foo bar.py", str(repo)))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-B")
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        assert "recognized as a search" in _deny_reason(result)

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
        assert "recognized as a search" in _deny_reason(first)
        assert "recognized as a search" in _deny_reason(second)

    def test_current_session_id_sentinel_file_is_never_consulted(self, repo, monkeypatch, tmp_path):
        """SC-DR-009: `.current-session-id` is documented last-writer-wins
        under concurrency and is not an acceptable fallback for this latch
        -- presence of that sentinel must not influence latching at all."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        (repo / ".current-session-id").write_text("some-other-session", encoding="utf-8")
        first = guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        assert "recognized as a search" in _deny_reason(first)
        assert "recognized as a search" in _deny_reason(second)

    def test_unresolvable_repo_root_fails_open_to_full_paragraph(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-noroot")
        no_git_dir = tmp_path / "not-a-repo"
        no_git_dir.mkdir()
        result = guard.check(_payload("grep foo bar.py", str(no_git_dir)))
        assert "recognized as a search" in _deny_reason(result)

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
        assert "recognized as a search" in _deny_reason(result)

    def test_stat_failure_on_read_fails_open_to_full_paragraph(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-latch-statfail")

        class _BoomPath:
            def is_file(self):
                raise OSError("simulated stat failure")

        monkeypatch.setattr(guard, "_latch_path", lambda cwd, sid: _BoomPath())
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        assert "recognized as a search" in _deny_reason(result)


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


class TestPowerShellDialectStaysBashOnly:
    """Row 21, `docs/reference/guard-dialect-coverage.md` -- disposition is
    "stay bash-only (c), declare SILENT", by design, not bigger-than-sized
    fixed. Never delegates to `search.answer` for a PowerShell command."""

    def test_powershell_command_returns_none_and_records_silent(self, repo):
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {"command": "Select-String -Pattern foo -Path bar.py"},
            "cwd": str(repo),
        }
        with _verdict.collecting() as silences:
            result = guard.check(payload)
        assert result is None
        assert _verdict.was_silent("guard_inprocess_search", silences)


class TestDenyVersusSubstitutionContract:
    """2026-08-11 -- `_footer()`'s full paragraph and `_ANSWERED_MARKER`'s
    latched short form must each independently state the deny-versus-
    substitution contract, and that statement must precede the rendered
    search results in `check()`'s composed payload -- see module docstring.

    Each test here is written to fail against the PRIOR text, established by
    reading `git diff coordinator_core/bash_guards/guard_inprocess_search.py`:
    the old `_ANSWERED_MARKER` was the bare string `"[answered in-process]"`
    (no "not a refusal" framing at all), and `check()` used to compose
    `"%s\\n\\n%s" % (rendered, _footer(cwd))` -- the caller's answer FIRST,
    the already-handled framing LAST. Both old shapes fail the assertions
    below; the new ones are calls into the real `check()`/`_footer()`, not
    a re-read of the source.
    """

    def test_full_paragraph_contract_precedes_rendered_results(self, repo, monkeypatch):
        """First-call composed payload: the explanatory paragraph's
        deny-versus-substitution statement must appear BEFORE the rendered
        search output. Fails against the old `rendered + footer` composition,
        which would put "ANSWERED-TEXT" first."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-contract-full")
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        reason = _deny_reason(result)
        assert "ANSWERED-TEXT" in reason
        assert reason.index("in-process") < reason.index("ANSWERED-TEXT"), (
            "the deny-versus-substitution contract must precede the "
            "rendered results -- got: %r" % reason
        )

    def test_full_paragraph_states_the_contract_explicitly(self, repo, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-contract-full-text")
        result = guard.check(_payload("grep foo bar.py", str(repo)))
        reason = _deny_reason(result)
        assert "Answered in-process" in reason
        assert "recognized as a search" in reason
        assert not reason.startswith("[Denied")

    def test_latched_marker_states_the_contract_independently(self):
        """The short latched form, taken alone (never composed with
        `check()`), must carry the same distinction as the full paragraph --
        this is the framing an agent gets on every call after the first in a
        session. Fails against the old bare `"[answered in-process]"` marker,
        which named the mechanism but not the deny-versus-substitution
        contract at all."""
        assert "Answered in-process" in guard._ANSWERED_MARKER
        assert "no subprocess spawned" in guard._ANSWERED_MARKER
        assert guard._ANSWERED_MARKER != "[answered in-process]"
        assert not guard._ANSWERED_MARKER.startswith("[Denied")

    def test_latched_marker_contract_precedes_rendered_results(self, repo, monkeypatch):
        """Second-call (latched) composed payload: the marker's contract
        statement must precede the rendered results, mirroring the
        first-call ordering requirement above. Fails against the old
        `rendered + footer` composition regardless of marker wording."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-contract-latched")
        guard.check(_payload("grep foo bar.py", str(repo)))
        second = guard.check(_payload("grep baz qux.py", str(repo)))
        reason = _deny_reason(second)
        assert guard._ANSWERED_MARKER in reason
        assert "ANSWERED-TEXT" in reason
        assert reason.index("in-process") < reason.index("ANSWERED-TEXT"), (
            "the latched marker's contract must precede the rendered "
            "results -- got: %r" % reason
        )
