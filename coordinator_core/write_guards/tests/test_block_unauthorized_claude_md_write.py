"""Behavioral tests for
coordinator_core.write_guards.block_unauthorized_claude_md_write -- the
CLAUDE.md-class write guard DR-104 (2026-07-27) reintroduces over DR-058
for one path class only (see the module's own docstring).

Three tests here are load-bearing acceptance criteria per DoE-claude
docs/plans/2026-07-27-claude-md-altitude-triage.md § C4, not coverage:

  AC8 (TestSubagentOriginatedDenied) -- a SUBAGENT-originated payload
      (agent_id present) is denied absent a grant. This is the case the
      +27% growth this guard exists to stop would have needed -- a guard
      that only fires EM-inline is worthless here.

  AC9 (TestRealScopeEqualsStatedScope) -- real scope equals stated scope,
      per path class. Reuses the shape pinned in
      coordinator_core/bash_guards/tests/test_check_blanket_git_add.py
      (the check_blanket_git_add scope-gap this guard's own negative-spec
      names as precedent): assert DENY for every path class this guard's
      docstring claims to cover, and ALLOW for a representative
      NOT-covered case, so the stated scope and the enforced scope are
      pinned against each other rather than merely described in prose.

  AC10 (TestDenyTextNamesAlternativeAndOverride) -- the EMITTED deny text
      names a concrete alternative (the discharge hierarchy, DEC-6) and
      the override path (the C5 grant CLI). Asserted against the RENDERED
      STRING returned by ``check()``, never against the code that builds
      it.

Seam: ``check_claude_md_write_grant`` is monkeypatched directly (module
import binding) to avoid any real ``.git/coordinator-sessions/`` fixture --
this file is about the guard's OWN detection/scope/deny-text behavior, not
the grant module's own persistence semantics (already covered by
``coordinator_core/session/tests/test_claude_md_grant.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.write_guards import block_unauthorized_claude_md_write as guard
from coordinator_core.win_portability import no_console_passthrough_kwargs

# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _payload(
    file_path: str,
    *,
    agent_id: str = "aexecutor-teammate-1234567890abcdef",
    tool_name: str = "Edit",
    cwd: str = "/repo",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "cwd": cwd,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _no_grant(monkeypatch):
    monkeypatch.setattr(guard, "check_claude_md_write_grant", lambda cwd: (False, None))


def _deny(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


class TestSubagentOriginatedDenied:
    def test_subagent_write_to_claude_md_denied(self, monkeypatch):
        _deny(monkeypatch, "CLAUDE.md")

    def test_subagent_edit_to_claude_md_denied(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md", tool_name="Edit"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_em_inline_write_allowed_no_agent_id(self, monkeypatch):
        """No agent_id -> EM-inline write -> always allow, even absent a
        grant -- this guard governs the DISPATCHED path only (DR-104's own
        new evidence is specifically about the executor path, not
        EM-inline authoring)."""
        _allow(monkeypatch, "CLAUDE.md", agent_id="")

    def test_subagent_write_allowed_with_live_grant(self, monkeypatch):
        monkeypatch.setattr(
            guard, "check_claude_md_write_grant", lambda cwd: (True, {"granted_by": "pm"})
        )
        _allow(monkeypatch, "CLAUDE.md")

    def test_subagent_write_allowed_with_override_env(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow(monkeypatch, "CLAUDE.md")

    def test_non_write_tool_allowed(self, monkeypatch):
        _allow(monkeypatch, "CLAUDE.md", tool_name="Read")


# explicit ``session_id``), still authorizes a SUBAGENT-shaped payload on


def _make_repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _live_session(repo, sid):
    from coordinator_core.session import core as session_core

    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps({"pid": "999", "last_activity": session_core.now_iso()}) + "\n",
        encoding="utf-8",
    )
    return sdir


class TestSubagentInheritsEmAcquiredGrant:

    def test_em_acquired_grant_authorizes_subagent_write(self, tmp_path, monkeypatch):
        from coordinator_core.session import claude_md_grant as cmg

        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "ac8-inherit-session")
        _live_session(repo, "ac8-inherit-session")

        granted = cmg.write_claude_md_write_grant(
            "pm", "PM said go ahead this session", cwd=str(repo)
        )
        assert granted is True

        monkeypatch.setattr(guard, "check_claude_md_write_grant", cmg.check_claude_md_write_grant)

        target = repo / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(repo),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is None, f"expected ALLOW (inherited EM grant), got {result!r}"

    def test_subagent_write_denied_absent_the_grant(self, tmp_path, monkeypatch):
        from coordinator_core.session import claude_md_grant as cmg

        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "ac8-inherit-session-negative")
        _live_session(repo, "ac8-inherit-session-negative")

        monkeypatch.setattr(guard, "check_claude_md_write_grant", cmg.check_claude_md_write_grant)

        target = repo / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(repo),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None, "expected DENY absent any grant on disk"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestRealScopeEqualsStatedScope:

    @pytest.mark.parametrize(
        "file_path",
        [
            "CLAUDE.md",
            "coordinator/CLAUDE.md",
            "global-doctrine/CLAUDE.md",
            "coordinator/templates/CLAUDE.md.tmpl",
            "some/nested/repo/CLAUDE.md",
        ],
    )
    def test_claude_md_class_paths_all_denied(self, monkeypatch, file_path):
        _deny(monkeypatch, file_path)

    def test_self_referential_scope_doe_claude_own_repo_root_claude_md_denied(self, monkeypatch):
        monkeypatch.setattr(guard, "_is_growth", lambda *a, **kw: True)
        _deny(monkeypatch, "CLAUDE.md", cwd="/Users/alice/X/DoE-claude")

    @pytest.mark.parametrize(
        "file_path",
        [
            "docs/wiki/some-page.md",
            "docs/plans/2026-07-27-x.md",
            "coordinator/CLAUDE.local.md",
            "README.md",
            "src/claude_md_helper.py",
        ],
    )
    def test_non_claude_md_class_paths_allowed(self, monkeypatch, file_path):
        _allow(monkeypatch, file_path)

    def test_backslash_path_still_matched(self, monkeypatch):
        _deny(monkeypatch, "coordinator\\CLAUDE.md")


class TestDenyTextNamesAlternativeAndOverride:
    def test_deny_text_names_the_discharge_hierarchy(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "discharge" in reason.lower()
        assert "mechanize" in reason.lower()
        assert "wiki" in reason.lower()

    def test_deny_text_does_not_presuppose_wiki_as_default_alternative(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "document-bloat-trim.md names the default fold target" not in reason
        assert "default fold target" not in reason

    def test_deny_text_no_longer_names_the_grant_cli_override_path(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_deny_text_no_longer_attributes_a_grant_command_to_the_em(
        self, monkeypatch
    ):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Report BLOCKED to your EM" in reason
        assert "EM runs this, not you" not in reason
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_deny_text_no_longer_names_the_rare_use_env_override(self, monkeypatch):
        """The env-override affordance is likewise gone from the rendered
        deny text (see C4(b)) -- ``_OVERRIDE_ENV_VAR`` stays wired in
        ``check()`` (checked first, defense-in-depth) but is no longer
        advertised to the dispatched subagent this deny addresses."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE=1" not in reason
        assert guard._OVERRIDE_ENV_VAR not in reason

    def test_deny_text_names_the_target_path(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator/CLAUDE.md" in reason

    def test_grant_cli_interpolates_the_resolved_claude_klabauter_root(self, monkeypatch):
        import coordinator_core.engine_root as mr

        monkeypatch.setattr(mr, "coordinator_engine_root", lambda: "/opt/some/claude-klabauter")
        assert guard._grant_cli_invocation() == (
            'PYTHONPATH="/opt/some/claude-klabauter" '
            'python3 -m coordinator_core.session.claude_md_grant grant pm '
            '"<verbatim PM note>"'
        )

    def test_grant_cli_falls_back_when_root_unresolvable(self, monkeypatch):
        import coordinator_core.engine_root as mr

        def _raise():
            raise RuntimeError("cannot resolve CLAUDE_KLABAUTER_ROOT")

        monkeypatch.setattr(mr, "coordinator_engine_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_falls_back_on_empty_root(self, monkeypatch):
        import coordinator_core.engine_root as mr

        monkeypatch.setattr(mr, "coordinator_engine_root", lambda: "")
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_never_propagates_an_unexpected_resolver_error(
        self, monkeypatch, capsys
    ):
        import coordinator_core.engine_root as mr

        def _raise():
            raise AttributeError("resolver drifted")

        monkeypatch.setattr(mr, "coordinator_engine_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK
        assert "could not resolve the claude-klabauter root" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "hostile_root",
        ['/opt/mak"ima', "/opt/$(whoami)", "/opt/mak`ima`", "/opt/mak\nima"],
    )
    def test_grant_cli_falls_back_on_a_shell_unsafe_root(
        self, monkeypatch, hostile_root
    ):
        import coordinator_core.engine_root as mr

        monkeypatch.setattr(mr, "coordinator_engine_root", lambda: hostile_root)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_deny_text_never_dead_ends(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Report BLOCKED to your EM" in reason
        assert "Target:" in reason
        assert "Reason:" in reason

    def test_deny_text_actionable_line_opens_the_shared_cue_window(self, monkeypatch):
        """Regression pin for a P1 review-integration finding: the deny
        text's own docstring claims the target path, grant command, and
        grant precondition sit inside a cue window that exempts them from
        the C8 byte-prose cap -- but that claim is only true if the actual
        rendered text matches ``_CUE_WINDOW_RE``
        (``coordinator_core.bash_guards._alternative_liveness``). A reword
        that drops the matching phrase (as happened here once already)
        silently moves ~150+ bytes of exempt content into the counted-prose
        budget with no test failure anywhere else in this file to catch it.
        """
        from coordinator_core.bash_guards._alternative_liveness import _CUE_WINDOW_RE

        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        actionable_line = "Report BLOCKED to your EM instead:"
        assert actionable_line in reason
        assert _CUE_WINDOW_RE.search(actionable_line), (
            "the deny text's actionable line no longer matches the shared "
            "cue-window regex -- the target path/grant command/precondition "
            "that follow it will render as counted prose, not exempt "
            "cue-window content"
        )

    def test_deny_text_names_the_structural_reason(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator/CLAUDE.md" in reason
        assert "needs a live CLAUDE.md write grant for this session" in reason


class TestDirectionalDenyGrowthOnly:
    def test_write_that_grows_the_file_is_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("short")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "a much longer replacement body"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_write_that_shrinks_the_file_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "additionalContext" in hso

    def test_write_that_is_size_neutral_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("abcde")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "vwxyz"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_edit_that_grows_the_file_is_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("hello world")
        payload = _payload(str(target), cwd=str(tmp_path))
        payload["tool_input"] = {"file_path": str(target), "old_string": "world", "new_string": "a much longer replacement string"}
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_edit_that_shrinks_the_file_is_advised_not_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("hello a much longer original string")
        payload = _payload(str(target), cwd=str(tmp_path))
        payload["tool_input"] = {
            "file_path": str(target),
            "old_string": "a much longer original string",
            "new_string": "x",
        }
        result = guard.check(payload)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert "additionalContext" in hso

    def test_advisory_no_longer_names_the_grant_cli_invocation(self, monkeypatch, tmp_path):
        """C4(c), docs/plans/2026-08-13-guard-messages-stop-handing-agents-
        the-keys.md (AC-1/AC-2): this leg fires only when ``agent_id`` is
        present -- i.e. only for a dispatched subagent, the exact audience
        forbidden from seeing an unlock statement in any shape. The
        resolved ``PYTHONPATH=... python3 -m
        coordinator_core.session.claude_md_grant grant pm`` invocation used
        to render here unconditionally (the same "shown the button, told
        not to press it" shape the deny leg's C4(b) reshape already closed)
        -- this is that same closure landing on the advisory leg."""
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason
        assert "PYTHONPATH" not in reason

    def test_advisory_still_attributes_the_grant_step_to_the_em_not_the_reader(
        self, monkeypatch, tmp_path
    ):
        target = tmp_path / "CLAUDE.md"
        target.write_text("a much longer original body")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "short"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "the EM" in reason
        assert "not this agent's" in reason
        assert "coordinator_core.session.claude_md_grant grant pm" not in reason

    def test_new_file_creation_is_always_growth_and_denied(self, monkeypatch, tmp_path):
        target = tmp_path / "CLAUDE.md"
        assert not target.exists()
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "brand new content"},
            "cwd": str(tmp_path),
            "agent_id": "aexecutor-teammate-1234567890abcdef",
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
