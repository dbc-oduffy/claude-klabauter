"""Tests for coordinator_core.bash_guards.block_noncanonical_branch_creation.

Covers the canonical-shape deny predicate (`is_canonical_branch`, never the
looser `is_allowed_branch`), the `git branch` create-vs-rename/delete/list
discrimination by FLAG PRESENCE, compound/env-prefixed shell shapes via
`resolve_command_positions`, the "fail open on a name never actually seen"
leg (unexpanded variable, command substitution, unterminated quote), the
sanctioned-longlived-prefix carve-out, and the repo-scoping gate
(`_is_hazard_repo`).

Pure Python -- no shell spawns, no real git repo required. `resolve_git_root`
and `_is_hazard_repo` are monkeypatched on THIS module's own imported
attributes (never the upstream definitions) per this package's injection
convention.

Spec backlink: coordinator_core/bash_guards/block_noncanonical_branch_creation.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard


def _payload(command, cwd="/repo"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": cwd,
    }


def _reason(out):
    assert out is not None, "expected an advisory envelope, got a bare allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _hazard_repo_by_default(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_creation_verb_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestSubstrateTable:

    def test_canonical_daily_branch_passes(self):
        assert guard.check(_payload("git checkout -b work/machine-b/2026-07-13")) is None

    def test_closeout_suffix_denies(self):
        _reason(guard.check(_payload("git checkout -b work/machine-b/2026-07-13-closeout")))

    def test_numeric_suffix_denies(self):
        _reason(guard.check(_payload("git checkout -b work/machine-b/2026-07-13-2")))

    def test_ci_green_suffix_denies(self):
        _reason(guard.check(_payload("git checkout -b work/machine-b/2026-07-13-ci-green")))

    def test_fix_prefix_denies(self):
        _reason(guard.check(_payload("git checkout -b fix/open-or-create-build-index-optout")))

    def test_docs_prefix_denies(self):
        _reason(guard.check(_payload("git checkout -b docs/scoped-to-memo-pins")))

    def test_mixed_case_daily_branch_denies(self):
        _reason(guard.check(_payload("git checkout -b work/MACHINE-B/2026-07-13")))


class TestGitBranchRenameVsCreate:
    def test_branch_rename_lowercase_m_passes(self):
        assert guard.check(_payload("git branch -m a b")) is None

    def test_branch_rename_uppercase_m_passes(self):
        assert guard.check(_payload("git branch -M a b")) is None

    def test_branch_creation_no_start_point_denies(self):
        _reason(guard.check(_payload("git branch bad-name")))

    def test_branch_creation_with_start_point_denies(self):
        _reason(guard.check(_payload("git branch bad-name main")))

    def test_bare_git_branch_list_allows(self):
        assert guard.check(_payload("git branch")) is None

    @pytest.mark.parametrize(
        "command",
        [
            "git branch --delete stray-fix-branch",
            "git branch --delete --force stray-fix-branch",
            "git branch --move a b",
            "git branch --copy a b",
            "git branch -c a b",
            "git branch -C a b",
            "git branch --list",
            "git branch --all",
        ],
    )
    def test_long_form_non_create_flags_allow(self, command):
        # short-flags-only `_BRANCH_NON_CREATE_FLAGS` set missed every
        # stray-fix-branch` was misclassified as a CREATION of
        assert guard.check(_payload(command)) is None

    def test_branch_creation_with_force_flag_still_denies(self):
        _reason(guard.check(_payload("git branch --force bad-name")))


class TestCompoundAndEnvPrefixedShapes:
    def test_compound_command_denies(self):
        _reason(guard.check(_payload("cmd && git checkout -b bad")))

    def test_env_prefixed_denies(self):
        _reason(guard.check(_payload("FOO=1 git checkout -b bad")))


class TestSwitchAndUppercaseFlags:
    def test_switch_lowercase_c_denies(self):
        _reason(guard.check(_payload("git switch -c bad")))

    def test_switch_uppercase_c_denies(self):
        _reason(guard.check(_payload("git switch -C bad")))

    def test_checkout_uppercase_b_denies(self):
        _reason(guard.check(_payload("git checkout -B bad")))

    def test_switch_long_form_create_denies(self):
        # `_SWITCH_CREATE_FLAGS` was `{-c, -C}` only, so `git switch
        _reason(guard.check(_payload("git switch --create bad-name")))

    def test_switch_long_form_force_create_denies(self):
        _reason(guard.check(_payload("git switch --force-create bad-name")))


class TestSanctionedLonglivedPrefixes:
    def test_feature_prefix_passes(self):
        assert guard.check(_payload("git checkout -b feature/x")) is None

    def test_release_prefix_passes(self):
        assert guard.check(_payload("git checkout -b release/x")) is None

    def test_migration_prefix_passes(self):
        assert guard.check(_payload("git checkout -b migration/x")) is None

    def test_prefixes_exported_for_sibling_guard(self):
        assert guard.SANCTIONED_LONGLIVED_PREFIXES == ("migration/", "release/", "feature/")


class TestDenyMessageRemediation:
    def test_remediation_offers_checkout_dash_b(self):
        # this deny fires while the user is CREATING a branch, so today's
        reason = _reason(guard.check(_payload("git checkout -b bad-name")))
        assert "git checkout -b work/" in reason


class TestRepoScoping:
    def test_out_of_scope_repo_allows_unconditionally(self, monkeypatch):
        monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: False)
        assert guard.check(_payload("git checkout -b fix/foo")) is None


class TestPowerShellIdiomDialectNeutral:
    """C4a (guard-dialect-coverage.md row 1): this guard gates on
    `token_matches_binary(tokens[0], "git")` -- the external `git` exe,
    byte-identical in both shell dialects. A PowerShell-typed caller invokes
    the SAME `git checkout -b <name>`/`git switch -c <name>` argv; the
    dialect-flavored surface is the STATEMENT SEPARATOR (`;`, PowerShell's
    idiomatic chain operator, vs bash's `&&`) and the env-assignment prefix
    shape (`$env:NAME = 'value';`, vs bash `NAME=value`). Both route through
    the SAME `resolve_command_positions` tokenizer today (no `_dialect.py`
    wiring exists in this module -- confirmed by grep), so this pins that
    the verdict does not change when the surrounding shell idiom does.

    Spec backlink: docs/reference/guard-dialect-coverage.md row 1 (C4a).
    """

    def test_semicolon_chained_powershell_style_denies(self):
        _reason(guard.check(_payload("Get-Location; git checkout -b bad-name")))

    def test_dollar_env_prefix_powershell_style_fails_open_same_as_bash(self):
        assert guard.check(_payload('git checkout -b "$env:BRANCH_NAME"')) is None

    def test_canonical_daily_branch_still_passes_under_semicolon_chain(self):
        assert guard.check(
            _payload("Get-Location; git checkout -b work/machine-b/2026-07-13")
        ) is None


class TestActualPowerShellToolName:
    """2026-08-19 subagent-boundary MATCHERS widening: the class above
    issues PowerShell-shaped text through `tool_name: "Bash"`, which per
    docs/reference/guard-tool-name-membership.md proves detection only, not
    that this guard is reached at all under a genuine PowerShell-tool
    payload -- the exact bypass this widening closes. This pins the real
    gate with `tool_name` actually set to `"PowerShell"`.
    """

    def test_denies_via_actual_powershell_tool_name(self):
        p = _payload("git checkout -b bad-name")
        p["tool_name"] = "PowerShell"
        _reason(guard.check(p))


class TestFailOpenOnUnreadableName:
    def test_unexpanded_shell_variable_passes(self):
        assert guard.check(_payload('git checkout -b "$BRANCH"')) is None

    def test_command_substitution_passes(self):
        assert guard.check(_payload('git checkout -b "$(date +%F)"')) is None

    def test_unterminated_quote_passes(self):
        assert guard.check(_payload('git checkout -b "unterminated')) is None

    def test_partial_substitution_quoted_form_passes(self):
        assert guard.check(
            _payload('git checkout -b "work/machine-b/$(date +%F)"')
        ) is None

    def test_partial_substitution_unquoted_form_passes(self):
        assert guard.check(
            _payload("git checkout -b work/machine-b/$(date +%F)")
        ) is None


class TestDesignatedDayBranch:

    def test_designated_branch_name_passes_whatever_its_shape(self, monkeypatch):
        import coordinator_core.daily_branch as daily_branch

        monkeypatch.setattr(
            daily_branch, "read_configured_day_branch",
            lambda root: "claude/compassionate-pascal-98ncw7",
        )
        assert guard.check(
            _payload("git checkout -b claude/compassionate-pascal-98ncw7")
        ) is None

    def test_non_designated_noncanonical_name_still_denied(self, monkeypatch):
        import coordinator_core.daily_branch as daily_branch

        monkeypatch.setattr(
            daily_branch, "read_configured_day_branch",
            lambda root: "claude/compassionate-pascal-98ncw7",
        )
        out = guard.check(_payload("git checkout -b fix/some-bug"))
        assert "not canonical" in _reason(out)
