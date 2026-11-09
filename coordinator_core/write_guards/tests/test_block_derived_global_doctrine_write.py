
from __future__ import annotations

import pytest

from coordinator_core import machine_profile
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
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    machine_profile.reset_cache()


def _advised(file_path, **kw):
    """The guard is advisory-by-ruling (deny_registry.ADVISORY_BY_RULING): a derived-copy
    write is recoverable, so the policy point answers allow plus one advisory line."""
    result = guard.check(_payload(file_path, **kw))
    assert result is not None, f"expected an advisory for: {file_path!r}"
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"].startswith(machine_profile.ADVISORY_PREFIX)
    return result


def _allow(file_path, **kw):
    result = guard.check(_payload(file_path, **kw))
    assert result is None, f"expected ALLOW for: {file_path!r}, got {result!r}"


class TestFiresOnDerivedLiveCopy:
    def test_posix_home_write_advised(self):
        _advised("/Users/alice/.claude/CLAUDE.md")

    def test_posix_home_edit_advised(self):
        _advised("/Users/alice/.claude/CLAUDE.md", tool_name="Edit")

    def test_multiedit_advised(self):
        _advised("/Users/alice/.claude/CLAUDE.md", tool_name="MultiEdit")

    def test_case_varied_advised(self):
        _advised("/Users/alice/.CLAUDE/Claude.MD")

    def test_windows_separator_form_advised(self):
        _advised(r"C:\alice\.claude\CLAUDE.md")

    def test_windows_separator_case_varied_advised(self):
        _advised(r"c:\alice\.CLAUDE\CLAUDE.MD")

    def test_claude_home_parent_root_advised(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _advised("/opt/claude-home/.claude/CLAUDE.md")

    def test_claude_home_parent_root_backslash_form_advised(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _advised("\\opt\\claude-home\\.claude\\CLAUDE.md")

    def test_claude_home_is_not_a_direct_claude_root(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", "/opt/claude-home")
        _allow("/opt/claude-home/CLAUDE.md")

    def test_claude_config_dir_advised(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/cfg")
        _advised("/opt/cfg/CLAUDE.md")

    @pytest.mark.parametrize("bad", ["/opt/claude-home/.claude", "relative/home"])
    def test_bad_claude_home_does_not_raise_and_home_roots_hold(self, monkeypatch, bad):
        monkeypatch.setenv("CLAUDE_HOME", bad)
        _advised("/Users/alice/.claude/CLAUDE.md")

    def test_tilde_expansion_advised(self):
        _advised("~/.claude/CLAUDE.md")

    def test_override_env_allows(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow("/Users/alice/.claude/CLAUDE.md")

    def test_non_guarded_tool_allowed(self):
        _allow("/Users/alice/.claude/CLAUDE.md", tool_name="Read")


class TestDerivedRulesDir:
    def test_home_rules_file_advised_naming_authoring_surface(self):
        ctx = _advised("/Users/alice/.claude/rules/rules.md")["hookSpecificOutput"]["additionalContext"]
        assert "global-doctrine/rules/rules.md" in ctx

    def test_edit_and_multiedit_advised(self):
        _advised("/Users/alice/.claude/rules/rules.md", tool_name="Edit")
        _advised("/Users/alice/.claude/rules/rules.md", tool_name="MultiEdit")

    def test_windows_separator_form_names_file(self):
        ctx = _advised(r"C:\alice\.claude\rules\Other.md")["hookSpecificOutput"]["additionalContext"]
        assert "global-doctrine/rules/Other.md" in ctx

    def test_tilde_form_advised(self):
        _advised("~/.claude/rules/rules.md")

    def test_claude_config_dir_rules_advised(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/cfg")
        ctx = _advised("/opt/cfg/rules/rules.md")["hookSpecificOutput"]["additionalContext"]
        assert "global-doctrine/rules/rules.md" in ctx

    def test_claude_md_advisory_does_not_name_rules_path(self):
        ctx = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))["hookSpecificOutput"]["additionalContext"]
        assert "global-doctrine/rules" not in ctx

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/alice/.claude/rules/sub/x.md",
            "/Users/alice/.claude/rules/notes.txt",
            "/Users/alice/.claude/rules/",
            "/Users/alice/repos/coordinator-content-repo/global-doctrine/rules/rules.md",
            "/Users/alice/repos/proj/.claude/rules/rules.md",
        ],
    )
    def test_non_derived_rules_paths_allowed(self, path):
        _allow(path)

    def test_override_env_allows_rules(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _allow("/Users/alice/.claude/rules/rules.md")


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


class TestAdvisoryTextNamesTheConsequence:
    def test_advisory_states_the_overwrite_consequence(self):
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "overwritten" in ctx.lower()

    def test_advisory_omits_the_machine_absolute_target_path(self):
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "/Users/alice" not in ctx

    def test_advisory_names_no_override_route_at_all(self, monkeypatch):
        """The advisory carries neither the override-doc pointer nor the override key."""
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "guard-override-keys.md" not in ctx
        assert guard._OVERRIDE_ENV_VAR not in ctx
        assert "/opt/authoring" not in ctx

    def test_unregistered_root_allows_the_write(self, monkeypatch):
        monkeypatch.setattr(guard, "registry_get", lambda key: None)
        _allow("/Users/alice/.claude/CLAUDE.md")

    @pytest.mark.parametrize("level", ["strict", "warn", "off"])
    def test_no_guard_level_lifts_the_advisory_to_a_deny(self, monkeypatch, level):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        machine_profile.reset_cache()
        result = guard.check(_payload("/Users/alice/.claude/CLAUDE.md"))
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "Doctrine surfaces reach every session" in out["additionalContext"]
