"""A row whose only undone act is its pytest acceptance is booked gate-owed, so a test-runner leg
(the checkpoint's `_owedGatesGreen`, or a refire's carried-owed leg) runs it, never an executor refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
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
  const calls = [];
  const run = (id, reply) => async () => { calls.push(id); return reply ?? ('DONE: ' + id); };
  const _rows = {};
  _rows.A = _runRow('A', [], null, run('A', %(reply)s));
  _rows.B = _runRow('B', [_rows.A], null, run('B'));
  const results = {};
  for (const id of Object.keys(_rows)) results[id] = await _rows[id];
  console.log(JSON.stringify({ calls, owed: _gateOwed, incomplete: _incompleteChunks }));
})();
"""

_DENIED = "only coordinator:test-runner runs pytest in a subagent"


def _go(reply):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"],
        input=_HARNESS % {"helper": emit._run_row_helper_js(), "reply": json.dumps(reply)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "reply",
    [
        f"PARTIAL: r.md\nthe pytest run was denied: {_DENIED}",
        "DONE_WITH_CONCERNS: r.md\nacceptance pytest run owed to coordinator:test-runner",
    ],
)
def test_reply_leaving_only_the_test_leg_owed_is_booked_gate_owed_and_releases_dependents(reply):
    out = _go(reply)
    assert out["calls"] == ["A", "B"]
    assert list(out["owed"]) == ["A"] and out["owed"]["A"].startswith("gate-blocker: guard-denied:")
    assert out["incomplete"] == []


def test_blocked_reply_whose_status_line_names_the_denial_is_booked_gate_owed():
    out = _go(f"BLOCKED: {_DENIED}; edits complete, the test-runner leg is owed\nr.md")
    assert out["calls"] == ["A", "B"]
    assert list(out["owed"]) == ["A"] and out["owed"]["A"].startswith("gate-blocker: guard-denied:")
    assert out["incomplete"] == []


def test_blocked_reply_naming_the_denial_below_another_blocker_stays_held():
    out = _go(f"BLOCKED: the spec contradicts the plan\n{_DENIED}")
    assert out["calls"] == ["A"]
    assert out["owed"] == {} and out["incomplete"] == ["A"]


def test_partial_with_no_test_leg_owed_stays_held():
    out = _go("PARTIAL: r.md\nthe edit is half done")
    assert out["calls"] == ["A"]
    assert out["owed"] == {} and out["incomplete"] == ["A"]


def test_explicit_marker_line_is_kept_verbatim_over_the_denial_fallback():
    line = "gate-blocker: guard-denied: coordinator_core/x/tests/test_a.py (check-test-suite-invocation)"
    out = _go(f"PARTIAL: r.md\n{line}\n{_DENIED}")
    assert out["owed"] == {"A": line}


def _compose_refire(carried):
    from pathlib import Path

    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
    from coordinator_core.session.record_homes import record_path

    from .conftest import REVIEW_KW

    row = WaveRow(
        id="A", title="t-A", surface="s", writes=["pkg/mod.py", "pkg/tests/test_mod.py"], reads=[],
        depends_on=[], body="Spec: docs/plans/p.md (A)\nSummary: x.\n",
    )
    return emit.compose_script(
        [[row]], name="wf", description="d",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=False, review_specs=[], carried_gate_owed=carried, **REVIEW_KW,
    )


def test_refire_carrying_an_owed_row_runs_the_test_runner_leg_not_an_executor_for_it():
    script = _compose_refire({"Z": ["python -m pytest pkg/tests/test_z.py"]})
    assert "'Z': 'gate-blocker: carried: gate owed by a prior run'" in script
    assert '"Z": ["pkg/tests/test_z.py"]' in script
    leg = script[script.index("function _owedGatesGreen"):]
    assert "agentType: 'coordinator:test-runner'" in leg or 'agentType: "coordinator:test-runner"' in leg
    assert "_runRow('Z'" not in script and "label: 'Z'" not in script


def test_refire_of_a_landed_owed_row_with_no_build_gate_carries_its_own_tests(tmp_path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_mod.py").write_text("", encoding="utf-8")
    assert emit._owed_test_scope(["pkg/mod.py", "pkg/tests/test_mod.py"], tmp_path) == ["pkg/tests/test_mod.py"]
    assert emit._owed_test_scope(["pkg/mod.py"], tmp_path) == ["pkg/tests/test_mod.py"]
    assert emit._owed_test_scope(["pkg/mod.py"], None) == []


def test_owed_test_scope_is_the_rows_own_declared_test_file():
    script = _compose_refire({})
    assert '"A": ["pkg/tests/test_mod.py"]' in script


def test_row_runner_books_the_pytest_denial_through_the_owed_helper():
    helper = emit._run_row_helper_js()
    assert "_gateOwedOrPytestOf(_text)" in helper
    assert "guard-denied: pytest owed to coordinator:test-runner" in helper


def test_executor_prompt_teaches_the_marker_for_a_denied_pytest_run():
    clause = emit._footprint_miss_clause()
    assert "gate-blocker: guard-denied: <test node ids> (check-test-suite-invocation)" in clause


_REFIRE_ROWS = (
    "- id: C1\n  title: first\n  change_kind: script-edit\n  surface: pkg/mod.py\n"
    "  writes:\n    - pkg/mod.py\n    - pkg/tests/test_mod.py\n  agent_type: coordinator:executor\n"
    "- id: C3\n  title: dependent\n  change_kind: doc-edit\n  surface: docs/x/c3.md\n"
    "  writes:\n    - docs/x/c3.md\n  agent_type: coordinator:executor\n"
    "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n"
)


def _halted_run_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    (root / "docs" / "plans" / "p.md").write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{_REFIRE_ROWS}```\n",
        encoding="utf-8", newline="\n",
    )
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / "pkg" / "tests" / "test_mod.py").write_text("", encoding="utf-8")
    return root


def test_refire_of_a_halted_run_with_no_checkpoint_sends_the_owed_row_to_the_test_runner(tmp_path):
    from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

    root = _halted_run_repo(tmp_path)
    plan = root / "docs" / "plans" / "p.md"
    plain = emit.emit_script(plan, repo_root=root, landed_rows=frozenset(), **REVIEW_KW)
    assert "Execute C1" in plain
    owed = emit.emit_script(
        plan, repo_root=root, landed_rows=frozenset(), owed_test_rows=frozenset({"C1"}), **REVIEW_KW
    )
    assert "Execute C1" not in owed and "Execute C3" in owed
    assert "'C1': 'gate-blocker: carried: gate owed by a prior run'" in owed
    assert '"C1": ["pkg/tests/test_mod.py"]' in owed


def _digest(tmp_path, deviations):
    out = tmp_path / "run.json"
    out.write_text(json.dumps({"result": {"next_action": {"params": {}}, "deviations": deviations}}), encoding="utf-8")
    return str(out)


def test_cli_reads_the_denied_pytest_rows_of_a_halted_run_output(tmp_path):
    from coordinator_core.ops.dispatch_emit.cli import _pytest_owed_rows_of

    run = _digest(tmp_path, [
        {"chunk": "C1", "kind": "partial", "anchor": "", "reason": f"PARTIAL: {_DENIED}"},
        {"chunk": "C2", "kind": "partial", "anchor": "", "reason": "half done"},
        {"chunk": "C3", "kind": "blocked", "anchor": "", "reason": _DENIED},
        {"chunk": "C4", "kind": "gate_owed", "anchor": "gate-blocker: guard-denied: x (check-test-suite-invocation)", "reason": ""},
    ])
    assert _pytest_owed_rows_of(run) == {"C1", "C4"}
    assert _pytest_owed_rows_of(str(tmp_path / "missing.json")) == frozenset()
