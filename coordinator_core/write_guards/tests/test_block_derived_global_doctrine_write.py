"""Behavioral tests for
coordinator_core.write_guards.block_derived_global_doctrine_write — the
derived-vs-authoring routing guard for the global-doctrine CLAUDE.md-class
surface family (see the module's own docstring for the full contract).

Covers: FIRES on the derived live copy (~/.claude/CLAUDE.md), case-varied
and Windows-separator-form (via pure string normalization — no WindowsPath
construction needed, unlike the contained_path/Path.resolve()-based guards
this module's docstring distinguishes itself from); SILENT on the authoring
surface, a repo-root project CLAUDE.md, ordinary ~/.claude config writes,
and the two coordinator/snippets/*.md surfaces; override env var honored.

Path literals below use the placeholder segment ``alice``, per this repo's
own concrete-path-citation guard convention (a generic username, not a real
operator's). Windows-separator literals are raw strings with a SINGLE
placeholder segment right after the drive root (that guard's own exemption
checks only the first segment after a drive root, so a doubled-backslash
source literal or a two-segment ``Users``/``alice`` shape would not read as
exempt the same way).
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import block_derived_global_doctrine_write as guard


def _payload(file_path: str, tool_name: str = "Write") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _fixed_home(monkeypatch):
    """Pin HOME/USERPROFILE so the derived-target set is deterministic
    across hosts, and so a Windows-separator-form payload can be asserted
    from a POSIX interpreter (this guard matches by string normalization,
    not real filesystem resolution — see module docstring)."""
    monkeypatch.setenv("HOME", "/Users/alice")
    monkeypatch.setenv("USERPROFILE", r"C:\alice")
    monkeypatch.delenv("CLAUDE_HOME", raising=False)


def _deny(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected DENY for: {file_path!r}"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result


def _allow(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


class TestFiresOnDerivedLiveCopy:
    def test_posix_home_write_denied(self):
        _deny("/Users/alice/.claude/CLAUDE.md")

    def test_posix_home_edit_denied(self):
        _deny("/Users/alice/.claude/CLAUDE.md", tool_name="Edit")

    def test_multiedit_denied(self):
        _deny("/Users/alice/.claude/CLAUDE.md", tool_name="MultiEdit")

    def test_case_varied_denied(self):
        _deny("/Users/alice/.CLAUDE/Claude.MD")

    def test_windows_separator_form_denied(self):
        """Windows-separator payload, asserted from a POSIX interpreter —
        this guard matches by pure string normalization, so no WindowsPath
        construction is needed (contrast the contained_path/Path.resolve()
        guards, which are unreachable this way off-Windows)."""
        _deny(r"C:\alice\.claude\CLAUDE.md")

    def test_windows_separator_case_varied_denied(self):
        _deny(r"c:\alice\.CLAUDE\CLAUDE.MD")

    def test_claude_home_direct_root_denied(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _deny("/opt/claude-home/CLAUDE.md")

    def test_tilde_expansion_denied(self):
        _deny("~/.claude/CLAUDE.md")

    def test_override_env_allows(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow("/Users/alice/.claude/CLAUDE.md")

    def test_non_guarded_tool_allowed(self):
        _allow("/Users/alice/.claude/CLAUDE.md", tool_name="Read")


class TestSilentOnEverythingElse:
    def test_authoring_surface_allowed(self):
        """DoE-claude's own authoring copy — writes here are correct."""
        _allow("/Users/alice/repos/DoE-claude/global-doctrine/CLAUDE.md")

    def test_repo_root_project_claude_md_allowed(self):
        _allow("/Users/alice/repos/some-project/CLAUDE.md")

    def test_dev_repo_coordinator_claude_md_allowed(self):
        """DoE's own coordinator/CLAUDE.md plugin-doctrine authoring
        surface — a DIFFERENT CLAUDE.md-class surface, not derived."""
        _allow("/Users/alice/repos/DoE-claude/coordinator/CLAUDE.md")

    def test_snippet_surface_allowed(self):
        _allow("/Users/alice/repos/DoE-claude/coordinator/snippets/em-operating-doctrine.md")

    def test_other_snippet_surface_allowed(self):
        _allow("/Users/alice/repos/DoE-claude/coordinator/snippets/agent-role-dispatched.md")

    def test_settings_json_allowed(self):
        _allow("/Users/alice/.claude/settings.json")

    def test_decisions_doc_allowed(self):
        _allow("/Users/alice/.claude/docs/decisions/DR-104.md")

    def test_bare_claude_md_relative_allowed(self):
        """No home-anchored .claude/ prefix at all — must not match on
        basename alone."""
        _allow("CLAUDE.md")


class TestDenyTextNamesAlternativeAndConsequence:
    def test_deny_text_names_authoring_alternative(self, monkeypatch):
        monkeypatch.setattr(
            guard,
            "registry_get",
            lambda key: "/opt/some/root" if key == "repos.doe_claude" else None,
        )
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "global-doctrine/CLAUDE.md" in reason

    def test_deny_text_states_silent_overwrite_consequence(self):
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "overwritten" in reason.lower()
        assert "no error" in reason.lower()

    def test_deny_text_names_the_target_path(self):
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "/Users/alice/.claude/CLAUDE.md" in reason

    def test_deny_text_names_no_override_route_at_all(self):
        """Inverted (was: `..._routes_to_the_override_doc_not_the_key`,
        which asserted the override-doc pointer WAS present). The deny text
        for this default (unregistered-root) payload shape is now a wholly
        different narrative — "not the authoring source — the authoring
        root is unregistered here — `machine-local set repos.doe_claude
        <path>`" — with no override-doc pointer and no key at all.
        Positively asserts both the absence of any override-note fragment
        AND the presence of the real, current unregistered-root remediation
        text, so this cannot pass vacuously on a reason carrying neither."""
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "guard-override-keys.md" not in reason
        assert guard._OVERRIDE_ENV_VAR not in reason
        assert "machine-local set repos.doe_claude" in reason

    def test_deny_text_resolves_authoring_root_via_registry(self, monkeypatch):
        monkeypatch.setattr(
            guard, "registry_get", lambda key: "/opt/some/doe-claude" if key == "repos.doe_claude" else None
        )
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "/opt/some/doe-claude/global-doctrine/CLAUDE.md" in reason

    def test_unregistered_root_names_the_key_not_a_fabricated_path(self, monkeypatch):
        """An unresolvable root renders the registry key the operator sets,
        never an invented location. A literal fallback here would name a path
        that exists on no machine — and, if it carried a codename, would
        publish as a placeholder naming nothing at all."""
        monkeypatch.setattr(guard, "registry_get", lambda key: None)
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "machine-local set repos.doe_claude" in reason
        assert "global-doctrine/CLAUDE.md" not in reason
