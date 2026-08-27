"""Behavioral tests for coordinator_core.write_guards.block_confined_agent_write
-- the confined-findings-agent sandbox-containment guard (see the module's
own docstring for the probe this discharges:
cross-repo/inbox/2026-08-17-example-cockpit-repo-em-agent-tools-declaration-not-
enforced.md, and the shape defect the rewrite closes:
docs/plans/2026-08-17-confined-findings-agents-cannot-write.md).

Mirrors the fixture and payload-building style of
test_block_subagent_plan_body_write.py: the back-pointer subagent_type
lookup and git-root resolution are monkeypatched directly rather than
exercised via real ``.git/coordinator-sessions/`` fixtures -- these tests
are about this guard's own matcher scope, identity-gate, containment, and
fail-open discipline, not the shared identity-resolution chain (already
covered by its own module's tests).
"""

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
    """A confined agent's write-shaped call INSIDE its own sandbox
    (state/subagent-share/<session_id>/) is allowed, on every matcher."""

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
        """A write whose target differs from the sandbox root only in the
        case of the ``state``/``subagent-share`` segments is still allowed --
        pins ``casefold_path`` wiring behaviorally (both operands routed
        through it, per the guard's own module docstring), not merely by
        import presence. ``casefold_path`` itself is unit-tested elsewhere;
        this is defense-in-depth at the call site.

        Platform-honest on BOTH case-sensitive and case-insensitive
        filesystems: the guard casefolds ``sandbox_root`` and
        ``candidate_raw`` to plain lowercase STRINGS before either is ever
        wrapped in ``Path(...)`` (see ``block_confined_agent_write.py``
        lines computing ``sandbox_root``/``candidate``). Neither of these
        case-varied path segments is created on disk by this test, so
        ``contained_path``'s internal ``.resolve()`` has nothing to
        case-correct against a real directory entry -- it only normalizes
        the already-casefolded strings syntactically. The outcome therefore
        does not depend on the host filesystem's own case-sensitivity, only
        on the guard's own pre-processing step actually running.
        """
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
    """A confined agent's write-shaped call OUTSIDE its own sandbox is
    denied, on every matcher -- including Edit, the hole the rewrite closes."""

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
        """The concrete probe from the dispatch brief: an unconfined Edit
        could modify the guard that confines this agent's OWN Bash surface."""
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
        """A different EM session's sandbox directory is not this agent's
        own sandbox, even though it lives under the same sandbox parent."""
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
        """A path outside the git tree entirely (e.g. a scratchpad sibling)
        is denied -- the original probe wrote to exactly this shape and
        succeeded; that hole must now be closed."""
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
        """Regression pin for the prefix-collision class: a sandbox segment
        named ``<sid>-evil`` -- which has the real session id as a literal
        string prefix -- is NOT this agent's own sandbox and must be denied.

        Containment here is structurally immune to this class today: it
        routes through ``contained_path``'s ``Path.relative_to()``
        (segment-aware), never a ``str.startswith()`` comparison, so
        ``.../sess-12345678-evil/...`` cannot satisfy containment under
        ``.../sess-12345678/`` regardless of the specific id string. This
        test exists so a FUTURE refactor that reintroduced a string-prefix
        check would fail a colocated test instead of failing silently --
        do not delete it as redundant with ``test_another_sessions_sandbox_
        denied`` above; that test covers an unrelated sandbox id, this one
        specifically pins the prefix-superset shape.
        """
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
    """EM main-loop writes, non-confined subagent types, and every
    lookup-fail leg are ALLOWED regardless of target path."""

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
        # An agent_id shape _resolve_subagent_identity() cannot canonicalize
        # (neither bare-hex nor named-teammate) resolves to "" -- fail-open.
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
    """The deny message states the one fact and the one alternative (fill
    the provisioned sidecar), names no override key, and carries no apology
    or self-justification."""

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

        # No override key named -- this guard's own env var must never
        # appear in the rendered text (operator_override_note is
        # audience-gated and this payload carries no positively-resolved
        # EM audience, so it renders nothing at all).
        assert "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE" not in reason

        # No apology / self-justification banned moves (B1, B4).
        for banned in ("sorry", "apolog", "real system", "not a refusal"):
            assert banned not in reason.lower()

    def test_deny_message_no_agent_type_leak(self, tmp_path, monkeypatch):
        """The message names the target and the alternative, not internal
        identity-resolution plumbing (subagent_type, agent_id shape)."""
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
