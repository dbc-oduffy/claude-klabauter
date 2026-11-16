"""A code row fenced from running tests by a run-only work class gets its test gate from a test-runner."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import agent_route
from coordinator_core.ops.dispatch_emit.emit import (
    _run_row_helper_js,
    compose_script,
    derive_plan_context,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW
from pathlib import Path

_PLAN = "docs/plans/p.md"
_FENCE_CLAUSE = "Test gate (run-only fence, work class fenced-tests)"
_SELF_RUN_CLAUSE = "Footprint check: when every test and check inside your footprint passes"
_TEST_RUNNER = "coordinator:test-runner"


def _table(run_only: bool) -> dict:
    return {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "fenced-tests",
                "match": {"paths": ["pkg/fenced/*"]},
                "agent_type": "spec:specialist",
                "on_mismatch": "redirect",
                "run_only": run_only,
            }
        ],
    }


def _repo(tmp_path, table: dict | None):
    for rel in ("pkg/fenced/a.py", "pkg/open/b.py"):
        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("x = 1\n", encoding="utf-8")
        tst = src.parent / "tests" / f"test_{src.name}"
        tst.parent.mkdir(exist_ok=True)
        tst.write_text("def test_x(): pass\n", encoding="utf-8")
    if table is not None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    return tmp_path


def _row(rid: str, path: str, **kw) -> WaveRow:
    return WaveRow(rid, "t", "pkg", [path], [], [], change_kind="code-edit", **kw)


def _script(repo, rows, **kw) -> str:
    text = "---\ntitle: P\n---\n\n# P\n\n## Goal\n\nG.\n\n## Tasks\n\nx\n"
    ctx = derive_plan_context(text, fallback_title="p", repo_root=repo.as_posix())
    return compose_script(
        [rows], name="wf", description="d", repo_root=repo, plan_path=_PLAN, plan_context=ctx,
        **REVIEW_KW, **kw,
    )


def _prompt_of(script: str, rid: str) -> str:
    start = script.index(f"Execute {rid}: t")
    return script[start : script.index("agentType", start)]


def test_the_fence_is_read_from_the_route_table_run_only_key(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(True))
    assert agent_route.run_only_fence(_row("C1", "pkg/fenced/a.py"), repo).id == "fenced-tests"
    assert agent_route.run_only_fence(_row("C2", "pkg/open/b.py"), repo) is None


def test_a_class_without_run_only_is_no_fence(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(False))
    assert agent_route.run_only_fence(_row("C1", "pkg/fenced/a.py"), repo) is None


def test_no_table_and_no_repo_are_no_fence(tmp_path: Path) -> None:
    repo = _repo(tmp_path, None)
    assert agent_route.run_only_fence(_row("C1", "pkg/fenced/a.py"), repo) is None
    assert agent_route.run_only_fence(_row("C1", "pkg/fenced/a.py"), None) is None


def test_a_non_authoring_row_under_the_class_is_no_fence(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(True))
    row = _row("C1", "pkg/fenced/a.py")._replace(change_kind="doc-edit")
    assert agent_route.run_only_fence(row, repo) is None


def test_the_fenced_executor_prompt_drops_the_self_run_test_demand(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(True))
    script = _script(repo, [_row("C1", "pkg/fenced/a.py")])
    prompt = _prompt_of(script, "C1")
    assert _FENCE_CLAUSE in prompt
    assert _SELF_RUN_CLAUSE not in prompt


def test_the_test_gate_is_a_test_runner_agent_call_over_the_fenced_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(True))
    script = _script(repo, [_row("C1", "pkg/fenced/a.py")])
    call = script[script.index("label: 'test:terminal'") - 3000 : script.index("label: 'test:terminal'") + 400]
    assert f"agentType: '{_TEST_RUNNER}'" in call
    assert "pkg/fenced/tests/test_a.py" in call
    assert "fence: C1 run no tests themselves" in script


def test_an_unfenced_row_emits_as_before(tmp_path: Path) -> None:
    repos = []
    for name, table in (("with-table", _table(True)), ("no-table", None)):
        root = tmp_path / name
        root.mkdir()
        repos.append(_repo(root, table))
    row = _row("C2", "pkg/open/b.py")
    for repo in repos:
        script = _script(repo, [row])
        prompt = _prompt_of(script, "C2")
        assert _SELF_RUN_CLAUSE in prompt
        assert "run-only fence" not in prompt
        assert "fence: " not in script


def test_a_fenced_row_beside_an_unfenced_one_fences_only_its_own_prompt(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _table(True))
    script = _script(repo, [_row("C1", "pkg/fenced/a.py"), _row("C2", "pkg/open/b.py")])
    assert _FENCE_CLAUSE in _prompt_of(script, "C1")
    assert _FENCE_CLAUSE not in _prompt_of(script, "C2")
    assert _SELF_RUN_CLAUSE in _prompt_of(script, "C2")


def test_the_engine_names_no_consumer_path() -> None:
    from pathlib import Path

    from coordinator_core.ops.dispatch_emit import emit

    for mod in (agent_route, emit):
        assert "likeness-prototype" not in Path(mod.__file__).read_text(encoding="utf-8")


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
  _rows.C1 = _runRow('C1', [], null, async () => %(reply)s);
  _rows.C2 = _runRow('C2', [_rows.C1], null, async () => 'DONE: C2');
  await _rows.C2;
  console.log(JSON.stringify({ halted: _halted, incomplete: _incompleteChunks, owed: _gateOwed,
    notStarted: _notStarted, reason: _heldDependentsReason() }));
})();
"""


def _run(reply: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, "-"],
        input=_HARNESS % {"helper": _run_row_helper_js(), "reply": json.dumps(reply)},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_done_reply_from_the_fenced_executor_releases_the_run_with_no_halt() -> None:
    out = _run("DONE: r.md")
    assert out["halted"] is None
    assert out["incomplete"] == [] and out["owed"] == {} and out["notStarted"] == []
    assert out["reason"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_gate_blocker_the_fence_prevents_does_halt_the_run() -> None:
    out = _run("PARTIAL: r.md\ngate-blocker: tests not run: pytest is test-runner only")
    assert out["incomplete"] == ["C1"] and out["notStarted"] == ["C2"]
    assert "dependents unstarted: C2" in out["reason"]
