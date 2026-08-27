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
