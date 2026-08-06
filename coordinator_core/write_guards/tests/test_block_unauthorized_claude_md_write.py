"""Behavioral tests for
coordinator_core.write_guards.block_unauthorized_claude_md_write -- the
CLAUDE.md-class write guard DR-104 (2026-07-27) reintroduces over DR-058
for one path class only (see the module's own docstring).

Three tests here are load-bearing acceptance criteria per example-doctrine-repo
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

import pytest

from coordinator_core.write_guards import block_unauthorized_claude_md_write as guard


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
    """Default fixture state: calling session holds NO live grant. Tests
    that need a granted state override this explicitly.
    """
    monkeypatch.setattr(guard, "check_claude_md_write_grant", lambda cwd: (False, None))


def _deny(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(monkeypatch, file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


# ---------------------------------------------------------------------------
# AC8 -- subagent-originated payload (agent_id present) is denied.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# AC9 -- real scope equals stated scope, per path class. Reuses the
# check_blanket_git_add shape: pin DENY for every claimed-covered class and
# ALLOW for a representative not-covered class.
# ---------------------------------------------------------------------------


class TestRealScopeEqualsStatedScope:
    """Every bullet in the module docstring's is_claude_md_class-derived
    class list, pinned by assertion -- not merely described in prose. This
    is the check_blanket_git_add defect (doctrine claimed a wider deny
    scope than the code enforced) in test form, per the guard's own
    negative-spec.
    """

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

    def test_self_referential_scope_example_doctrine_repo_own_repo_root_claude_md_denied(self, monkeypatch):
        """Self-referential scope, resolved deliberately (see module
        docstring) -- this guard's own authoring repo's root CLAUDE.md is
        NOT excluded. Excluding it would reproduce the exact
        stated-vs-real scope gap this guard's negative-spec calls out.

        This is a SCOPE test (is the path matched as CLAUDE.md-class at
        all), not a direction test -- the growth/shrink comparison is
        pinned separately (``TestDirectionalDenyGrowthOnly``). Pin growth
        True here so this scope assertion does not depend on this dev
        machine's real example-doctrine-repo checkout's CLAUDE.md byte content.
        """
        monkeypatch.setattr(guard, "_is_growth", lambda *a, **kw: True)
        _deny(monkeypatch, "CLAUDE.md", cwd="/Users/example-operator/X/example-doctrine-repo")

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
        """Windows-style separators normalize before the class check --
        stated scope must not silently narrow on one platform."""
        _deny(monkeypatch, "coordinator\\CLAUDE.md")


# ---------------------------------------------------------------------------
# AC10 -- the emitted deny text names a concrete alternative and the
# override path, asserted against the rendered string.
# ---------------------------------------------------------------------------


class TestDenyTextNamesAlternativeAndOverride:
    def test_deny_text_names_the_discharge_hierarchy(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "discharge" in reason.lower()
        assert "mechanize" in reason.lower()
        assert "wiki" in reason.lower()

    def test_deny_text_does_not_presuppose_wiki_as_default_alternative(self, monkeypatch):
        """Regression for the inversion caught in review-integration: the
        deny text must never read as presupposing wiki-folding as the
        default fold target -- it names the full hierarchy instead.
        """
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "document-bloat-trim.md names the default fold target" not in reason
        assert "default fold target" not in reason

    def test_deny_text_names_the_grant_cli_override_path(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator_core.session.claude_md_grant grant pm" in reason

    def test_deny_text_names_the_rare_use_env_override(self, monkeypatch):
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE=1" in reason

    def test_deny_text_names_the_target_path(self, monkeypatch):
        result = guard.check(_payload("coordinator/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "coordinator/CLAUDE.md" in reason

    def test_grant_cli_interpolates_the_resolved_claude_klabauter_root(self, monkeypatch):
        """The resolved-root branch. Asserting only that the module path appears
        would pass identically on the fallback branch, which is how the original
        dead-end shipped unnoticed -- pin the interpolated root itself."""
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: "/opt/some/claude-klabauter")
        assert guard._grant_cli_invocation() == (
            'PYTHONPATH="/opt/some/claude-klabauter" '
            'python3 -m coordinator_core.session.claude_md_grant grant pm '
            '"<verbatim PM note>"'
        )

    def test_grant_cli_falls_back_when_root_unresolvable(self, monkeypatch):
        """RuntimeError is the resolver's one documented failure."""
        import coordinator_core.claude_klabauter_root as mr

        def _raise():
            raise RuntimeError("cannot resolve CLAUDE_KLABAUTER_ROOT")

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_falls_back_on_empty_root(self, monkeypatch):
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: "")
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_grant_cli_never_propagates_an_unexpected_resolver_error(
        self, monkeypatch, capsys
    ):
        """A raise here would convert a clean block into a crashed PreToolUse
        guard. An undocumented failure -- a rename, a signature drift -- still
        falls back, but must say so rather than passing for normal operation."""
        import coordinator_core.claude_klabauter_root as mr

        def _raise():
            raise AttributeError("resolver drifted")

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", _raise)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK
        assert "could not resolve the claude-klabauter root" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "hostile_root",
        ['/opt/mak"ima', "/opt/$(whoami)", "/opt/mak`ima`", "/opt/mak\nima"],
    )
    def test_grant_cli_falls_back_on_a_shell_unsafe_root(
        self, monkeypatch, hostile_root
    ):
        """The rendered command is pasted verbatim into a shell. A root that
        would break out of the double quotes yields a remediation that silently
        does the wrong thing -- the same defect class this resolution fixes."""
        import coordinator_core.claude_klabauter_root as mr

        monkeypatch.setattr(mr, "coordinator_claude_klabauter_root", lambda: hostile_root)
        assert guard._grant_cli_invocation() == guard._GRANT_CLI_INVOCATION_FALLBACK

    def test_deny_text_never_dead_ends(self, monkeypatch):
        """Nothing dead-ends -- state why, and give the override (binding,
        design-as-offers). A bare NO with no alternative is a guard
        fighting agent eagerness rather than redirecting it."""
        result = guard.check(_payload("CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "Still want CLAUDE.md?" in reason


# ---------------------------------------------------------------------------
# Directional deny (guard-class census RESHAPE, C18): deny GROWTH only,
# advise (do not block) on shrink/size-neutral. Both directions pinned
# against a REAL on-disk file -- Write/Edit go through ``_is_growth``'s own
# read of ``abs_file_path``, so a payload targeting a nonexistent path
# always falls into the "cannot determine" -> deny branch (already covered
# by every test above, all of which target a file that does not exist on
# disk). These tests use ``tmp_path`` so the growth/shrink comparison
# actually engages the byte-size measurement.
# ---------------------------------------------------------------------------


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

    def test_advisory_names_a_concrete_alternative(self, monkeypatch, tmp_path):
        """Axis-A (obligation): any emission that asks the agent to
        reconsider must name a concrete alternative -- this advisory names
        the same grant CLI the deny leg names."""
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
        assert "coordinator_core.session.claude_md_grant grant pm" in reason

    def test_new_file_creation_is_always_growth_and_denied(self, monkeypatch, tmp_path):
        """A CLAUDE.md-class file that does not yet exist has a 0-byte
        baseline -- there is no shrink case for content that does not
        exist yet, so creation is always denied absent a grant."""
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
