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
import tempfile
import uuid
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.bash_guards.tests import guard_message_corpus as corpus

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_MULTIPROBE_CONFIRMED_CMD = (
    'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; pwd; whoami'
)

_MULTIPROBE_UNRECOGNIZED_CMD = (
    'echo "=== facts $PWD ==="; pwd; whoami; curl -s http://example.com'
)

_PLUMBING_CONFIRMED_CMD = "find . -type f | head -n 5"

#: `xargs -n1 stat | head -n 20` -- genuinely HEAD_TAIL_PLUMBING-shaped, but
#: `xargs` is not a recognized upstream generator for
#: `check_head_tail_plumbing_rewrite`, so that seam yields no `updatedInput`
#: -- exercises the "unrecognized shape" case. `xargs` is multi-spawn, so the
#: guard's generic advisory still fires.
_PLUMBING_UNRECOGNIZED_CMD = "xargs -n1 stat | head -n 20"

#: A single-process upstream: no outlet is cheaper than the command itself.
_PLUMBING_SINGLE_PROCESS_CMD = "docker ps | head -n 20"


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
    hso = out["hookSpecificOutput"]
    if "permissionDecision" not in hso:
        assert "updatedInput" in hso
        return "allow"
    return hso["permissionDecision"]


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
        out = evaluate_payload_json(
            _payload(_PLUMBING_UNRECOGNIZED_CMD), host_is_windows=True
        )
        assert out is not None
        assert _decision(out) == "allow"
        assert "additionalContext" in out["hookSpecificOutput"]

    @pytest.mark.parametrize("host_is_windows", [True, False])
    def test_single_process_upstream_stays_silent(self, host_is_windows):
        out = evaluate_payload_json(
            _payload(_PLUMBING_SINGLE_PROCESS_CMD), host_is_windows=host_is_windows
        )
        assert out is None

    def test_macos_leg_never_denies(self):
        out = evaluate_payload_json(
            _payload(_PLUMBING_CONFIRMED_CMD), host_is_windows=False
        )
        assert out is None or _decision(out) != "deny"


#: Fail-closed guards with no `-fire` corpus row. Shrink-only: an entry leaves
#: when its row lands, and a new fail-closed guard may not join.
_NO_FIRE_ROW_YET = frozenset(
    {
        "block-disarm-marker-sentinel-creation",
        "block-stash-destruction",
        "block-subagent-stash-creation",
    }
)

#: Fail-closed guards whose isolated deny the real chain intercepts with a
#: rewrite or advisory first (DR-280); the classes above characterize them.
_CHAIN_SHADOWED = frozenset({"multiprobe-banner", "plumbing-and-loops"})


def _fail_closed_entries():
    chain = dispatch._build_guard_chain(
        cmd="echo reachability-probe",
        session_id="reachability-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )
    return [e for e in chain if e.fail_closed]


def _fire_rows():
    rows = corpus.CONFINEMENT_ROWS + corpus.PLATFORM_CONDITIONED_ROWS
    return {r.guard: r for r in rows if r.expected_speaker and r.row_id.endswith("-fire")}


def _verdict(out):
    if isinstance(out, dict):
        return out.get("hookSpecificOutput", {}).get("permissionDecision")
    return None


def _fire(row):
    """Return (isolated verdict, chain verdict) for one corpus fire row."""
    sid = "reachability-%s" % uuid.uuid4().hex
    with tempfile.TemporaryDirectory(dir=corpus._neutral_scratch_parent()) as scratch:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CLAUDE_CODE_SESSION_ID", sid)
            extra = dict(row.setup(Path(scratch), mp)) if row.setup else {}
            cmd = extra.pop(corpus._CMD_OVERRIDE_KEY, row.input)
            cwd = extra.pop(corpus._CWD_OVERRIDE_KEY, scratch)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "session_id": sid,
                "cwd": cwd,
            }
            payload.update(extra)
            isolated = corpus.capture_one_guard(
                row.guard, cmd, sid, cwd, payload, host_is_windows=row.host_is_windows
            )
            chain_out = evaluate_payload_json(
                json.dumps(payload), host_is_windows=row.host_is_windows
            )
    return _verdict(isolated.envelope), _verdict(chain_out)


def test_fail_closed_enumeration_is_non_empty():
    assert _fail_closed_entries()


def test_every_fail_closed_guard_has_a_fire_row_or_a_ledger_entry():
    rows = _fire_rows()
    missing = {e.name for e in _fail_closed_entries() if e.name not in rows}
    assert missing == _NO_FIRE_ROW_YET


@pytest.mark.parametrize(
    "name",
    sorted(
        e.name
        for e in _fail_closed_entries()
        if e.name not in _NO_FIRE_ROW_YET and e.name not in _CHAIN_SHADOWED
    ),
)
def test_a_deny_the_guard_issues_survives_the_chain(name):
    isolated, via_chain = _fire(_fire_rows()[name])
    if isolated != "deny":
        assert via_chain != "deny"
        return
    assert via_chain == "deny", f"{name} denies in isolation but the chain returned {via_chain!r}"


def test_shadowed_ledger_names_only_live_fail_closed_guards():
    live = {e.name for e in _fail_closed_entries()}
    assert _CHAIN_SHADOWED <= live
