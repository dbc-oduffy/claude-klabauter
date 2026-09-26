
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_confined_agent_write as guard
from coordinator_core.write_guards import engine


def _payload(
    repo_root: Path,
    abs_file_path: str,
    tool_name: str = "Write",
    agent_id: str = "areviewer-teammate-1234567890abcdef",
    session_id: str = "sess-12345678",
) -> dict:
    tool_input: dict = {"file_path": abs_file_path}
    if tool_name == "Edit":
        tool_input.update({"old_string": "x", "new_string": "y"})
    elif tool_name == "Write":
        tool_input["content"] = "some content"
    elif tool_name == "MultiEdit":
        tool_input["edits"] = [{"old_string": "x", "new_string": "y"}]
    elif tool_name == "NotebookEdit":
        tool_input = {"notebook_path": abs_file_path, "new_source": "x"}
    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(repo_root),
        "agent_id": agent_id,
        "session_id": session_id,
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE", raising=False)


def _stub_git_root(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


def _stub_subagent_type(subagent_type: str):
    def _fake(git_root, agent_id, expected_em_session_id=""):
        return subagent_type

    return _fake


def _stub_lookup_fail():
    def _fake(git_root, agent_id, expected_em_session_id=""):
        return ""

    return _fake


def _sandbox_path(repo_root: Path, session_id: str, *rel) -> str:
    return str(repo_root / "state" / "subagent-share" / session_id / Path(*rel))


class TestAC1Discovery:
    """Module is discovered by engine._discover_guards(); MATCHERS is the
    engine's full write-shaped matcher set."""

    def test_discovered_by_engine(self):
        names, import_failed = engine.discover_guard_names()
        assert import_failed == []
        assert "block_confined_agent_write" in names

    def test_matchers_is_full_write_shaped_set(self):
        assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class TestInSandboxAllowed:

    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
    def test_in_sandbox_allowed(self, tmp_path, monkeypatch, tool_name):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = _sandbox_path(tmp_path, "sess-12345678", "sidecar.md")
        payload = _payload(tmp_path, target, tool_name=tool_name)
        result = guard.check(payload)
        assert result is None

    def test_in_sandbox_subdirectory_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = _sandbox_path(tmp_path, "sess-12345678", "nested", "notes.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None

    def test_case_varied_sandbox_path_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(
            tmp_path / "STATE" / "Subagent-Share" / "sess-12345678" / "SIDECAR.md"
        )
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None


class TestOutOfSandboxDenied:

    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
    def test_out_of_sandbox_denied(self, tmp_path, monkeypatch, tool_name):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "coordinator_core" / "write_guards" / "engine.py")
        payload = _payload(tmp_path, target, tool_name=tool_name)
        result = guard.check(payload)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert target in hso["permissionDecisionReason"]

    def test_edit_the_confining_bash_guard_itself_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(
            tmp_path
            / "coordinator_core"
            / "bash_guards"
            / "block_reviewer_bash_outside_allowlist.py"
        )
        payload = _payload(tmp_path, target, tool_name="Edit")
        result = guard.check(payload)
        assert result is not None

    def test_another_sessions_sandbox_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = _sandbox_path(tmp_path, "other-session-99999999", "sidecar.md")
        payload = _payload(tmp_path, target, tool_name="Write", session_id="sess-12345678")
        result = guard.check(payload)
        assert result is not None

    def test_outside_repo_path_denied(self, tmp_path, monkeypatch, tmp_path_factory):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        outside_root = tmp_path_factory.mktemp("outside-repo")
        target = str(outside_root / "scratchpad" / "escaped.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is not None

    def test_sid_prefix_collision_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        session_id = "sess-12345678"
        target = _sandbox_path(tmp_path, session_id + "-evil", "sidecar.md")
        payload = _payload(tmp_path, target, tool_name="Write", session_id=session_id)
        result = guard.check(payload)
        assert result is not None


class TestNonConfinedAndFailOpen:

    def test_no_agent_id_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write", agent_id="")
        result = guard.check(payload)
        assert result is None

    @pytest.mark.parametrize(
        "subagent_type",
        ["coordinator:executor", "coordinator:enricher", "coordinator:review-integrator"],
    )
    def test_non_confined_kind_allowed(self, tmp_path, monkeypatch, subagent_type):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type(subagent_type)
        )
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None

    def test_unresolvable_git_root_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd: None)
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None

    def test_backpointer_lookup_failure_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_lookup_fail())
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None

    def test_unresolvable_canonical_id_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write", agent_id="not-a-valid-shape")
        result = guard.check(payload)
        assert result is None

    def test_ambiguous_sentinel_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type("AMBIGUOUS"))
        target = str(tmp_path / "anywhere.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None


class TestDenyMessageRegister:

    def test_deny_message_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "some" / "new" / "file.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]

        # WHAT HAPPENED (one fact) + WHAT TO DO INSTEAD (terse alternative).
        assert target in reason
        assert "sidecar" in reason.lower()

        assert "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE" not in reason

        for banned in ("sorry", "apolog", "real system", "not a refusal"):
            assert banned not in reason.lower()

    def test_deny_message_no_agent_type_leak(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "x.md")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator:code-reviewer" not in reason


class TestToolNameDefenseInDepth:
    """A tool name outside MATCHERS never reaches the identity-resolution
    logic at all, defense-in-depth alongside the engine's own MATCHERS
    filtering."""

    def test_non_matcher_tool_allowed_without_lookup(self, tmp_path, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("should not be called for a non-matcher tool")

        monkeypatch.setattr(guard, "resolve_repo_root", _boom)
        payload = _payload(tmp_path, str(tmp_path / "x.md"), tool_name="Bash")
        result = guard.check(payload)
        assert result is None

    def test_override_env_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE", "1")
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        target = str(tmp_path / "coordinator_core" / "write_guards" / "engine.py")
        payload = _payload(tmp_path, target, tool_name="Write")
        result = guard.check(payload)
        assert result is None


class TestBothSandboxRootsAreHonoured:

    SID = "sess-12345678"

    def _machinery_sidecar(self, repo_root: Path) -> str:
        from coordinator_core.session import machinery_paths

        return str(Path(machinery_paths.share_dir(str(repo_root), self.SID)) / "r.md")

    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
    def test_machinery_root_sidecar_is_allowed(self, tmp_path, monkeypatch, tool_name):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        payload = _payload(
            tmp_path, self._machinery_sidecar(tmp_path), tool_name=tool_name
        )
        assert guard.check(payload) is None

    def test_legacy_root_sidecar_is_still_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        payload = _payload(tmp_path, _sandbox_path(tmp_path, self.SID, "r.md"))
        assert guard.check(payload) is None

    def test_a_path_outside_both_roots_is_still_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        outside = str(tmp_path / "coordinator_core" / "write_guards" / "engine.py")
        verdict = guard.check(_payload(tmp_path, outside, tool_name="Edit"))
        assert verdict is not None
        assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_a_sibling_sessions_sandbox_is_denied_under_the_new_root_too(
        self, tmp_path, monkeypatch
    ):
        from coordinator_core.session import machinery_paths

        monkeypatch.setattr(guard, "resolve_repo_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:code-reviewer"),
        )
        other = str(
            Path(machinery_paths.share_dir(str(tmp_path), "sess-99999999")) / "r.md"
        )
        verdict = guard.check(_payload(tmp_path, other, tool_name="Edit"))
        assert verdict is not None
        assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
