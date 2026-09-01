"""Behavioral tests for
coordinator_core.write_guards.block_foreign_family_sidecar_write -- the
sidecar-leaf-is-written-only-by-the-agent-it-names guard.

Per the chunk brief (state/dispatch-briefs/2026-09-01-a-sidecar-leaf-is-
written-only-by-the-agent-it-names/C1.md), each fail-open arm is reached by
making the back-pointer genuinely unreadable on a real temp git root --
never by monkeypatching ``_read_backpointer_subagent_type``'s return value
directly -- so these tests build real
``.git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt`` and
``.git/coordinator-sessions/<em_sid>/dispatched-agents.txt`` fixtures on
``tmp_path``. Only the repo-root RESOLUTION itself (``resolve_repo_root``,
a distinct concern from the back-pointer chain -- "which repo", not
"what does this agent's back-pointer say") is stubbed to point at
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_foreign_family_sidecar_write as guard

_AGENT_A = "aaaaaaaaaaaaaaaa"  # bare-hex, 16 chars
_AGENT_B = "bbbbbbbbbbbbbbbb"  # bare-hex, 16 chars
_EM_SESSION_ID = "em-session-12345678"
_SESSION_ID = "sess-abcdef12"


def _payload(
    repo_root: Path,
    rel_file_path: str,
    agent_id: str = "",
    session_id: str = _SESSION_ID,
    tool_name: str = "Write",
) -> dict:
    payload: dict = {
        "tool_name": tool_name,
        "tool_input": {"file_path": rel_file_path, "content": "x"},
        "cwd": str(repo_root),
        "session_id": session_id,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def _write_backpointer(
    repo_root: Path,
    agent_id: str,
    em_session_id: str,
    dispatched_rows: list[tuple[str, str, str]] | None = None,
    dispatched_agents_present: bool = True,
) -> None:
    """Build a real ``.agents/<agent_id>/em-session-id.txt`` ->
    ``dispatched-agents.txt`` chain under ``repo_root/.git/coordinator-sessions/``.

    ``dispatched_rows`` are ``(agent_id, col2, subagent_type)`` 3-column
    rows written tab-separated; pass multiple rows for the same
    ``agent_id`` to exercise the ambiguous-match fail-closed-to-"" arm.
    """
    agents_dir = repo_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text(em_session_id + "\n", encoding="utf-8")

    if not dispatched_agents_present:
        return

    session_dir = repo_root / ".git" / "coordinator-sessions" / em_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    rows = dispatched_rows if dispatched_rows is not None else []
    lines = "\n".join("\t".join(row) for row in rows)
    (session_dir / "dispatched-agents.txt").write_text(
        lines + ("\n" if lines else ""), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _stub_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: str(tmp_path))
    return tmp_path


class TestLeg1Applicability:
    """Pure-string gate -- never reaches identity resolution."""

    def test_path_outside_sidecar_dir_allows_without_io(self, tmp_path, monkeypatch):
        def _boom(cwd):
            raise AssertionError("leg 1 should have short-circuited before repo-root resolution")

        monkeypatch.setattr(guard, "resolve_repo_root", _boom)
        payload = _payload(tmp_path, "docs/plans/some-plan.md", agent_id=_AGENT_A)
        assert guard.check(payload) is None

    def test_leaf_missing_dot_separator_allows(self, tmp_path, monkeypatch):
        def _boom(cwd):
            raise AssertionError("leg 1 should have short-circuited before repo-root resolution")

        monkeypatch.setattr(guard, "resolve_repo_root", _boom)
        payload = _payload(
            tmp_path, f"state/subagent-share/{_SESSION_ID}/notes.md", agent_id=_AGENT_A
        )
        assert guard.check(payload) is None

    def test_leaf_not_md_allows(self, tmp_path, monkeypatch):
        def _boom(cwd):
            raise AssertionError("leg 1 should have short-circuited before repo-root resolution")

        monkeypatch.setattr(guard, "resolve_repo_root", _boom)
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_A}.txt",
            agent_id=_AGENT_A,
        )
        assert guard.check(payload) is None


class TestEmMainLoopWrite:
    def test_no_agent_id_allows(self, tmp_path):
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_A}.md",
            agent_id="",
        )
        assert guard.check(payload) is None


class TestDeny:
    def test_same_family_different_member_denied(self, tmp_path):
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            _EM_SESSION_ID,
            dispatched_rows=[(_AGENT_A, "x", "coordinator:executor")],
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert _AGENT_A in reason
        assert "coordinator:executor" in reason


class TestCarveOut1LabelMismatch:
    """An integrator writing a REVIEWER's sidecar -- different family, allow."""

    def test_integrator_writing_reviewer_sidecar_allowed(self, tmp_path):
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            _EM_SESSION_ID,
            dispatched_rows=[(_AGENT_A, "x", "coordinator:review-integrator")],
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorcode-reviewer.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None


class TestCarveOut2IdMatch:
    """A code-reviewer writing its own sidecar (same label, same id) -- allow."""

    def test_agent_writing_own_sidecar_allowed(self, tmp_path):
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            _EM_SESSION_ID,
            dispatched_rows=[(_AGENT_A, "x", "coordinator:code-reviewer")],
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorcode-reviewer.{_AGENT_A}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None


class TestCarveOut3FailOpenArms:
    """Each arm reached by a genuinely unreadable back-pointer on real
    tmp_path filesystem state, never by monkeypatching the resolver's
    return value.
    """

    def test_missing_backpointer_file_entirely_allows(self, tmp_path):
        # No .agents/<agent_id>/em-session-id.txt written at all.
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None

    def test_backpointer_names_different_session_than_caller_allows(self, tmp_path):
        # The back-pointer resolves a REAL em session id, but not the one
        # making the call -- expected_em_session_id mismatch.
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            "some-other-em-session",
            dispatched_rows=[(_AGENT_A, "x", "coordinator:executor")],
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None

    def test_missing_dispatched_agents_file_allows(self, tmp_path):
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            _EM_SESSION_ID,
            dispatched_agents_present=False,
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None

    def test_ambiguous_dispatched_agents_row_allows(self, tmp_path):
        _write_backpointer(
            tmp_path,
            _AGENT_A,
            _EM_SESSION_ID,
            dispatched_rows=[
                (_AGENT_A, "x", "coordinator:executor"),
                (_AGENT_A, "y", "coordinator:code-reviewer"),
            ],
        )
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/coordinatorexecutor.{_AGENT_B}.md",
            agent_id=_AGENT_A,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None


class TestAcceptanceOracle:
    """The 2026-08-31 incident itself, replayed.

    Not a synthetic fixture: the caller id, the resolved dispatched type and
    the target leaf below are the real values recorded on the incident,
    filed at ``state/bug-backlog/2026-08-31-missing-sidecar-provisioning-
    sends-an-integrator-receipt-into-a-siblings-file.yaml``. Discharges exit
    criterion 2 of docs/plans/2026-09-01-a-sidecar-leaf-is-written-only-by-
    the-agent-it-names.md -- the guard must deny the write that actually
    happened, and allow the write that should have happened instead.
    """

    _CALLER = "a13be9aa2ab0dd63f"
    _SIBLING = "a200555c09c99b946"
    _TYPE = "coordinator:review-integrator"
    _LABEL = "coordinatorreview-integrator"

    def _backpointer(self, tmp_path):
        _write_backpointer(
            tmp_path,
            self._CALLER,
            _EM_SESSION_ID,
            dispatched_rows=[(self._CALLER, "x", self._TYPE)],
        )

    def test_the_write_that_happened_is_denied(self, tmp_path):
        self._backpointer(tmp_path)
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/{self._LABEL}.{self._SIBLING}.md",
            agent_id=self._CALLER,
            session_id=_EM_SESSION_ID,
        )
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_the_write_that_should_have_happened_is_allowed(self, tmp_path):
        self._backpointer(tmp_path)
        payload = _payload(
            tmp_path,
            f"state/subagent-share/{_SESSION_ID}/{self._LABEL}.{self._CALLER}.md",
            agent_id=self._CALLER,
            session_id=_EM_SESSION_ID,
        )
        assert guard.check(payload) is None

    @pytest.mark.parametrize(
        "separator_form",
        [
            "state/subagent-share/{sid}/{label}.{sibling}.md",
            r"state\subagent-share\{sid}\{label}.{sibling}.md",
        ],
        ids=["posix-separators", "windows-separators"],
    )
    def test_same_target_denies_in_both_separator_forms(
        self, tmp_path, separator_form
    ):
        """Windows is first-class: the incident's own path arrives
        backslashed on one host and slashed on another, and a guard that
        only matched one form would fail open on exactly half the fleet.
        """
        self._backpointer(tmp_path)
        payload = _payload(
            tmp_path,
            separator_form.format(
                sid=_SESSION_ID, label=self._LABEL, sibling=self._SIBLING
            ),
            agent_id=self._CALLER,
            session_id=_EM_SESSION_ID,
        )
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
