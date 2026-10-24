"""A landed commit with incomplete or blocked rows ends at revise_and_refire; an empty set keeps close_job.

These pin the composed JS text only; none executes the arm."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_in_run_close import _script

_OPEN = "else if (_incompleteChunks.length > 0 || _blockedChunks.length > 0) {"


def test_open_rows_arm_sits_between_sibling_arm_and_close_job(monkeypatch):
    script = _script(monkeypatch)
    assert script.count(_OPEN) == 1
    branch = script.index("if (_commit && _commit.committed === true) { const _blockedRows")
    at = script.index(_OPEN, branch)
    assert script.index("_nextAction = { kind: 'refire_after_sibling'", branch) < at
    assert at < script.index("_nextAction = { kind: 'close_job'", branch)
    assert script.index("for (const _uk of Object.keys(_commit.incomplete_reasons") < at


def test_open_rows_record_keeps_close_and_fire_beside_params(monkeypatch):
    script = _script(monkeypatch)
    arm = script[script.index(_OPEN) :].split("_reviseAndRefire = _mkRevise(); }", 1)[0]
    assert "kind: 'revise_and_refire', op: 'workflow.revise'" in arm
    assert "params: { run_id: _runId, note: 'landed with rows open: '" in arm
    params = arm.split("params: {", 1)[1].split("}", 1)[0]
    assert "close" not in params and "fire" not in params
    assert "close: _commit.close ?? null, fire: _commit.close?.siblings?.released ?? []" in arm
    assert "halted: '" not in arm and "REVISE_NEXT_ACTION" not in arm


def test_reason_names_landed_rows_open_ahead_of_verdict_fail(monkeypatch):
    script = _script(monkeypatch)
    reason = "? 'landed-rows-open' : 'delivery verdict FAIL'"
    assert script.count(reason) == 1
    pred = script[: script.index(reason)].rsplit("(_commit && ", 1)[1]
    assert "_commit.committed === true" in pred
    assert "_incompleteChunks.length > 0" in pred and "_blockedChunks.length > 0" in pred


def test_close_job_still_composed_for_the_empty_case(monkeypatch):
    script = _script(monkeypatch)
    assert "else { _nextAction = { kind: 'close_job', op: null," in script
