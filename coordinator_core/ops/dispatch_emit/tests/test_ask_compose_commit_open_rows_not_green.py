"""A landed commit with incomplete or blocked rows ends at revise_and_refire; an empty set keeps close_job.

Most pin the composed JS text only; the `_drive_commit_block` tests execute the commit arm under node."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_in_run_close import _script
from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    composed,
    repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_reserved_refire_reads_halt_record_notes import (  # noqa: F401
    SLICE,
    _slice_sizing,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


_HARNESS = """
let _runId = 'r', _incompleteChunks = %s, _blockedChunks = [], _reviseAndRefire = null;
let _commit = %s, _nextAction = { params: {} };
const _mkRevise = () => ({ reason: 'x' });
%s
console.log(JSON.stringify({ kind: _nextAction.kind, open: _incompleteChunks }));
"""


def _drive_commit_block(monkeypatch, *, incomplete, commit):
    script = _script(monkeypatch)
    start = script.index("    if (_commit && _commit.committed === true) { const _blockedRows")
    end = script.index("    else { if (!_halted) _halted = { halted: 'commit'", start)
    program = _HARNESS % (json.dumps(incomplete), json.dumps(commit), script[start:end])
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_credited_chunk_in_commit_reply_is_not_open_and_closes(monkeypatch):
    got = _drive_commit_block(
        monkeypatch, incomplete=["X"], commit={"committed": True, "credited_chunks": ["X"]}
    )
    assert got == {"kind": "close_job", "open": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_uncredited_incomplete_reason_still_revises_and_refires(monkeypatch):
    got = _drive_commit_block(
        monkeypatch,
        incomplete=[],
        commit={"committed": True, "incomplete_reasons": {"X": "r"}, "credited_chunks": ["Y"]},
    )
    assert got["kind"] == "revise_and_refire" and got["open"] == ["X"]


def test_commit_schema_declares_credited_chunks():
    from coordinator_core.ops.dispatch_emit.ask_compose import _COMMIT_SCHEMA

    assert _COMMIT_SCHEMA["properties"]["credited_chunks"]["items"] == {"type": "string"}


def _land_slice(repo):
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    rl.start(repo, SLICE)
    rl.bind(repo, SLICE, REL, ask_text="the ask")
    status = repo / "scratch" / "warp" / SLICE / "sibling-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"status": "landed", "sha": "abc1234", "run_id": SLICE}), encoding="utf-8")


def test_refiring_a_landed_reserved_run_mints_a_residue_run_not_a_refusal(repo, composed):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import op
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    _slice_sizing(repo)
    _land_slice(repo)
    reply = op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": SLICE}, repo_root=repo)
    assert reply["run_id"] != SLICE
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == SLICE


def test_reserved_run_still_refuses_a_landed_run_called_directly(tmp_path):
    from coordinator_core.ops.dispatch_emit import op

    status = tmp_path / "scratch" / "warp" / "r1" / "sibling-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"status": "landed", "sha": "abc"}), encoding="utf-8")
    sizing = {"em_analysis": {"ask_parent": "p1 r1"}}
    assert op._reserved_run_landed(tmp_path, "r1") is True
    assert op._reserved_run_landed(tmp_path, "r2") is False
    with pytest.raises(ValueError, match="already landed"):
        op._reserved_run(tmp_path, "r1", sizing, "state/sizings/s.yaml")


def test_close_job_still_composed_for_the_empty_case(monkeypatch):
    script = _script(monkeypatch)
    assert "else { _nextAction = { kind: 'close_job', op: null," in script
