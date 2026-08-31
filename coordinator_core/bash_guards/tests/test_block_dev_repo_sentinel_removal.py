"""Tests for coordinator_core.bash_guards.block_dev_repo_sentinel_removal.

Covers the DENY set (`rm`/`unlink`, `mv` source, `git rm`/`git mv` source,
`find -delete`/`find -exec rm`, targeted `python -c`), the mirror-image ALLOW
set (mentions/reads/`mv`-as-destination, which is the sentinel-CREATION
guard's territory, not this one's), the ADVISORY set for unexaminable
indirection, the override env var, and dispatch-level (end-to-end via
`dispatch.evaluate_payload_json`) ordering ahead of `offer-git-c`.

Pure Python -- no shell spawns, no filesystem writes.

Spec backlink: coordinator_core/bash_guards/block_dev_repo_sentinel_removal.py
Precedent test shape: coordinator_core/bash_guards/tests/test_block_worktree_sentinel_creation.py
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from coordinator_core.bash_guards import _verdict
from coordinator_core.bash_guards import block_dev_repo_sentinel_removal as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY


SENTINEL = ".coordinator-dev-repo"

#: Same bridge-to-C8 gate as `test_command_tokenizer_length_ceiling.py`'s
#: own `requires_powershell_grammar` -- the PowerShell cases below need
#: `tree-sitter-pwsh` actually importable; C8 (not this cohort) is what
#: declares it in `[project].dependencies`. Absence is covered separately
#: by the unmarked ImportError->SILENT case in `_dialect.py`'s own test
#: surface, not re-derived here.
_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _GRAMMAR_PRESENT,
    reason="PowerShell grammar package not installed; C8 declares it in pyproject.toml.",
)


def _payload(command, agent_id=None, agent_type=None, tool_name="Bash"):
    p = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _deny_reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


def _advisory_text(out):
    assert out is not None, "expected an advisory envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "additionalContext" in hso
    return hso["additionalContext"]


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None

    def test_no_sentinel_mention_allows(self):
        assert guard.check(_payload("git status && ls -la")) is None


class TestDirectRemovalDenies:
    def test_rm_denies(self):
        _deny_reason(guard.check(_payload("rm %s" % SENTINEL)))

    def test_rm_dash_f_denies(self):
        _deny_reason(guard.check(_payload("rm -f ./%s" % SENTINEL)))

    def test_unlink_denies(self):
        _deny_reason(guard.check(_payload("unlink %s" % SENTINEL)))

    def test_mv_source_denies(self):
        _deny_reason(guard.check(_payload("mv %s /tmp/x" % SENTINEL)))

    def test_git_rm_denies(self):
        _deny_reason(guard.check(_payload("git rm %s" % SENTINEL)))

    def test_git_mv_source_denies(self):
        _deny_reason(guard.check(_payload("git mv %s other" % SENTINEL)))

    def test_git_dash_capital_c_rm_denies(self):
        _deny_reason(guard.check(_payload("git -C /repo rm %s" % SENTINEL)))

    def test_bash_dash_c_wrapped_rm_denies(self):
        _deny_reason(guard.check(_payload('bash -c "rm %s"' % SENTINEL)))

    def test_case_varied_basename_denies(self):
        _deny_reason(guard.check(_payload("rm .COORDINATOR-DEV-REPO")))

    def test_windows_separator_path_denies(self):
        # A backslash-separated path (Windows separator form) -- not a
        # literal machine path, purely a separator-style probe. Built with
        # doubled backslashes so a single backslash survives shlex's own
        # posix-mode escape handling in the tokenized command text.
        sep = chr(92) * 2
        cmd = "rm relative%sdir%s%s" % (sep, sep, SENTINEL)
        _deny_reason(guard.check(_payload(cmd)))

    def test_find_delete_denies(self):
        _deny_reason(guard.check(_payload("find . -name %s -delete" % SENTINEL)))

    def test_find_exec_rm_denies(self):
        _deny_reason(guard.check(_payload("find . -name %s -exec rm {} \\;" % SENTINEL)))

    def test_python_dash_c_remove_verb_denies(self):
        code = "import os; os.remove('%s')" % SENTINEL
        _deny_reason(guard.check(_payload('python3 -c "%s"' % code)))


class TestPassSet:
    """The regression tests that matter most -- ordinary work must not
    false-trip this guard."""

    def test_rm_unrelated_file_allows(self):
        assert guard.check(_payload("rm somefile.txt")) is None

    def test_mv_unrelated_allows(self):
        assert guard.check(_payload("mv a b")) is None

    def test_git_rm_unrelated_allows(self):
        assert guard.check(_payload("git rm coordinator/foo.py")) is None

    def test_python_read_only_probe_allows(self):
        code = "from pathlib import Path; print(Path('%s').exists())" % SENTINEL
        assert guard.check(_payload('python3 -c "%s"' % code)) is None

    def test_mention_in_comment_allows(self):
        assert guard.check(_payload("echo 'do not touch %s'" % SENTINEL)) is None

    def test_grep_allows(self):
        assert guard.check(_payload("grep -n %s some/file" % SENTINEL)) is None

    def test_cat_allows(self):
        assert guard.check(_payload("cat %s" % SENTINEL)) is None

    def test_mv_as_destination_allows(self):
        """Creation shape -- out of THIS guard's scope (the sentinel-
        creation guard's territory)."""
        assert guard.check(_payload("mv something %s" % SENTINEL)) is None


class TestAdvisoryOnUnexaminableIndirection:
    """Two-leg split (2026-08-05): the advisory envelope is now produced by
    `check_advisory()` only -- `check()` (the CONFINEMENT_DENY-registered
    leg) never returns it. See the module's own "TWO-LEG SPLIT" docstring
    section."""

    def test_xargs_advisory_not_deny(self):
        out = guard.check_advisory(_payload("echo %s | xargs rm" % SENTINEL))
        _advisory_text(out)

    def test_xargs_check_leg_allows_with_no_content(self):
        # The CONFINEMENT_DENY leg must not shadow anything for this input
        # -- it returns bare `None`, not the advisory envelope.
        assert guard.check(_payload("echo %s | xargs rm" % SENTINEL)) is None

    def test_bare_file_interpreter_unrelated_allows(self):
        out = guard.check_advisory(_payload("bash /tmp/some-script.sh"))
        # Unrelated to the sentinel -- a bare interpreter file with no
        # sentinel mention anywhere is allowed outright.
        assert out is None

    def test_stdin_piped_interpreter_advisory(self):
        out = guard.check_advisory(_payload("echo %s | bash" % SENTINEL))
        _advisory_text(out)

    def test_stdin_check_leg_allows_with_no_content(self):
        assert guard.check(_payload("echo %s | bash" % SENTINEL)) is None

    def test_advisory_does_not_name_sentinel(self):
        out = guard.check_advisory(_payload("echo %s | xargs rm" % SENTINEL))
        text = _advisory_text(out)
        assert SENTINEL not in text


class TestOverrideEnvVar:
    def test_override_allows_direct_deny(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL", "1")
        assert guard.check(_payload("rm %s" % SENTINEL)) is None

    def test_override_allows_indirection_advisory(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL", "1")
        assert guard.check(_payload("echo %s | xargs rm" % SENTINEL)) is None

    def test_override_allows_advisory_leg(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL", "1")
        assert guard.check_advisory(_payload("echo %s | xargs rm" % SENTINEL)) is None


class TestDenyMessageDiscipline:
    def test_deny_reason_never_names_the_sentinel(self):
        reason = _deny_reason(guard.check(_payload("rm %s" % SENTINEL)))
        assert SENTINEL not in reason

    def test_deny_reason_is_offer_shaped(self):
        reason = _deny_reason(guard.check(_payload("rm %s" % SENTINEL)))
        assert reason.split(":", 1)[1].strip().lower().startswith("instead")

    def test_deny_reason_advertises_override(self):
        # RETARGETED (2026-08-17, override-key message-register ruling): a
        # guard message names the guard that fired and nothing else about
        # its override -- no key, no assignment form
        # (docs/reference/guard-override-keys.md, opening sentence). This
        # deny message renders via the shared `operator_override_note`
        # helper, which no longer interpolates the bare key or any
        # assignment form -- it points to the reference doc instead.
        # Asserting a pasteable `KEY=1` literal was stale against that
        # doctrine.
        reason = _deny_reason(guard.check(_payload("rm %s" % SENTINEL)))
        # RETARGETED 2026-08-30 (DR-290 form 1 -> form 2).
        assert OVERRIDE_KEYS_DOC_DISPLAY in reason


class TestNotIdentityGated:
    def test_denies_without_any_identity_fields(self):
        _deny_reason(guard.check(_payload("rm %s" % SENTINEL)))

    def test_denies_with_subagent_identity(self):
        out = guard.check(
            _payload(
                "rm %s" % SENTINEL,
                agent_id="a0123456789abcdef",
                agent_type="coordinator:executor",
            )
        )
        _deny_reason(out)


class TestReachableThroughTheDispatchChain:
    """Guard-level tests are not sufficient -- `offer-git-c`'s allow+
    updatedInput short-circuit would hide an ordering bug (same class of
    bug the sibling sentinel guards were found and fixed for)."""

    @staticmethod
    def _decision(command):
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
        if not out:
            return "allow"
        rendered = json.dumps(out)
        if '"deny"' in rendered:
            return "deny"
        if "additionalContext" in rendered:
            return "advisory"
        return "allow"

    def test_bare_rm_denied_end_to_end(self):
        # Deny leg retired (C13); the sole registered leg now advises
        # instead of denying -- see module's own "CLASS-CENSUS CONVERSION".
        assert self._decision("rm %s" % SENTINEL) == "advisory"

    def test_cd_prefixed_rm_denied_end_to_end(self):
        assert self._decision("cd /repo && rm %s" % SENTINEL) == "advisory"

    def test_git_dash_c_prefixed_rm_denied_end_to_end(self):
        assert self._decision("git -C /repo status; git rm %s" % SENTINEL) == "advisory"

    def test_unrelated_rm_allowed_end_to_end(self):
        assert self._decision("rm somefile.txt") == "allow"

    def test_registered_ahead_of_offer_git_c(self):
        # Still must not fall through to offer-git-c's bare rewrite --
        # advisory now, not deny, but not a silent allow either.
        assert self._decision("cd /repo && rm %s" % SENTINEL) == "advisory"


class TestAdvisoryLegAtItsNewChainPosition:
    """The deny leg (`block-dev-repo-sentinel-removal`, a CONFINEMENT_DENY
    entry) was RETIRED by C13 (`docs/plans/2026-08-06-apply-guard-class-
    census.md`) -- `block-dev-repo-sentinel-removal-advisory` is now the
    guard's SOLE registered entry, still its OWN ADVISORY_REWRITE entry,
    registered after every CONFINEMENT_DENY hard deny and ahead of
    `offer-git-c`. Covers the gap named in `state/audits/2026-08-05-
    confinement-deny-band-return-shapes.md`: the three trigger shapes
    proven live-reachable there, plus the non-shadowing property the audit
    calls out as undefended."""

    #: The three trigger shapes verified reachable in the audit.
    _TRIGGER_SHAPES = [
        "bash s.sh  # %s" % SENTINEL,
        "echo %s | xargs rm" % SENTINEL,
        "find . -name '%s' | xargs rm" % SENTINEL,
    ]

    def test_advisory_leg_sits_after_every_confinement_deny_entry(self):
        chain = dispatch._build_guard_chain(
            cmd="echo x",
            session_id="chain-position-probe",
            cwd="/tmp",
            payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
            policy_file=None,
            host_is_windows=None,
        )
        names = [e.name for e in chain]
        assert "block-dev-repo-sentinel-removal" not in names
        advisory_idx = names.index("block-dev-repo-sentinel-removal-advisory")
        offer_git_c_idx = names.index("offer-git-c")

        assert chain[advisory_idx].band == dispatch.GuardBand.ADVISORY_REWRITE

        last_confinement_deny_idx = max(
            i for i, e in enumerate(chain) if e.band == dispatch.GuardBand.CONFINEMENT_DENY
        )
        assert advisory_idx > last_confinement_deny_idx
        assert advisory_idx < offer_git_c_idx

    def test_direct_deny_leg_never_returns_the_advisory_envelope(self):
        """The CONFINEMENT_DENY-registered `check()` must not return the
        advisory shape for any of the three trigger shapes -- only `None`
        or a deny."""
        for cmd in self._TRIGGER_SHAPES:
            out = guard.check(_payload(cmd))
            assert out is None, (cmd, out)

    def test_advisory_leg_fires_for_each_trigger_shape(self):
        for cmd in self._TRIGGER_SHAPES:
            out = guard.check_advisory(_payload(cmd))
            _advisory_text(out)

    def test_advisory_no_longer_shadows_a_downstream_deny(self):
        """An input that trips this guard's advisory AND is denied by a
        LATER hard-deny guard in the chain (here, `block-disarm-marker-
        sentinel-creation`, a CONFINEMENT_DENY entry sitting well before
        this guard's own advisory leg, which is now the guard's SOLE
        registered entry since C13 retired the deny leg) must still
        return that deny end-to-end. If this guard's ADVISORY_REWRITE
        entry ever regressed into shadowing (an `allow`+`additionalContext`
        return that `evaluate_payload_json` treated as terminal), it would
        short-circuit before the later guard was ever reached -- the exact
        band-contract violation `state/audits/2026-08-05-confinement-deny-
        band-return-shapes.md` found. Directly probes this guard's own
        `check()` first (proving IT specifically returns `None`, not
        content -- it is unregistered but still directly callable) before
        checking the overall end-to-end decision -- `block-approval-
        sentinel-creation` (an unrelated, earlier guard) also denies
        indirection shapes by construction and would mask this property if
        only the end-to-end decision were asserted, per this dispatch's
        own audit finding."""
        cmd = "echo %s | xargs rm; git stash drop" % SENTINEL
        payload = _payload(cmd)

        # This guard's own (unregistered) `check()` must not shadow
        # anything for this input.
        assert guard.check(payload) is None

        chain = dispatch._build_guard_chain(
            cmd=cmd,
            session_id="non-shadow-probe",
            cwd="/tmp",
            payload={
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "session_id": "non-shadow-probe",
                "cwd": "/tmp",
            },
            policy_file=None,
            host_is_windows=False,
        )
        names = [e.name for e in chain]
        start = 0
        # Walk the entire chain -- proves nothing before the first later
        # deny (including this guard's own advisory entry) returns an
        # allow-with-content envelope that would have masked it.
        first_non_none = None
        for i in range(start, len(chain)):
            envelope = chain[i].fn()
            if envelope is not None:
                first_non_none = (i, chain[i].name, envelope)
                break
        assert first_non_none is not None, "expected a later guard to deny this input"
        idx, name, envelope = first_non_none
        hso = envelope["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny", (name, envelope)
        assert "updatedInput" not in hso

        # And the overall end-to-end decision is a deny too, not an
        # accidental allow.
        out = dispatch.evaluate_payload_json(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestPowerShellDialect:
    """C4f (`docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-
    stay-silent.md`), triaged at `docs/reference/guard-dialect-coverage.md`
    row 26 -- `rm` already fires unaided (real PowerShell alias, C3's
    alias-collision finding); `unlink` has no PowerShell equivalent, so
    `Remove-Item`/`ri`/`rd`/`del` needed adding. Converted via
    `Dialect.POWERSHELL` in `_sentinel_removal_guard.evaluate` (widened
    `_remove_arg_denies`), tokenized through `_dialect.resolve_segments_
    for_dialect` (tree-sitter-pwsh) instead of the bash `shlex` path.
    """

    @requires_powershell_grammar
    def test_remove_item_denies(self):
        out = guard.check_advisory(
            _payload("Remove-Item %s" % SENTINEL, tool_name="PowerShell")
        )
        _advisory_text(out)

    @requires_powershell_grammar
    def test_ri_alias_denies(self):
        out = guard.check_advisory(_payload("ri %s" % SENTINEL, tool_name="PowerShell"))
        _advisory_text(out)

    @requires_powershell_grammar
    def test_rd_alias_denies(self):
        out = guard.check_advisory(_payload("rd %s" % SENTINEL, tool_name="PowerShell"))
        _advisory_text(out)

    @requires_powershell_grammar
    def test_del_alias_denies(self):
        out = guard.check_advisory(_payload("del %s" % SENTINEL, tool_name="PowerShell"))
        _advisory_text(out)

    @requires_powershell_grammar
    def test_rm_alias_still_denies_under_powershell(self):
        # `rm` is a real PowerShell alias -- already covered by alias
        # collision, not a new matcher; proven here under the PowerShell
        # dialect specifically, not just the bash leg above.
        out = guard.check_advisory(_payload("rm %s" % SENTINEL, tool_name="PowerShell"))
        _advisory_text(out)

    @requires_powershell_grammar
    def test_remove_item_with_recurse_force_flags_denies(self):
        out = guard.check_advisory(
            _payload(
                "Remove-Item -Recurse -Force %s" % SENTINEL, tool_name="PowerShell"
            )
        )
        _advisory_text(out)

    @requires_powershell_grammar
    def test_remove_item_unrelated_target_allows(self):
        assert (
            guard.check_advisory(_payload("Remove-Item somefile.txt", tool_name="PowerShell"))
            is None
        )

    @requires_powershell_grammar
    def test_git_rm_denies_under_powershell(self):
        # git rm/mv is a dialect-neutral external exe -- already correct,
        # exercised here under the PowerShell dialect's own tokenizer to
        # prove the tokenizer swap did not regress it.
        out = guard.check_advisory(_payload("git rm %s" % SENTINEL, tool_name="PowerShell"))
        _advisory_text(out)

    @requires_powershell_grammar
    def test_unlink_alias_has_no_powershell_equivalent_and_allows(self):
        # `unlink` is a bash-only spelling with no PowerShell alias at all
        # -- under the PowerShell dialect it must not deny/advise (it
        # cannot execute in PowerShell in the first place), unlike its
        # bash-leg coverage in TestDirectRemovalDenies.
        assert (
            guard.check_advisory(_payload("unlink %s" % SENTINEL, tool_name="PowerShell"))
            is None
        )

    def test_unrecognized_tool_name_records_silent_not_a_guess(self):
        with _verdict.collecting() as silences:
            out = guard.check_advisory(
                _payload("Remove-Item %s" % SENTINEL, tool_name="Zsh")
            )
        assert out is None
        assert _verdict.was_silent(guard._GUARD_NAME, silences)

    def test_check_leg_also_silent_on_unrecognized_dialect(self):
        with _verdict.collecting() as silences:
            out = guard.check(_payload("rm %s" % SENTINEL, tool_name="Zsh"))
        assert out is None
        assert _verdict.was_silent(guard._GUARD_NAME, silences)


class TestPowerShellIndirectionDeclinesRatherThanClean:
    """The indirection-wrapper walk (`xargs`/piped-interpreter/`-c`-flag/
    `env`-wrapped payloads) stays bash-only by design under
    `Dialect.POWERSHELL` -- out of C4f's triaged scope. Left silently
    unconverted, a PowerShell indirection wrapper would fall through to a
    bare ALLOW indistinguishable from a genuinely clean command -- the
    exact false-clean this plan exists to close. These pin that it instead
    records SILENT (declined to rule) rather than reading as cleared."""

    @requires_powershell_grammar
    def test_xargs_wrapper_records_silent_not_clean(self):
        with _verdict.collecting() as silences:
            out = guard.check_advisory(
                _payload("echo %s | xargs rm" % SENTINEL, tool_name="PowerShell")
            )
        assert out is None
        assert _verdict.was_silent(guard._GUARD_NAME, silences)

    @requires_powershell_grammar
    def test_dash_c_flag_wrapper_records_silent_not_clean(self):
        with _verdict.collecting() as silences:
            out = guard.check_advisory(
                _payload('bash -c "rm %s"' % SENTINEL, tool_name="PowerShell")
            )
        assert out is None
        assert _verdict.was_silent(guard._GUARD_NAME, silences)

    @requires_powershell_grammar
    def test_env_wrapped_direct_removal_still_denies_not_silent(self):
        # `_env_skip_index` already walks past a leading `env`/`VAR=value`
        # prefix to the real argv0 -- this is fully examinable and denies
        # DIRECTLY, the control proving the shape check does not
        # over-classify an env-prefixed but otherwise plain command as
        # unresolved indirection.
        with _verdict.collecting() as silences:
            out = guard.check_advisory(
                _payload("env FOO=bar rm %s" % SENTINEL, tool_name="PowerShell")
            )
        _advisory_text(out)
        assert not _verdict.was_silent(guard._GUARD_NAME, silences)

    @requires_powershell_grammar
    def test_ordinary_command_does_not_record_silent(self):
        """A genuinely clean, non-wrapper PowerShell command must NOT
        record SILENT -- only actual indirection shapes do; this is the
        control proving the shape check is not over-firing."""
        with _verdict.collecting() as silences:
            out = guard.check_advisory(_payload("Get-ChildItem", tool_name="PowerShell"))
        assert out is None
        assert not _verdict.was_silent(guard._GUARD_NAME, silences)
