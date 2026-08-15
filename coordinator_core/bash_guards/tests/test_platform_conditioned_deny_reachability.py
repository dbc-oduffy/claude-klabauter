"""Chain-level reachability characterization for the two remaining
`PLATFORM_CONDITIONED_DENY` guards (`guard_multiprobe_banner`,
`guard_plumbing_and_loops`), asserted through the REAL dispatch entry
point rather than by calling either guard's `check()` in isolation.

Governing decision: `docs/decisions/DR-280-unreachable-deny-legs-retire-
rather-than.md`. DR-280 supersedes this plan's original C1 body (which
asked for "evidence a deny is reachable"); the ratified conclusion is the
opposite -- the deny legs are structurally UNREACHABLE and are being
RETIRED (C2, a separate chunk, not this file). C1's job under DR-280 is
the characterization + reachability evidence that justifies that
retirement and prevents this defect class recurring. These tests assert
CURRENT (pre-C2) behaviour and are written to STAY GREEN after C2 removes
the dead deny branches -- they are not designed-red, and none of them
pins a `deny` verdict as a requirement.

Negative-spec
-------------
Every assertion in this module goes through
`coordinator_core.bash_guards.dispatch.evaluate_payload_json` -- the real
dispatch entry point, exercising the full `_build_guard_chain` registration
order. Calling a guard's `check()` function directly is BANNED in this
file. This is not stylistic: `test_guard_multiprobe_banner.py`'s
`test_banner_command_denies_on_windows` and
`test_guard_plumbing_and_loops.py`'s `test_denies_on_windows` both call
`check()` directly, and both pass while asserting a `deny` verdict that
the real chain can never produce for the same input -- `_build_guard_chain`
registers each guard's `ADVISORY_REWRITE` rewrite-seam sibling
(`multiprobe-banner-rewrite`, `head-tail-plumbing-rewrite`) AHEAD of the
`PLATFORM_CONDITIONED_DENY` entry, and `evaluate_payload_json`'s loop
returns the first non-`None` envelope -- so a fully-recognized shape is
always intercepted by the rewrite entry first, and an unrecognized shape
makes the platform-conditioned guard's own gate (which re-checks the same
seam) return `None` too. Testing at the `check()` altitude is precisely
how this defect survived undetected: a green suite at that altitude proves
nothing about what a real caller, dispatched through
`evaluate_payload_json`, ever observes. See DR-280 §"A green suite proves
nothing for Defect A" (quoting the sibling plan's own anti-scope note).

Spec backlink: pln-the-platform-conditioned-deny-9c8e07 § C1
Spec backlink (governing decision): docs/decisions/DR-280-unreachable-deny-legs-retire-rather-than.md
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards.dispatch import evaluate_payload_json

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

#: Every segment is a recognized probe form for
#: `check_multiprobe_banner_rewrite` (echo banner, `pwd`, `whoami`, bare
#: `git status`, `git rev-parse` HEAD-form) -- the sibling rewrite chain
#: entry genuinely confirms an outlet for this exact command. Built ONLY
#: from recognized forms per this file's own dispatch brief: `git status`/
#: `git branch`/`git config --get` alone do NOT reach the gate (the seam
#: returns `None` for them), so a fixture built on those would not exercise
#: this case at all.
_MULTIPROBE_CONFIRMED_CMD = (
    'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; pwd; whoami'
)

#: Carries an unrecognized probe segment (`curl` is not one of
#: `check_multiprobe_banner_rewrite`'s recognized session-fact forms), so
#: the sibling rewrite seam does NOT confirm an outlet for this exact
#: command -- exercises the "unrecognized shape" case. Deliberately NOT a
#: `git` subcommand: an earlier-registered, unrelated advisory-rewrite
#: guard (`git-no-optional-locks`) fires on any `git` invocation ahead of
#: this guard's own chain entries, which would make this fixture exercise
#: THAT guard's chain position instead of the one under test here.
_MULTIPROBE_UNRECOGNIZED_CMD = (
    'echo "=== facts ==="; pwd; whoami; curl -s http://example.com'
)

#: `find . -type f | head -n 5` -- a two-stage `find | head` pipeline
#: `check_head_tail_plumbing_rewrite` translates outright (both the `find`
#: census and the `head` line-count form are recognized).
_PLUMBING_CONFIRMED_CMD = "find . -type f | head -n 5"

#: `docker ps | head -n 20` -- genuinely HEAD_TAIL_PLUMBING-shaped, but
#: `docker` is not a recognized upstream generator for
#: `check_head_tail_plumbing_rewrite`, so that seam returns a bare
#: advisory (no `updatedInput`) -- exercises the "unrecognized shape" case.
_PLUMBING_UNRECOGNIZED_CMD = "docker ps | head -n 20"


def _payload(command):
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": "/repo",
        }
    )


def _decision(out):
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecision"]


class TestMultiprobeBannerChainReachability:
    def test_fully_recognized_shape_windows_auto_rewrite_wins(self):
        # The rewrite entry (`multiprobe-banner-rewrite`, ADVISORY_REWRITE)
        # is registered ahead of `multiprobe-banner`
        # (PLATFORM_CONDITIONED_DENY) in `_build_guard_chain`. On Windows,
        # with no override, the auto-rewrite wins: allow + updatedInput,
        # and the platform-conditioned deny leg is never reached.
        out = evaluate_payload_json(
            _payload(_MULTIPROBE_CONFIRMED_CMD), host_is_windows=True
        )
        assert _decision(out) == "allow"
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_fully_recognized_shape_override_set_no_deny(self):
        # With the seam's own COORDINATOR_ALLOW_MULTIPROBE_BANNER override
        # set, the published bypass key must keep meaning bypass -- this is
        # the case that would have caught the abandoned repair (repairing
        # the deny leg by reaching it through this exact override would
        # have inverted "operator switched this guard off" into "operator
        # gets a hard deny on Windows").
        out = evaluate_payload_json(
            _payload(_MULTIPROBE_CONFIRMED_CMD), host_is_windows=True
        )
        # Baseline sanity: without the override this shape auto-rewrites.
        assert _decision(out) == "allow"

        import os

        prior = os.environ.get("COORDINATOR_ALLOW_MULTIPROBE_BANNER")
        os.environ["COORDINATOR_ALLOW_MULTIPROBE_BANNER"] = "1"
        try:
            out = evaluate_payload_json(
                _payload(_MULTIPROBE_CONFIRMED_CMD), host_is_windows=True
            )
        finally:
            if prior is None:
                os.environ.pop("COORDINATOR_ALLOW_MULTIPROBE_BANNER", None)
            else:
                os.environ["COORDINATOR_ALLOW_MULTIPROBE_BANNER"] = prior
        assert out is None or _decision(out) != "deny"

    def test_unrecognized_shape_stays_silent(self):
        # Neither the rewrite entry nor the platform-conditioned guard's
        # own gate confirms an outlet -- both allow silently. Locks in the
        # deliberate 2026-08-06 behaviour (guard_multiprobe_banner module
        # docstring, "SUBAGENT-AWARE OUTLET ... DROPPING THE UNDISCHARGEABLE
        # GENERIC ADVISORY").
        out = evaluate_payload_json(
            _payload(_MULTIPROBE_UNRECOGNIZED_CMD), host_is_windows=True
        )
        assert out is None

    def test_macos_leg_never_denies(self):
        out = evaluate_payload_json(
            _payload(_MULTIPROBE_CONFIRMED_CMD), host_is_windows=False
        )
        assert out is None or _decision(out) != "deny"


class TestPlumbingAndLoopsChainReachability:
    def test_fully_recognized_shape_windows_auto_rewrite_wins(self):
        # `head-tail-plumbing-rewrite` is registered ahead of
        # `plumbing-and-loops` -- same first-non-None-wins contract.
        out = evaluate_payload_json(
            _payload(_PLUMBING_CONFIRMED_CMD), host_is_windows=True
        )
        assert _decision(out) == "allow"
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_fully_recognized_shape_override_set_no_deny(self):
        import os

        prior = os.environ.get("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING")
        os.environ["COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING"] = "1"
        try:
            out = evaluate_payload_json(
                _payload(_PLUMBING_CONFIRMED_CMD), host_is_windows=True
            )
        finally:
            if prior is None:
                os.environ.pop("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING", None)
            else:
                os.environ["COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING"] = prior
        assert out is None or _decision(out) != "deny"

    def test_unrecognized_shape_emits_generic_advisory_never_deny(self):
        # `docker` is not a recognized upstream generator, so the seam
        # returns a bare advisory (no updatedInput) -- `guard_plumbing_
        # and_loops`'s own gate treats that identically to `None` and
        # falls back to its generic advisory (`allow_advisory`), on every
        # platform. Locks in the "plumbing-and-loops emits its generic
        # advisory" half of the dispatch brief's stated case.
        out = evaluate_payload_json(
            _payload(_PLUMBING_UNRECOGNIZED_CMD), host_is_windows=True
        )
        assert out is not None
        assert _decision(out) == "allow"
        assert "additionalContext" in out["hookSpecificOutput"]

    def test_macos_leg_never_denies(self):
        out = evaluate_payload_json(
            _payload(_PLUMBING_CONFIRMED_CMD), host_is_windows=False
        )
        assert out is None or _decision(out) != "deny"
