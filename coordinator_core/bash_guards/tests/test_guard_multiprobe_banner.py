"""Tests for coordinator_core.bash_guards.guard_multiprobe_banner (BX-7).

Coverage:
  - a genuine multi-probe banner command whose EVERY probe segment is one
    of `check_multiprobe_banner_rewrite`'s recognized forms DENIES on
    Windows (naming the literal, real `python3 -c` rewrite the sibling
    chain entry computed for THIS exact command) and ADVISES the same
    literal rewrite elsewhere -- for a MAIN-LOOP caller (no `agent_id`).
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

from coordinator_core.bash_guards import guard_multiprobe_banner as guard


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

#: Every segment IS one of `_bt_probe_segment_kind`'s recognized forms
#: (echo, pwd, whoami, bare `git status`, `git rev-parse HEAD`) -- the
#: sibling rewrite chain entry (`check_multiprobe_banner_rewrite`)
#: genuinely confirms a rewrite for this exact command, so this is the
#: fixture for testing the promoted (2026-07-29) seam-confirmed deny/advise
#: path -- the literal rewritten command should appear in the message.
_BANNER_CMD_CONFIRMED = (
    'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
)


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "additionalContext" not in hso
    return hso["permissionDecisionReason"]


def _ctx(out):
    assert out is not None, "expected an allow_advisory envelope, got None"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in hso
    return hso["additionalContext"]


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
    def test_banner_command_denies_on_windows(self):
        # Driven via the host_is_windows kwarg -- the sanctioned mechanism
        # (AC-8/AC-11), not os.name monkeypatching. Uses the ALL-RECOGNIZED
        # fixture -- the sibling rewrite chain entry genuinely confirms an
        # outlet for this exact command, so a deny toward it is a deny
        # toward a real target, not a hazard.
        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        reason = _reason(out)
        assert "multi-probe-banner" in reason
        # New (2026-07-29): the Example is the LITERAL rewritten command
        # BX-16 already computed for THIS command, not a fixed template --
        # it always starts with a python3 -c invocation of the generated
        # script.
        assert "python3 -c" in reason
        assert "COORDINATOR_OVERRIDE_MULTIPROBE_BANNER" in reason

    def test_banner_command_advises_on_posix(self):
        out = guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=False)
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        assert "python3 -c" in ctx

    def test_host_is_windows_default_tracks_real_host(self, monkeypatch):
        import os

        # Omitting the kwarg must still track the real host (mirrors
        # guard_grep_via_bash's own contract) -- this is the ONLY test in
        # this module allowed to touch os.name, and it exists specifically
        # to prove the default still works, not to drive the platform leg.
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
        # host, and not what `_resolve_host_is_windows`'s own default
        # (which THIS module's `check()` calls unconditionally by omitting
        # `host_is_windows`) is responsible for.
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
        reason = _reason(guard.check(_payload(_BANNER_CMD_CONFIRMED)))
        assert "multi-probe-banner" in reason

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

    def test_banner_confirmed_rewrite_example_matches_sibling_chain_entry(self):
        # The Example shown must be BYTE-IDENTICAL to what
        # `check_multiprobe_banner_rewrite` itself would compute for the
        # same command -- proves this guard reads the sibling's answer
        # rather than re-deriving a parallel one that could drift.
        from coordinator_core.bash_guards.dispatch_checks import (
            check_multiprobe_banner_rewrite,
        )

        expected = check_multiprobe_banner_rewrite(_BANNER_CMD_CONFIRMED, "sess1")
        expected_cmd = expected["hookSpecificOutput"]["updatedInput"]["command"]
        reason = _reason(
            guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        )
        assert expected_cmd in reason

    def test_deny_command_field_carries_full_command_not_bare_evidence(self):
        # Review: code-reviewer -- Finding 1 (C19a): the "Command:" field in
        # the deny message must render the caller's FULL command, not
        # `primary.evidence` (only the matched banner-marker segment, e.g.
        # the bare `echo "=== facts ==="`). Pin that a segment which is NOT
        # part of the banner-marker echo (here, the trailing `git rev-parse
        # HEAD`) shows up in the deny reason -- `primary.evidence` alone
        # would never contain it.
        reason = _reason(
            guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        )
        assert "  Command:  %s" % _BANNER_CMD_CONFIRMED in reason
        assert "git rev-parse HEAD" in reason

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
        # Plenty of segments, no banner-marked echo -- must not fire.
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
        # Separator characters (`;`, `|`) INSIDE a quoted banner string must
        # not be treated as shell separators -- this guard is tokenizer-
        # based via _shape_classifier, never a regex over raw command text.
        # Every segment here (echo, pwd, whoami, git status) IS one of
        # `_bt_probe_segment_kind`'s recognized forms, so the seam confirms
        # a rewrite and this guard fires normally -- proving the quoted
        # `;`/`|` did not fabricate extra segments or corrupt the banner-
        # marker scan (a corrupted split would either misclassify the shape
        # entirely or make `classify_command` fail to match it, both of
        # which would make this assertion fail).
        cmd = 'echo "=== notes; test | pipe ==="; pwd; whoami; git status'
        ctx = _ctx(guard.check(_payload(cmd), host_is_windows=False))
        assert "multi-probe-banner" in ctx

    def test_quoted_pipe_in_grep_pattern_still_wins_precedence(self):
        # A grep pattern containing a literal `|` inside quotes must not
        # be split into a bogus extra piped segment -- confirms the
        # tokenizer, not a naive split, drives precedence here too.
        cmd = (
            'echo "=== search ==="; '
            'grep -n "foo; bar | baz" file.txt; pwd; whoami'
        )
        assert guard.check(_payload(cmd), host_is_windows=True) is None


class TestSubagentOutlet:
    """2026-08-06 (B2 friction fix): `python3 -c` is a blocked
    indirection-wrapper outlet for a subagent under
    `block_subagent_destructive_action` ("no subagent-honored override for
    this guard") -- recommending it there hands the subagent a command the
    next guard denies. These tests pin that a subagent caller (`agent_id`
    present) gets the scratch-script form instead, and that a main-loop
    caller (no `agent_id`/`agent_type`) is unaffected."""

    def test_subagent_gets_script_outlet_not_python3_dash_c_on_windows(self, monkeypatch):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        out = guard.check(
            _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
            host_is_windows=True,
        )
        reason = _reason(out)
        assert "multi-probe-banner" in reason
        assert "python3 /fake/root/state/subagent-share/sess1/multiprobe.py" in reason
        assert "\"import os" not in reason
        assert "'import os" not in reason

    def test_subagent_gets_script_outlet_not_python3_dash_c_on_posix(self, monkeypatch):
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/root")
        out = guard.check(
            _payload(_BANNER_CMD_CONFIRMED, agent_id="a" * 16),
            host_is_windows=False,
        )
        ctx = _ctx(out)
        assert "multi-probe-banner" in ctx
        assert "python3 /fake/root/state/subagent-share/sess1/multiprobe.py" in ctx
        assert '"import os' not in ctx
        assert "'import os" not in ctx

    def test_subagent_script_body_matches_seam_code(self, monkeypatch):
        # The script body handed to the subagent must be the SAME code the
        # seam already computed (recovered from its `-c <shlex.quote(...)>`
        # argv), not a re-derived stand-in.
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
        # No `agent_id`/`agent_type` at all -> identity resolution is
        # skipped outright (the no-agent-id short-circuit) -- pin that
        # `resolve_git_root` is never even called for this caller shape.
        def _boom(cwd):
            raise AssertionError("resolve_git_root must not be called for a main-loop caller")

        monkeypatch.setattr(guard, "resolve_git_root", _boom)
        reason = _reason(
            guard.check(_payload(_BANNER_CMD_CONFIRMED), host_is_windows=True)
        )
        assert "python3 -c" in reason

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


class TestCrashPropagatesForFailClosed:
    """Review: code-reviewer -- Finding 3: this guard is registered in
    `dispatch.py`'s `guard_chain` with `fail_closed=True`, whose whole
    contract is that an internal bug reaches `dispatch._crash_deny` rather
    than being swallowed as a silent allow. Before this fix, `check()`
    wrapped its entire body in a catch-all that returned `None` on ANY
    exception -- defeating that registration. Pins that a crash inside
    `check()` now propagates all the way out (uncaught), the same as
    `guard_grep_via_bash.check`."""

    def test_classify_command_crash_propagates(self, monkeypatch):
        def _boom(cmd):
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
