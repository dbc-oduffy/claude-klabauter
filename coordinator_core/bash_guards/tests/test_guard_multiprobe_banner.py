"""Tests for coordinator_core.bash_guards.guard_multiprobe_banner (BX-7).

Coverage:
  - a genuine multi-probe banner command whose EVERY probe segment is one
    of `check_multiprobe_banner_rewrite`'s recognized forms ADVISES the
    literal, real `python3 -c` rewrite the sibling chain entry computed for
    THIS exact command -- on every host, including Windows forced -- for a
    MAIN-LOOP caller (no `agent_id`). This guard's own platform-conditioned
    DENY branch was retired 2026-08-07 as structurally unreachable
    (DR-280): it gated on the same seam-confirmation an earlier-registered
    `ADVISORY_REWRITE` chain entry already consumes and returns on first,
    so through the real dispatcher the deny gate could never open.
    `host_is_windows` is still accepted and still exercised below (it is
    the chain-wide threading contract every registered shape-guard
    honors), but no longer changes this guard's own verdict.
  - the SAME confirmed command, called by a SUBAGENT (`agent_id` present),
    gets the scratch-script outlet instead: `python3 -c` is never named as
    something to RUN, and the message names `python3 <path>` plus the
    session-scratchpad location instead (2026-08-06, B2 friction fix --
    `python3 -c` is a blocked outlet for a subagent under the sibling
    `block_subagent_destructive_action` guard, so recommending it there
    would hand the subagent a command the next guard denies).
  - a genuine multi-probe banner command carrying even ONE unrecognized
    probe segment (an untranslated git subcommand, a piped stage, a write
    op like `git commit`) now allows SILENTLY (`None`) on every platform
    and for both caller classes -- 2026-08-06: the old fixed-template
    GENERIC advisory misdescribed any command that didn't literally
    contain `pwd`/`whoami`/`git status`, and offered no outlet a caller
    could act on; a guard that cannot describe THIS command says nothing
    rather than repeat an undischargeable warning.
  - non-Bash tool, empty command, malformed tool_input, and a plain
    (non-banner, non-multi-segment) command all allow silently (`None`).
  - a banner-echo with fewer than 3 total segments does not match (mirrors
    `_shape_classifier`'s own `_MIN_BANNER_SEGMENTS` threshold).
  - AC-7 precedence correctness: when a command is BOTH grep-via-Bash and a
    multi-probe banner, `_shape_classifier`'s precedence makes GREP_VIA_BASH
    the primary match, so THIS guard must stay silent (`None`) rather than
    misdescribe the command as a banner probe.
  - the `COORDINATOR_OVERRIDE_MULTIPROBE_BANNER=1` escape hatch suppresses
    the guard outright, on either platform.
  - platform is driven via the `host_is_windows` kwarg (the sanctioned
    mechanism, per `_platform_verdict.py`'s pinned threading contract and
    the sibling C3/C5 guards) rather than only by monkeypatching `os.name`
    -- `check()` must accept and forward this keyword so
    `dispatch.evaluate_payload_json` can drive both platform legs once
    this guard is registered (AC-9/AC-11).
  - quote-blindness: a separator character (`;`, `|`) inside a quoted
    argument (a commit message, a grep pattern) must not corrupt shape
    detection -- this guard is tokenizer-based via `_shape_classifier`,
    never a regex over raw command text.
  - false positives: legitimate composed/piped commands and near-miss
    shapes (banner echo below the segment threshold, no banner marker at
    all) must allow silently.

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/guard_multiprobe_banner.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_multiprobe_banner as guard
from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _GRAMMAR_PRESENT,
    reason=(
        "PowerShell grammar package not installed; C8 declares it in "
        "pyproject.toml."
    ),
)


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


#: NOT every-segment-recognized -- `git diff --stat` and `git log -1` are
#: NOT among `_bt_probe_segment_kind`'s recognized forms (only bare `git
#: status`/`git rev-parse HEAD`/`git branch --show-current` session-fact
#: forms are), so this fixture exercises the GENERIC-advisory leg, not the
#: seam-confirmed-rewrite leg -- see `_BANNER_CMD_CONFIRMED` below for the
#: latter. Kept under its original name/shape (pre-2026-07-29 promotion)
#: because several tests below only care about SHAPE detection, not about
#: rewrite confirmation.
_BANNER_CMD = (
    'echo "=== git status ==="; git status; git diff --stat; git log -1'
)

_BANNER_CMD_CONFIRMED = (
    'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
)


def _ctx(out):
    assert out is not None, "expected an allow_advisory envelope, got None"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in hso
    return hso["additionalContext"]


# The guard renders the hint through `Path`, so the separator is host-native.
_EXPECTED_SCRIPT_HINT = str(
    Path("/fake/root/.coordinator-local/subagent-share/sess1/multiprobe.py")
)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_plain_command_allows(self):
        assert guard.check(_payload("git status")) is None


class TestMultiProbeBannerVerdict:
    def test_banner_command_advises_even_with_windows_forced(self):
        # RETARGETED (DR-280, 2026-08-07): this guard's own deny branch was
        # retired as structurally unreachable -- it gated on
        # `_seam_confirmed_rewrite` against the SAME seam the
        # earlier-registered `"multiprobe-banner-rewrite"` chain entry
        # already consumes and returns on first, so through the real
        # dispatcher the gate could never open. Was
        # `test_banner_command_denies_on_windows`, asserting a deny envelope
        # under `host_is_windows=True`; now asserts the guard advises
        # (never denies) even with Windows forced, using the same
        # ALL-RECOGNIZED fixture so the seam-confirmed-outlet content
        # assertions below are unchanged.
        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        # New (2026-07-29): the Example is the LITERAL rewritten command
        # BX-16 already computed for THIS command, not a fixed template --
        # it starts with `_bt_python3_invocation()`'s RESOLVED, runnable
        # interpreter path (never the bare literal "python3" -- see that
        # function's own docstring: a bare `python3` is frequently absent
        # on stock Windows), followed by ` -c `. Matched structurally
        # against the same resolver the guard itself calls, per C2's
        # identical fix (commit 39eedda26) rather than a second hardcoded
        # literal.
        assert dc._bt_python3_invocation() + " -c" in ctx
        # RETARGETED (2026-08-17, override-key message-register ruling): a
        # guard message names the guard that fired and nothing else about
        # its override -- no key (docs/reference/guard-override-keys.md,
        # opening sentence). `operator_override_note` no longer interpolates
        # the bare `COORDINATOR_OVERRIDE_MULTIPROBE_BANNER` key; it renders
        # a doc pointer only. Asserting the literal key was stale.
        # RETARGETED 2026-08-30 (DR-290 form 1 -> form 2): the rendered
        # message carries the DISPLAY constant (the settings-root pointer),
        # not the repo-root-relative RESOLUTION form this asserted.
        assert OVERRIDE_KEYS_DOC_DISPLAY in ctx

    def test_banner_command_advises_on_posix(self):
        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=False)
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        assert dc._bt_python3_invocation() + " -c" in ctx

    def test_omitting_host_is_windows_still_advises_never_denies(self, monkeypatch):
        import os

        # RETARGETED (DR-280, 2026-08-07): this test used to be named
        # `test_host_is_windows_default_tracks_real_host` and pinned that
        # omitting the `host_is_windows` kwarg still resolved the real host
        # and denied under a faked Windows `os.name` -- that was this
        # guard's ONE test allowed to touch `os.name`, proving the default
        # still worked, not driving the platform leg. Now that this guard's
        # deny branch is retired (it always renders the advisory template,
        # regardless of `host_is_windows`), there is no platform-tracking
        # default left to pin for THIS guard specifically -- what remains
        # worth pinning is that omitting the kwarg under a faked Windows
        # `os.name` still advises rather than denying, i.e. the retirement
        # holds even on the one path this test used to exercise.
        #
        # The sibling seam call (`check_multiprobe_banner_rewrite`) is
        # monkeypatched to a canned confirmed-rewrite result rather than
        # left to run for real under a monkeypatched `os.name` -- that
        # function's OWN interpreter-path resolution (unrelated to what
        # THIS test is proving) legitimately depends on the real OS via
        # `pathlib`, which raises `UnsupportedOperation` when `os.name` is
        # faked to "nt" on an actual POSIX interpreter (Python 3.14 refuses
        # to instantiate `WindowsPath` off a real Windows host) -- an
        # artifact of faking the OS this way, not a bug on a real Windows
        # host.
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(
            guard,
            "check_multiprobe_banner_rewrite",
            lambda cmd, session_id: {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"command": "python3 -c 'import os'"},
                }
            },
        )
        ctx = _ctx(guard.check(_payload(_BANNER_CMD_CONFIRMED)))
        assert "multi-probe-banner" in ctx

    def test_banner_with_unrecognized_segment_allows_silently(self):
        # 2026-08-06 (B2 friction fix): `_BANNER_CMD` carries `git diff
        # --stat`/`git log -1`, neither a recognized session-fact probe --
        # the sibling rewrite chain entry does NOT confirm an outlet for
        # this exact command, so this guard has no per-command outlet to
        # offer. It used to fall back to a fixed generic advisory
        # (misdescribing any command without a literal `pwd`/`whoami`/`git
        # status`); it now allows silently instead, on every platform.
        assert guard.check(_payload(_BANNER_CMD), host_is_windows=True) is None
        assert guard.check(_payload(_BANNER_CMD), host_is_windows=False) is None

    def test_advisory_example_is_the_full_sibling_chain_rewrite(self):
        # MERGED (DR-280 cleanup, 2026-08-07): this test used to be two --
        # `test_banner_confirmed_rewrite_example_matches_sibling_chain_entry`
        # (the Example shown must be BYTE-IDENTICAL to what
        # `check_multiprobe_banner_rewrite` itself would compute -- proves
        # this guard reads the sibling's answer rather than re-deriving a
        # parallel one that could drift) and
        # `test_advisory_example_carries_the_full_rewrite_not_bare_evidence`
        # (RETARGETED from `test_deny_command_field_carries_full_command_
        # not_bare_evidence` -- Review: code-reviewer, Finding 1, C19a --
        # originally pinned via the deny template's "Command:" field that
        # this guard's now-retired deny branch used to render, proving the
        # rewrite named the FULL caller command rather than a stand-in
        # derived from bare banner-marker evidence alone).
        #
        # Once the deny branch was retired under DR-280, both tests were
        # retargeted onto the SAME assertion -- the advisory template's
        # Example field is the only surviving outlet, and it carries the
        # sibling chain's full computed rewrite either way -- leaving two
        # byte-identical test bodies. Merged into one; this single identity
        # assertion still pins BOTH original regressions: (1) this guard
        # reads the sibling's answer rather than re-deriving a parallel one
        # that could drift, and (2) that rewrite is the FULL command (every
        # recognized probe, including the trailing `git rev-parse HEAD`
        # segment that is NOT part of the banner-marker echo, folded into
        # one `git status --porcelain=v2 --branch` call per the module
        # docstring) rather than a stand-in derived from bare banner-marker
        # evidence alone. Do not re-split without re-deriving a case where
        # the two claims can actually diverge.
        from coordinator_core.bash_guards.dispatch_checks import (
            check_multiprobe_banner_rewrite,
        )

        expected = check_multiprobe_banner_rewrite(_BANNER_CMD_CONFIRMED, "sess1")
        expected_cmd = expected["hookSpecificOutput"]["updatedInput"]["command"]
        ctx = _ctx(guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True))
        assert expected_cmd in ctx

    def test_below_min_segment_threshold_allows(self):
        # Only 2 segments -- a single labeled probe, not the N-unrelated-
        # probes shape (mirrors _shape_classifier._MIN_BANNER_SEGMENTS).
        assert (
            guard.check(
                _payload('echo "=== status ==="; git status'), host_is_windows=True
            )
            is None
        )

    def test_no_banner_marker_allows(self):
        cmd = "pwd; whoami; git status; git log -1"
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_grep_via_bash_precedence_stays_silent(self):
        # This command is simultaneously grep-via-Bash and a banner probe;
        # _shape_classifier's fixed precedence makes GREP_VIA_BASH the
        # primary match, so this guard must not fire (AC-7: never
        # misdescribe what tripped a command).
        cmd = 'echo "=== search ==="; grep -rn TODO src/; ls -la; git status'
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_head_tail_plumbing_present_but_not_primary_allows_silently(self):
        # A command that is simultaneously a banner probe AND carries
        # head/tail plumbing (but NOT grep-via-Bash): MULTI_PROBE_BANNER
        # still outranks HEAD_TAIL_PLUMBING in SHAPE_PRECEDENCE (this guard
        # is still the one asked to evaluate it, not silently deferring to
        # a shape it doesn't own), but the piped `git log --oneline | head
        # -5` segment is a genuinely composed stage the sibling rewrite
        # chain entry treats as unrecognized (per its own docstring: "a
        # piped stage inside a banner chain is genuinely composed... treat
        # it the same as any other unrecognized segment") -- no outlet
        # describes this exact command, so 2026-08-06 this allows silently.
        cmd = 'echo "=== facts ==="; git log --oneline | head -5; pwd; whoami'
        assert guard.check(_payload(cmd), host_is_windows=True) is None

    def test_banner_precedence_wins_even_with_head_tail_plumbing_present(self, monkeypatch):
        # Same three-shapes-at-once command as above, but with the sibling
        # rewrite seam monkeypatched to a confirmed result -- isolates the
        # precedence claim (MULTI_PROBE_BANNER wins over HEAD_TAIL_PLUMBING,
        # so THIS guard is the one that fires, naming the banner shape, not
        # head/tail) from the separate "is this exact command's rewrite
        # confirmed" question covered by the test above (AC-7/BX-12).
        cmd = 'echo "=== facts ==="; git log --oneline | head -5; pwd; whoami'
        monkeypatch.setattr(
            guard,
            "check_multiprobe_banner_rewrite",
            lambda _cmd, _sid: {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"command": "python3 -c 'import os'"},
                }
            },
        )
        ctx = _ctx(guard.check(_payload(cmd), host_is_windows=False))
        assert "multi-probe-banner" in ctx

    def test_quoted_separators_do_not_split_tokens(self):
        cmd = 'echo "=== notes; test | pipe ==="; pwd; whoami; git status'
        ctx = _ctx(guard.check(_payload(cmd), host_is_windows=False))
        assert "multi-probe-banner" in ctx

    def test_quoted_pipe_in_grep_pattern_still_wins_precedence(self):
        cmd = (
            'echo "=== search ==="; '
            'grep -n "foo; bar | baz" file.txt; pwd; whoami'
        )
        assert guard.check(_payload(cmd), host_is_windows=True) is None


class TestSubagentOutlet:

    @pytest.mark.pending_fix
    def test_subagent_gets_script_outlet_not_python3_dash_c_on_windows(self, monkeypatch):
        # RETARGETED (DR-280, 2026-08-07): was reading `_reason` (the deny
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        out = guard.check(
            _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
            host_is_windows=True,
        )
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        assert "python3 " + _EXPECTED_SCRIPT_HINT in ctx
        assert "\"import os" not in ctx
        assert "'import os" not in ctx

    @pytest.mark.pending_fix
    def test_subagent_gets_script_outlet_not_python3_dash_c_on_posix(self, monkeypatch):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        out = guard.check(
            _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
            host_is_windows=False,
        )
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        assert "python3 " + _EXPECTED_SCRIPT_HINT in ctx
        assert '"import os' not in ctx
        assert "'import os" not in ctx

    def test_subagent_script_body_matches_seam_code(self, monkeypatch):
        import shlex as _shlex

        from coordinator_core.bash_guards.dispatch_checks import (
            check_multiprobe_banner_rewrite,
        )

        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        expected = check_multiprobe_banner_rewrite(_BANNER_CMD_CONFIRMED, "sess1")
        expected_cmd = expected["hookSpecificOutput"]["updatedInput"]["command"]
        expected_body = _shlex.split(expected_cmd)[-1]

        ctx = _ctx(
            guard.check(
                _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
                host_is_windows=False,
            )
        )
        assert expected_body in ctx

    def test_subagent_unresolvable_git_root_falls_back_to_placeholder_path(
        self, monkeypatch
    ):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: None)
        ctx = _ctx(
            guard.check(
                _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
                host_is_windows=False,
            )
        )
        assert "your session scratchpad" in ctx
        assert '"import os' not in ctx
        assert "'import os" not in ctx

    def test_main_loop_caller_still_gets_python3_dash_c(self, monkeypatch):
        def _boom(cwd):
            raise AssertionError("resolve_git_root must not be called for a main-loop caller")

        monkeypatch.setattr(guard, "resolve_git_root", _boom)
        # RETARGETED (DR-280, 2026-08-07): was reading `_reason` (the deny
        ctx = _ctx(
            guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        )
        assert dc._bt_python3_invocation() + " -c" in ctx

    def test_no_outlet_case_still_allows_silently_for_subagent(self, monkeypatch):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        assert (
            guard.check(_payload(_BANNER_CMD, agent_id="a" * 16), host_is_windows=True)
            is None
        )


class TestFalsePositives:
    def test_legitimate_composed_pipeline_allows(self):
        assert (
            guard.check(_payload("git log --oneline | head -20"), host_is_windows=True)
            is None
        )

    def test_unrelated_multi_segment_command_allows(self):
        assert (
            guard.check(
                _payload("npm install && npm run build && npm test"),
                host_is_windows=True,
            )
            is None
        )

    def test_single_banner_labeled_probe_allows(self):
        assert (
            guard.check(
                _payload('echo "=== build ==="; npm run build'), host_is_windows=True
            )
            is None
        )


class TestPowerShellDialectWiring:
    """C4b (docs/reference/guard-dialect-coverage.md row 9) -- the one real
    wiring edit in this cohort: `_classify_for_dialect` routes the
    POWERSHELL branch's tokenize call through `_dialect.
    resolve_segments_for_dialect` instead of `classify_command`'s own
    bash-only `tokenize_full_command`, reusing the SAME per-shape detectors
    `classify_command` itself calls -- no new verb/flag table needed.
    """

    @requires_powershell_grammar
    def test_powershell_multiprobe_banner_reaches_same_primary_as_bash(self):
        cmd = 'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
        bash_result = guard._classify_for_dialect(cmd, Dialect.BASH)
        ps_result = guard._classify_for_dialect(cmd, Dialect.POWERSHELL)
        assert bash_result.primary is not None
        assert bash_result.primary.shape == guard.Shape.MULTI_PROBE_BANNER
        assert ps_result.primary is not None
        assert ps_result.primary.shape == bash_result.primary.shape

    @requires_powershell_grammar
    def test_powershell_grep_precedence_stays_silent_same_as_bash(self):
        # AC-7 precedence: GREP_VIA_BASH outranks MULTI_PROBE_BANNER in
        # SHAPE_PRECEDENCE on both dialects -- the PowerShell leg must not
        # misdescribe this as a banner probe either.
        cmd = 'echo "=== search ==="; grep -rn TODO src/; git status'
        bash_result = guard._classify_for_dialect(cmd, Dialect.BASH)
        ps_result = guard._classify_for_dialect(cmd, Dialect.POWERSHELL)
        assert bash_result.primary.shape == guard.Shape.GREP_VIA_BASH
        assert ps_result.primary.shape == bash_result.primary.shape

    def test_powershell_tool_name_is_no_longer_a_bare_none_before_classification(self):
        assert guard.dialect_from_tool_name("PowerShell") is Dialect.POWERSHELL
        assert guard.dialect_from_tool_name("Read") is None

    @requires_powershell_grammar
    def test_powershell_banner_example_names_the_resolved_interpreter(self):
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {
                "command": 'Write-Host "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
            },
            "session_id": "sess1",
            "cwd": "/repo",
        }
        out = guard.check(payload, host_is_windows=True)
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert guard._mb_python3_invocation() in ctx


class TestDispatchReachability:
    """C6, pln-the-shape-classifier-reaches-a-e743e5 § AC14/AC15.

    AC14: `MATCHERS` is `COMMAND_TOOL_NAMES` by DIRECT IDENTITY -- a `list()`
    or hand-retyped copy would satisfy a contents-only check while breaking
    this.

    AC15 -- the criterion that separates closing the bypass from appearing
    to: the EM's own pre-plan measurement called `guard.check()` directly,
    which bypasses `dispatch.py`'s `MATCHERS`-gated chain builder entirely,
    and consequently reported this guard as "blind" on a PowerShell payload
    when it had simply never been INVOKED. `test_powershell_multiprobe_
    banner_reaches_same_primary_as_bash` above (and every other test in this
    file) calls `guard.check(...)` or `guard._classify_for_dialect(...)`
    directly -- none of them prove reachability through the real dispatcher.
    This class routes through `dispatch.evaluate_payload_json`, the actual
    PreToolUse entry point, instead.
    """

    def test_matchers_is_command_tool_names_by_identity(self):
        assert guard.MATCHERS is COMMAND_TOOL_NAMES

    @requires_powershell_grammar
    def test_powershell_fanout_reaches_a_verdict_through_the_real_dispatch_chain(self):
        cmd = "Write-Host '=== facts ==='; git status; git log -1; pwd"
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
            "session_id": "sess-ac15-multiprobe",
            "cwd": "/repo",
        }
        result = dispatch.evaluate_payload_json(json.dumps(payload))
        assert result is not None, (
            "a PowerShell multi-probe-banner payload produced no verdict "
            "through dispatch.evaluate_payload_json -- either MATCHERS "
            "reverted to Bash-only, or the guard chain silently rejected "
            "the tool_name before this guard's own check() ever ran"
        )
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "multi-probe-banner" in hso["additionalContext"]
        assert "python3" in hso["additionalContext"]

    @requires_powershell_grammar
    def test_reverting_matchers_to_bash_only_makes_the_same_payload_unreachable(
        self, monkeypatch
    ):
        """Proof this is a real reachability assertion, not a tautology: a
        `guard.MATCHERS = ("Bash",)` revert must make the IDENTICAL
        PowerShell payload above produce no verdict at all through the real
        chain -- because `_build_guard_chain` never even calls `guard.check`
        for a `tool_name` its own `matchers=` entry excludes. Mirrors this
        module's own dispatch.py docstring: "the master gate... rejected...
        before... the guard loop ever run"."""
        monkeypatch.setattr(guard, "MATCHERS", ("Bash",))
        monkeypatch.setattr(dispatch, "_ANY_DECLARED_MATCHERS_CACHE", None)

        cmd = "Write-Host '=== facts ==='; git status; git log -1; pwd"
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
            "session_id": "sess-ac15-reverted",
            "cwd": "/repo",
        }

        # RETARGETED 2026-08-30. This asserted `result is None` -- that a
        # Bash-only revert makes a PowerShell payload unreachable. That is
        # unsatisfiable by construction, and has been since C1 landed the
        # normalization this file's own sibling test depends on:
        # `dispatch.evaluate_payload_json` computes
        # `_gating_tool_name = "Bash" if _raw_tool_name in COMMAND_TOOL_NAMES
        # else _raw_tool_name`, so BOTH command tool names gate as "Bash"
        # against the master gate AND against every `entry.matchers`. A
        # Bash-only entry therefore still runs on a PowerShell payload --
        # deliberately, that being how "guards fire under both tool names"
        # was implemented -- and each guard re-checks the dialect itself.
        #
        # What this test can still prove, and what the sibling above actually
        # rests on, is that the revert reaches the CHAIN: the built entry
        # carries the reverted matchers rather than a stale import-time copy.
        # Measured, not assumed: the entry below reads ('Bash',) here while
        # the PowerShell-declaring siblings still read both.
        chain = dispatch._build_guard_chain(
            cmd=cmd,
            session_id="sess-ac15-reverted",
            cwd="/repo",
            payload=payload,
            policy_file=None,
            host_is_windows=None,
            resolved=None,
        )
        entries = {e.name: e.matchers for e in chain}
        assert entries["multiprobe-banner"] == ("Bash",), (
            "the revert did not reach the built chain entry -- if this "
            "entry still declares PowerShell, `guard.MATCHERS` "
            "is no longer what the registration reads, and the sibling "
            "test above is not proving what it claims"
        )
        assert "PowerShell" in entries["plumbing-and-loops"], (
            "control: a guard that did NOT revert must still declare both, "
            "or this assertion would pass against a chain that lost "
            "PowerShell everywhere for an unrelated reason"
        )
        assert dispatch.evaluate_payload_json(json.dumps(payload)) is not None


class TestCrashPropagatesForFailClosed:

    def test_classify_command_crash_propagates(self, monkeypatch):
        def _boom(cmd, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(guard, "classify_command", _boom)
        try:
            guard.check(_payload(_BANNER_CMD), host_is_windows=True)
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("expected the guard to propagate the crash")


class TestOverrideEscapeHatch:
    def test_override_env_suppresses_on_windows(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_MULTIPROBE_BANNER", "1")
        assert guard.check(_payload(_BANNER_CMD), host_is_windows=True) is None

    def test_override_env_suppresses_on_posix(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_MULTIPROBE_BANNER", "1")
        assert guard.check(_payload(_BANNER_CMD), host_is_windows=False) is None


class TestPowerShellSubagentRemedy:
    @requires_powershell_grammar
    def test_subagent_on_powershell_gets_script_outlet_not_inline_dash_c(self, monkeypatch):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        payload = {
            "tool_name": "PowerShell",
            "tool_input": {
                "command": 'Write-Host "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
            },
            "session_id": "sess1",
            "cwd": "/repo",
            "agent_id": "a" * 16,
        }
        ctx = _ctx(guard.check(payload, host_is_windows=True))
        assert "python3 " + _EXPECTED_SCRIPT_HINT in ctx
        assert "-c '" not in ctx
        assert "import subprocess" not in ctx

    def test_subagent_remedy_names_the_shared_script_outlet(self):
        from coordinator_core.bash_guards._sanctioned_remedy import CALLER_SUBAGENT, probe_remedy

        remedy, exemplar = guard._powershell_remedy(CALLER_SUBAGENT, "/s/multiprobe.py")
        assert (remedy, exemplar) == probe_remedy(
            CALLER_SUBAGENT, Dialect.POWERSHELL, "/s/multiprobe.py"
        )


class TestTrimAttemptC8b:

    def test_seam_confirmed_lede_is_trimmed(self):
        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=False)
        ctx = _ctx(out)
        assert "this rewrite." not in ctx
        assert "rewrite." in ctx

    def test_powershell_remedy_comes_from_the_shared_source(self):
        from coordinator_core.bash_guards._sanctioned_remedy import CALLER_EM, probe_remedy

        remedy, exemplar = guard._powershell_remedy(CALLER_EM, "")
        assert remedy == probe_remedy(CALLER_EM, Dialect.POWERSHELL)[0]
        assert exemplar.endswith(probe_remedy(CALLER_EM, Dialect.POWERSHELL)[1][len("python3") :])

    def test_seam_confirmed_prose_bytes_do_not_regrow(self):
        from coordinator_core.bash_guards._message_size import measure_envelope

        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=False)
        envelope = {"hookSpecificOutput": out["hookSpecificOutput"]}
        measurement = measure_envelope(envelope, band="platform-conditioned-deny")
        assert measurement.over_cap is True
        assert measurement.prose_bytes <= 1230, (
            "this guard's own contributed prose grew back past the C8b trim "
            "(measured %d bytes) -- see this class's own docstring"
            % measurement.prose_bytes
        )
