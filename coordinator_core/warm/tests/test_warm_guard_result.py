"""Coverage for `coordinator_core.warm.warm_guard_result.interpret` — the ONE correct reading of
a `try_warm_guard_dispatch` outcome for the Bash `PreToolUse` guard.

The module's own docstring names the security failure this pins: `try_warm_guard_dispatch`
counts ANY well-formed JSON-RPC response as `hit=True`, error envelopes included. A caller that
reads `hit=True` as "a verdict arrived" turns an engine error into a SILENT PASS — the guard
evaluated nothing and the command runs anyway. `GUARD_DID_NOT_RUN` is the only safe reading of
every non-verdict shape; this file exists to make a regression toward `NO_OBJECTION` (or any
other permissive disposition) on those shapes fail loudly.

Not wired into any hook here — see `warm_guard_result.py`'s own docstring for why.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinator_core.warm.warm_guard_result import (
    DENY,
    GUARD_DID_NOT_RUN,
    NO_OBJECTION,
    interpret,
)


def test_dispositions_are_three_distinct_values() -> None:
    """The whole defect class this module exists to prevent is GUARD_DID_NOT_RUN being confused
    with NO_OBJECTION. Pin all three constants distinct, not just that pair."""
    values = {DENY, NO_OBJECTION, GUARD_DID_NOT_RUN}
    assert len(values) == 3
    assert GUARD_DID_NOT_RUN != NO_OBJECTION
    assert GUARD_DID_NOT_RUN != DENY
    assert NO_OBJECTION != DENY


def test_no_hit_is_guard_did_not_run() -> None:
    """hit=False -- METHOD_NOT_FOUND or no reachable server -- is the ordinary cold path."""
    disposition, reason = interpret(False, None)
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


def test_hit_true_with_error_envelope_is_guard_did_not_run() -> None:
    """THE TRAP: a hit that carries a JSON-RPC error envelope is an engine failure, not a
    verdict. Reading this as anything but GUARD_DID_NOT_RUN is the silent-pass security defect
    this module exists to prevent."""
    response = {"error": {"code": -32000, "message": "internal failure"}}
    disposition, reason = interpret(True, response)
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


def test_error_key_present_but_null_is_not_treated_as_error() -> None:
    response = {"error": None, "result": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}
    disposition, reason = interpret(True, response)
    assert disposition == DENY
    assert reason == "no"


def test_deny_propagates_reason_verbatim() -> None:
    response = {"result": {"permissionDecision": "deny", "permissionDecisionReason": "blocked: secrets"}}
    disposition, reason = interpret(True, response)
    assert disposition == DENY
    assert reason == "blocked: secrets"


@pytest.mark.parametrize(
    "bad_reason",
    [None, "", "   ", 0, [], {}],
    ids=["missing", "empty", "whitespace", "int", "list", "dict"],
)
def test_deny_with_unusable_reason_still_denies_with_substitute(bad_reason: Any) -> None:
    result: dict[str, Any] = {"permissionDecision": "deny"}
    if bad_reason is not None:
        result["permissionDecisionReason"] = bad_reason
    disposition, reason = interpret(True, {"result": result})
    assert disposition == DENY
    assert isinstance(reason, str) and reason.strip()
    assert reason != bad_reason


def test_empty_result_with_no_decision_key_is_no_objection() -> None:
    disposition, reason = interpret(True, {"result": {}})
    assert disposition == NO_OBJECTION
    assert reason is None


def test_literal_allow_is_guard_did_not_run_never_a_verdict() -> None:
    disposition, reason = interpret(True, {"result": {"permissionDecision": "allow"}})
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


@pytest.mark.parametrize(
    "unexpected_decision",
    ["ask", "ALLOW", "Deny", "unknown", 1, True, [], {}],
)
def test_unrecognised_permission_decision_is_guard_did_not_run(unexpected_decision: Any) -> None:
    disposition, reason = interpret(True, {"result": {"permissionDecision": unexpected_decision}})
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


@pytest.mark.parametrize(
    "response",
    [None, "a string", 0, 1, [], ["result"], True, 3.14],
    ids=["none", "string", "zero", "one", "empty_list", "list", "bool", "float"],
)
def test_non_dict_response_is_guard_did_not_run(response: Any) -> None:
    disposition, reason = interpret(True, response)
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


def test_response_missing_result_key_is_guard_did_not_run() -> None:
    disposition, reason = interpret(True, {"something_else": 1})
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


@pytest.mark.parametrize(
    "bad_result",
    [None, "a string", 0, 1, [], True],
)
def test_non_dict_result_is_guard_did_not_run(bad_result: Any) -> None:
    disposition, reason = interpret(True, {"result": bad_result})
    assert disposition == GUARD_DID_NOT_RUN
    assert reason is None


_JUNK_SHAPES = [
    None,
    "",
    0,
    1,
    -1,
    3.14,
    [],
    {},
    True,
    False,
    b"bytes",
    {"error": {}},
    {"error": []},
    {"error": "totally an error"},
    {"result": None},
    {"result": "nope"},
    {"result": {"permissionDecision": None}},
    {"result": {"permissionDecision": {"nested": "dict"}}},
    {"result": {"permissionDecision": "deny", "permissionDecisionReason": {"nested": "dict"}}},
    {"result": {}, "error": {}},
    {"unexpected": object()},
    ["result", "error"],
    {1: 2, "result": {3: 4}},
]


@pytest.mark.parametrize("response", _JUNK_SHAPES)
@pytest.mark.parametrize("hit", [True, False])
def test_interpret_never_raises_and_always_returns_a_known_disposition(hit: bool, response: Any) -> None:
    disposition, reason = interpret(hit, response)
    assert disposition in (DENY, NO_OBJECTION, GUARD_DID_NOT_RUN)
    if disposition != DENY:
        assert reason is None
    else:
        assert isinstance(reason, str)
