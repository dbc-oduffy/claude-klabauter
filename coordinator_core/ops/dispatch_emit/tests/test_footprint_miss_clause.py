"""A row with no build gate still learns the outside-footprint report line."""

import dataclasses
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    _row_prompt,
    _run_row_helper_js,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.gate_owed import (
    KIND_OUTSIDE_FOOTPRINT,
    report_line,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  const _gateOwed = {}, _chunkReason = {};
  let _halted = null;
  const _rowPlan = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
%(helper)s
  const _rows = {};
  const calls = [];
  const run = (id, reply) => async () => { calls.push(id); return reply; };
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({ calls, reason: _heldDependentsReason() }));
})();
"""

_PLAN = "docs/plans/p.md"
_LINE_PREFIX = report_line(KIND_OUTSIDE_FOOTPRINT, "<first failing test>")
_GATE = (
    "row_build_gate:\n"
    "  - when: {surface_glob: 'Source/**/*.cpp'}\n"
    "    command: 'build.bat Editor'\n"
)


def _row(rid, path):
    return WaveRow(
        id=rid, title="t", surface=path, writes=[path], reads=[], depends_on=[],
    )


def _ctx(extra=""):
    text = f"---\ntitle: P\n{extra}---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    return derive_plan_context(text, fallback_title="p")


def test_no_gate_row_prompt_carries_the_outside_footprint_line():
    prompt = _row_prompt(_row("C1", "tools/a.py"), _PLAN, _ctx(_GATE))
    assert _LINE_PREFIX in prompt
    assert "Build gate" not in prompt


def test_build_gate_row_carries_the_line_once():
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, _ctx(_GATE))
    assert "Build gate (mandatory)" in prompt
    assert prompt.count(f"gate-blocker: {KIND_OUTSIDE_FOOTPRINT}:") == 1


def test_slot_row_does_not_gain_the_clause():
    ctx = dataclasses.replace(_ctx(_GATE), slot_row_ids=frozenset({"C1"}))
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, ctx)
    assert _LINE_PREFIX not in prompt


_CLAUSE_REPLY = "verbatim as the second line of your reply"
_CONTRACT_REPLY = "verbatim as a second line of the reply"


def test_no_gate_row_prompt_asks_for_the_line_in_the_reply():
    prompt = _row_prompt(_row("C1", "tools/a.py"), _PLAN, _ctx(_GATE))
    assert _CLAUSE_REPLY in prompt
    assert _CONTRACT_REPLY in prompt


def test_slot_row_prompt_has_neither_reply_sentence():
    ctx = dataclasses.replace(_ctx(_GATE), slot_row_ids=frozenset({"C1"}))
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, ctx)
    assert _CLAUSE_REPLY not in prompt
    assert _CONTRACT_REPLY not in prompt


def test_build_gate_row_keeps_one_clause_and_one_contract_tail():
    prompt = _row_prompt(_row("C1", "Source/Mod/a.cpp"), _PLAN, _ctx(_GATE))
    assert prompt.count(_CLAUSE_REPLY) == 1
    assert prompt.count(_CONTRACT_REPLY) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_runner_releases_dependent_on_the_reply_the_prompt_asks_for():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    reply = "PARTIAL: r.md\ngate-blocker: outside-footprint: t::x"
    rows = (
        f"_rows.A = _runRow('A', [], null, run('A', {json.dumps(reply)}));"
        "_rows.B = _runRow('B', [_rows.A], null, run('B', 'DONE: B'));"
    )
    proc = subprocess.run(
        [node, "-"], input=_HARNESS % {"helper": _run_row_helper_js(), "rows": rows},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["calls"] == ["A", "B"]
    assert out["reason"] is None
