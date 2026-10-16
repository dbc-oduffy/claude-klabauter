"""Chunk-id invariant: an id-less row never dispatches and terminal_commit never receives a non-string id."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _run_row_helper_js, compose_script
from coordinator_core.ops.dispatch_emit.wake_digest import BAD_CHUNK_ID_REASON

from .conftest import REVIEW_KW
from .test_emit import _two_wave_fixture


def _script() -> str:
    return compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)


def test_run_row_inner_refuses_a_non_string_id_before_any_agent_call():
    helper = _run_row_helper_js()
    inner = helper[helper.index("async function _runRowInner") :]
    guard = "if (typeof id !== 'string' || !id) {"
    assert guard in inner
    assert inner.index(guard) < inner.index("await Promise.all(deps)")
    assert inner.index(guard) < inner.index("await run()")
    refused = inner[inner.index(guard) : inner.index("await Promise.all(deps)")]
    assert "_incompleteChunks.push" not in refused


def test_the_id_refusal_rides_the_halt_carrier_not_unanswered():
    helper = _run_row_helper_js()
    inner = helper[helper.index("async function _runRowInner") :]
    refused = inner[: inner.index("await Promise.all(deps)")]
    assert "if (!_halted) _halted = 'refusal: invalid chunk id '" in refused
    assert "_rowSeq" in refused
    assert "_unansweredBriefs" not in refused


def test_row_position_counter_advances_per_registration():
    helper = _run_row_helper_js()
    assert "let _rowSeq = 0;" in helper
    assert helper.index("_rowSeq++;") < helper.index("const p = _runRowInner(")


def test_terminal_commit_params_are_refused_not_thrown_on_a_non_string_id():
    script = _script()
    assert "typeof x !== 'string' || !x" in script
    assert "? null : { incomplete_chunks: [...new Set([..._incompleteChunks, ..._notStarted])]" in script
    assert "throw" not in script[script.index("next_action:") :]
    assert ".filter(" not in script[script.index("incomplete_chunks:") : script.index("incomplete_chunks:") + 200]


def test_next_action_is_none_and_decision_required_names_the_refusal():
    script = _script()
    assert "? 'none' : 'terminal_commit'" in script
    assert "? null : 'dispatch.terminal_commit'" in script
    assert BAD_CHUNK_ID_REASON in script
