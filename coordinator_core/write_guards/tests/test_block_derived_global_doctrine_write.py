
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
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _authoring_registered(monkeypatch):
    monkeypatch.setattr(guard, "registry_get", lambda key: "/opt/authoring" if key == "repos.content_root" else None)
    monkeypatch.setattr("coordinator_core.write_guards._guard_level.level_for", lambda name: "strict")


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
        _deny(r"C:\alice\.claude\CLAUDE.md")

    def test_windows_separator_case_varied_denied(self):
        _deny(r"c:\alice\.CLAUDE\CLAUDE.MD")

    def test_claude_home_parent_root_denied(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _deny("/opt/claude-home/.claude/CLAUDE.md")

    def test_claude_home_parent_root_backslash_form_denied(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _deny("\\opt\\claude-home\\.claude\\CLAUDE.md")

    def test_claude_home_is_not_a_direct_claude_root(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _allow("/opt/claude-home/CLAUDE.md")

    def test_claude_config_dir_denied(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/cfg")
        _deny("/opt/cfg/CLAUDE.md")

    @pytest.mark.parametrize("bad", ["/opt/claude-home/.claude", "relative/home"])
    def test_bad_claude_home_does_not_raise_and_home_roots_hold(self, monkeypatch, bad):
        monkeypatch.setenv("CLAUDE_HOME", bad)
        _deny("/Users/alice/.claude/CLAUDE.md")

    def test_tilde_expansion_denied(self):
        _deny("~/.claude/CLAUDE.md")

    def test_override_env_allows(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow("/Users/alice/.claude/CLAUDE.md")

    def test_non_guarded_tool_allowed(self):
        _allow("/Users/alice/.claude/CLAUDE.md", tool_name="Read")


class TestSilentOnEverythingElse:
    def test_authoring_surface_allowed(self):
        _allow("/Users/alice/repos/coordinator-content-repo/global-doctrine/CLAUDE.md")

    def test_repo_root_project_claude_md_allowed(self):
        _allow("/Users/alice/repos/some-project/CLAUDE.md")

    def test_dev_repo_coordinator_claude_md_allowed(self):
        """DoE's own coordinator/CLAUDE.md plugin-doctrine authoring
        surface — a DIFFERENT CLAUDE.md-class surface, not derived."""
        _allow("/Users/alice/repos/coordinator-content-repo/coordinator/CLAUDE.md")

    def test_snippet_surface_allowed(self):
        _allow("/Users/alice/repos/coordinator-content-repo/coordinator/snippets/em-operating-doctrine.md")

    def test_other_snippet_surface_allowed(self):
        _allow("/Users/alice/repos/coordinator-content-repo/coordinator/snippets/agent-role-dispatched.md")

    def test_settings_json_allowed(self):
        _allow("/Users/alice/.claude/settings.json")

    def test_decisions_doc_allowed(self):
        _allow("/Users/alice/.claude/docs/decisions/DR-104.md")

    def test_bare_claude_md_relative_allowed(self):
        _allow("CLAUDE.md")


class TestDenyTextNamesAlternativeAndConsequence:
    def test_deny_text_names_authoring_alternative(self, monkeypatch):
        monkeypatch.setattr(
            guard,
            "registry_get",
            lambda key: "/opt/some/root" if key == "repos.content_root" else None,
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

    def test_deny_text_names_no_override_route_at_all(self, monkeypatch):
        """Inverted (was: `..._routes_to_the_override_doc_not_the_key`,
        which asserted the override-doc pointer WAS present). The deny text
        for this default (unregistered-root) payload shape is now a wholly
        different narrative — "not the authoring source — the authoring
        root is unregistered here — `machine-local set repos.content_root
        <path>`" — with no override-doc pointer and no key at all.
        Positively asserts both the absence of any override-note fragment
        AND the presence of the real, current unregistered-root remediation
        text, so this cannot pass vacuously on a reason carrying neither.

        `registry_get` is patched because the branch under test is the
        UNREGISTERED one, and this box has `repos.content_root` registered --
        without the patch the guard renders the registered narrative and the
        test measures the wrong branch. It read green only while nobody who
        ran it had the key set."""
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "guard-override-keys.md" not in reason
        assert guard._OVERRIDE_ENV_VAR not in reason
        assert "/opt/authoring/global-doctrine/CLAUDE.md" in reason

    def test_deny_text_resolves_authoring_root_via_registry(self, monkeypatch):
        monkeypatch.setattr(
            guard, "registry_get", lambda key: "/opt/some/coordinator-content-repo" if key == "repos.content_root" else None
        )
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "/opt/some/coordinator-content-repo/global-doctrine/CLAUDE.md" in reason

    def test_unregistered_root_allows_the_write(self, monkeypatch):
        monkeypatch.setattr(guard, "registry_get", lambda key: None)
        _allow("/Users/alice/.claude/CLAUDE.md")

    @pytest.mark.parametrize("level,expect", [("warn", "additionalContext"), ("off", None)])
    def test_guard_level_relaxes_the_deny(self, monkeypatch, level, expect):
        monkeypatch.setattr("coordinator_core.write_guards._guard_level.level_for", lambda name: level)
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        if expect is None:
            assert result is None
        else:
            out = result["hookSpecificOutput"]
            assert "permissionDecision" not in out
            assert "blast radius" in out[expect]
            assert "machine-local set coordinator.guard_level" in out[expect]
