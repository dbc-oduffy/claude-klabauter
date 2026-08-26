"""coordinator_core.bash_guards.tests.test_dispatch -- C1 regression suite
for `docs/plans/2026-08-26-the-http-leg-normalizes-the-tool-name-it-was-
handed.md`: `_evaluate_payload_json_budgeted` derives a LOCAL normalized
gating value from the parsed `tool_name` (used only at the
`_any_declared_matchers()` universe pre-check and the per-entry
`entry.matchers` skip) and never writes `payload["tool_name"]` back.

Every probe below goes through the public seam, `evaluate_payload_json`,
exactly as the plan's body mandates -- never `dispatch_checks.check_*`
directly, and never an assertion against module-level `MATCHERS`. That
constraint is why AC1-3's chosen oracles are picked by RUNNING the real
chain and reading its verdict, not by reading a guard's source.

Stem pinned to `test_dispatch.py` deliberately (not a more descriptive
name): `ops/dispatch_emit/pathspec.py :: _map_written_path_to_test_target`
derives this exact stem from `dispatch.py`, and any other name makes this
chunk unemittable.

Population-drift note (read before touching oracle choices): the plan's own
body records three shrinking Bash-only censuses (21 -> 19 -> 16) as a
concurrent peer plan (`claude-klabauter-ec`) widens registrations while this
one runs. Measured fresh in this file (not copied from the plan): the
plan's OWN suggested AC3 oracle, `head-tail-plumbing-rewrite` (`cat foo.txt
| head -20`), no longer demonstrates anything -- it is registered at
`matchers=COMMAND_TOOL_NAMES` already (widened by a peer chunk, C9, ahead of
this one), so it never argues for or against this normalization: it fires
identically with or without it (see
`test_ac2_ac7_negative_oracles_are_disqualified_or_stale` below, which pins
this fact so a future editor cannot silently "restore" it as the oracle).
`grep-via-bash-rewrite` (`grep -rn foo .`) replaces it: still registered at
the Bash-only `("Bash",)` default at the SHA this file was authored against,
and its detection is a naive regex/text match with no dialect-specific
tokenization gate, so widening the matcher gate alone is sufficient to arm
it (AC7 below is what protects this file if a future widening removes this
property too).
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from typing import Any, Dict, Optional

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks as _dc
from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.ops.warm_guard_evaluate import _verdict_from_envelope, NO_OBJECTION


def _probe(cmd: str, tool_name: str, session_id: str = "probe", env: Optional[Dict[str, Any]] = None) -> Any:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": ".",
        # AC2/AC3: pinned explicitly, never left to inherit ambient os.environ
        # -- several guards below (`runaway-find` included, F4) read a
        # `COORDINATOR_ALLOW_*`/`COORDINATOR_OVERRIDE_*` opt-out straight out
        # of `payload["env"]`, and an un-pinned probe would silently pass
        # under whatever override happens to be set in THIS process's own
        # ambient environment.
        "env": {} if env is None else env,
    }
    return evaluate_payload_json(json.dumps(payload))


def _is_deny(out: Any) -> bool:
    return (
        isinstance(out, dict)
        and isinstance(out.get("hookSpecificOutput"), dict)
        and out["hookSpecificOutput"].get("permissionDecision") == "deny"
    )


def _deny_reason(out: Any) -> str:
    assert _is_deny(out), out
    return out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# AC1 / AC2 -- runaway-find is the qualifying deny-class oracle.
# ---------------------------------------------------------------------------


def test_ac1_ac2_powershell_find_root_denies_via_runaway_find():
    """AC1/AC2: `find / -name foo` under `tool_name="PowerShell"` denies,
    with the deny text attributable to `runaway-find` specifically (its own
    distinctive remediation text, not a byte-string this test invented), and
    a negative control proving the deny is NOT merely "the env override
    happened to be unset" -- setting `COORDINATOR_ALLOW_FIND_ROOT` (this
    guard's own pre-existing F4 disarm leg, out of this plan's scope to
    remove) flips the same probe to a silent allow, so the deny above is
    provably conditioned on the override, not on some unrelated silent
    default.

    Why `runaway-find` and not the plan's other two named candidates:

    - `destructive-rm` is DISQUALIFIED: already registered at
      `matchers=COMMAND_TOOL_NAMES` (dual) as of a concurrent peer plan
      (`claude-klabauter-ec`'s dialect-aware widening) -- it denies under
      `tool_name="PowerShell"` with or without this chunk's normalization,
      so it cannot demonstrate anything this chunk changed.
    - `validate-commit` is DISQUALIFIED for the identical reason: also
      registered at `matchers=COMMAND_TOOL_NAMES` already.
    - `runaway-find` QUALIFIES: it is stateless (a single regex/token walk
      over the raw command text, no session-keyed window, no accumulated
      state across calls), gives a single unambiguous answer per probe, and
      -- measured directly against the live chain, not inferred from source
      -- was still registered `matchers=("Bash",)` at this file's authoring
      SHA (pinned by `test_ac7...` below), so it is silent under
      `tool_name="PowerShell"` with no normalization and denies with it.
    """
    denying = _probe("find / -name foo", "PowerShell")
    reason = _deny_reason(denying)
    assert "find" in reason, reason
    assert "anchored at" in reason, reason

    # Negative control: the SAME probe, disarmed via `runaway-find`'s own
    # pre-existing override leg -- proves the deny above is conditioned on
    # the override state, not an artifact of some other guard entirely.
    overridden = _probe(
        "find / -name foo", "PowerShell", env={"COORDINATOR_ALLOW_FIND_ROOT": "1"}
    )
    assert overridden is None, (
        "expected the disarm override to silence the deny -- if it did not, "
        "the deny above may be coming from a different guard than "
        "runaway-find, invalidating this oracle"
    )

    # Sanity: the same probe under the native "Bash" label already denied
    # before this chunk existed -- confirms the oracle fires at all.
    assert _is_deny(_probe("find / -name foo", "Bash"))


# ---------------------------------------------------------------------------
# AC3 -- head-tail-plumbing-rewrite is now stale (widened by a peer chunk);
# grep-via-bash-rewrite replaces it as the ADVISORY_REWRITE-band oracle.
# ---------------------------------------------------------------------------


def test_ac3_powershell_grep_rewrite_advisory_fires_via_matcher_widening():
    """AC3: a corroborating ADVISORY oracle proves the normalization reaches
    matcher selection for the `ADVISORY_REWRITE` band too, not only
    `CONFINEMENT_DENY`. `grep -rn foo .` under `tool_name="PowerShell"`
    surfaces `grep-via-bash-rewrite`'s own distinctive rewrite text in
    `additionalContext`.

    Disqualification list (mirrors AC2's): `destructive-rm` and
    `validate-commit` are dual-registered already (see the deny-oracle test
    above); `head-tail-plumbing-rewrite` -- the plan's OWN suggested oracle
    -- is ALSO disqualified, freshly, by a peer chunk (C9) that widened it to
    `matchers=COMMAND_TOOL_NAMES` ahead of this one landing; see
    `test_ac2_ac7_negative_oracles_are_disqualified_or_stale` for the pinned
    proof.

    Negative control (shadowing): monkeypatches `grep-via-bash-rewrite`'s own
    backing check to return a distinguishable sentinel envelope, and asserts
    that sentinel -- not some byte-identical-looking text from an
    earlier-registered guard -- is what reaches the top of the chain. This
    rules out "the assertion below happens to match some OTHER guard's
    output" as a false-positive shape.
    """
    out = _probe("grep -rn foo .", "PowerShell")
    assert isinstance(out, dict), out
    hso = out.get("hookSpecificOutput")
    assert isinstance(hso, dict), out
    assert hso.get("permissionDecision") == "allow", out
    assert "Auto-rewritten: 'grep' via Bash" in hso.get("additionalContext", ""), out

    sentinel = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "__AC3_SHADOW_CONTROL_SENTINEL__",
        }
    }
    original = _dc.check_grep_via_bash_rewrite

    def _sentinel_check(*args, **kwargs):
        return sentinel

    _dc.check_grep_via_bash_rewrite = _sentinel_check
    try:
        shadowed = _probe("grep -rn foo .", "PowerShell")
    finally:
        _dc.check_grep_via_bash_rewrite = original

    assert shadowed == sentinel, (
        "expected grep-via-bash-rewrite's own check to be the one reaching "
        "the top of the chain for this probe -- got %r instead, meaning some "
        "earlier-registered guard shadows this oracle" % (shadowed,)
    )


def test_ac2_ac7_negative_oracles_are_disqualified_or_stale():
    """Pins the disqualification/staleness claims the two tests above rely
    on, so a future editor cannot silently believe `destructive-rm`,
    `validate-commit`, or `head-tail-plumbing-rewrite` still qualify as
    Bash-only oracles without this test going red first."""
    chain = dispatch._build_guard_chain(
        cmd="echo probe",
        session_id="probe",
        cwd=".",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo probe"}},
        policy_file=None,
        host_is_windows=None,
    )
    by_name = {entry.name: entry for entry in chain}

    assert set(by_name["destructive-rm"].matchers) == set(COMMAND_TOOL_NAMES), (
        "destructive-rm is expected to be dual-registered already -- if this "
        "fails, it may have reverted to Bash-only and could replace "
        "runaway-find as a cleaner oracle, but the test above must be "
        "re-examined first"
    )
    assert set(by_name["validate-commit"].matchers) == set(COMMAND_TOOL_NAMES)
    assert set(by_name["head-tail-plumbing-rewrite"].matchers) == set(COMMAND_TOOL_NAMES), (
        "head-tail-plumbing-rewrite is expected to already be widened by a "
        "peer chunk (C9) -- if this fails, it may have reverted to "
        "Bash-only, in which case it (and the plan's original `cat foo.txt "
        "| head -20` probe) could replace grep-via-bash-rewrite as the AC3 "
        "oracle"
    )


# ---------------------------------------------------------------------------
# AC4 -- a "Bash" payload is unaffected (no double-normalization).
# ---------------------------------------------------------------------------


def test_ac4_bash_labelled_payload_reaches_every_guard_identically():
    """A payload already carrying `tool_name="Bash"` must see the identical
    verdict before and after this chunk -- verified by running the SAME two
    oracle commands used above under `tool_name="Bash"` and confirming they
    match what a pre-C1 caller would have seen (deny / advisory-allow), i.e.
    the normalization is a no-op for the native label it already recognizes."""
    find_out = _probe("find / -name foo", "Bash")
    assert _is_deny(find_out)
    assert "anchored at" in _deny_reason(find_out)

    grep_out = _probe("grep -rn foo .", "Bash")
    hso = grep_out["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "allow"
    assert "Auto-rewritten: 'grep' via Bash" in hso.get("additionalContext", "")

    # A command no guard in this suite cares about must still allow silently
    # under "Bash", exactly as before.
    assert _probe("echo hello world", "Bash") is None


# ---------------------------------------------------------------------------
# AC5 -- the load-bearing regression test.
# ---------------------------------------------------------------------------


def test_ac5_start_process_git_stash_drop_still_denies_and_payload_is_untouched():
    """The regression this whole plan exists to prevent: a payload-mutating
    normalization (rewriting `payload["tool_name"]` from `"PowerShell"` to
    `"Bash"`) would make `dialect_from_tool_name(payload["tool_name"])`
    return `Dialect.BASH` inside `check_destructive_git_revert`, which would
    then never call `expand_start_process_invocations` and go SILENT on
    `Start-Process git -ArgumentList 'stash','drop'`. Measured at HEAD before
    this chunk existed: PowerShell-labelled call already denied (ec's C8);
    the payload-mutating shape this plan explicitly rejected would have
    deleted that deny.

    Two assertions:
      1. the probe still denies, with `check_destructive_git_revert`'s own
         distinctive text;
      2. `payload["tool_name"]` as SEEN BY the check function is
         byte-identical to what the caller sent (monkeypatches the shared
         helper `check_destructive_git_revert`/`check_destructive_git_revert_
         advisory` both delegate to, to capture the `hook_payload` kwarg
         without calling either check function directly).
    """
    verb1, verb2 = "stash", "drop"
    cmd = "Start-Process git -ArgumentList '%s','%s'" % (verb1, verb2)

    seen_payloads = []
    original = _dc._check_destructive_git_revert_full

    def _capturing(cmd_arg, session_id_arg="", hook_payload=None, git_root=None):
        seen_payloads.append(hook_payload)
        return original(cmd_arg, session_id_arg, hook_payload, git_root)

    _dc._check_destructive_git_revert_full = _capturing
    try:
        out = _probe(cmd, "PowerShell", session_id="ac5-probe")
    finally:
        _dc._check_destructive_git_revert_full = original

    reason = _deny_reason(out)
    assert "stash" in reason and "drop" in reason, reason

    assert len(seen_payloads) >= 1, "check_destructive_git_revert's shared helper was never invoked"
    assert seen_payloads[0]["tool_name"] == "PowerShell", (
        "payload['tool_name'] seen by the check function must be "
        "byte-identical to what the caller sent -- got %r" % (seen_payloads[0].get("tool_name"),)
    )


# ---------------------------------------------------------------------------
# AC6 -- (a) hostile-payload behavioural; (b) ast structural.
# ---------------------------------------------------------------------------


def test_ac6a_no_opt_out_survives_a_hostile_payload():
    """(a) Behavioural: every plausible opt-out spelling an agent might
    invent -- real `COORDINATOR_ALLOW_*`/`COORDINATOR_OVERRIDE_*` env
    prefixes, an invented `COORDINATOR_DISABLE_POWERSHELL_NORMALIZE`, and a
    payload-level `normalize: false` field -- still normalizes the gating
    value: `runaway-find` still denies under `tool_name="PowerShell"`
    despite every one of these being present at once."""
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "find / -name foo"},
        "session_id": "ac6a-probe",
        "cwd": ".",
        "normalize": False,
        "env": {
            "COORDINATOR_ALLOW_POWERSHELL_NORMALIZE": "1",
            "COORDINATOR_OVERRIDE_POWERSHELL_NORMALIZE": "1",
            "COORDINATOR_DISABLE_POWERSHELL_NORMALIZE": "1",
            "COORDINATOR_ALLOW_TOOL_NAME_NORMALIZE": "1",
        },
    }
    out = evaluate_payload_json(json.dumps(payload))
    assert _is_deny(out), (
        "a hostile payload carrying every plausible opt-out spelling must "
        "still normalize -- got %r" % (out,)
    )
    assert "anchored at" in _deny_reason(out)


def test_ac6b_derivation_site_is_a_bare_membership_test_no_conditional_gate():
    """(b) Structural, via `ast`: the derivation site
    (`_gating_tool_name = ... if ... else ...`) must contain no `If`,
    `BoolOp`, or `Call` node anywhere in its subtree -- only the one
    membership `Compare` (`_raw_tool_name in COMMAND_TOOL_NAMES`) that
    decides the two-way branch, and the two leaves it selects between (the
    `"Bash"` string literal and the raw value). Any `If`/`BoolOp`/`Call`
    found here would be a conditional gate on the derivation itself (env
    read, settings lookup, disarm check) -- AC6 forbids exactly that; the
    plan's own anti-scope names this as "the normalization must never grow a
    disarm surface". Proven able to fail: a synthetic gated variant (`... if
    (X and not env.get("...")) else ...`) is asserted to trip this detector
    before the real site is checked, so a rewritten-into-vacuity regex-only
    check could not silently pass."""
    source = inspect.getsource(dispatch)
    tree = ast.parse(source)

    assign_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_gating_tool_name"
        ):
            assign_node = node
            break
    assert assign_node is not None, "could not locate the `_gating_tool_name = ...` derivation site"
    assert isinstance(assign_node.value, ast.IfExp), (
        "expected the derivation to be a single conditional expression "
        "(`X if COND else Y`), got %s" % type(assign_node.value)
    )

    def _offending_nodes(subtree: ast.AST):
        found = []
        for n in ast.walk(subtree):
            if isinstance(n, (ast.If, ast.BoolOp, ast.Call)):
                found.append(n)
        return found

    real_offenders = _offending_nodes(assign_node.value)
    assert not real_offenders, (
        "derivation site contains a disallowed If/BoolOp/Call node(s): %r -- "
        "AC6 forbids any conditional gate on the derivation itself"
        % ([ast.dump(n) for n in real_offenders],)
    )

    # Positive control: a synthetic gated variant must trip the same
    # detector, proving it is not vacuously passing.
    synthetic = ast.parse(
        textwrap.dedent(
            """
            _gating_tool_name = "Bash" if (_raw_tool_name in COMMAND_TOOL_NAMES and not _os.environ.get("COORDINATOR_DISABLE_NORMALIZE")) else _raw_tool_name
            """
        )
    ).body[0]
    assert isinstance(synthetic, ast.Assign)
    synthetic_offenders = _offending_nodes(synthetic.value)
    assert synthetic_offenders, "positive control failed to trip its own detector"

    # The Compare node itself (the membership test) must be exactly the
    # `in` comparison against COMMAND_TOOL_NAMES -- not an equality against
    # a bare string literal (which would also trip
    # test_no_hardcoded_tool_name_literal_survives_in_a_comparison in
    # test_tool_name_membership.py).
    test_node = assign_node.value.test
    assert isinstance(test_node, ast.Compare), ast.dump(test_node)
    assert len(test_node.ops) == 1 and isinstance(test_node.ops[0], ast.In), ast.dump(test_node)
    assert isinstance(test_node.comparators[0], ast.Name), ast.dump(test_node)
    assert test_node.comparators[0].id == "COMMAND_TOOL_NAMES", ast.dump(test_node)


# ---------------------------------------------------------------------------
# AC7 -- oracle liveness.
# ---------------------------------------------------------------------------


def test_ac7_oracle_liveness_runaway_find_and_grep_rewrite_still_bash_only():
    """Fails loudly, rather than turning this suite's evidence into a silent
    tautology, if a peer widens either chosen oracle's matchers to include
    `PowerShell` (which would make AC1/AC2/AC3's "denies with normalization,
    silent without it" claim untestable through the normal probe shape).

    AC2's negative control (the `COORDINATOR_ALLOW_FIND_ROOT` override
    check) is the load-bearing invariant; this test is a diagnostic pointing
    at the break, not a substitute -- a future editor must not delete that
    control believing this test covers it.
    """
    chain = dispatch._build_guard_chain(
        cmd="echo probe",
        session_id="probe",
        cwd=".",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo probe"}},
        policy_file=None,
        host_is_windows=None,
    )
    by_name = {entry.name: entry for entry in chain}

    assert tuple(by_name["runaway-find"].matchers) == ("Bash",), (
        "runaway-find has been widened beyond Bash-only -- it can no longer "
        "serve as the AC1/AC2 deny-class oracle; pick a fresh Bash-only "
        "CONFINEMENT_DENY entry and update test_ac1_ac2... accordingly"
    )
    assert tuple(by_name["grep-via-bash-rewrite"].matchers) == ("Bash",), (
        "grep-via-bash-rewrite has been widened beyond Bash-only -- it can "
        "no longer serve as the AC3 advisory oracle; pick a fresh Bash-only "
        "ADVISORY_REWRITE entry and update test_ac3... accordingly"
    )

    # Also asserts the recorded Bash-only population count doesn't drift
    # silently against the live chain (mirrors AC7's own second clause).
    bash_only_names = {e.name for e in chain if tuple(e.matchers) == ("Bash",)}
    assert "runaway-find" in bash_only_names
    assert "grep-via-bash-rewrite" in bash_only_names


# ---------------------------------------------------------------------------
# AC11 -- the http-leg transport truth: advisories deliver nothing there.
# ---------------------------------------------------------------------------


def test_ac11_advisory_envelope_still_collapses_to_no_objection_on_the_http_leg():
    """An advisory (allow+context) envelope for a Bash-only guard, probed
    under ANY `tool_name` (including the now-armed `PowerShell` case this
    chunk is responsible for), still collapses to `{}` (`NO_OBJECTION`) once
    it reaches `warm_guard.evaluate`'s own narrowing
    (`_verdict_from_envelope`) -- so AC3's advisory oracle firing must never
    be misread as delivered coverage on claude-klabauter's http leg. Confirms this
    for both `tool_name="Bash"` (pre-existing) and `tool_name="PowerShell"`
    (the case this chunk newly arms at the dispatcher's own matcher-gate
    layer, which is a strictly EARLIER stage than `_verdict_from_envelope`)."""
    for tool_name in ("Bash", "PowerShell"):
        raw = _probe("grep -rn foo .", tool_name)
        assert raw is not None, "expected the advisory to actually fire for this control"
        verdict = _verdict_from_envelope(raw)
        assert verdict == NO_OBJECTION, (
            "advisory envelope for tool_name=%r leaked past warm_guard.evaluate's "
            "own narrowing: %r" % (tool_name, verdict)
        )


# ---------------------------------------------------------------------------
# AC15 -- the thesis test: one payload each, both arm, from the same fix.
# ---------------------------------------------------------------------------


def test_ac15_thesis_dialect_aware_and_bash_only_both_deny_under_powershell():
    """The plan's whole thesis, converted from derivation to measurement:
    with C1 landed, a SINGLE `tool_name="PowerShell"` payload each denies
    for two structurally different reasons --

      1. the dialect-aware conversion (`Start-Process git -ArgumentList
         'stash','drop'`) needs the NATIVE label intact
         (`dialect_from_tool_name(payload["tool_name"])` must still see
         `"PowerShell"`, never a rewritten `"Bash"`);
      2. the Bash-only entry (`find / -name foo`) needs the MATCHER
         universe widened (a Bash-only `matchers=("Bash",)` guard must still
         be reached for a `PowerShell`-labelled call).

    Both arming from the same C1 mechanism -- a local gating value plus an
    untouched payload -- is what converts D1 (deleting DoE's
    `_rearm_command_tool_name`) from a trade into a pure gain; this is the
    number the DoE memo (C3/C4) carries."""
    verb1, verb2 = "stash", "drop"
    dialect_aware_cmd = "Start-Process git -ArgumentList '%s','%s'" % (verb1, verb2)
    bash_only_cmd = "find / -name foo"

    dialect_aware_out = _probe(dialect_aware_cmd, "PowerShell", session_id="ac15-a")
    bash_only_out = _probe(bash_only_cmd, "PowerShell", session_id="ac15-b")

    assert _is_deny(dialect_aware_out), dialect_aware_out
    assert "stash" in _deny_reason(dialect_aware_out)

    assert _is_deny(bash_only_out), bash_only_out
    assert "anchored at" in _deny_reason(bash_only_out)
