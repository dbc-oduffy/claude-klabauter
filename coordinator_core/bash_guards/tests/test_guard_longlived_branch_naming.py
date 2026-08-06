"""Tests for coordinator_core.bash_guards.guard_longlived_branch_naming.

Covers: the advisory fires on all three sanctioned longlived prefixes across
both `checkout -b/-B` and `switch -c/-C` creation forms; it NEVER denies
under any input including malformed/empty names; the envelope is
allow+additionalContext shaped; canonical `work/*` and `main` creations
never even trigger this guard; `git branch -m`/`-M` is untouched (this
guard does not inspect `git branch` at all); and the AC13 repo-scoping gate
(`_is_hazard_repo` false -> pass-through untouched).

Pure Python -- no shell spawns, no real git repo required. `resolve_git_root`
and `_is_hazard_repo` are monkeypatched on THIS module's own imported
attributes (never the upstream definitions), per this package's injection
convention.

Spec backlink: coordinator_core/bash_guards/guard_longlived_branch_naming.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_longlived_branch_naming as guard


def _payload(command, cwd="/repo"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": cwd,
    }


def _ctx(out):
    assert out is not None, "expected an advisory envelope, got allow/no-op"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in hso
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _hazard_repo_by_default(monkeypatch):
    """Every test in this file runs "inside" a hazard repo by default --
    the one test exercising AC13 (out-of-scope repo passes untouched)
    overrides this locally."""
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)


class TestNonBashOrEmpty:
    def test_non_bash_tool_passes(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_passes(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_passes(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_creation_verb_mention_passes(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestAdvisoryFiresOnAllSanctionedPrefixes:
    def test_feature_prefix_checkout_b_fires(self):
        ctx = _ctx(guard.check(_payload("git checkout -b feature/x")))
        assert "feature/" in ctx
        assert "instead" in ctx.lower()

    def test_release_prefix_checkout_b_fires(self):
        ctx = _ctx(guard.check(_payload("git checkout -b release/x")))
        assert "release/" in ctx

    def test_migration_prefix_checkout_b_fires(self):
        ctx = _ctx(guard.check(_payload("git checkout -b migration/x")))
        assert "migration/" in ctx

    def test_checkout_uppercase_b_fires(self):
        _ctx(guard.check(_payload("git checkout -B feature/x")))

    def test_switch_lowercase_c_fires(self):
        _ctx(guard.check(_payload("git switch -c feature/x")))

    def test_switch_uppercase_c_fires(self):
        _ctx(guard.check(_payload("git switch -C feature/x")))

    def test_switch_long_create_fires(self):
        ctx = _ctx(guard.check(_payload("git switch --create feature/x")))
        assert "feature/" in ctx

    def test_switch_long_force_create_fires(self):
        ctx = _ctx(guard.check(_payload("git switch --force-create feature/x")))
        assert "feature/" in ctx

    def test_compound_command_fires(self):
        _ctx(guard.check(_payload("cmd && git checkout -b feature/x")))

    def test_env_prefixed_fires(self):
        _ctx(guard.check(_payload("FOO=1 git checkout -b feature/x")))


class TestEnvelopeShape:
    def test_envelope_is_allow_advisory_shaped(self):
        out = guard.check(_payload("git checkout -b feature/x"))
        assert out == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": out["hookSpecificOutput"]["additionalContext"],
            }
        }


class TestCanonicalAndUnscopedNamesDoNotTrigger:
    def test_canonical_daily_branch_passes_untouched(self):
        assert guard.check(_payload("git checkout -b work/machine-b/2026-07-13")) is None

    def test_main_checkout_passes_untouched(self):
        assert guard.check(_payload("git checkout main")) is None

    def test_noncanonical_non_sanctioned_prefix_passes_untouched(self):
        # Out of THIS guard's scope entirely -- C1's deny guard owns this
        # shape, not this advisory.
        assert guard.check(_payload("git checkout -b fix/open-or-create")) is None


class TestGitBranchUntouched:
    def test_bare_git_branch_creation_not_inspected(self):
        # This guard only looks at checkout -b/-B and switch -c/-C -- it
        # does not inspect `git branch` at all (see module docstring).
        assert guard.check(_payload("git branch feature/x")) is None

    def test_branch_rename_lowercase_m_passes(self):
        assert guard.check(_payload("git branch -m a b")) is None

    def test_branch_rename_uppercase_m_passes(self):
        assert guard.check(_payload("git branch -M feature/old feature/new")) is None


class TestNeverDeniesUnderAnyInput:
    """AC12 -- this guard must NEVER return a deny envelope, under any
    input, including malformed/empty names."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git checkout -b feature/",
            "git checkout -b feature/x/y/../../etc",
            'git checkout -b "$BRANCH"',
            'git checkout -b "$(date +%F)"',
            'git checkout -b "unterminated',
            "git checkout -b",
            "git switch -c",
            "git switch --create",
            "git switch --force-create",
            'git switch --create "$BRANCH"',
            "git checkout -b feature/;rm -rf /",
            "git switch --create feature/;rm -rf /",
        ],
    )
    def test_no_input_shape_ever_denies(self, cmd):
        out = guard.check(_payload(cmd))
        if out is not None:
            assert out["hookSpecificOutput"].get("permissionDecision") != "deny"


class TestFailOpenOnUnreadableName:
    def test_unexpanded_shell_variable_passes(self):
        assert guard.check(_payload('git checkout -b "$BRANCH"')) is None

    def test_command_substitution_passes(self):
        assert guard.check(_payload('git checkout -b "$(date +%F)"')) is None

    def test_unterminated_quote_passes(self):
        assert guard.check(_payload('git checkout -b "unterminated')) is None


class TestRepoScoping:
    def test_out_of_scope_repo_passes_untouched(self, monkeypatch):
        monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: False)
        assert guard.check(_payload("git checkout -b feature/x")) is None


class TestNoOverrideTokenNamed:
    def test_advisory_text_never_names_override_env_var(self):
        ctx = _ctx(guard.check(_payload("git checkout -b feature/x")))
        assert "COORDINATOR_OVERRIDE" not in ctx
        assert "COORDINATOR_ALLOW" not in ctx
        assert "COORDINATOR_DISABLE" not in ctx


class TestAlternativeLivenessCue:
    def test_offer_carries_alternative_liveness_cue_token(self):
        ctx = _ctx(guard.check(_payload("git checkout -b feature/x")))
        assert "Use instead:" in ctx

    def test_offer_carries_concrete_command_alternative(self):
        ctx = _ctx(guard.check(_payload("git checkout -b feature/x")))
        assert "git checkout -b work/" in ctx
