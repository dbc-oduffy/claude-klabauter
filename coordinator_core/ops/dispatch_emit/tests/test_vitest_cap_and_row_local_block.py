"""The box's vitest worker cap reaches every emitted test-runner brief, and a
ROW-LOCAL-BLOCK reply never arms the stop-rule halt."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core import machine_resolver
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

pytestmark = pytest.mark.spawns_process


def _compose() -> str:
    row = WaveRow(
        id="C1", title="t", surface="dispatch_emit", writes=["a.py", "tests/test_a.py"], reads=[],
        depends_on=[], body="",
    )
    return compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md", **REVIEW_KW
    )


def _set_cap(monkeypatch, value):
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: value if key == KEY_VITEST_MAX_WORKERS else None
    )


def test_cap_is_threaded_into_terminal_and_per_row_runner_briefs(monkeypatch):
    _set_cap(monkeypatch, "2")
    script = _compose()
    assert "test:terminal" in script
    assert script.count("--maxWorkers=2") >= 1
    assert "--maxWorkers=2" in emit._run_row_helper_js()


def test_unset_cap_emits_nothing_new():
    assert "maxWorkers" not in _compose()
    assert emit._vitest_cap_clause() == ""


def test_non_positive_cap_is_ignored(monkeypatch):
    _set_cap(monkeypatch, "0")
    assert "maxWorkers" not in _compose()


def test_executor_brief_names_the_row_local_token():
    assert f"{emit._ROW_LOCAL_BLOCK_TOKEN}:" in emit._stop_rule_clause()


def test_halt_is_guarded_by_the_row_local_token():
    script = _compose()
    assert emit._ROW_LOCAL_BLOCK_JS_RE in script
    assert (
        f"if ({emit._STOP_RULE_JS_RE}.test(_text) && !{emit._ROW_LOCAL_BLOCK_JS_RE}.test(_text))"
        in script
    )


_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [], _landed = {};
  const _gateOwed = {}, _chunkReason = {};
  let _halted = null;
  const _rowPlan = {};
  const _planHeld = {};
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('no agent'); };
  const logs = [];
  const log = (m) => logs.push(m);
%(helper)s
  const prompts = [];
  const replies = %(replies)s;
  const run = (id) => async () => {
    const n = prompts.filter((p) => p.id === id).length;
    prompts.push({ id, widen: _widenText(_widenOf[id] || '') });
    return replies[id][Math.min(n, replies[id].length - 1)];
  };
  const _rows = {};
  %(rows)s
  for (const id of Object.keys(_rows)) await _rows[id];
  console.log(JSON.stringify({
    calls: prompts.map((p) => p.id), widened: _widened, widenOf: _widenOf, stoppedBy: _stoppedBy,
    halted: _halted, incomplete: _incompleteChunks, prompts,
  }));
})();
"""

_BLOCK = "BLOCKED: x\nROW-LOCAL-BLOCK: %s"
_ROW = "_rows.A = _runRow('A', [], null, run('A'), { title: 't', paths: ['a.py'], declared: [] });"


def _go(replies, rows=_ROW):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"],
        input=_HARNESS % {"helper": emit._run_row_helper_js(), "rows": rows, "replies": json.dumps(replies)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_in_ask_block_widens_and_redispatches_the_row_once():
    out = _go({"A": [_BLOCK % "pkg/helper.py", "DONE: a"]})
    assert out["calls"] == ["A", "A"]
    assert out["widened"] == [{"row": "A", "path": "pkg/helper.py"}]
    assert "pkg/helper.py" in out["prompts"][1]["widen"]
    assert out["incomplete"] == []


def test_a_second_block_on_the_same_row_stays_row_local():
    out = _go({"A": [_BLOCK % "pkg/helper.py", _BLOCK % "pkg/other.py"]})
    assert out["calls"] == ["A", "A"]
    assert out["widened"] == [{"row": "A", "path": "pkg/helper.py"}]
    assert out["incomplete"] == ["A"] and out["halted"] is None


@pytest.mark.parametrize(
    "value",
    ["state/handoffs/x.md", "../outside.py", "/abs/path.py", "scope is wrong", "a.py b.py", ".git/config"],
)
def test_out_of_ask_block_halts_exactly_as_at_base(value):
    out = _go({"A": [_BLOCK % value]})
    assert out["calls"] == ["A"] and out["widened"] == []
    assert out["incomplete"] == ["A"]


def test_a_peer_held_path_is_not_widened():
    rows = _ROW + "_rows.B = _runRow('B', [], null, run('B'), { title: 't', paths: ['shared.py'], declared: [] });"
    out = _go({"A": [_BLOCK % "shared.py"], "B": ["DONE: b"]}, rows)
    assert out["calls"].count("A") == 1 and out["widened"] == []


def test_a_stop_rule_reply_still_halts_without_widening():
    out = _go({"A": ["DONE: x\nSTOP-RULE-FIRED: premise refuted"]})
    assert out["calls"] == ["A"] and out["widened"] == []
    assert out["stoppedBy"] == ["A"]


def test_executor_brief_asks_for_the_exact_in_ask_path():
    assert "re-dispatches the row once" in emit._stop_rule_clause()
