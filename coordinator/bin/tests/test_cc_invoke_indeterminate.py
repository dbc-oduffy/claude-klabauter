"""test_cc_invoke_indeterminate — a delivered-but-unanswered mutation is a
TYPE, not a string a caller has to regex out of a generic RuntimeError.

WHAT THIS EXISTS TO PIN. `-32004 WARM_DISPATCH_INDETERMINATE` is the one
refusal that says nothing about whether the write landed, and the client
cannot narrow it: `warm/client.py :: _try_warm_dispatch_inner` sets
`delivered` immediately after `flush()`, and its own zero-byte branch already
concedes that flush "proves the bytes left THIS process into the pipe buffer
-- it never proved the server read them". Delivered-then-stalled and
died-mid-flight are therefore indistinguishable at that layer BY
CONSTRUCTION, and no amount of work on the envelope recovers the answer.

The only thing that can recover it is a caller reconciling against the
artifact the op would have written. That caller has to be able to CATCH this
case by name to know it should reconcile at all -- hence
`WarmDispatchIndeterminate`, and hence this suite. `cross-repo-memo draft` is
the first consumer: it computes `state/memo-outbox/<topic>.md` from argv
before dispatch and stats it after.

THE COLD RUNG IS THE ONE THAT ACTUALLY FIRES, which is the non-obvious half.
`cc_invoke` warm-reaches first; on a miss it spawns `coordinator_core.invoke`,
and THAT child warm-reaches again (`invoke/__main__ :: _wait_for_warm_boot`).
A server that takes the child's bytes and never answers produces the -32004
envelope there, on stdout, with exit 1 (`_exit_code_for_response` returns 2
only for STRUCTURAL_PIN_ERROR) -- so it arrives through
`_raise_on_process_failure`, never through `cc_invoke`'s rung (4) envelope
parse. A suite that only covered the warm branch would pass while the path an
operator actually hits stayed untyped.

NO SPAWNS. Every case here drives `_raise_on_process_failure` /
`_apply_warm_envelope` / `_stdout_error_code` in-process with synthesised
stdout, because the subject is the classification ladder, not the transport.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_LIB_DIR = _TESTS_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)


def _envelope(code, message="delivered, never answered"):
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    )


# ---------------------------------------------------------------------------
# The honesty pin for the restated constant
# ---------------------------------------------------------------------------

def test_restated_code_matches_the_engines_own_constant():
    """`_WARM_DISPATCH_INDETERMINATE_CODE` is a second copy of the engine's
    constant, restated because `_raise_on_process_failure` runs on an
    already-failing path and may not pay an import that can raise. This is the
    test that keeps the copy honest: if the engine ever renumbers, this fails
    loudly rather than the cold rung silently ceasing to match anything."""
    from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE

    assert _mod._WARM_DISPATCH_INDETERMINATE_CODE == WARM_DISPATCH_INDETERMINATE


def test_indeterminate_is_a_runtimeerror_subclass():
    """Every pre-existing `except RuntimeError:` around a transport call must
    keep catching this unchanged -- the type is additive discrimination for
    callers that want it, never a new escape from existing handlers."""
    assert issubclass(_mod.WarmDispatchIndeterminate, RuntimeError)


# ---------------------------------------------------------------------------
# _stdout_error_code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stdout_text",
    [
        "",
        "   ",
        "not json at all",
        "[1, 2, 3]",                                   # JSON, not an object
        json.dumps({"jsonrpc": "2.0", "result": {}}),  # success envelope
        json.dumps({"error": "a bare string, not a dict"}),
    ],
    ids=["empty", "blank", "not-json", "json-array", "success", "error-not-a-dict"],
)
def test_stdout_error_code_returns_none_for_everything_that_is_not_a_code(stdout_text):
    """Never raises and never guesses -- this runs on a failing path, and a
    wrong answer here would RECLASSIFY a failure rather than merely fail to
    decorate one."""
    assert _mod._stdout_error_code(stdout_text) is None


def test_stdout_error_code_recovers_the_code():
    assert _mod._stdout_error_code(_envelope(-32004)) == -32004
    assert _mod._stdout_error_code(_envelope(-32001)) == -32001


# ---------------------------------------------------------------------------
# The cold rung -- the path an operator actually reaches
# ---------------------------------------------------------------------------

def test_cold_rc1_with_indeterminate_envelope_raises_the_typed_error():
    with pytest.raises(_mod.WarmDispatchIndeterminate) as caught:
        _mod._raise_on_process_failure(
            1, _envelope(-32004), "", "memo.draft", "/engine"
        )
    assert caught.value.op == "memo.draft"
    assert "indeterminate" in str(caught.value).lower()


def test_cold_rc1_with_any_other_code_stays_a_plain_runtimeerror():
    """The new rung must not widen: an ordinary op-level error is still the
    generic branch, and specifically NOT the indeterminate subclass, or every
    routine failure would start telling callers to go reconcile."""
    with pytest.raises(RuntimeError) as caught:
        _mod._raise_on_process_failure(
            1, _envelope(-32603), "", "memo.draft", "/engine"
        )
    assert not isinstance(caught.value, _mod.WarmDispatchIndeterminate)


# ---------------------------------------------------------------------------
# Precedence -- `_raise_on_process_failure`'s docstring pins the existing
# routing order (stderr-ImportError > rc==2 > stdout-ImportError > generic).
# The indeterminate rung was inserted immediately above `generic`, so all four
# pre-existing rungs must still take exactly the inputs they took before.
# ---------------------------------------------------------------------------

def test_structural_pin_still_wins_over_an_indeterminate_envelope():
    """rc==2 is the engine's own non-self-healing discriminator and outranks
    this rung. Contrived (the engine exits 1 for -32004), and pinned anyway:
    the point is that inserting a rung did not move an existing one."""
    with pytest.raises(_mod.StructuralPinError):
        _mod._raise_on_process_failure(
            2, _envelope(-32004), "", "memo.draft", "/engine"
        )


def test_stderr_importerror_still_wins_over_an_indeterminate_envelope():
    with pytest.raises(RuntimeError) as caught:
        _mod._raise_on_process_failure(
            1,
            _envelope(-32004),
            "ImportError: No module named coordinator_core",
            "memo.draft",
            "/engine",
        )
    assert not isinstance(caught.value, _mod.WarmDispatchIndeterminate)
    assert "will not import/start" in str(caught.value)


# ---------------------------------------------------------------------------
# The warm rung
# ---------------------------------------------------------------------------

def test_warm_hit_indeterminate_envelope_raises_the_typed_error():
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32004, "message": "no response within 30.0s"},
    }
    with pytest.raises(_mod.WarmDispatchIndeterminate) as caught:
        _mod._apply_warm_envelope("memo.draft", envelope, "", None)
    assert caught.value.op == "memo.draft"


def test_warm_hit_other_error_is_not_the_typed_error():
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "internal error"},
    }
    with pytest.raises(RuntimeError) as caught:
        _mod._apply_warm_envelope("memo.draft", envelope, "", None)
    assert not isinstance(caught.value, _mod.WarmDispatchIndeterminate)
