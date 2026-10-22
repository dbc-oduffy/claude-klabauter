"""A commit-stage refusal with nothing left to revise keeps the terminal_commit next_action."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ELSE_PREFIX = "    else { if (!_halted) _halted = { halted: 'commit'"
_RESUME_PREFIX = "  if (_resumeLedger && _nextAction && _nextAction.params && _gate.resume_ledger.inline_review"

_HARNESS = """
let _halted = null, _reviseAndRefire = 'unset', _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r';
let _incompleteChunks = %s, _commit = %s, _resumeLedger = %s, _gate = %s;
let _nextAction = %s;
const _mkRevise = () => ({ reason: 'x' });
if (false) {}
%s
console.log(JSON.stringify({ halted: _halted, revise: _reviseAndRefire, na: _nextAction }));
"""

_IR = {"integration_stem": "stem", "slices": 1, "fixes": 1}


def _script() -> str:
    return _compose(in_run_commit=True, script_path="x.mjs")


def _line(prefix: str) -> str:
    lines = [ln for ln in _script().split("\n") if ln.startswith(prefix)]
    assert len(lines) == 1
    return lines[0]


def _drive(js_line: str, *, incomplete=(), commit=None, resume=False, gate=None, na=None) -> dict:
    na = na if na is not None else {"kind": "terminal_commit", "op": OP_NAME,"params": {"inline_review": _IR}}
    program = _HARNESS % (
        json.dumps(list(incomplete)), json.dumps(commit or {"refused": "since-base-overlap"}),
        json.dumps(resume), json.dumps(gate or {}), json.dumps(na), js_line,
    )
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_refusal_with_no_incomplete_chunk_keeps_terminal_commit_and_review():
    got = _drive(_line(_ELSE_PREFIX))
    assert got["na"]["kind"] == "terminal_commit"
    assert got["na"]["params"]["inline_review"] == _IR
    assert got["revise"] == "unset" or got["revise"] is None
    assert got["na"]["params"]["halted"] == got["halted"]
    assert got["halted"]["halted"] == "commit"
    assert got["halted"]["plan"] == "p.md" and got["halted"]["sizing"] == "s.yaml"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_refusal_with_an_incomplete_chunk_still_revises():
    got = _drive(_line(_ELSE_PREFIX), incomplete=["C1"])
    assert got["na"]["kind"] == "revise_and_refire"
    assert got["revise"] == {"reason": "x"}


def test_halt_record_params_carry_run_outcome():
    script = _script()
    assert "run_outcome: { inline_review: _nextAction.params.inline_review" in script
    assert "_nextAction.kind === 'terminal_commit'" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_resume_ledger_inline_review_replaces_the_empty_review():
    recorded = {**_IR, "resumed_run_id": "old-run"}
    got = _drive(
        _line(_RESUME_PREFIX), resume=True, gate={"resume_ledger": {"inline_review": recorded}},
        na={"kind": "terminal_commit", "op": OP_NAME,"params": {"inline_review": {}}},
    )
    assert got["na"]["params"]["inline_review"] == recorded


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_no_recorded_review_leaves_the_computed_one():
    got = _drive(_line(_RESUME_PREFIX), resume=True, gate={"resume_ledger": {}})
    assert got["na"]["params"]["inline_review"] == _IR
