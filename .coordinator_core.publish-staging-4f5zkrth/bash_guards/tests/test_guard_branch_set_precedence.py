"""Tests for coordinator_core.bash_guards.guard_branch_set_precedence.

Covers: the advisory fires with the real branch name and count present in
`additionalContext`, it NEVER returns a deny under any input, it returns
`None` when no other branch has commits ahead, its envelope is allow +
additionalContext shaped, the AC16 recency two-leg filter (should_prompt_
rename True exclusion, >48h age exclusion), AC13 repo scoping, and that no
git subprocess fires on a non-matching command.

Pure Python -- no shell spawns, no real git repo required. Every seam
(`resolve_git_root`, `_is_hazard_repo`, `_other_canonical_branches`,
`_ahead_of_main`, `_now`, `_today`) is monkeypatched on THIS module's own
imported attribute (never the upstream definition), per this package's
injection convention. The `branch_set_provider` kwarg is exercised directly
in the deterministic-firing test, mirroring how C2's alternative-liveness
gate will drive this guard.

Spec backlink: coordinator_core/bash_guards/guard_branch_set_precedence.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_branch_set_precedence as guard

_FIXED_NOW = 1722700000.0  # 2026-08-03T14:26:40Z-ish, arbitrary fixed anchor
_FIXED_TODAY = "2026-08-03"


def _payload(command, cwd="/repo"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": cwd,
    }


def _ctx(out):
    assert out is not None, "expected an advisory envelope, got allow-with-nothing/None"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in hso
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _hazard_repo_and_clock(monkeypatch):
    """Every test runs "inside" a hazard repo, with a fixed deterministic
    clock -- the one test exercising AC13 overrides `_is_hazard_repo`
    locally."""
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    monkeypatch.setattr(guard, "_now", lambda: _FIXED_NOW)
    monkeypatch.setattr(guard, "_today", lambda: _FIXED_TODAY)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_no_checkout_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None

    def test_non_checkout_b_command_allows_and_spends_no_git_subprocess(self, monkeypatch):
        calls = []
        monkeypatch.setattr(guard, "_other_canonical_branches", lambda cwd=None: calls.append(1) or [])
        assert guard.check(_payload("git status")) is None
        assert calls == []


class TestDeterministicFiring:
    def test_advisory_fires_with_real_branch_and_count(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 3600  # 1h old -- well within 48h
        provider = lambda: [("work/delphipro/2026-07-31", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 12)
        # This test isolates the "fires with real name/count" property, not
        # the AC16 should_prompt_rename leg -- that leg has its own
        # dedicated test in TestRecencyFilter.
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )

        ctx = _ctx(out)
        assert "work/delphipro/2026-07-31" in ctx
        assert "12" in ctx
        assert "instead" in ctx.lower()
        assert "`git checkout work/delphipro/2026-07-31`" in ctx

    def test_shipped_message_uses_injected_provider_not_a_stub(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 60
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 3)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )

        ctx = _ctx(out)
        assert "work/delphipro/2026-08-01 already has 3 unmerged commits" in ctx


class TestNeverDenies:
    def test_never_returns_deny_shape(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 60
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 3)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )

        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso.get("permissionDecision") != "deny"
        assert "permissionDecisionReason" not in hso

    def test_never_denies_on_bad_input(self):
        assert guard.check({"tool_name": "Bash", "tool_input": None}) is None
        assert guard.check({}) is None


class TestNoOtherBranchAhead:
    def test_empty_candidate_list_returns_none(self, monkeypatch):
        monkeypatch.setattr(guard, "_other_canonical_branches", lambda cwd=None: [])
        assert guard.check(_payload("git checkout -b work/delphipro/2026-08-03")) is None

    def test_zero_ahead_count_returns_none(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 60
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 0)
        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is None


class TestRecencyFilter:
    def test_should_prompt_rename_true_excludes_candidate(self, monkeypatch):
        # Same-day span end already == today would make should_prompt_rename
        # False; force True by giving a branch whose span end != today and
        # whose last commit is fresh (< 48h).
        recent_epoch = _FIXED_NOW - 3600
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 5)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: True)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is None

    def test_stale_over_48h_excludes_candidate(self, monkeypatch):
        stale_epoch = _FIXED_NOW - (guard._HOURS_48_SECONDS + 3600)
        provider = lambda: [("work/delphipro/2026-07-20", stale_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 7)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is None

    def test_boundary_at_exactly_48h_is_not_excluded_by_age_leg(self, monkeypatch):
        # Exactly 48h old survives the age leg (> 48h excludes, == does not);
        # should_prompt_rename may still exclude it on its own -- force it
        # False here to isolate the age-leg boundary behavior.
        boundary_epoch = _FIXED_NOW - guard._HOURS_48_SECONDS
        provider = lambda: [("work/delphipro/2026-07-20", boundary_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 4)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is not None


class TestRepoScopingAC13:
    def test_out_of_scope_repo_passes_untouched(self, monkeypatch):
        monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: False)
        calls = []
        monkeypatch.setattr(guard, "_other_canonical_branches", lambda cwd=None: calls.append(1) or [])

        out = guard.check(_payload("git checkout -b work/x/2026-08-01"))

        assert out is None
        assert calls == []


class TestNonCanonicalOrUnreadableTargetSpendsNoSubprocess:
    def test_non_canonical_target_allows_and_spends_no_subprocess(self, monkeypatch):
        calls = []
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: calls.append(1) or "/repo")
        assert guard.check(_payload("git checkout -b fix/some-bug")) is None
        assert calls == []

    def test_mixed_case_target_allows_and_spends_no_subprocess(self, monkeypatch):
        calls = []
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: calls.append(1) or "/repo")
        assert guard.check(_payload("git checkout -b work/DELPHIPRO/2026-08-03")) is None
        assert calls == []

    def test_unexpanded_variable_target_allows(self):
        assert guard.check(_payload('git checkout -b "$BRANCH"')) is None

    def test_command_substitution_target_allows(self):
        assert guard.check(_payload('git checkout -b "$(date +%F)"')) is None


class TestSwitchFlagVocabulary:
    """Finding 2 -- C5 must also match `git switch -c/-C/--create/
    --force-create`, mirroring C7's `_classify_segment`, not just
    `checkout -b/-B`."""

    def _fire(self, monkeypatch, cmd):
        recent_epoch = _FIXED_NOW - 3600
        provider = lambda: [("work/delphipro/2026-07-31", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 12)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)
        return guard.check(_payload(cmd), branch_set_provider=provider)

    def test_switch_lowercase_c_fires(self, monkeypatch):
        out = self._fire(monkeypatch, "git switch -c work/delphipro/2026-08-03")
        assert _ctx(out) is not None

    def test_switch_uppercase_c_fires(self, monkeypatch):
        out = self._fire(monkeypatch, "git switch -C work/delphipro/2026-08-03")
        assert _ctx(out) is not None

    def test_switch_long_create_fires(self, monkeypatch):
        out = self._fire(monkeypatch, "git switch --create work/delphipro/2026-08-03")
        assert _ctx(out) is not None

    def test_switch_long_force_create_fires(self, monkeypatch):
        out = self._fire(monkeypatch, "git switch --force-create work/delphipro/2026-08-03")
        assert _ctx(out) is not None

    def test_switch_non_canonical_target_allows_and_spends_no_subprocess(self, monkeypatch):
        calls = []
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: calls.append(1) or "/repo")
        assert guard.check(_payload("git switch -c fix/some-bug")) is None
        assert calls == []


class TestPreFilterWordBoundary:
    """Finding 4 -- C5's pre-filter must be a word-boundary match (aligned
    to C7's shape), not a bare substring, and must also gate on `switch`
    now that C5 matches it."""

    def test_substring_only_mention_does_not_falsely_gate_through(self, monkeypatch):
        # "mycheckout" contains "checkout" as a substring but is not the
        # word "checkout" -- word-boundary pre-filter should not match, and
        # the command isn't git-shaped anyway so this must allow either way.
        assert guard.check(_payload("echo mycheckout")) is None

    def test_switch_mention_is_not_filtered_out_by_prefilter(self, monkeypatch):
        # Regression guard for Finding 2 + Finding 4 interaction: the
        # pre-filter must admit "switch" or the new switch-matching logic
        # in _extract_checkout_target would be dead on arrival.
        recent_epoch = _FIXED_NOW - 3600
        provider = lambda: [("work/delphipro/2026-07-31", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 1)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)
        out = guard.check(
            _payload("git switch -c work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is not None


class TestNeverDeniesOnNewSwitchShapes:
    """AC12 -- the widened switch/long-form matching must not open a new
    deny or exception path."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git switch -c",
            "git switch -C",
            "git switch --create",
            "git switch --force-create",
            "git switch --create ",
            'git switch -c "$BRANCH"',
            'git switch --create "$(date +%F)"',
            "git switch -c work/delphipro/;rm -rf /",
        ],
    )
    def test_no_switch_shape_ever_denies_or_raises(self, cmd):
        out = guard.check(_payload(cmd))
        if out is not None:
            assert out["hookSpecificOutput"].get("permissionDecision") != "deny"


class TestPowerShellIdiomDialectNeutral:
    """C4a (guard-dialect-coverage.md row 4): this guard gates on
    `token_matches_binary(tokens[0], "git")` -- the external `git` exe,
    byte-identical in both shell dialects. No `_dialect.py` import exists
    in this module (confirmed by grep), so a PowerShell-idiom surrounding
    shape (`;` chain instead of `&&`) reaches the SAME tokenizer and must
    reach the SAME verdict.

    Spec backlink: docs/reference/guard-dialect-coverage.md row 4 (C4a).
    """

    def test_semicolon_chained_powershell_style_still_fires(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 3600
        provider = lambda: [("work/delphipro/2026-07-31", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 12)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("Get-Location; git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        ctx = _ctx(out)
        assert "work/delphipro/2026-07-31" in ctx


class TestEnvelopeShape:
    def test_allow_advisory_shape(self, monkeypatch):
        recent_epoch = _FIXED_NOW - 60
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 1)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)

        out = guard.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )

        assert set(out.keys()) == {"hookSpecificOutput"}
        hso = out["hookSpecificOutput"]
        assert set(hso.keys()) == {"hookEventName", "permissionDecision", "additionalContext"}
